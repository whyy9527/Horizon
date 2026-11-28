"""Read public Xiaohongshu and Douyin trend snapshots as source evidence."""

import csv
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPException
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .public_social_works import read_work, work_address


BEIJING = ZoneInfo("Asia/Shanghai")
BOARD_ROOT = "https://okdata.online/zh/tools/hot-search"
ARCHIVE_ROOT = "https://raw.githubusercontent.com/okdataapi/china-trends/main/data"
DRAMA_ROOT = "https://www.soduanju.com/bangdan"
DRAMA_PUBLISHER = "https://hongguoduanju.com/"
LIVE_SUMMARY = re.compile(r"共 (\d+) 条，读取于 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
LIVE_ROW = re.compile(r"^\| (\d+) \| \[(.+)\]\((https?://\S+)\) \| *(\d*) *\|", re.M)
DRAMA_SYNC = re.compile(r"本站同步 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})")
TABLE_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S)
TABLE_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S)


def read_text(url):
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=12) as response:
        return response.read().decode("utf-8-sig")


def read_live(url):
    page = read_text(url)
    summary = LIVE_SUMMARY.search(page)
    if summary is None:
        raise ValueError("missing live snapshot timestamp and row count")
    count, observed = summary.groups()
    rows = [
        {"rank": int(rank), "title": title, "heat": int(heat) if heat else None, "search_url": link}
        for rank, title, link, heat in LIVE_ROW.findall(page)
    ]
    if len(rows) != int(count):
        raise ValueError(f"read {len(rows)} of {count} rows")
    return datetime.fromisoformat(observed).replace(tzinfo=BEIJING), rows


def read_archive(url):
    records = list(csv.DictReader(io.StringIO(read_text(url))))
    observed_times = {record["read_at_utc"] for record in records}
    if len(observed_times) != 1:
        raise ValueError("expected one observation time per daily board")
    observed = datetime.fromisoformat(observed_times.pop().replace("Z", "+00:00"))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    rows = [
        {
            "rank": int(record["rank"]),
            "title": record["name"],
            "heat": int(record["heat"]) if record["heat"] else None,
            "search_url": record["url"],
        }
        for record in records
    ]
    return observed.astimezone(BEIJING), rows


def read_short_drama(url):
    page = read_text(url)
    sync = DRAMA_SYNC.search(page)
    count = re.search(r"共 (\d+) 部作品", page)
    if sync is None or count is None:
        raise ValueError("missing short drama board timestamp and row count")
    observed = datetime.fromisoformat(sync[1]).replace(tzinfo=BEIJING)
    rows = []
    for row in TABLE_ROW.findall(page):
        cells = TABLE_CELL.findall(row)
        if not cells:  # Header row.
            continue
        title = re.search(r"<a\b[^>]*>(.*?)</a>", cells[1], re.S)
        if not title:
            raise ValueError("missing short drama title")
        rows.append({
            "rank": int(re.sub(r"<[^>]+>", "", cells[0]).strip()),
            "title": unescape(title[1].strip()),
            "genre": cells[3].strip(),
            "heat": int(cells[4].strip()),
        })
    if len(rows) != int(count[1]):
        raise ValueError(f"read {len(rows)} of {count[1]} short drama rows")
    return observed, rows


class PublisherSeriesLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.by_title = {}

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            values = dict(attrs)
            label, href = values.get("aria-label") or "", values.get("href") or ""
            if label.startswith("查看") and re.fullmatch(r"/detail\?series_id=\d+", href):
                self.by_title.setdefault(label[2:], urljoin(DRAMA_PUBLISHER, href))


