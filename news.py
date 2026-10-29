import asyncio
import json
import os
import sys
import tomllib
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import httpx

from classify import Fingerprint, Rules, build_stories, fingerprint
from models import Article, Grade, Source
from render import format_briefing, format_caption, plain_text, render_chart

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "feeds.toml"
RULES_PATH = ROOT / "rules.toml"
SENT_PATH = ROOT / "state" / "sent.json"
STORIES_PATH = ROOT / "state" / "stories.jsonl"
SENT_TTL_SECONDS = 7 * 24 * 3600
STORY_TTL_SECONDS = 24 * 3600
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_TITLE = 300
MAX_PUBLISHER = 60
TELEGRAM_ATTEMPTS = 3
NO_PREVIEW = json.dumps({"is_disabled": True})
USER_AGENT = "Mozilla/5.0 (compatible; bio-news-bot)"
GOOGLE_NEWS_LOCALES = {
    "en": "hl=en-US&gl=US&ceid=US:en",
    "ko": "hl=ko&gl=KR&ceid=KR:ko",
}


def load_sources(config: dict) -> list[Source]:
    feeds = [Source(f["name"], f["url"], ZoneInfo(f["tz"])) for f in config.get("feeds", [])]
    queries = [
        Source(
            q["name"],
            f"https://news.google.com/rss/search?q={quote_plus(q['q'])}&{GOOGLE_NEWS_LOCALES[q['lang']]}",
            ZoneInfo(q["tz"]),
        )
        for q in config.get("queries", [])
    ]
    return feeds + queries


