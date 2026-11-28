import asyncio
import json

import httpx
import pytest

from src import public_social_boards, public_social_works
from src.public_social_works import xiaohongshu_note


def test_xiaohongshu_app_share_link_reports_login_instead_of_accepting_placeholder(monkeypatch):
    share = "https://xhslink.cn/o/Fixture123"

    async def fetch(client, method, url):
        assert method == "GET" and url == share
        request = httpx.Request("GET", "https://www.xiaohongshu.com/login")
        return httpx.Response(200, request=request, text="小红书登录")

    monkeypatch.setattr(public_social_works, "safe_request", fetch)
    assert public_social_works.work_address("xiaohongshu", share) == (None, True)
    assert public_social_works.work_address("xiaohongshu", "https://xhslink.cn/other/abc") == (None, False)
    with pytest.raises(ValueError, match="requires sign-in"):
        asyncio.run(public_social_works.read_xiaohongshu(share, None))


def test_note_requires_matching_identity_and_uses_note_cover():
    note_id = "c" * 24
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    note = {"@type": "Article", "name": "AI 辅助创作 - 小红书",
            "description": "这是作品笔记正文，介绍如何在自己绘图时试用 AI。",
            "image": ["http://sns-webpic-qc.xhscdn.com/note-cover.jpg"]}
    page = ('<meta property="og:image" content="https://picasso-static.xiaohongshu.com/logo.png">'
            f'<script type="application/ld+json">{json.dumps(note)}</script>')

    work = xiaohongshu_note(url, note_id, page)
    assert work["id"] == note_id
    assert work["title"] == "AI 辅助创作"
    assert work["content"] == note["description"]
    assert work["cover_url"] == "https://sns-webpic-qc.xhscdn.com/note-cover.jpg"
    with pytest.raises(ValueError, match="requested note"):
        xiaohongshu_note(url, "d" * 24, page)
    with pytest.raises(ValueError, match="original text and image"):
        xiaohongshu_note(url, note_id, page.split("<script")[0])


def test_board_links_and_configured_topics_require_original_work(monkeypatch):
    url = "https://www.douyin.com/video/123"
    topic = "AI是否已证明黎曼猜想"
    original = {"id": "123", "url": url, "title": "一场数学采访",
                "content": "采访讨论 Claude 和黎曼猜想。", "cover_url": "https://cdn.douyinpic.com/cover.jpg"}
    calls = []

    def read(platform, work_url):
        calls.append((platform, work_url))
        return original

    monkeypatch.setattr(public_social_boards, "read_work", read)
    rows = [
        {"title": topic, "search_url": "https://www.douyin.com/search/math"},
        {"title": "另一条热搜", "search_url": url},
        {"title": "错配的话题", "search_url": "https://www.douyin.com/search/invalid"},
        {"title": "只有搜索链接", "search_url": "https://www.douyin.com/search/missing"},
    ]
    board = {"platform": "douyin", "kind": "general", "status": "ok", "rows": rows}
    public_social_boards.resolve_general_works([board], [
        {"platform": "douyin", "topic": topic, "url": url, "terms": ["Claude", "黎曼猜想"]},
        {"platform": "douyin", "topic": "错配的话题", "url": url, "terms": ["不在作品里的词"]},
    ])

    assert calls == [("douyin", url)]
    assert rows[0]["work"]["association"]["kind"] == "verified_topic_link"
    assert rows[1]["work"]["association"]["kind"] == "board_links_work"
    assert "work" not in rows[2] and "topic evidence" in rows[2]["work_error"]
    assert "work" not in rows[3] and "without an identified" in rows[3]["work_error"]
