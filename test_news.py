import calendar
import time

from news import Article, chunk, format_messages, load_sources, parse_feed, prune, select_new

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Drug A &amp; B approved</title><link>https://ex.com/a</link><pubDate>Wed, 28 Oct 2026 20:51:12 +0000</pubDate></item>
<item><title>No date</title><link>https://ex.com/b</link></item>
<item><title>No link</title></item>
</channel></rss>"""

NOW = calendar.timegm(time.strptime("2026-10-29 00:00:00", "%Y-%m-%d %H:%M:%S"))
DAY = 24 * 3600


def article(link: str, published: float | None = NOW, source: str = "S", title: str = "t") -> Article:
    return Article(source, title, link, published)


def test_parse_feed_reads_entries_and_skips_missing_link():
    articles = parse_feed("S", RSS)

    assert [a.link for a in articles] == ["https://ex.com/a", "https://ex.com/b"]
    assert articles[0].title == "Drug A & B approved"
    assert articles[0].published == NOW - 3 * 3600 - 8 * 60 - 48
    assert articles[1].published is None


def test_parse_feed_returns_empty_on_garbage():
    assert parse_feed("S", b"<html>blocked</html>") == []


def test_load_sources_encodes_google_news_query():
    config = {
        "feeds": [{"name": "F", "url": "https://f.com/rss"}],
        "queries": [{"name": "Q", "q": "바이오 OR 신약", "lang": "ko"}],
    }

    sources = load_sources(config)

    assert sources[0].url == "https://f.com/rss"
    assert sources[1].url == (
        "https://news.google.com/rss/search?q=%EB%B0%94%EC%9D%B4%EC%98%A4+OR+%EC%8B%A0%EC%95%BD"
        "&hl=ko&gl=KR&ceid=KR:ko"
    )


def test_select_new_skips_sent_duplicate_and_stale():
    sent_one = article("https://ex.com/sent")
    fresh = article("https://ex.com/fresh")
    stale = article("https://ex.com/stale", published=NOW - 2 * DAY)
    undated = article("https://ex.com/undated", published=None)

    result = select_new([sent_one, fresh, fresh, stale, undated], {sent_one.key: NOW}, NOW, DAY)

    assert result == [fresh, undated]


def test_chunk_splits_under_limit_and_drops_leading_blank():
    assert chunk(["aaaa", "", "bbbb"], 6) == ["aaaa", "bbbb"]
    assert chunk(["aa", "bb"], 10) == ["aa\nbb"]


def test_format_messages_groups_by_source_and_escapes_html():
    articles = [
        article("https://ex.com/1?a=1&b=2", source="X", title="<A> & B"),
        article("https://ex.com/2", source="Y"),
        article("https://ex.com/3", source="X"),
    ]

    [message] = format_messages(articles)

    assert message == (
        "🧬 <b>바이오 뉴스 3건</b>\n"
        "\n<b>X</b>\n"
        '• <a href="https://ex.com/1?a=1&amp;b=2">&lt;A&gt; &amp; B</a>\n'
        '• <a href="https://ex.com/3">t</a>\n'
        "\n<b>Y</b>\n"
        '• <a href="https://ex.com/2">t</a>'
    )


def test_format_messages_splits_long_batches():
    articles = [article(f"https://ex.com/{i}", title="x" * 200) for i in range(100)]

    messages = format_messages(articles)

    assert len(messages) > 1
    assert all(len(m) <= 4096 for m in messages)
    assert sum(m.count("• ") for m in messages) == 100


def test_prune_drops_entries_older_than_ttl():
    assert prune({"old": NOW - 8 * DAY, "new": NOW - DAY}, NOW) == {"new": NOW - DAY}
