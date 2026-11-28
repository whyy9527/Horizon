import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from src import rss_extract_cli
from src.models import ContentItem, SourceType
from src.scrapers.rss import RSSFetchResult


def test_extraction_receipt_identifies_failed_articles_without_dropping_other_sources(tmp_path, monkeypatch):
    def article(title, status, content=None):
        return ContentItem(
            id=f"rss:test:{title}", source_type=SourceType.RSS, title=title,
            url=f"https://example.com/{title}", published_at=datetime.now(timezone.utc),
            content=content, metadata={"content_extraction": {"status": status}},
        )

    failed = article("missing", "failed")
    successful = article("complete", "extracted", "Complete article.")
    results = [
        RSSFetchResult("Outage", "https://example.com/outage", "http_200", [failed]),
        RSSFetchResult("Partial", "https://example.com/partial", "http_200", [successful, failed]),
        RSSFetchResult("Quiet", "https://example.com/quiet", "http_200", []),
    ]
    monkeypatch.setattr(rss_extract_cli.RSSScraper, "fetch_with_receipts", AsyncMock(return_value=results))
    social = {"schema": "horizon-social-boards/v1", "boards": [
        {"platform": "douyin", "status": "ok", "rows": [{"rank": 1, "title": "AI topic"}]},
        {"platform": "xiaohongshu", "status": "missing", "reason": "HTTP 404"},
    ]}
    monkeypatch.setattr(rss_extract_cli, "collect", lambda platforms, days, links: social)
    config = tmp_path / "config.json"
    config.write_text('{"sources": {"social_trends": {"platforms": ["douyin", "xiaohongshu"], "window_days": 7}}}')
    output = tmp_path / "articles.json"

    args = rss_extract_cli.build_parser().parse_args(["--config", str(config), "--output", str(output)])
    result = json.loads(json.dumps(asyncio.run(rss_extract_cli.run(args))))

    assert [item["content"] for item in result["items"]] == ["Complete article."]
    assert result["social_boards"] == social
    outage, partial, quiet = result["source_receipts"]
    assert outage["status"] == "failed"
    assert outage["failed_articles"] == [{"title": "missing", "url": str(failed.url), "status": "failed"}]
    assert partial["status"] == "http_200" and "1/2" in partial["error"]
    assert quiet["status"] == "http_200" and quiet["error"] is None
