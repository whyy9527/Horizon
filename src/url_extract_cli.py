"""Extract public web-page bodies from a supplied URL manifest, without AI."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import httpx
from pydantic import BaseModel, Field

from .extractors import ExtractorRegistry
from .models import ExtractorConfig


class URLExtractionInput(BaseModel):
    """One caller-owned URL identity to resolve into extracted public text."""

    id: str = Field(min_length=1)
    url: str = Field(min_length=1)


class URLExtractionInputManifest(BaseModel):
    """Generic URL manifest accepted by the extraction-only entry point."""

    urls: List[URLExtractionInput] = Field(default_factory=list)
    extractors: Dict[str, ExtractorConfig] = Field(default_factory=dict)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON URL manifest")
    parser.add_argument("--output", required=True, help="Path for the JSON extraction receipt")
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--article-concurrency", type=int, default=8)
    parser.add_argument("--extractor", default="trafilatura")
    return parser


async def run(args: argparse.Namespace) -> dict:
    if args.timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    if args.article_concurrency <= 0:
        raise ValueError("--article-concurrency must be greater than zero")
    input_path = Path(args.input).expanduser().resolve()
    manifest = URLExtractionInputManifest.model_validate(
        json.loads(input_path.read_text(encoding="utf-8"))
    )
    extractor = ExtractorRegistry(manifest.extractors).get(args.extractor)
    if extractor is None:
        raise ValueError(f"unknown extractor: {args.extractor}")

    seen_urls: set[str] = set()
    inputs = []
    for entry in manifest.urls:
        if entry.url in seen_urls:
            continue
        seen_urls.add(entry.url)
        inputs.append(entry)
    semaphore = asyncio.Semaphore(args.article_concurrency)

    async def extract(entry: URLExtractionInput) -> dict:
        try:
            async with semaphore:
                content, cleaning = await extractor.extract_with_receipt(entry.url, client)
        except Exception as exc:  # Individual public pages must not abort the batch.
            return {"id": entry.id, "url": entry.url, "status": "error", "error": str(exc)}
        if not isinstance(content, str) or not content.strip():
            return {"id": entry.id, "url": entry.url, "status": "failed", "error": None}
        receipt = {
            "id": entry.id,
            "url": entry.url,
            "status": "extracted",
            "content": content,
            "content_length": len(content),
            "error": None,
        }
        if cleaning:
            receipt["html_cleaning"] = cleaning
        return receipt

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        receipts = await asyncio.gather(*(extract(entry) for entry in inputs))
    items = [
        {"id": receipt["id"], "url": receipt["url"], "content": receipt["content"]}
        for receipt in receipts
        if receipt["status"] == "extracted"
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "extraction_only": True,
        "extractor": args.extractor,
        "items": items,
        "receipts": receipts,
    }


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    result = asyncio.run(run(args))
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
