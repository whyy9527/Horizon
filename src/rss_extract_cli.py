"""Fetch RSS article bodies and configured public boards without an AI stage."""

import argparse
import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Literal

import httpx
from pydantic import BaseModel, Field, field_validator

from .extractors import ExtractorRegistry
from .models import ExtractorConfig, RSSSourceConfig
from .public_social_boards import collect
from .public_social_works import work_address
from .scrapers.rss import RSSScraper

logger = logging.getLogger(__name__)


class SocialWorkLink(BaseModel):
    platform: Literal["xiaohongshu", "douyin"]
    topic: str = Field(min_length=1)
    url: str
    terms: list[str] = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def check_original_url(cls, url, info):
        if "platform" in info.data and not work_address(info.data["platform"], url)[1]:
            raise ValueError("expected a platform original work URL")
        return url

    @field_validator("terms")
    @classmethod
    def check_terms(cls, terms):
        if any(not term.strip() for term in terms):
            raise ValueError("topic evidence terms must be non-blank")
        return terms


class SocialBoardsConfig(BaseModel):
    platforms: list[Literal["xiaohongshu", "douyin"]] = Field(min_length=1)
    window_days: int = Field(gt=0)
    work_links: list[SocialWorkLink] = Field(default_factory=list)


class RSSExtractionSources(BaseModel):
    """Source types handled by this extraction-only entry point."""

    rss: List[RSSSourceConfig] = Field(default_factory=list)
    social_trends: SocialBoardsConfig | None = None


class RSSExtractionConfig(BaseModel):
    """Configuration accepted by the extraction-only entry point.

    This deliberately does not require an ``ai`` section: article retrieval is a
    local deterministic stage, not a Horizon analysis run.
    """

    sources: RSSExtractionSources
    extractors: Dict[str, ExtractorConfig] = Field(default_factory=dict)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON config containing the extraction sources")
    parser.add_argument("--output", required=True, help="Path for the JSON extraction receipt")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--source-concurrency", type=int, default=6)
    parser.add_argument("--article-concurrency", type=int, default=8)
    parser.add_argument("--extractor", help="Override source extractors; otherwise use source config or trafilatura")
    return parser


async def run(args: argparse.Namespace) -> dict:
    if args.hours <= 0:
        raise ValueError("--hours must be greater than zero")
    config_path = Path(args.config).expanduser().resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    config = RSSExtractionConfig.model_validate(payload)
    sources = [
        source.model_copy(
            update={
                "content_extractor": args.extractor or source.content_extractor or "trafilatura",
                "require_content_extraction": True,
            }
        )
        for source in config.sources.rss
        if source.enabled
    ]
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        scraper = RSSScraper(
            sources,
            client,
            ExtractorRegistry(config.extractors),
            source_concurrency=args.source_concurrency,
            article_concurrency=args.article_concurrency,
        )
        social = config.sources.social_trends
        social_boards = None
        if social is None:
            results = await scraper.fetch_with_receipts(since)
        else:
            results, social_boards = await asyncio.gather(
                scraper.fetch_with_receipts(since),
                asyncio.to_thread(
                    collect, tuple(dict.fromkeys(social.platforms)), social.window_days,
                    [link.model_dump() for link in social.work_links],
                ),
            )
    fetched_items = [item for result in results for item in result.items]
    extraction_counts = Counter(
        str(item.metadata.get("content_extraction", {}).get("status", "unknown"))
        for item in fetched_items
    )
    items = []
    source_receipts = []
    for result in results:
        failures = []
        for item in result.items:
            extraction_status = item.metadata.get("content_extraction", {}).get("status", "unknown")
            if extraction_status == "extracted" and isinstance(item.content, str) and item.content.strip():
                items.append(item)
            else:
                failures.append({
                    "title": item.title,
                    "url": str(item.url),
                    "status": "empty_body" if extraction_status == "extracted" else extraction_status,
                })
        receipt = {
            "source": result.source_name,
            "url": result.source_url,
            "status": result.status,
            "item_count": len(result.items),
            "error": result.error,
        }
        if failures:
            error = f"Full text extraction failed for {len(failures)}/{len(result.items)} RSS articles"
            receipt.update(error=error, failed_articles=failures)
            if len(failures) == len(result.items):
                receipt["status"] = "failed"
                logger.warning("%s: %s", result.source_name, error)
        source_receipts.append(receipt)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds"),
        "config_path": str(config_path),
        "extractor": args.extractor or "per-source",
        "extraction_only": True,
        "items": [item.model_dump(mode="json") for item in items],
        "source_receipts": source_receipts,
        "extraction_status_counts": dict(sorted(extraction_counts.items())),
        **({"social_boards": social_boards} if social_boards is not None else {}),
    }


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = asyncio.run(run(args))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
