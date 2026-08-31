from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from pathlib import Path

from src.url_extract_cli import run


def test_url_extraction_runner_returns_only_full_bodies(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "urls.json"
    input_path.write_text(
        json.dumps(
            {
                "urls": [
                    {"id": "first", "url": "https://example.com/first"},
                    {"id": "second", "url": "https://example.com/second"},
                    {"id": "duplicate", "url": "https://example.com/first"},
                ]
            }
        ),
        encoding="utf-8",
    )

    class FakeExtractor:
        async def extract(self, url, _client):
            return "Full public body." if url.endswith("first") else None

    class FakeRegistry:
        def __init__(self, _configs) -> None:
            pass

        def get(self, name):
            assert name == "trafilatura"
            return FakeExtractor()

    monkeypatch.setattr("src.url_extract_cli.ExtractorRegistry", FakeRegistry)
    result = asyncio.run(
        run(
            Namespace(
                input=str(input_path),
                output=str(tmp_path / "unused.json"),
                timeout=8.0,
                article_concurrency=2,
                extractor="trafilatura",
            )
        )
    )

    assert result["extraction_only"] is True
    assert result["items"] == [
        {
            "id": "first",
            "url": "https://example.com/first",
            "content": "Full public body.",
        }
    ]
    assert [receipt["status"] for receipt in result["receipts"]] == ["extracted", "failed"]
