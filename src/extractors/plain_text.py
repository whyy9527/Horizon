"""Public plain-text extractor for Markdown and other already-readable documents."""

import logging
from typing import Optional

import httpx

from .base import BaseExtractor
from .trafilatura import ARTICLE_HEADERS
from ..models import PlainTextExtractorConfig
from ..url_security import UnsafeURLError, safe_request

logger = logging.getLogger(__name__)


class PlainTextExtractor(BaseExtractor):
    def __init__(self, config: PlainTextExtractorConfig):
        self._config = config

    async def extract(self, url: str, client: httpx.AsyncClient) -> Optional[str]:
        try:
            response = await safe_request(client, "GET", url, headers=ARTICLE_HEADERS)
            response.raise_for_status()
        except (httpx.HTTPError, UnsafeURLError) as exc:
            logger.warning("Failed to fetch public text %s: %s", url, exc)
            return None
        content = response.text.strip()
        return content or None
