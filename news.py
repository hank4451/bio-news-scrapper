import asyncio
import calendar
import hashlib
import html
import json
import os
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import httpx

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "feeds.toml"
STATE_PATH = ROOT / "state" / "sent.json"
TELEGRAM_LIMIT = 4096
STATE_TTL_SECONDS = 7 * 24 * 3600
USER_AGENT = "Mozilla/5.0 (compatible; bio-news-bot)"
GOOGLE_NEWS_LOCALES = {
    "en": "hl=en-US&gl=US&ceid=US:en",
    "ko": "hl=ko&gl=KR&ceid=KR:ko",
}


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    link: str
    published: float | None

    @property
    def key(self) -> str:
        return hashlib.sha1(self.link.encode()).hexdigest()[:16]


def load_sources(config: dict) -> list[Source]:
    feeds = [Source(f["name"], f["url"]) for f in config.get("feeds", [])]
    queries = [
        Source(q["name"], f"https://news.google.com/rss/search?q={quote_plus(q['q'])}&{GOOGLE_NEWS_LOCALES[q['lang']]}")
        for q in config.get("queries", [])
    ]
    return feeds + queries


def parse_feed(source: str, content: bytes) -> list[Article]:
    articles = []
    for entry in feedparser.parse(content).entries:
        if not entry.get("link"):
            continue
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        published = calendar.timegm(parsed) if parsed else None
        articles.append(Article(source, entry.get("title", entry.link).strip(), entry.link, published))
    return articles


async def fetch(client: httpx.AsyncClient, source: Source) -> list[Article]:
    try:
        response = await client.get(source.url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        print(f"[warn] {source.name}: {e!r}", file=sys.stderr)
        return []
    return parse_feed(source.name, response.content)


def select_new(articles: list[Article], sent: dict[str, float], now: float, max_age_seconds: float) -> list[Article]:
    fresh: dict[str, Article] = {}
    for article in articles:
        if article.key in sent or article.key in fresh:
            continue
        if article.published is not None and now - article.published > max_age_seconds:
            continue
        fresh[article.key] = article
    return list(fresh.values())


def chunk(lines: list[str], limit: int) -> list[str]:
    messages = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            messages.append(current.strip())
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    messages.append(current.strip())
    return messages


def format_messages(articles: list[Article]) -> list[str]:
    by_source: dict[str, list[Article]] = {}
    for article in articles:
        by_source.setdefault(article.source, []).append(article)

    lines = [f"🧬 <b>바이오 뉴스 {len(articles)}건</b>"]
    for source, items in by_source.items():
        lines += ["", f"<b>{html.escape(source)}</b>"]
        lines += [f'• <a href="{html.escape(a.link)}">{html.escape(a.title)}</a>' for a in items]
    return chunk(lines, TELEGRAM_LIMIT)


async def send(client: httpx.AsyncClient, token: str, chat_id: str, text: str) -> None:
    response = await client.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
    )
    if response.is_error:
        raise RuntimeError(f"telegram {response.status_code}: {response.text}")


def load_state(path: Path) -> dict[str, float]:
    return json.loads(path.read_text()) if path.exists() else {}


def prune(sent: dict[str, float], now: float) -> dict[str, float]:
    return {key: ts for key, ts in sent.items() if now - ts < STATE_TTL_SECONDS}


async def run() -> None:
    config = tomllib.loads(CONFIG_PATH.read_text())
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    now = time.time()
    sent = load_state(STATE_PATH)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(*(fetch(client, s) for s in load_sources(config)))
        articles = select_new([a for r in results for a in r], sent, now, config["max_age_hours"] * 3600)
        if not sent:
            await send(client, token, chat_id, f"🧬 첫 실행: 기사 {len(articles)}건 기록. 다음 실행부터 새 기사만 발송")
        elif articles:
            for text in format_messages(articles):
                await send(client, token, chat_id, text)
                await asyncio.sleep(1)

    sent.update({a.key: now for a in articles})
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(prune(sent, now), indent=0, sort_keys=True) + "\n")
    print(f"sent {len(articles)} articles")


if __name__ == "__main__":
    asyncio.run(run())
