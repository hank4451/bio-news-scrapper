import html
import io
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
from matplotlib import font_manager, pyplot as plt

from models import OTHER, SEOUL, Article, Grade, Story

TELEGRAM_LIMIT = 4000
MAX_ENTITIES = 100
TAG = re.compile(r"<[^>]+>")
OPEN_TAG = re.compile(r"<[a-z]")
GRADE_LABELS = {
    Grade.BREAKING: "🔴 속보",
    Grade.EXCLUSIVE: "🟠 단독",
    Grade.MAJOR: "🟡 주요",
    Grade.GENERAL: "⚪ 일반",
}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_MUTED = "#52514e"
REGIONS = (("해외", False, "#2a78d6"), ("국내", True, "#eb6834"))

for font in (Path(__file__).parent / "fonts").glob("*.ttf"):
    font_manager.fontManager.addfont(str(font))
plt.rcParams.update({"font.family": "NanumGothic", "axes.unicode_minus": False})


def format_time(published: datetime | None, tz: ZoneInfo) -> str:
    if published is None:
        return "시간 미상"
    local = f"{published.astimezone(tz):%m/%d %H:%M %Z}"
    if tz.key == SEOUL.key:
        return local
    return f"{local} (KST {published.astimezone(SEOUL):%m/%d %H:%M})"


def visible_len(text: str) -> int:
    return len(html.unescape(TAG.sub("", text)).encode("utf-16-le")) // 2


def entities(text: str) -> int:
    return len(OPEN_TAG.findall(text))


def chunk(lines: list[str], limit: int, max_entities: int = MAX_ENTITIES) -> list[str]:
    messages = []
    current = ""
    for line in lines:
        too_long = visible_len(current) + visible_len(line) + 1 > limit
        if current and (too_long or entities(current) + entities(line) > max_entities):
            messages.append(current.strip())
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    messages.append(current.strip())
    return messages


def link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url)}">{html.escape(text)}</a>'


def meta(article: Article) -> str:
    flag = "🇰🇷" if article.source.domestic else "🌐"
    return f"{flag} {html.escape(article.publisher)} · {format_time(article.published, article.source.tz)}"


def related(story: Story) -> str:
    seen = {story.lead.publisher}
    links = []
    for article in story.articles[1:]:
        if article.publisher not in seen:
            seen.add(article.publisher)
            links.append(link(article.link, article.publisher))
    return f" ↳ {', '.join(links)}" if links else ""


def headline(story: Story) -> str:
    lead = story.lead
    return f"<b>{link(lead.link, lead.title)}</b>\n<i>{meta(lead)} · {story.category}</i>{related(story)}"


def general_lines(stories: list[Story]) -> list[str]:
    by_category: dict[str, list[Story]] = {}
    for story in stories:
        by_category.setdefault(story.category, []).append(story)

    lines = []
    for category, items in sorted(by_category.items(), key=lambda kv: (kv[0] == OTHER, -len(kv[1]))):
        lines.append(f"<b>▸ {category} {len(items)}</b>")
        lines += [f"• {link(s.lead.link, s.lead.title)} <i>— {meta(s.lead)}</i>{related(s)}" for s in items]
    return lines


def format_briefing(stories: list[Story]) -> list[str]:
    blocks = []
    for grade in Grade:
        items = [s for s in stories if s.grade == grade]
        if not items:
            continue
        blocks += ["", f"<b>{GRADE_LABELS[grade]} {len(items)}</b>"]
        if grade == Grade.GENERAL:
            pieces = chunk(general_lines(items), TELEGRAM_LIMIT, MAX_ENTITIES - 1)
            blocks += [f"<blockquote expandable>{piece}</blockquote>" for piece in pieces]
        else:
            for story in items:
                blocks += ["", headline(story)]
    return chunk(blocks, TELEGRAM_LIMIT)


def format_caption(stories: list[Story], now: datetime) -> str:
    grades = Counter(s.grade for s in stories)
    summary = " · ".join(f"{GRADE_LABELS[g]} {grades[g]}" for g in Grade if grades[g])
    articles = sum(len(s.articles) for s in stories)
    return (
        f"🧬 <b>바이오 브리핑</b> · {now.astimezone(SEOUL):%m/%d %H:%M} KST\n"
        f"기사 {articles}건 · 사건 {len(stories)}건\n{summary}"
    )


def render_chart(stories: list[Story], now: datetime) -> bytes:
    counts = Counter((s.category, s.lead.source.domestic) for s in stories)
    totals = Counter(s.category for s in stories)
    categories = sorted(totals, key=lambda c: (c == OTHER, -totals[c]))
    positions = range(len(categories))

    fig, ax = plt.subplots(figsize=(8, 0.55 * len(categories) + 1.4), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    left = [0] * len(categories)
    for label, domestic, color in REGIONS:
        values = [counts[(c, domestic)] for c in categories]
        ax.barh(positions, values, left=left, height=0.62, color=color, edgecolor=SURFACE, linewidth=2, label=label)
        left = [a + b for a, b in zip(left, values)]

    for y, total in enumerate(left):
        ax.text(total + max(left) * 0.015, y, str(total), va="center", color=INK, fontsize=11, fontweight="bold")

    ax.set_yticks(positions, categories)
    ax.invert_yaxis()
    ax.set_xticks([])
    ax.set_xlim(0, max(left) * 1.1)
    ax.tick_params(axis="y", length=0, labelsize=12, labelcolor=INK)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1), ncols=2, frameon=False, labelcolor=INK_MUTED, fontsize=10)
    ax.set_title(
        f"분야별 사건 수 · {now.astimezone(SEOUL):%m/%d %H:%M} KST", loc="left", color=INK, fontsize=14, fontweight="bold", pad=12
    )

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return buffer.getvalue()
