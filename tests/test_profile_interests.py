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


# --- 조건 목록 ---


def test_조건은_늘_지킨다고_말한_것만_저장한_때_순으로_한_번씩() -> None:
    lines = [
        Line("condition", 2, "2만 원 이하", day=3),
        Line("condition", 2, "300쪽 이하", day=1),
        Line("condition", 1, "2020년 이후", day=2),  # 부탁할 때만 붙인 것
        Line("condition", 3, "300쪽 이하", day=4),
        _topic(2, "SF"),
    ]

    assert interests.conditions(lines) == ["300쪽 이하", "2만 원 이하"]


# --- 챗봇 벡터에 섞을 줄 ---


def test_살아_있는_좋아함_줄만_고른다() -> None:
    like_sf = _topic(2, "SF", day=0)
    like_romance = _topic(2, "로맨스", day=0)
    avoid_romance = _topic(-2, "로맨스", day=1)
    title = _topic(2, keyword="『데미안』", day=0)
    condition = Line("condition", 2, "300쪽 이하", day=0)
    request_to_avoid = _topic(-1, "SF", day=2)

    kept = interests.living_likes(
        [like_sf, like_romance, avoid_romance, title, condition, request_to_avoid]
    )

    # 로맨스는 뒤에 싫다고 했다. 피함의 부탁(-1)은 싫다는 뜻이 아니라 SF 는 남는다
    assert kept == [like_sf, title]


def test_싫다고_한_뒤에_다시_좋아한_줄은_남는다() -> None:
    avoid = _topic(-2, "로맨스", day=0)
    like = _topic(2, "로맨스", day=1)

    assert interests.living_likes([avoid, like]) == [like]


def test_어디에_가운데_하나라도_뒤에_피했으면_섞지_않는다() -> None:
    both = _topic(2, "추리/스릴러", "소설", day=0)
    avoid = _topic(-2, "소설", day=1)

    assert interests.living_likes([both, avoid]) == []


# --- 책을 관심사로 가르기 (#437) ---


def _axis(i: int) -> list[float]:
    vector = [0.0] * 4
    vector[i] = 1.0
    return vector


# 세부 카테고리 넷만 이름 벡터가 있다고 친다. 책 벡터가 가리키는 축의 세부 카테고리가 가장 가깝다
_NAMED = {"추리/스릴러": _axis(0), "SF": _axis(1), "로맨스": _axis(2), "요리": _axis(3)}


def _book(class_no, category, vector=None) -> str | None:
    return interests.book_interest(class_no, category, vector, _NAMED.get)


def test_세부_분류_번호가_표에_있으면_그_관심사다() -> None:
    assert _book("594.5", "생활과학", _axis(0)) == "요리"
    assert _book("004.5", "총류") == "IT"


def test_표에_없는_문학_번호는_이름_벡터가_가장_가까운_세부_카테고리다() -> None:
    assert _book("813.7", "한국문학", _axis(1)) == "SF"


def test_번호가_없는_문학_책도_이름_벡터로_가른다() -> None:
    assert _book(None, "한국소설", _axis(2)) == "로맨스"


def test_번호가_없는_그_밖의_책은_도서관_분류가_속한_카테고리다() -> None:
    assert _book(None, "경제학", _axis(0)) == "경제경영"
    # 표에 없는 번호도 같다(분류로 본다)
    assert _book("210", "지리", _axis(0)) == "여행"


def test_어느_카테고리에도_안_들면_이름_벡터로_가른다() -> None:
    assert _book(None, "종교", _axis(3)) == "요리"


def test_이름_벡터가_필요한데_책_벡터가_없으면_가르지_않는다() -> None:
    assert _book("813.7", "한국문학", None) is None
    assert _book(None, "종교", None) is None


def test_가장_가까운_세부_카테고리는_이름_벡터가_있는_것_가운데서_고른다() -> None:
    vector = [0.6, 0.8, 0.0, 0.0]

    assert interests.nearest_subcategory(vector, _NAMED.get) == "SF"
    assert interests.nearest_subcategory(vector, lambda _name: None) is None
