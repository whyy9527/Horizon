"""Read explicitly identified publication dates from original article HTML."""

from datetime import datetime, time, timezone
import json
import re

from bs4 import BeautifulSoup


MONTH_DATE = re.compile(r"[A-Za-z]+ [0-9]{1,2}, [0-9]{4}")
ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "TechArticle", "DiscussionForumPosting"}


def parse_publication(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if MONTH_DATE.fullmatch(value):
            parsed = datetime.strptime(value, "%B %d, %Y")
        else:
            parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return {"published_at": parsed.isoformat(), "precision": "instant"}
    day = datetime.combine(parsed.date(), time.min, timezone.utc)
    return {"published_at": day.isoformat(), "precision": "day"}


def _structured_dates(node):
    if isinstance(node, list):
        for item in node:
            yield from _structured_dates(item)
    elif isinstance(node, dict):
        types = node.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if isinstance(types, list) and any(kind in ARTICLE_TYPES for kind in types if isinstance(kind, str)):
            yield node.get("datePublished")
        yield from _structured_dates(node.get("@graph"))


def _unique(values):
    unique = {}
    for value in values:
        date = parse_publication(value)
        if date:
            unique[date["published_at"], date["precision"]] = date
    return next(iter(unique.values())) if len(unique) == 1 else None


def article_publication(html):
    soup = BeautifulSoup(html, "html.parser")
    marked = [element.get("content") for element in soup.select(
        'meta[property="article:published_time"], meta[itemprop="datePublished"]')]
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            marked.extend(_structured_dates(json.loads(script.string or "")))
        except (ValueError, RecursionError):
            continue
    if any(parse_publication(value) for value in marked):
        return _unique(marked)

    labelled = []
    for line in soup.select("main p"):
        label, separator, value = line.get_text(" ", strip=True).partition(":")
        if separator and label.casefold() in {"published", "published on"}:
            labelled.append(value)
    if labelled:
        return _unique(labelled)

    heading = soup.select_one("main h1")
    if heading is not None:
        for region in list(heading.parents)[:3]:
            if region.name in {"main", "body"}:
                break
            dates = [line.get_text(" ", strip=True) for line in region.select("time, p")]
            dates = [value for value in dates if MONTH_DATE.fullmatch(value)]
            if dates:
                return _unique(dates)
    return None