def short_drama_work(title):
    index = PublisherSeriesLinks()
    index.feed(read_text(DRAMA_PUBLISHER))
    if (url := index.by_title.get(title)) is None:
        raise ValueError("title not found in the publisher's public catalog")
    page = read_text(url)
    for block in re.findall(r'<script\b[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        data = json.loads(block)
        for entry in data.get("@graph", []) if isinstance(data, dict) else []:
            if entry.get("@type") in ("TVSeason", "TVSeries") and entry.get("name") == title and entry.get("url") == url:
                body, cover = entry.get("description"), entry.get("image")
                if isinstance(body, str) and body.strip() and isinstance(cover, str) and cover.startswith("https://"):
                    return {"url": url, "content": body.strip(), "cover_url": cover}
    raise ValueError("publisher page is missing matching synopsis or cover")


def read_board(task):
    platform, day, today, kind = task
    if kind == "short_drama":
        board_url = source_url = f"{DRAMA_ROOT}/date-{day}.html"
        reader = read_short_drama
    else:
        board = f"{BOARD_ROOT}/{platform}"
        live = day == today
        board_url = f"{board}/" if live else f"{board}/{day}/"
        source_url = f"{board}.md" if live else f"{ARCHIVE_ROOT}/{platform}/{day}.csv"
        reader = read_live if live else read_archive
    receipt = {
        "platform": platform,
        "kind": kind,
        "date": day.isoformat(),
        "board_url": board_url,
        "source_url": source_url,
    }
    try:
        observed, rows = reader(source_url)
        receipt["observed_at"] = observed.isoformat()
        if observed.date() != day:
            return {**receipt, "status": "missing", "reason": "snapshot is from a different date"}
        return {**receipt, "status": "ok", "rows": rows}
    except HTTPError as exc:
        status = "missing" if exc.code == 404 else "error"
        return {**receipt, "status": status, "reason": f"HTTP {exc.code}"}
    except (HTTPException, OSError, ValueError, IndexError, KeyError) as exc:
        return {**receipt, "status": "error", "reason": str(exc)}


def resolve_general_works(boards, work_links):
    configured = {(link["platform"], link["topic"]): link for link in work_links}
    candidates = {}
    for board in boards:
        if board["kind"] != "general" or board["status"] != "ok":
            continue
        platform = board["platform"]
        for row in board["rows"]:
            board_link = row["search_url"]
            link = configured.get((platform, row["title"]))
            if work_address(platform, board_link)[1]:
                key = platform, board_link
                evidence = {"kind": "board_links_work"}
            elif link is not None:
                key = platform, link["url"]
                evidence = {"kind": "verified_topic_link", "matched_terms": link["terms"]}
            else:
                row["work_error"] = "board provides a topic link without an identified original work"
                continue
            candidates.setdefault(key, []).append((row, evidence))

    def resolve(candidate):
        key, rows = candidate
        try:
            work = read_work(*key)
        except (HTTPException, OSError, ValueError) as exc:
            for row, _ in rows:
                row["work_error"] = str(exc)
            return
        text = (work["title"] + "\n" + work["content"]).casefold()
        for row, evidence in rows:
            if all(term.casefold() in text for term in evidence.get("matched_terms", [])):
                row["work"] = {**work, "association": evidence}
            else:
                row["work_error"] = "original work does not contain the configured topic evidence"

    if candidates:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(resolve, candidates.items()))


def collect(platforms, window_days, work_links=()):
    now = datetime.now(BEIJING)
    day = now.date()
    days = [day - timedelta(days=offset) for offset in range(window_days)]
    tasks = [(platform, board_day, now.date(), "general") for board_day in days for platform in platforms]
    if "douyin" in platforms:
        tasks.insert(len(platforms), ("douyin", day, now.date(), "short_drama"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        boards = list(pool.map(read_board, tasks))
    resolve_general_works(boards, work_links)
    for board in boards:
        if board["kind"] == "short_drama" and board["status"] == "ok":
            leader = min((row for row in board["rows"] if row["genre"] == "AI短剧"),
                         key=lambda row: row["rank"], default=None)
            if leader is not None:
                try:
                    leader["work"] = short_drama_work(leader["title"])
                except (HTTPException, OSError, ValueError) as exc:
                    leader["work_error"] = str(exc)
    return {
        "schema": "horizon-social-boards/v1",
        "date": day.isoformat(),
        "collected_at": now.isoformat(),
        "boards": boards,
    }
