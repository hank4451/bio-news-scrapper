from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from models import SEOUL, Article, Grade, Source, Story
from render import (
    MAX_ENTITIES,
    TELEGRAM_LIMIT,
    chunk,
    entities,
    format_briefing,
    format_caption,
    format_time,
    plain_text,
    render_chart,
    shorten,
    visible_len,
)

NEW_YORK = ZoneInfo("America/New_York")
UTC_ZONE = ZoneInfo("UTC")
US = Source("US", "https://us", NEW_YORK)
KR = Source("KR", "https://kr", SEOUL)
NOW = datetime(2026, 10, 29, 5, 7, tzinfo=UTC)


def story(
    title: str, grade: Grade, category: str = "임상", source: Source = US, publisher: str = "STAT", extra=(), follow_up=False
) -> Story:
    lead = Article(source, publisher, title, f"https://ex.com/{title}?a=1&b=2", datetime(2026, 10, 28, 20, 51, tzinfo=UTC))
    return Story([lead, *extra], grade, category, follow_up)


def test_format_time_shows_local_zone_and_kst():
    published = datetime(2026, 10, 28, 20, 51, tzinfo=UTC)

    assert format_time(published, NEW_YORK) == "10/28 16:51 EDT (KST 10/29 05:51)"
    assert format_time(published, UTC_ZONE) == "10/28 20:51 UTC (KST 10/29 05:51)"
    assert format_time(published, SEOUL) == "10/29 05:51 KST"
    assert format_time(datetime(2026, 12, 1, 15, tzinfo=UTC), NEW_YORK) == "12/01 10:00 EST (KST 12/02 00:00)"
    assert format_time(None, SEOUL) == "시간 미상"


def test_chunk_measures_visible_text_only():
    long_link = f'<a href="https://ex.com/{"x" * 5000}">t</a>'

    assert chunk([long_link, long_link], 10) == [f"{long_link}\n{long_link}"]
    assert chunk(["aaaa", "", "bbbb"], 6) == ["aaaa", "bbbb"]
    assert visible_len("<b>a&amp;b</b>") == 3
    assert visible_len("🇰🇷") == 4
    assert chunk(["<b>a</b>", "<i>b</i>", "<b>c</b>"], 100, 2) == ["<b>a</b>\n<i>b</i>", "<b>c</b>"]


def test_format_briefing_orders_grades_and_escapes_html():
    related = Article(US, "Fierce", "dup", "https://ex.com/dup", None)
    same_publisher = Article(US, "STAT", "dup2", "https://ex.com/dup2", None)
    stories = [
        story("<Big> & approval", Grade.MAJOR, "허가·규제", extra=(related, same_publisher)),
        story("국내 소식", Grade.GENERAL, "임상", source=KR, publisher="히트뉴스"),
        story("Breaking news", Grade.BREAKING),
    ]

    [message] = format_briefing(stories)

    assert message.index("🔴 속보") < message.index("🟡 주요") < message.index("⚪ 일반")
    assert '<b><a href="https://ex.com/&lt;Big&gt; &amp; approval?a=1&amp;b=2">&lt;Big&gt; &amp; approval</a></b>' in message
    assert "<i>🌐 STAT · 10/28 16:51 EDT (KST 10/29 05:51) · 허가·규제</i> ↳ " in message
    assert '<a href="https://ex.com/dup">Fierce</a>' in message
    assert "dup2" not in message
    assert "<blockquote expandable><b>▸ 임상 1</b>\n• " in message
    assert "<i>— 🇰🇷 히트뉴스 · 10/29 05:51 KST</i></blockquote>" in message


def test_format_briefing_splits_long_general_section_into_closed_blockquotes():
    stories = [story(f"{i} {'x' * 200}", Grade.GENERAL) for i in range(60)]

    messages = format_briefing(stories)

    assert len(messages) > 1
    assert all(visible_len(m) <= TELEGRAM_LIMIT for m in messages)
    assert all(m.count("<blockquote expandable>") == m.count("</blockquote>") for m in messages)
    assert sum(m.count("• ") for m in messages) == 60


def test_format_briefing_keeps_each_message_under_entity_limit():
    stories = [story(f"{i}", Grade.GENERAL) for i in range(80)] + [story(f"m{i}", Grade.MAJOR) for i in range(40)]

    messages = format_briefing(stories)

    assert all(entities(m) <= MAX_ENTITIES for m in messages)
    assert all(m.count("<blockquote expandable>") == m.count("</blockquote>") for m in messages)
    assert sum(m.count("• ") for m in messages) == 80


def test_format_caption_counts_articles_stories_and_grades():
    extra = Article(US, "Fierce", "dup", "https://ex.com/dup", None)
    stories = [story("a", Grade.MAJOR, extra=(extra,)), story("b", Grade.GENERAL), story("c", Grade.MAJOR, follow_up=True)]

    assert format_caption(stories, NOW) == (
        "🧬 <b>바이오 브리핑</b> · 10/29 14:07 KST\n기사 4건 · 새 사건 2건\n🟡 주요 1 · ⚪ 일반 1 · ↻ 후속 보도 1"
    )


def test_render_chart_returns_png():
    stories = [story("a", Grade.GENERAL, "임상"), story("b", Grade.GENERAL, "기타", source=KR)]

    assert render_chart(stories, NOW).startswith(b"\x89PNG")


def test_shorten_keeps_short_titles_and_cuts_long_ones():
    assert shorten("짧은 제목") == "짧은 제목"
    assert shorten("가" * 41) == "가" * 39 + "…"
    assert len(shorten("x" * 100)) == 40


def test_format_briefing_shortens_general_but_keeps_headline_titles_full():
    long_title = "대원제약, 위식도역류질환 신약 파도프라잔 임상 3상 성공…연내 품목허가 신청 추진"

    [message] = format_briefing([story(long_title, Grade.MAJOR), story(long_title + " 2", Grade.GENERAL)])

    assert f">{long_title}</a></b>" in message
    assert f">{shorten(long_title + ' 2')}</a>" in message


def test_format_briefing_lists_follow_ups_last_in_collapsed_section():
    stories = [story("new", Grade.GENERAL), story("old major", Grade.MAJOR, follow_up=True)]

    [message] = format_briefing(stories)

    assert "🟡 주요" not in message
    assert message.index("⚪ 일반 1") < message.index("↻ 후속 보도 1")
    assert message.endswith("</blockquote>")
    assert "old major" in message.split("↻ 후속 보도 1")[1]


def test_plain_text_strips_tags_and_entities():
    assert plain_text('<b><a href="https://x">A &amp; B</a></b>') == "A & B"


def test_render_chart_ignores_follow_ups():
    stories = [story("a", Grade.GENERAL, "임상"), story("b", Grade.GENERAL, "정책", follow_up=True)]

    assert render_chart(stories, NOW).startswith(b"\x89PNG")
