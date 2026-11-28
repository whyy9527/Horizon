"""Read the original caption or note and its cover from a public work page."""

import asyncio
import json
import re
from urllib.parse import urlsplit, urlunsplit

import httpx
from playwright.async_api import Error, async_playwright

from .url_security import UnsafeURLError, safe_request, validate_public_http_url


def work_address(platform, url):
    """Return the platform work ID, if present, and whether a public link is supported."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None, False
    if parsed.scheme != "https" or parsed.username is not None or port not in (None, 443):
        return None, False
    if platform == "douyin" and parsed.hostname in {"www.douyin.com", "douyin.com"}:
        match = re.fullmatch(r"/video/(\d+)/?", parsed.path)
        return (match[1], True) if match else (None, False)
    if platform == "xiaohongshu":
        if parsed.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"}:
            match = re.fullmatch(r"/(?:explore|discovery/item)/([a-f0-9]{24})/?", parsed.path)
            return (match[1], True) if match else (None, False)
        if parsed.hostname == "xhslink.com":
            return None, bool(re.fullmatch(r"/(?:a/)?[A-Za-z0-9]+/?", parsed.path))
        if parsed.hostname == "xhslink.cn":
            return None, bool(re.fullmatch(r"/o/[A-Za-z0-9]+/?", parsed.path))
    return None, False


def xiaohongshu_note(url, original_id, page):
    note_id, _ = work_address("xiaohongshu", url)
    if not note_id or (original_id is not None and note_id != original_id):
        raise ValueError("Xiaohongshu response did not identify the requested note")
    for block in re.findall(r'<script\b[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        data = json.loads(block)
        if not isinstance(data, dict) or data.get("@type") not in {"Article", "SocialMediaPosting", "VideoObject"}:
            continue
        title = data.get("headline") or data.get("name")
        image = data.get("image") or data.get("thumbnailUrl")
        if isinstance(image, list):
            image = next(iter(image), None)
        if isinstance(image, dict):
            image = image.get("url")
        if isinstance(title, str):
            title = title.removesuffix(" - 小红书")
        return make_work(url, note_id, title, data.get("description"), image, "xhscdn.com")
    raise ValueError("Xiaohongshu note page did not expose original text and image")


def make_work(url, work_id, title, content, cover, cdn):
    if not isinstance(title, str) or not title.strip() or not isinstance(content, str) or len(content.strip()) < 12:
        raise ValueError("original work has no substantive text")
    if not isinstance(cover, str):
        raise ValueError("original work has no cover")
    image = urlsplit(cover)
    if (image.scheme not in {"http", "https", ""} or image.username is not None
            or image.port not in (None, 443) or not image.hostname
            or not (image.hostname == cdn or image.hostname.endswith("." + cdn))):
        raise ValueError("original work has no platform cover")
    return {
        "url": url,
        "id": work_id,
        "title": title.strip(),
        "content": content.strip(),
        "cover_url": urlunsplit(image._replace(scheme="https")),
    }


async def read_xiaohongshu(url, note_id):
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = await safe_request(client, "GET", url)
        response.raise_for_status()
        if response.url.host == "www.xiaohongshu.com" and response.url.path == "/login":
            raise ValueError("Xiaohongshu requires sign-in to read this original note")
        return xiaohongshu_note(str(response.url), note_id, response.text)


async def read_douyin(url, video_id):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(service_workers="block")
            checked_origins = set()

            async def public_requests_only(route):
                request = route.request
                if request.resource_type in {"image", "media", "font"}:
                    await route.abort()
                    return
                origin = urlsplit(request.url)[:2]
                try:
                    if origin not in checked_origins:
                        await validate_public_http_url(request.url)
                        checked_origins.add(origin)
                except UnsafeURLError:
                    await route.abort()
                    return
                await route.continue_()

            await context.route("**/*", public_requests_only)
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            if response is None or response.status >= 400:
                raise ValueError("Douyin original video page unavailable")
            cover = page.locator('meta[name="lark:url:video_cover_image_url"]')
            image = await cover.get_attribute("content", timeout=12000)
            canonical = await page.locator('link[rel="canonical"]').get_attribute("href", timeout=3000)
            if work_address("douyin", page.url)[0] != video_id or work_address("douyin", canonical or "")[0] != video_id:
                raise ValueError("Douyin response did not identify the requested video")
            caption = await page.locator("h1").inner_text(timeout=3000)
            parts = re.split(r"(?<=[。？！])|\n", caption, maxsplit=1)
            title, body = parts if len(parts) == 2 and len(parts[1].strip()) >= 12 else (parts[0], caption)
            return make_work(canonical, video_id, title, body, image, "douyinpic.com")
        finally:
            await browser.close()


def read_work(platform, url):
    work_id, supported = work_address(platform, url)
    if not supported:
        raise ValueError("expected a public original note or video link")
    try:
        if platform == "xiaohongshu":
            return asyncio.run(read_xiaohongshu(url, work_id))
        return asyncio.run(read_douyin(url, work_id))
    except (httpx.HTTPError, Error) as exc:
        raise ValueError(f"could not read {platform} original work ({type(exc).__name__})") from exc
