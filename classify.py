import re
from dataclasses import dataclass

from models import OTHER, Article, Grade, Story

HANGUL = re.compile(r"[가-힣]")
NON_WORD = re.compile(r"\W+")
ENGLISH_WORD = re.compile(r"[a-z0-9$]+")
STOPWORDS = frozenset(
    "a an the of to in on for and or with by at as from is are its it this that new over after into says how why what stat plus".split()
)
MIN_SHARED = {"ko": 8, "en": 3}
MIN_OVERLAP = 0.5
MAJOR_PUBLISHERS = 3


def compile_any(patterns: list[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


@dataclass(frozen=True)
class Rules:
    breaking: re.Pattern
    exclusive: re.Pattern
    major: re.Pattern
    categories: list[tuple[str, re.Pattern]]

    @classmethod
    def from_config(cls, config: dict) -> "Rules":
        grades = config["grades"]
        return cls(
            compile_any(grades["breaking"]),
            compile_any(grades["exclusive"]),
            compile_any(grades["major"]),
            [(c["name"], compile_any(c["patterns"])) for c in config["categories"]],
        )

    def category(self, title: str) -> str:
        for name, pattern in self.categories:
            if pattern.search(title):
                return name
        return OTHER

    def grade(self, title: str) -> Grade:
        if self.breaking.search(title):
            return Grade.BREAKING
        if self.exclusive.search(title):
            return Grade.EXCLUSIVE
        if self.major.search(title):
            return Grade.MAJOR
        return Grade.GENERAL


def fingerprint(title: str) -> tuple[str, set[str]]:
    title = title.lower()
    if HANGUL.search(title):
        compact = NON_WORD.sub("", title)
        return "ko", {compact[i : i + 2] for i in range(len(compact) - 1)}
    return "en", {w for w in ENGLISH_WORD.findall(title) if len(w) > 1 and w not in STOPWORDS}


def same_story(a: tuple[str, set[str]], b: tuple[str, set[str]]) -> bool:
    (lang, a_tokens), (other_lang, b_tokens) = a, b
    if lang != other_lang:
        return False
    shared = len(a_tokens & b_tokens)
    return shared >= MIN_SHARED[lang] and shared / min(len(a_tokens), len(b_tokens)) >= MIN_OVERLAP


def make_story(articles: list[Article], rules: Rules) -> Story:
    grade = min(rules.grade(a.title) for a in articles)
    if grade == Grade.GENERAL and len({a.publisher for a in articles}) >= MAJOR_PUBLISHERS:
        grade = Grade.MAJOR
    categories = [rules.category(a.title) for a in articles]
    return Story(articles, grade, next((c for c in categories if c != OTHER), OTHER))


def build_stories(articles: list[Article], rules: Rules) -> list[Story]:
    groups: list[tuple[tuple[str, set[str]], list[Article]]] = []
    for article in articles:
        mark = fingerprint(article.title)
        for lead_mark, members in groups:
            if same_story(lead_mark, mark):
                members.append(article)
                break
        else:
            groups.append((mark, [article]))
    stories = [make_story(members, rules) for _, members in groups]
    return sorted(stories, key=lambda s: (s.grade, -s.lead.timestamp))
