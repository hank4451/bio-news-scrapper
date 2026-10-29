import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

import tomllib
from pathlib import Path

from classify import Rules
from models import SEOUL, Article, Source
from news import (
    MAX_PUBLISHER,
    MAX_TITLE,
    STORY_TTL_SECONDS,
    as_plain,
    load_seen,
    load_sources,
    parse_feed,
    parse_published,
    plan,
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
<item><title>Phish</title><link>tg://resolve?domain=evil</link></item>
<item><title>Script</title><link>javascript:alert(1)</link></item>
</channel></rss>"""
RULES = Rules.from_config(tomllib.loads((Path(__file__).parent / "rules.toml").read_text()))


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


def test_parse_feed_truncates_oversized_title_and_publisher():
    huge = f"""<rss><channel><item><title>{"t" * 5000}</title><link>https://ex.com/x</link>
<source url="https://p">{"p" * 500}</source></item></channel></rss>""".encode()

    [item] = parse_feed(SOURCE, huge)

    assert len(item.title) == MAX_TITLE
    assert len(item.publisher) == MAX_PUBLISHER


def test_parse_published_applies_source_timezone_to_naive_dates():
    assert parse_published({"published": "2026-10-29 13:49:00"}, SEOUL) == datetime(2026, 10, 29, 4, 49, tzinfo=UTC)
    assert parse_published({"updated": "2026-10-28"}, NEW_YORK) == datetime(2026, 10, 28, 4, tzinfo=UTC)


def test_parse_published_keeps_explicit_offset_and_treats_unknown_offset_as_utc():
    assert parse_published({"published": "Wed, 28 Oct 2026 16:51:00 -0400"}, SEOUL) == datetime(
        2026, 10, 28, 20, 51, tzinfo=UTC
    )
    assert parse_published({"published": "Wed, 28 Oct 2026 16:51:00 -0000"}, NEW_YORK) == datetime(
        2026, 10, 28, 16, 51, tzinfo=UTC
    )


def test_parse_published_falls_back_to_feedparser_struct_then_none():
    struct = (2026, 10, 28, 0, 0, 0, 2, 301, 0)

    assert parse_published({"published": "Wed, 28 Oct 2026", "published_parsed": struct}, SEOUL) == datetime(
        2026, 10, 28, tzinfo=UTC
    )
    assert parse_published({"published": "yesterday"}, SEOUL) is None
    assert parse_published({}, SEOUL) is None


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


def test_select_new_skips_sent_duplicate_stale_and_undated():
    sent_one = article("https://ex.com/sent")
    fresh = article("https://ex.com/fresh")
    stale = article("https://ex.com/stale", published=datetime(2026, 10, 26, tzinfo=UTC))
    undated = article("https://ex.com/undated", published=None)

    result = select_new([sent_one, fresh, fresh, stale, undated], {sent_one.key: NOW}, NOW, DAY)

    assert result == [fresh]


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


ENTITY_ERROR = (400, {"ok": False, "description": "Bad Request: ENTITIES_TOO_LONG"})
CHAT_ERROR = (400, {"ok": False, "description": "Bad Request: chat not found"})
RATE_LIMITED = (429, {"ok": False, "parameters": {"retry_after": 0}})
OK = (200, {"ok": True})


def send_with(responses: list[tuple[int, dict]]) -> list[dict]:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(httpx.QueryParams(request.content.decode())))
        status, body = responses[len(calls) - 1]
        return httpx.Response(status, json=body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await telegram(client, "token", "chat", "sendMessage", {"text": "<b>hi</b>", "parse_mode": "HTML"})

    asyncio.run(go())
    return calls


def test_telegram_resends_plain_text_when_formatting_rejected():
    calls = send_with([ENTITY_ERROR, OK])

    assert calls == [
        {"chat_id": "chat", "text": "<b>hi</b>", "parse_mode": "HTML"},
        {"chat_id": "chat", "text": "hi"},
    ]


def test_telegram_raises_when_plain_text_also_fails():
    with pytest.raises(RuntimeError, match="telegram sendMessage 400"):
        send_with([ENTITY_ERROR, ENTITY_ERROR])


def test_telegram_does_not_retry_unrelated_bad_request():
    with pytest.raises(RuntimeError, match="chat not found"):
        send_with([CHAT_ERROR])


def test_telegram_waits_and_retries_when_rate_limited():
    assert len(send_with([RATE_LIMITED, OK])) == 2


def test_telegram_gives_up_after_repeated_rate_limits():
    with pytest.raises(RuntimeError, match="429"):
        send_with([RATE_LIMITED] * 3)


def kr_article(title: str, publisher: str) -> Article:
    return Article(Source("K", "https://k", SEOUL), publisher, title, f"https://ex.com/{title}", datetime(2026, 10, 29, tzinfo=UTC))


PLAN_NOW = datetime(2026, 10, 29, 5, tzinfo=UTC)


def test_plan_bootstrap_sends_only_notice():
    [(method, payload, files)] = plan([kr_article("a", "P")], RULES, [], PLAN_NOW, bootstrap=True)

    assert method == "sendMessage"
    assert "1건 기록" in payload["text"]
    assert files is None


def test_plan_nothing_new_sends_nothing():
    assert plan([], RULES, [], PLAN_NOW, bootstrap=False) == []


def test_plan_sends_chart_then_silent_text_without_urgent_news():
    deliveries = plan([kr_article("머크, 대전에 바이오 생산센터 준공", "P")], RULES, [], PLAN_NOW, bootstrap=False)

    assert [method for method, _, _ in deliveries] == ["sendPhoto", "sendMessage"]
    assert deliveries[0][2]["photo"][1].startswith(b"\x89PNG")
    assert [payload["disable_notification"] for _, payload, _ in deliveries] == ["true", "true"]


def test_plan_notifies_once_for_breaking_news():
    deliveries = plan([kr_article("[속보] 셀트리온 FDA 허가", "P")], RULES, [], PLAN_NOW, bootstrap=False)

    assert [payload["disable_notification"] for _, payload, _ in deliveries] == ["false", "true"]


def test_plan_follow_up_only_skips_chart_and_stays_silent():
    seen = remember([], [kr_article("[속보] 대원제약, 위식도역류질환 신약 3상 성공", "A")], NOW)

    deliveries = plan(
        [kr_article("[속보] 대원제약, 위식도역류질환 신약 '파도프라잔' 임상 3상 성공", "B")], RULES, seen, PLAN_NOW, False
    )

    assert [method for method, _, _ in deliveries] == ["sendMessage"]
    assert deliveries[0][1]["text"].startswith("🧬 <b>바이오 브리핑</b>")
    assert deliveries[0][1]["disable_notification"] == "true"
