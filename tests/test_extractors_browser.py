import asyncio
from unittest.mock import AsyncMock, patch
import pytest

from src.extractors.browser import BrowserExtractor
from src.extractors.registry import ExtractorRegistry
from src.models import BrowserExtractorConfig


def test_browser_is_available_only_when_selected():
    registry = ExtractorRegistry({})
    assert isinstance(registry.get("browser"), BrowserExtractor)
    assert not isinstance(registry.get("trafilatura"), BrowserExtractor)


def test_browser_rejects_private_navigation_before_launching():
    pytest.importorskip("playwright.async_api")
    with patch("playwright.async_api.async_playwright") as launch:
        result = asyncio.run(BrowserExtractor(BrowserExtractorConfig()).extract(
            "http://localhost/private", AsyncMock()
        ))
    assert result is None
    launch.assert_not_called()
