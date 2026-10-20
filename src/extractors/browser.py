"""Read JavaScript-rendered public articles without using a user's browser profile."""

import asyncio
import logging
from urllib.parse import urlsplit
from urllib.request import getproxies

import httpx

from .trafilatura import TrafilaturaExtractor
from ..models import BrowserExtractorConfig
from ..url_security import UnsafeURLError, validate_public_http_url

logger = logging.getLogger(__name__)


class BrowserExtractor(TrafilaturaExtractor):
    def __init__(self, config: BrowserExtractorConfig):
        super().__init__(config)
        self._semaphore = asyncio.Semaphore(1)

    async def extract_with_receipt(self, url: str, client: httpx.AsyncClient) -> tuple[str | None, dict]:
        try:
            from playwright.async_api import async_playwright

            await validate_public_http_url(url)
            proxy_url = getproxies().get("https")
            launch = {"headless": False}
            if proxy_url:
                launch["proxy"] = {"server": proxy_url}
            async with self._semaphore, async_playwright() as playwright:
                browser = await playwright.chromium.launch(**launch)
                try:
                    context = await browser.new_context(service_workers="block")
                    validated = set()

                    async def public_requests_only(route):
                        request = route.request
                        if request.resource_type in {"image", "media", "font"}:
                            await route.abort()
                            return
                        origin = urlsplit(request.url)[:2]
                        try:
                            if origin not in validated:
                                await validate_public_http_url(request.url)
                                validated.add(origin)
                        except UnsafeURLError:
                            await route.abort()
                            return
                        await route.continue_()

                    await context.route("**/*", public_requests_only)
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.locator("main h1, article h1").first.wait_for(timeout=30000)
                    return self.extract_text_with_receipt(await page.content())
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("Browser article extraction failed for %s: %s", url, type(exc).__name__)
            return None, {}
