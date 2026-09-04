from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

import httpx

from src.extractors import ExtractorRegistry, TrafilaturaExtractor
from src.extractors.trafilatura import ARTICLE_HEADERS
from src.models import RSSSourceConfig, TrafilaturaExtractorConfig
from src.scrapers.rss import RSSScraper

URL = "https://example.com/article"


def _extractor() -> TrafilaturaExtractor:
    return TrafilaturaExtractor(TrafilaturaExtractorConfig())


def _client(text: str = "<html><body><p>Article text.</p></body></html>", status: int = 200) -> AsyncMock:
    response = MagicMock()
    response.text = text
    response.raise_for_status.return_value = None
    if status >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=str(status), request=MagicMock(), response=MagicMock()
        )
    client = AsyncMock()
    client.get.return_value = response
    return client


def _trafilatura_mock(extracted: str) -> MagicMock:
    mod = MagicMock()
    mod.extract.return_value = extracted
    return mod


def test_returns_extracted_text():
    client = _client()
    with patch.dict(sys.modules, {"trafilatura": _trafilatura_mock("Extracted article text.")}):
        result = asyncio.run(_extractor().extract(URL, client))
    assert result == "Extracted article text."
    client.get.assert_awaited_once_with(
        URL,
        follow_redirects=False,
        headers=ARTICLE_HEADERS,
    )


def test_returns_none_when_trafilatura_returns_empty():
    client = _client()
    with patch.dict(sys.modules, {"trafilatura": _trafilatura_mock("")}):
        result = asyncio.run(_extractor().extract(URL, client))
    assert result is None


def test_returns_none_on_http_error():
    client = _client(status=404)
    with patch.dict(sys.modules, {"trafilatura": _trafilatura_mock("text")}):
        result = asyncio.run(_extractor().extract(URL, client))
    assert result is None


def test_returns_none_when_trafilatura_raises():
    client = _client()
    mock_traf = _trafilatura_mock("text")
    mock_traf.extract.side_effect = RuntimeError("parse error")
    with patch.dict(sys.modules, {"trafilatura": mock_traf}):
        result = asyncio.run(_extractor().extract(URL, client))
    assert result is None


def test_returns_none_when_not_installed():
    client = _client()
    with patch.dict(sys.modules, {"trafilatura": None}):
        result = asyncio.run(_extractor().extract(URL, client))
    assert result is None


def test_rejects_http_200_browser_challenge_text():
    text = (
        "JavaScript is disabled In order to continue, we need to verify that you're not a robot. "
        "This requires JavaScript. Enable JavaScript and then reload the page."
    )
    with patch.dict(sys.modules, {"trafilatura": _trafilatura_mock(text)}):
        assert asyncio.run(_extractor().extract(URL, _client())) is None


def test_rejects_challenge_header_before_extraction():
    client = _client()
    client.get.return_value.headers = {"cf-mitigated": "challenge"}
    parser = _trafilatura_mock("Some apparent body")
    with patch.dict(sys.modules, {"trafilatura": parser}):
        assert asyncio.run(_extractor().extract(URL, client)) is None
    parser.extract.assert_not_called()


def test_article_discussing_bot_checks_is_not_a_challenge():
    text = "Researchers studied the phrase 'verify that you're not a robot' and its usability."
    with patch.dict(sys.modules, {"trafilatura": _trafilatura_mock(text)}):
        assert asyncio.run(_extractor().extract(URL, _client())) == text


def test_browser_challenge_becomes_failed_rss_item_without_feed_fallback():
    source = RSSSourceConfig(
        name="Test", url="https://example.com/feed.xml",
        content_extractor="trafilatura", require_content_extraction=True,
    )
    scraper = RSSScraper([source], _client(), ExtractorRegistry({}))
    challenge = (
        "JavaScript is disabled In order to continue, we need to verify that you're not a robot. "
        "This requires JavaScript. Enable JavaScript and then reload the page."
    )
    with patch.dict(sys.modules, {"trafilatura": _trafilatura_mock(challenge)}):
        item = asyncio.run(scraper._build_item(
            source, {"title": "A real headline", "link": URL, "summary": "Feed excerpt must not return"},
            datetime.now(timezone.utc),
        ))
    assert item.content is None
    assert item.metadata["content_extraction"]["status"] == "failed"
