"""Explicit validation DNS must preserve private-address and redirect checks."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.url_security import UnsafeURLError, safe_request, validate_public_http_url


def reply(address=None, record_type="A", status="NOERROR", returncode=0):
    answer = f"example.com. 60 IN {record_type} {address}\n" if address else ""
    output = f";; ->>HEADER<<- opcode: QUERY, status: {status}, id: 1\n{answer}"
    process = MagicMock(returncode=returncode)
    process.communicate = AsyncMock(return_value=(output.encode(), b""))
    return process


class ValidationDNSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"HORIZON_VALIDATION_DNS": "192.0.2.53"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.executable = patch("src.url_security.shutil.which", return_value="/usr/bin/dig")
        self.executable.start()
        self.addCleanup(self.executable.stop)

    async def test_configured_dns_uses_both_families_without_system_resolution(self):
        processes = [reply("93.184.216.34"), reply("2606:2800:220:1:248:1893:25c8:1946", "AAAA")]
        with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=processes)) as run:
            with patch("src.url_security.socket.getaddrinfo", side_effect=AssertionError("system DNS used")):
                self.assertEqual(await validate_public_http_url("https://example.com/"), "https://example.com/")
        calls = run.await_args_list
        self.assertEqual({call.args[5] for call in calls}, {"A", "AAAA"})
        for call in calls:
            self.assertEqual(call.args[:4], ("/usr/bin/dig", "@192.0.2.53", "-q", "example.com."))
            self.assertIn("+tcp", call.args)

    async def test_configured_dns_does_not_allow_private_or_fake_answers(self):
        for address in ("10.0.0.1", "127.0.0.1", "198.18.1.1", "169.254.169.254", "::1", "fc00::1"):
            with self.subTest(address=address):
                with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=[reply("93.184.216.34"), reply(address, "AAAA" if ":" in address else "A")])):
                    with self.assertRaisesRegex(UnsafeURLError, "non-public"):
                        await validate_public_http_url("https://example.com/")

    async def test_dns_failure_never_falls_back_to_system_dns(self):
        for process in (reply(status="SERVFAIL"), reply(status="NXDOMAIN"), reply(returncode=9)):
            with self.subTest(process=process):
                with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
                    with patch("src.url_security.socket.getaddrinfo", side_effect=AssertionError("system DNS used")):
                        with self.assertRaisesRegex(UnsafeURLError, "DNS lookup failed"):
                            await validate_public_http_url("https://example.com/")

    async def test_no_answers_are_rejected(self):
        with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock(return_value=reply())):
            with self.assertRaisesRegex(UnsafeURLError, "no addresses"):
                await validate_public_http_url("https://example.com/")

    async def test_bad_configuration_and_missing_dig_fail_closed(self):
        with patch.dict(os.environ, {"HORIZON_VALIDATION_DNS": "https://dns.example/"}):
            with self.assertRaisesRegex(UnsafeURLError, "DNS server IP"):
                await validate_public_http_url("https://example.com/")
        with patch("src.url_security.shutil.which", return_value=None):
            with self.assertRaisesRegex(UnsafeURLError, "dig executable"):
                await validate_public_http_url("https://example.com/")

    async def test_redirect_to_private_address_is_still_rejected(self):
        client = MagicMock()
        client.get = AsyncMock(return_value=MagicMock(status_code=302, headers={"location": "http://127.0.0.1/admin"}))
        with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=[reply("93.184.216.34"), reply()])):
            with self.assertRaisesRegex(UnsafeURLError, "non-public"):
                await safe_request(client, "GET", "https://example.com/")
        client.get.assert_awaited_once()

    async def test_dns_is_unmodified_when_no_resolver_is_configured(self):
        with patch.dict(os.environ):
            os.environ.pop("HORIZON_VALIDATION_DNS", None)
            with patch("src.url_security.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("198.18.1.1", 443))]):
                with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock()) as run:
                    with self.assertRaisesRegex(UnsafeURLError, "non-public"):
                        await validate_public_http_url("https://example.com/")
                run.assert_not_called()

    async def test_cancellation_reaps_both_dns_processes(self):
        started = asyncio.Event()
        active = 0

        async def pending():
            nonlocal active
            active += 1
            if active == 2:
                started.set()
            await asyncio.Future()

        processes = [MagicMock(returncode=None), MagicMock(returncode=None)]
        for process in processes:
            process.communicate = AsyncMock(side_effect=pending)
            process.wait = AsyncMock(return_value=-9)
        with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=processes)):
            task = asyncio.create_task(validate_public_http_url("https://example.com/"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        for process in processes:
            process.kill.assert_called_once()
            process.wait.assert_awaited_once()

    async def test_one_dns_failure_reaps_the_other_process_before_returning(self):
        started = asyncio.Event()

        async def failure_after_other_query_started():
            await started.wait()
            return b";; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 1\n", b""

        async def pending():
            started.set()
            await asyncio.Future()

        failed_process = MagicMock(returncode=0)
        failed_process.communicate = AsyncMock(side_effect=failure_after_other_query_started)
        pending_process = MagicMock(returncode=None)
        pending_process.communicate = AsyncMock(side_effect=pending)
        pending_process.wait = AsyncMock(return_value=-9)
        with patch("src.url_security.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=[failed_process, pending_process])):
            with self.assertRaisesRegex(UnsafeURLError, "DNS lookup failed"):
                await validate_public_http_url("https://example.com/")
            pending_process.kill.assert_called_once()
            pending_process.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
