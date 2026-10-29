import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from classify import Rules, build_stories, fingerprint, same_story
from models import OTHER, SEOUL, Article, Grade, Source

RULES = Rules.from_config(tomllib.loads((Path(__file__).parent / "rules.toml").read_text()))
KR = Source("KR", "https://kr", SEOUL)


def article(title: str, publisher: str = "P", hour: int = 0) -> Article:
    return Article(KR, publisher, title, f"https://ex.com/{publisher}/{title}", datetime(2026, 10, 29, hour, tzinfo=UTC))


@pytest.mark.parametrize(
    ("title", "grade"),
    [
        ("[속보] 셀트리온, 美 FDA 허가", Grade.BREAKING),
        ("Breaking: Pfizer halts trial", Grade.BREAKING),
        ("[단독] 삼성바이오, 신규 수주", Grade.EXCLUSIVE),
        ("Exclusive: Novartis weighs bid", Grade.EXCLUSIVE),
        ("Samsung Bioepis inks exclusive supply deal", Grade.GENERAL),
        ("FDA approves Lilly obesity pill", Grade.MAJOR),
        ("Astellas bets up to $1.34B on Audax", Grade.MAJOR),
        ("대원제약, 신약 임상 3상 성공", Grade.MAJOR),
        ("한미약품, 2조원 규모 기술수출", Grade.MAJOR),
        ("한올바이오파마, 3분기 누적 매출 1242억원", Grade.GENERAL),
    ],
)
def test_grade(title, grade):
    assert RULES.grade(title) == grade


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("FDA grants priority review to Merck drug", "허가·규제"),
        ("식약처, 바이오시밀러 3상 면제 기준 구체화", "허가·규제"),
        ("Anthropic acquires one-person biotech Mithril", "M&A·라이선스"),
        ("프로티나, 日 제약사 2곳과 L/O 논의", "M&A·라이선스"),
        ("Caravel Bio raises $12M Series A", "투자·IPO"),
        ("Ascletis drug hits goals in phase 2b diabetes trial", "임상"),
        ("US bans imports from 3 Chinese biotech companies", "정책"),
        ("Biogen notches 3rd straight quarter of growth", "실적·경영"),
        ("머크, 대전에 바이오 생산센터 준공", "생산·제조"),
        ("New atlas of colorectal tumors sheds light", "연구"),
        ("집중호우시 도심 물난리 잦아드나", OTHER),
    ],
)
def test_category(title, category):
    assert RULES.category(title) == category


def test_build_stories_groups_rewritten_korean_headlines():
    stories = build_stories(
        [
            article("대원제약, 위식도역류질환 신약 3상 성공…연내 허가 신청", "비즈워치"),
            article("대원제약, 위식도역류질환 신약 '파도프라잔' 임상 3상 성공", "뉴스1"),
            article("머크, 대전에 아태 바이오공정 생산센터 준공", "히트뉴스"),
        ],
        RULES,
        [],
    )

    assert [len(s.articles) for s in stories] == [2, 1]


def test_build_stories_groups_english_but_not_across_languages():
    stories = build_stories(
        [
            article("Anthropic acquires one-person biotech Mithril", "Fierce"),
            article("Anthropic buys Mithril, a one-person biotech", "Endpoints"),
            article("앤트로픽, 1인 신약 개발사 인수", "AI타임스"),
        ],
        RULES,
        [],
    )

    assert sorted(len(s.articles) for s in stories) == [1, 2]


def test_build_stories_promotes_widely_covered_story_and_orders_by_grade_then_time():
    stories = build_stories(
        [
            article("정부, 제약바이오 유망기업 200곳 육성…5년 집중 지원", "A", hour=1),
            article("정부, 제약바이오 유망기업 200곳 육성…최대 5년 지원", "B", hour=2),
            article("정부, 제약바이오 유망기업 200곳 육성", "C", hour=3),
            article("[단독] 신규 수주 소식", "D", hour=0),
            article("한올바이오파마, 3분기 누적 매출 1242억원", "E", hour=5),
            article("머크, 대전에 아태 바이오공정 생산센터 준공", "F", hour=6),
        ],
        RULES,
        [],
    )

    assert [s.grade for s in stories] == [Grade.EXCLUSIVE, Grade.MAJOR, Grade.GENERAL, Grade.GENERAL]
    assert stories[1].category == "정책"
    assert [s.lead.publisher for s in stories[2:]] == ["F", "E"]


def test_build_stories_empty():
    assert build_stories([], RULES, []) == []


def test_build_stories_marks_follow_up_of_previously_sent_story_and_sorts_it_last():
    previous = [fingerprint("대원제약, 위식도역류질환 신약 3상 성공…연내 허가 신청")]

    stories = build_stories(
        [
            article("대원제약, 위식도역류질환 신약 '파도프라잔' 임상 3상 성공", "뉴스1", hour=9),
            article("한올바이오파마, 3분기 누적 매출 1242억원", "E", hour=1),
        ],
        RULES,
        previous,
    )

    assert [(s.lead.publisher, s.follow_up) for s in stories] == [("E", False), ("뉴스1", True)]


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("SK바이오사이언스, 3분기 매출 증가", "SK바이오사이언스, 독감백신 출하", False),
        ("한미약품, 비만약 임상 3상 진입", "유한양행, 비만약 임상 3상 진입", False),
        ("삼성바이오로직스, 3분기 매출 1조 돌파", "삼성바이오로직스, 4공장 본격 가동", False),
        ("대원제약, 위식도역류질환 신약 3상 성공…연내 허가 신청", "대원제약, 위식도역류질환 신약 '파도프라잔' 임상 3상 성공", True),
        ("파도프라잔 연내 품목허가 신청 확정···임상3상 완료", "대원제약, P-CAB 신약 '파도프라잔' 임상 3상 완료", True),
        ("동구바이오제약, 30억원 규모 자사주 전량 소각 결정", "동구바이오제약, 30억원 자사주 전량 소각...주주가치 제고", True),
        ("Lilly's triple-G drug beats Ozempic", "Lilly cuts Ozempic rival price", False),
        ("Anthropic acquires one-person biotech Mithril", "Anthropic buys Mithril, a one-person biotech", True),
    ],
)
def test_same_story(a, b, same):
    assert same_story(fingerprint(a), fingerprint(b)) is same