def parse_published(entry: dict, tz: ZoneInfo) -> datetime | None:
    raw = entry.get("published") or entry.get("updated")
    if raw:
        with suppress(TypeError, ValueError):
            parsed = parsedate_to_datetime(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        with suppress(ValueError):
            parsed = datetime.fromisoformat(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*struct[:6], tzinfo=UTC) if struct else None


def parse_feed(source: Source, content: bytes) -> list[Article]:
    articles = []
    for entry in feedparser.parse(content).entries:
        link = entry.get("link", "")
        if not link.startswith(("https://", "http://")):
            continue
        publisher = (entry.get("source", {}).get("title") or source.name)[:MAX_PUBLISHER]
        title = entry.get("title", link).replace("\ufeff", "").strip().removesuffix(f" - {publisher}")[:MAX_TITLE]
        articles.append(Article(source, publisher, title, link, parse_published(entry, source.tz)))
    return articles


async def fetch(client: httpx.AsyncClient, source: Source) -> list[Article]:
    content = bytearray()
    try:
        async with client.stream("GET", source.url) as response:
            response.raise_for_status()
            async for part in response.aiter_bytes():
                content += part
                if len(content) > MAX_FEED_BYTES:
                    print(f"[warn] {source.name}: feed exceeds {MAX_FEED_BYTES} bytes", file=sys.stderr)
                    return []
    except httpx.HTTPError as e:
        print(f"[warn] {source.name}: {e!r}", file=sys.stderr)
        return []
    return parse_feed(source, bytes(content))


def select_new(articles: list[Article], sent: dict[str, float], now: float, max_age_seconds: float) -> list[Article]:
    fresh: dict[str, Article] = {}
    for article in articles:
        if article.key in sent or article.key in fresh:
            continue
        if article.published is None or now - article.timestamp > max_age_seconds:
            continue
        fresh[article.key] = article
    return list(fresh.values())


def as_plain(payload: dict) -> dict:
    return {key: plain_text(value) if key in ("text", "caption") else value for key, value in payload.items() if key != "parse_mode"}


async def telegram(
    client: httpx.AsyncClient, token: str, chat_id: str, method: str, payload: dict, files: dict | None = None
) -> None:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = {"chat_id": chat_id, **payload}
    for _ in range(TELEGRAM_ATTEMPTS):
        response = await client.post(url, data=data, files=files)
        if response.status_code == 429:
            await asyncio.sleep(response.json()["parameters"]["retry_after"])
        elif response.status_code == 400 and "parse_mode" in data and "entit" in response.text.lower():
            print(f"[warn] telegram {method} rejected formatting, resending as plain text: {response.text}", file=sys.stderr)
            data = {"chat_id": chat_id, **as_plain(payload)}
        else:
            break
    if response.is_error:
        raise RuntimeError(f"telegram {method} {response.status_code}: {response.text}")


def load_sent(path: Path) -> dict[str, float]:
    return json.loads(path.read_text()) if path.exists() else {}


def prune(sent: dict[str, float], now: float) -> dict[str, float]:
    return {key: ts for key, ts in sent.items() if now - ts < SENT_TTL_SECONDS}


def load_seen(path: Path) -> list[list]:
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def remember(seen: list[list], articles: list[Article], now: float) -> list[list]:
    entries = list(seen)
    for article in articles:
        lang, tokens = fingerprint(article.title)
        entries.append([lang, sorted(tokens), now])
    return [entry for entry in entries if now - entry[2] < STORY_TTL_SECONDS]


def previous_stories(seen: list[list]) -> list[Fingerprint]:
    return [(lang, set(tokens)) for lang, tokens, _ in seen]


def plan(
    articles: list[Article], rules: Rules, seen: list[list], now: datetime, bootstrap: bool
) -> list[tuple[str, dict, dict | None]]:
    if bootstrap:
        return [("sendMessage", {"text": f"🧬 첫 실행: 기사 {len(articles)}건 기록. 다음 실행부터 새 기사만 발송"}, None)]
    if not articles:
        return []

    stories = build_stories(articles, rules, previous_stories(seen))
    caption = format_caption(stories, now)
    deliveries = []
    if any(not s.follow_up for s in stories):
        chart = {"photo": ("briefing.png", render_chart(stories, now), "image/png")}
        deliveries.append(("sendPhoto", {"caption": caption, "parse_mode": "HTML"}, chart))
        caption = ""
    deliveries += [
        ("sendMessage", {"text": text, "parse_mode": "HTML", "link_preview_options": NO_PREVIEW}, None)
        for text in format_briefing(stories, caption)
    ]

    urgent = any(s.grade <= Grade.EXCLUSIVE and not s.follow_up for s in stories)
    for index, (_, payload, _) in enumerate(deliveries):
        payload["disable_notification"] = "false" if urgent and index == 0 else "true"
    return deliveries


def save_state(sent: dict[str, float], seen: list[list], delivered: list[Article], now: float) -> None:
    sent.update({a.key: now for a in delivered})
    SENT_PATH.parent.mkdir(exist_ok=True)
    SENT_PATH.write_text(json.dumps(prune(sent, now), indent=0, sort_keys=True) + "\n")
    STORIES_PATH.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in remember(seen, delivered, now)))


async def run() -> None:
    config = tomllib.loads(CONFIG_PATH.read_text())
    rules = Rules.from_config(tomllib.loads(RULES_PATH.read_text()))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    now = datetime.now(UTC)
    bootstrap = not SENT_PATH.exists()
    sent = load_sent(SENT_PATH)
    seen = load_seen(STORIES_PATH)

    timeout = httpx.Timeout(10, connect=5)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(*(fetch(client, s) for s in load_sources(config)))
        articles = select_new([a for r in results for a in r], sent, now.timestamp(), config["max_age_hours"] * 3600)
        delivered = False
        try:
            for method, payload, files in plan(articles, rules, seen, now, bootstrap):
                if delivered:
                    await asyncio.sleep(1)
                await telegram(client, token, chat_id, method, payload, files)
                delivered = True
        finally:
            save_state(sent, seen, articles if delivered else [], now.timestamp())
    print(f"processed {len(articles)} articles, delivered={delivered}")


if __name__ == "__main__":
    asyncio.run(run())
