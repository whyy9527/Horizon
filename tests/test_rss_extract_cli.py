from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
import pytest

from src.models import ContentItem, SourceType
from src.rss_extract_cli import run
from src.scrapers.rss import RSSFetchResult


@pytest.mark.parametrize("configured,override,expected", [
    (None, None, "trafilatura"), ("browser", None, "browser"),
    ("browser", "trafilatura", "trafilatura"),
])
def test_extraction_only_runner_enforces_reader_body_without_ai(
    tmp_path: Path, monkeypatch, configured, override, expected
) -> None:
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": {
                    "rss": [
                        {
                            "name": "Example",
                            "url": "https://example.com/feed.xml",
                            "category": "tech-ai",
                            "content_extractor": configured,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class FakeScraper:
        def __init__(self, sources, *_args, **_kwargs) -> None:
            captured["source"] = sources[0]

        async def fetch_with_receipts(self, _since):
            item = ContentItem(
                id="rss:example:1",
                source_type=SourceType.RSS,
                title="Article",
                url="https://example.com/article",
                content="Full extracted body.",
                published_at=datetime.now(timezone.utc),
                metadata={
                    "content_extraction": {
                        "status": "extracted",
                        "extractor": "trafilatura",
                    }
                },
            )
            failed_item = ContentItem(
                id="rss:example:2",
                source_type=SourceType.RSS,
                title="Unavailable body",
                url="https://example.com/unavailable",
                content=None,
                published_at=datetime.now(timezone.utc),
                metadata={"content_extraction": {"status": "failed"}},
            )
            return [
                RSSFetchResult(
                    source_name="Example",
                    source_url="https://example.com/feed.xml",
                    status="http_200",
                    items=[item, failed_item],
                )
            ]

    monkeypatch.setattr("src.rss_extract_cli.RSSScraper", FakeScraper)
    result = asyncio.run(
        run(
            Namespace(
                config=str(config_path),
                output=str(tmp_path / "unused.json"),
                hours=24,
                timeout=8.0,
                source_concurrency=2,
                article_concurrency=2,
                extractor=override,
            )
        )
    )

    assert captured["source"].content_extractor == expected
    assert captured["source"].require_content_extraction is True
    assert result["extraction_only"] is True
    assert result["extraction_status_counts"] == {"extracted": 1, "failed": 1}
    assert len(result["items"]) == 1
    assert result["items"][0]["content"] == "Full extracted body."
