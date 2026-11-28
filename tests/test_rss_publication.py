import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx

from src.extractors.article_publication import article_publication
from src.models import RSSSourceConfig
from src.scrapers.rss import RSSScraper


def run_feed(mode, extractor=None):
    feed = """<rss version="2.0"><channel><title>Example</title>
    <item><title>Midnight entry</title><link>https://example.com/midnight</link>
    <pubDate>Fri, 27 Nov 2026 00:00:00 GMT</pubDate></item>
    <item><title>Discussion</title><link>https://example.com/discussion</link>
    <pubDate>Fri, 27 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
    source = RSSSourceConfig(name="Test", url="https://example.com/feed", publication_time=mode,
                             content_extractor="trafilatura" if extractor else None)
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, text=feed, request=httpx.Request("GET", str(source.url)))
    scraper = RSSScraper([source], client, extractor)
    cutoff = datetime(2026, 11, 27, 1, tzinfo=timezone.utc)
    return asyncio.run(scraper.fetch(cutoff))


def test_calendar_date_overlap_preserves_midnight_and_exact_timestamps():
    assert [item.title for item in run_feed("feed")] == ["Discussion"]
    dated, timed = run_feed("calendar_date")
    assert dated.published_at == datetime(2026, 11, 27, tzinfo=timezone.utc)
    assert dated.metadata["published_precision"] == "day"
    assert timed.published_at.hour == 12
    assert "published_precision" not in timed.metadata


def test_aggregator_retains_discussion_time_when_article_date_is_known():
    from types import SimpleNamespace

    publication = article_publication("<main><p>Published on: November 23, 2026</p></main>")
    extract = AsyncMock(return_value=("Full article body", {"publication": publication}))
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(extract_with_receipt=extract))
    item, = run_feed("article", registry)
    assert item.published_at == datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert item.metadata["publication_basis"] == "original_article"
    assert item.metadata["published_precision"] == "day"
    assert item.metadata["feed_published_at"] == "2026-11-27T12:00:00+00:00"
    assert extract.await_args.kwargs == {"include_publication": True}


def test_unambiguous_article_date_uses_published_field():
    html = """<script type="application/ld+json">{"@graph":[
    {"@type":"WebPage","datePublished":"2026-11-27"},
    {"@type":"DiscussionForumPosting","datePublished":"2026-10-26T20:53:08Z",
     "dateModified":"2026-11-27T12:00:00Z"}]}</script>"""
    assert article_publication(html) == {
        "published_at": "2026-10-26T20:53:08+00:00", "precision": "instant"}
