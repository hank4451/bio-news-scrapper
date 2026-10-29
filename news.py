import asyncio
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import httpx

from classify import Fingerprint, Rules, build_stories, fingerprint
from models import Article, Source
from render import format_briefing, format_caption, plain_text, render_chart

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "feeds.toml"
RULES_PATH = ROOT / "rules.toml"
SENT_PATH = ROOT / "state" / "sent.json"
STORIES_PATH = ROOT / "state" / "stories.jsonl"
SENT_TTL_SECONDS = 7 * 24 * 3600
STORY_TTL_SECONDS = 24 * 3600
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


def parse_published(raw: str | None, tz: ZoneInfo) -> datetime | None:
    if not raw:
        return None
    for parser in (parsedate_to_datetime, datetime.fromisoformat):
        try:
            parsed = parser(raw)
        except (TypeError, ValueError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    return None


def parse_feed(source: Source, content: bytes) -> list[Article]:
    articles = []
    for entry in feedparser.parse(content).entries:
        if not entry.get("link"):
            continue
        publisher = entry.get("source", {}).get("title") or source.name
        title = entry.get("title", entry.link).replace("\ufeff", "").strip().removesuffix(f" - {publisher}")
        published = parse_published(entry.get("published") or entry.get("updated"), source.tz)
        articles.append(Article(source, publisher, title, entry.link, published))
    return articles


async def fetch(client: httpx.AsyncClient, source: Source) -> list[Article]:
    try:
        response = await client.get(source.url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        print(f"[warn] {source.name}: {e!r}", file=sys.stderr)
        return []
    return parse_feed(source, response.content)


def select_new(articles: list[Article], sent: dict[str, float], now: float, max_age_seconds: float) -> list[Article]:
    fresh: dict[str, Article] = {}
    for article in articles:
        if article.key in sent or article.key in fresh:
            continue
        if article.published is not None and now - article.timestamp > max_age_seconds:
            continue
        fresh[article.key] = article
    return list(fresh.values())


def as_plain(payload: dict) -> dict:
    return {key: plain_text(value) if key in ("text", "caption") else value for key, value in payload.items() if key != "parse_mode"}


async def telegram(
    client: httpx.AsyncClient, token: str, chat_id: str, method: str, payload: dict, files: dict | None = None
) -> None:
    url = f"https://api.telegram.org/bot{token}/{method}"
    response = await client.post(url, data={"chat_id": chat_id, **payload}, files=files)
    if response.status_code == 400 and "parse_mode" in payload:
        print(f"[warn] telegram {method} rejected formatting, resending as plain text: {response.text}", file=sys.stderr)
        response = await client.post(url, data={"chat_id": chat_id, **as_plain(payload)}, files=files)
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


async def run() -> None:
    config = tomllib.loads(CONFIG_PATH.read_text())
    rules = Rules.from_config(tomllib.loads(RULES_PATH.read_text()))
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    now = datetime.now(UTC)
    sent = load_sent(SENT_PATH)
    seen = load_seen(STORIES_PATH)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(*(fetch(client, s) for s in load_sources(config)))
        articles = select_new([a for r in results for a in r], sent, now.timestamp(), config["max_age_hours"] * 3600)
        if not sent:
            await telegram(
                client, token, chat_id, "sendMessage",
                {"text": f"🧬 첫 실행: 기사 {len(articles)}건 기록. 다음 실행부터 새 기사만 발송"},
            )
        elif articles:
            stories = build_stories(articles, rules, previous_stories(seen))
            caption = format_caption(stories, now)
            messages = format_briefing(stories)
            if any(not s.follow_up for s in stories):
                await telegram(
                    client, token, chat_id, "sendPhoto",
                    {"caption": caption, "parse_mode": "HTML"},
                    {"photo": ("briefing.png", render_chart(stories, now), "image/png")},
                )
            else:
                messages.insert(0, caption)
            for text in messages:
                await asyncio.sleep(1)
                await telegram(
                    client, token, chat_id, "sendMessage",
                    {"text": text, "parse_mode": "HTML", "link_preview_options": json.dumps({"is_disabled": True})},
                )

    sent.update({a.key: now.timestamp() for a in articles})
    SENT_PATH.parent.mkdir(exist_ok=True)
    SENT_PATH.write_text(json.dumps(prune(sent, now.timestamp()), indent=0, sort_keys=True) + "\n")
    STORIES_PATH.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in remember(seen, articles, now.timestamp())))
    print(f"processed {len(articles)} articles")


if __name__ == "__main__":
    asyncio.run(run())
