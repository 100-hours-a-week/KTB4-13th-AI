"""⑥ 취향 기억 → 관심사 점수(app/profile/interests.py) 테스트(#435). DB·모델 없이 돈다."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.profile import interests

# BE 는 한국 시간으로 보낸다(명세 ⑥ memories[].created_at)
_DAY0 = datetime(2026, 10, 1, 0, 10, tzinfo=ZoneInfo("Asia/Seoul"))


@dataclass(frozen=True)
class Line:
    type: str
    strength: int
    keyword: str
    applies_to: tuple[str, ...] = ()
    day: int = 0
    created_at: datetime = _DAY0

    def __post_init__(self) -> None:
        # 날짜를 숫자로 적으면 그날의 저장한 때로 바꾼다
        object.__setattr__(self, "created_at", _DAY0 + timedelta(days=self.day))


def _topic(
    strength: int, *names: str, keyword: str = "추리 소설", day: int = 0
) -> Line:
    return Line("topic", strength, keyword, names, day)


# --- 한 줄이 어느 관심사에 점수를 주나 ---


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (_topic(2, "추리/스릴러"), ("추리/스릴러",)),
        (_topic(2, "추리/스릴러", "소설"), ("추리/스릴러", "소설")),
        (_topic(2, keyword="고양이"), ("키워드:고양이",)),  # 어디에가 비면 핵심어로
        (_topic(2, keyword="『데미안』"), ()),  # 제목만 말한 줄은 관심사가 없다
        (_topic(2, "소설", keyword="『데미안』"), ()),
        (Line("author", 2, "김영하"), ("작가:김영하",)),
        (Line("mood", 2, "잔잔한"), ("분위기:잔잔한",)),
        (Line("condition", 2, "300쪽 이하"), ()),
    ],
)
def test_종류와_어디에로_관심사를_정한다(line: Line, expected: tuple[str, ...]) -> None:
    assert interests.interests_of(line) == expected


# --- 점수 ---


def test_좋아함은_쌓인다() -> None:
    lines = [_topic(2, "추리/스릴러", day=0), _topic(2, "추리/스릴러", day=7)]

    assert interests.memory_scores(lines) == {"추리/스릴러": 4.0}


def test_어디에가_여럿이면_세기를_나눠_준다() -> None:
    scores = interests.memory_scores([_topic(2, "추리/스릴러", "소설")])

    assert scores == {"추리/스릴러": 1.0, "소설": 1.0}


def test_마지막_줄이_피함이면_음수다() -> None:
    lines = [
        _topic(2, "로맨스", day=0),
        _topic(2, "로맨스", day=1),
        _topic(-2, "로맨스", day=2),
    ]

    # 두 번 좋아한다고 했어도 마지막에 싫다고 했으면 뺀다
    assert interests.memory_scores(lines) == {"로맨스": -2.0}


def test_피함_뒤의_좋아함만_더한다() -> None:
    lines = [
        _topic(3, "로맨스", day=0),
        _topic(-2, "로맨스", day=1),
        _topic(1, "로맨스", day=2),
        _topic(2, "로맨스", day=3),
    ]

    assert interests.memory_scores(lines) == {"로맨스": 3.0}


def test_순서는_받은_차례가_아니라_저장한_때로_본다() -> None:
    lines = [_topic(-2, "로맨스", day=5), _topic(2, "로맨스", day=1)]

    assert interests.memory_scores(lines) == {"로맨스": -2.0}


def test_피함의_부탁은_건너뛰어_방향을_바꾸지_않는다() -> None:
    lines = [_topic(2, "로맨스", day=0), _topic(-1, "로맨스", day=1)]

    assert interests.memory_scores(lines) == {"로맨스": 2.0}


def test_관심사마다_따로_센다() -> None:
    lines = [
        _topic(2, "SF", day=0),
        _topic(-2, "추리/스릴러", day=1),
        Line("author", 3, "김영하", day=2),
        _topic(2, keyword="『데미안』", day=3),
        Line("condition", 2, "300쪽 이하", day=4),
    ]

    assert interests.memory_scores(lines) == {
        "SF": 2.0,
        "추리/스릴러": -2.0,
        "작가:김영하": 3.0,
    }
