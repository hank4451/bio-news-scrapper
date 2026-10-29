import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from models import SEOUL, Article, Source
from news import (
    STORY_TTL_SECONDS,
    as_plain,
    load_seen,
    load_sources,
    parse_feed,
    parse_published,
    previous_stories,
    prune,
    remember,
    select_new,
    telegram,
)

NEW_YORK = ZoneInfo("America/New_York")
SOURCE = Source("S", "https://s.com/rss", NEW_YORK)
NOW = datetime(2026, 10, 29, tzinfo=UTC).timestamp()
DAY = 24 * 3600

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>\xef\xbb\xbfDrug A &amp; B approved</title><link>https://ex.com/a</link><pubDate>Wed, 28 Oct 2026 20:51:12 +0000</pubDate></item>
<item><title>Merck opens hub - Reuters</title><link>https://ex.com/g</link><source url="https://reuters.com">Reuters</source></item>
<item><title>No link</title></item>
</channel></rss>"""


def article(link: str, published: datetime | None = datetime(2026, 10, 28, 12, tzinfo=UTC)) -> Article:
    return Article(SOURCE, "S", "t", link, published)


def test_parse_feed_reads_entries_publisher_and_cleans_title():
    first, google = parse_feed(SOURCE, RSS)

    assert first.title == "Drug A & B approved"
    assert first.publisher == "S"
    assert first.published == datetime(2026, 10, 28, 20, 51, 12, tzinfo=UTC)
    assert google.title == "Merck opens hub"
    assert google.publisher == "Reuters"
    assert google.published is None


def test_parse_feed_returns_empty_on_garbage():
    assert parse_feed(SOURCE, b"<html>blocked</html>") == []


def test_parse_published_applies_source_timezone_to_naive_dates():
    assert parse_published("2026-10-29 13:49:00", SEOUL) == datetime(2026, 10, 29, 4, 49, tzinfo=UTC)
    assert parse_published("2026-10-28", NEW_YORK) == datetime(2026, 10, 28, 4, tzinfo=UTC)


def test_parse_published_keeps_explicit_offset_and_rejects_garbage():
    assert parse_published("Wed, 28 Oct 2026 16:51:00 -0400", SEOUL) == datetime(2026, 10, 28, 20, 51, tzinfo=UTC)
    assert parse_published("yesterday", SEOUL) is None
    assert parse_published(None, SEOUL) is None


def test_load_sources_encodes_google_news_query_and_timezone():
    config = {
        "feeds": [{"name": "F", "url": "https://f.com/rss", "tz": "Europe/London"}],
        "queries": [{"name": "Q", "q": "바이오 OR 신약", "lang": "ko", "tz": "Asia/Seoul"}],
    }

    feed, query = load_sources(config)

    assert feed.tz.key == "Europe/London"
    assert not feed.domestic
    assert query.domestic
    assert query.url == (
        "https://news.google.com/rss/search?q=%EB%B0%94%EC%9D%B4%EC%98%A4+OR+%EC%8B%A0%EC%95%BD"
        "&hl=ko&gl=KR&ceid=KR:ko"
    )


def test_select_new_skips_sent_duplicate_and_stale():
    sent_one = article("https://ex.com/sent")
    fresh = article("https://ex.com/fresh")
    stale = article("https://ex.com/stale", published=datetime(2026, 10, 26, tzinfo=UTC))
    undated = article("https://ex.com/undated", published=None)

    result = select_new([sent_one, fresh, fresh, stale, undated], {sent_one.key: NOW}, NOW, DAY)

    assert result == [fresh, undated]


def test_prune_drops_entries_older_than_ttl():
    assert prune({"old": NOW - 8 * DAY, "new": NOW - DAY}, NOW) == {"new": NOW - DAY}


def test_remember_adds_fingerprints_and_expires_old_ones(tmp_path):
    seen = [["en", ["old"], NOW - STORY_TTL_SECONDS], ["en", ["kept"], NOW - 60]]

    entries = remember(seen, [Article(SOURCE, "S", "Anthropic acquires Mithril", "https://x", None)], NOW)

    assert entries == [["en", ["kept"], NOW - 60], ["en", ["acquires", "anthropic", "mithril"], NOW]]
    assert previous_stories(entries)[1] == ("en", {"acquires", "anthropic", "mithril"})

    path = tmp_path / "stories.jsonl"
    assert load_seen(path) == []
    path.write_text('["ko", ["대원"], 1.0]\n\n')
    assert load_seen(path) == [["ko", ["대원"], 1.0]]


def test_as_plain_strips_formatting():
    payload = {"text": "<b>A &amp; B</b>", "parse_mode": "HTML", "link_preview_options": "{}"}

    assert as_plain(payload) == {"text": "A & B", "link_preview_options": "{}"}


def send_with(responses: list[int]) -> list[dict]:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(responses[len(calls) - 1], json={"ok": False})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await telegram(client, "token", "chat", "sendMessage", {"text": "<b>hi</b>", "parse_mode": "HTML"})

    asyncio.run(go())
    return calls


def test_telegram_resends_plain_text_when_formatting_rejected():
    calls = send_with([400, 200])

    assert calls == [
        {"chat_id": "chat", "text": "<b>hi</b>", "parse_mode": "HTML"},
        {"chat_id": "chat", "text": "hi"},
    ]


def test_telegram_raises_when_plain_text_also_fails():
    with pytest.raises(RuntimeError, match="telegram sendMessage 400"):
        send_with([400, 400])
