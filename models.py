import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")
OTHER = "기타"


class Grade(IntEnum):
    BREAKING = 0
    EXCLUSIVE = 1
    MAJOR = 2
    GENERAL = 3


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    tz: ZoneInfo

    @property
    def domestic(self) -> bool:
        return self.tz.key == SEOUL.key


@dataclass(frozen=True)
class Article:
    source: Source
    publisher: str
    title: str
    link: str
    published: datetime | None

    @property
    def key(self) -> str:
        return hashlib.sha1(self.link.encode()).hexdigest()[:16]

    @property
    def timestamp(self) -> float:
        return self.published.timestamp() if self.published else 0.0


@dataclass(frozen=True)
class Story:
    articles: list[Article]
    grade: Grade
    category: str

    @property
    def lead(self) -> Article:
        return self.articles[0]
