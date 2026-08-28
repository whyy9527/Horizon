"""RSS feed scraper implementation."""

import asyncio
import calendar
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
import httpx
import feedparser

from .base import BaseScraper
from ..extractors import ExtractorRegistry
from ..models import ContentItem, SourceType, RSSSourceConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RSSFetchResult:
    """Items and a source-level receipt from one RSS feed."""

    source_name: str
    source_url: str
    status: str
    items: List[ContentItem]
    error: Optional[str] = None


class RSSScraper(BaseScraper):
    """Scraper for RSS/Atom feeds."""

    def __init__(
        self,
        sources: List[RSSSourceConfig],
        http_client: httpx.AsyncClient,
        extractors: Optional[ExtractorRegistry] = None,
        source_concurrency: int = 1,
        article_concurrency: int = 1,
    ):
        """Initialize RSS scraper.

        Args:
            sources: List of RSS feed configurations
            http_client: Shared async HTTP client
            extractors: Optional registry of content extractors for full article fetching
        """
        super().__init__({"sources": sources}, http_client)
        self._extractors = extractors
        self._source_concurrency = max(1, source_concurrency)
        self._article_semaphore = asyncio.Semaphore(max(1, article_concurrency))

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch RSS feed items.

        Args:
            since: Only fetch items published after this time

        Returns:
            List[ContentItem]: Fetched content items
        """
        results = await self.fetch_with_receipts(since)
        return [item for result in results for item in result.items]

    async def fetch_with_receipts(self, since: datetime) -> List[RSSFetchResult]:
        """Fetch enabled feeds while retaining success or failure per source."""
        sources = [source for source in self.config["sources"] if source.enabled]
        semaphore = asyncio.Semaphore(self._source_concurrency)

        async def fetch_source(source: RSSSourceConfig) -> RSSFetchResult:
            async with semaphore:
                return await self._fetch_feed(source, since)

        return list(await asyncio.gather(*(fetch_source(source) for source in sources)))

    async def _fetch_feed(
        self, source: RSSSourceConfig, since: datetime
    ) -> RSSFetchResult:
        """Fetch items from a single RSS feed.

        Args:
            source: RSS feed configuration
            since: Only fetch items after this time

        Returns:
            List[ContentItem]: Feed content items
        """
        try:
            # Expand environment variables in URL (e.g. ${LWN_TOKEN})
            feed_url = re.sub(
                r"\$\{(\w+)\}",
                lambda m: os.environ.get(m.group(1), m.group(0)).strip(),
                str(source.url),
            )

            # Fetch feed content
            response = await self.client.get(feed_url, follow_redirects=True)
            response.raise_for_status()

            # Parse feed
            feed = feedparser.parse(response.text)

            effective_since = since
            if source.max_age_hours:
                effective_since = max(
                    since,
                    datetime.now(timezone.utc) - timedelta(hours=source.max_age_hours),
                )

            entries = []
            for entry in feed.entries:
                published_at = self._parse_date(entry)
                if not published_at or published_at < effective_since:
                    continue
                entries.append((entry, published_at))

            items = list(
                await asyncio.gather(
                    *(self._build_item(source, entry, published_at) for entry, published_at in entries)
                )
            )
            return RSSFetchResult(
                source_name=source.name,
                source_url=str(source.url),
                status=f"http_{response.status_code}",
                items=items,
            )

        except httpx.HTTPError as exc:
            logger.warning("Error fetching RSS feed %s: %s", source.name, exc)
            return RSSFetchResult(source.name, str(source.url), "error", [], str(exc))
        except Exception as exc:
            logger.warning("Error parsing RSS feed %s: %s", source.name, exc)
            return RSSFetchResult(source.name, str(source.url), "error", [], str(exc))

    async def _build_item(
        self, source: RSSSourceConfig, entry: dict, published_at: datetime
    ) -> ContentItem:
        feed_id = str(source.url).split("//", 1)[1].replace("/", "_")
        entry_id = entry.get("id", entry.get("link", ""))
        entry_hash = hashlib.sha256(str(entry_id).encode("utf-8")).hexdigest()[:16]
        feed_content = "" if source.require_content_extraction else self._extract_content(entry)
        content: Optional[str] = feed_content or None
        extraction = {
            "requested": bool(source.content_extractor),
            "status": "not_requested",
            "extractor": source.content_extractor,
            "content_length": 0,
        }
        if not source.require_content_extraction:
            extraction["feed_content_length"] = len(feed_content)
        if source.content_extractor:
            extractor = self._extractors.get(source.content_extractor) if self._extractors else None
            url = entry.get("link", "")
            if not extractor:
                extraction["status"] = "extractor_unavailable"
            elif not url:
                extraction["status"] = "missing_article_url"
            else:
                async with self._article_semaphore:
                    full = await extractor.extract(url, self.client)
                if full:
                    content = full
                    extraction["status"] = "extracted"
                    extraction["content_length"] = len(full)
                else:
                    extraction["status"] = "failed"
            if source.require_content_extraction and extraction["status"] != "extracted":
                content = None

        return ContentItem(
            id=self._generate_id("rss", feed_id, entry_hash),
            source_type=SourceType.RSS,
            title=entry.get("title", "Untitled"),
            url=entry.get("link", str(source.url)),
            content=content,
            author=entry.get("author", source.name),
            published_at=published_at,
            profile=source.profile,
            metadata={
                "feed_name": source.name,
                "category": source.category,
                "source_family": source.source_family,
                "tags": [tag.term for tag in entry.get("tags", [])],
                "content_extraction": extraction,
            },
        )

    def _parse_date(self, entry: dict) -> Optional[datetime]:
        """Parse publication date from feed entry.

        Args:
            entry: Feed entry data

        Returns:
            datetime: Parsed publication date or None
        """
        # Try different date fields
        for field in ["published", "updated", "created"]:
            if field in entry:
                try:
                    # Try parsing structured time first
                    if f"{field}_parsed" in entry and entry[f"{field}_parsed"]:
                        return datetime.fromtimestamp(
                            calendar.timegm(entry[f"{field}_parsed"]), tz=timezone.utc
                        )
                    # Fallback to string parsing
                    date_str = entry[field]
                    return parsedate_to_datetime(date_str)
                except Exception:
                    continue

        return None

    def _extract_content(self, entry: dict) -> str:
        """Extract text content from feed entry.

        Args:
            entry: Feed entry data

        Returns:
            str: Extracted text content
        """
        # Try different content fields
        if "summary" in entry:
            return entry.summary
        if "description" in entry:
            return entry.description
        if "content" in entry and entry.content:
            # content is usually a list
            return entry.content[0].get("value", "")

        return ""
