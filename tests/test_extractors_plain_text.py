from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx

from src.extractors import PlainTextExtractor
from src.models import PlainTextExtractorConfig


def test_plain_text_extractor_preserves_public_markdown():
    response = MagicMock()
    response.text = "# Model card\n\nFull public content.\n"
    response.raise_for_status.return_value = None
    client = AsyncMock()
    client.get.return_value = response

    result = asyncio.run(
        PlainTextExtractor(PlainTextExtractorConfig()).extract(
            "https://example.com/README.md", client
        )
    )

    assert result == "# Model card\n\nFull public content."


def test_plain_text_extractor_omits_http_failures():
    response = MagicMock()
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="404", request=MagicMock(), response=MagicMock()
    )
    client = AsyncMock()
    client.get.return_value = response

    result = asyncio.run(
        PlainTextExtractor(PlainTextExtractorConfig()).extract(
            "https://example.com/missing.md", client
        )
    )

    assert result is None
