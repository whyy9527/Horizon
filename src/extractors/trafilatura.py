"""Trafilatura-based article extractor."""

import logging
import re
from typing import Optional

import httpx

from .base import BaseExtractor
from ..models import TrafilaturaExtractorConfig
from ..url_security import UnsafeURLError, safe_request

logger = logging.getLogger(__name__)

ARTICLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

CHALLENGE_TEXT = re.compile(
    r"(?:javascript is disabled\s+)?"
    r"in order to continue, we need to verify that you're not a robot\.\s*"
    r"this requires javascript\.\s*enable javascript and then reload the page\.?"
    r"|(?:just a moment[.!…]*\s*)?enable javascript and cookies to continue[.!…]*",
    re.IGNORECASE,
)


class TrafilaturaExtractor(BaseExtractor):
    def __init__(self, config: TrafilaturaExtractorConfig):
        self._config = config

    async def extract(self, url: str, client: httpx.AsyncClient) -> Optional[str]:
        try:
            import trafilatura
        except ImportError:
            logger.warning("trafilatura is not installed; install with: uv pip install trafilatura>=2.1.0")
            return None

        try:
            response = await safe_request(client, "GET", url, headers=ARTICLE_HEADERS)
            response.raise_for_status()
        except (httpx.HTTPError, UnsafeURLError) as e:
            logger.warning("Failed to fetch article %s: %s", url, e)
            return None

        if response.headers.get("cf-mitigated") == "challenge":
            logger.warning("Browser challenge returned instead of article %s", url)
            return None

        try:
            text = trafilatura.extract(
                response.text,
                favor_precision=self._config.favor_precision,
                favor_recall=self._config.favor_recall,
            ) or None
            if text and CHALLENGE_TEXT.fullmatch(" ".join(text.split())):
                logger.warning("Browser challenge extracted instead of article %s", url)
                return None
            return text
        except Exception as e:
            logger.warning("trafilatura extraction failed for %s: %s", url, e)
            return None
