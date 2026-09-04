"""Fetch RSS article bodies with Horizon extractors, without invoking any AI stage."""

import argparse
import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import httpx
from pydantic import BaseModel, Field

from .extractors import ExtractorRegistry
from .models import ExtractorConfig, RSSSourceConfig
from .scrapers.rss import RSSScraper


class RSSExtractionSources(BaseModel):
    """The RSS-only slice of a Horizon source configuration."""

    rss: List[RSSSourceConfig] = Field(default_factory=list)


class RSSExtractionConfig(BaseModel):
    """Configuration accepted by the extraction-only entry point.

    This deliberately does not require an ``ai`` section: article retrieval is a
    local deterministic stage, not a Horizon analysis run.
    """

    sources: RSSExtractionSources
    extractors: Dict[str, ExtractorConfig] = Field(default_factory=dict)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON config containing sources.rss")
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
        results = await scraper.fetch_with_receipts(since)
    fetched_items = [item for result in results for item in result.items]
    extraction_counts = Counter(
        str(item.metadata.get("content_extraction", {}).get("status", "unknown"))
        for item in fetched_items
    )
    items = [
        item
        for item in fetched_items
        if item.metadata.get("content_extraction", {}).get("status") == "extracted"
        and isinstance(item.content, str)
        and item.content.strip()
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since.isoformat(timespec="seconds"),
        "config_path": str(config_path),
        "extractor": args.extractor or "per-source",
        "extraction_only": True,
        "items": [item.model_dump(mode="json") for item in items],
        "source_receipts": [
            {
                "source": result.source_name,
                "url": result.source_url,
                "status": result.status,
                "item_count": len(result.items),
                "error": result.error,
            }
            for result in results
        ],
        "extraction_status_counts": dict(sorted(extraction_counts.items())),
    }


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = asyncio.run(run(args))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
