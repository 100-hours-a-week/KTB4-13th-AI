"""④ 관심사별 목록의 자리 나누기·번갈아 놓기·한 작가 상한(app/feed/interest_list.py) 테스트(#447)."""

import pytest

from app.feed import interest_list as il
from app.feed.interest_picks import Pick

# --- 자리 나누기 ---


def test_점수에_비례해_자리를_나눈다() -> None:
    assert il.allocate({"요리": 3.0, "SF": 1.0}, total=20) == {"요리": 15, "SF": 5}


def test_나눠_떨어지지_않는_자리는_소수점_아래가_큰_관심사부터_준다() -> None:
    seats = il.allocate({"가": 1.0, "나": 1.0, "다": 1.0}, total=20)

    assert sorted(seats.values()) == [6, 7, 7]
    assert sum(seats.values()) == 20


def test_큰_것부터_다섯_관심사까지만_자리를_받는다() -> None:
    scores = {f"관심사{n}": float(10 - n) for n in range(7)}

    seats = il.allocate(scores)

    assert set(seats) == {f"관심사{n}" for n in range(5)}
    assert sum(seats.values()) == il.TOTAL_SEATS


def test_음수와_분위기는_자리를_받지_않는다() -> None:
    seats = il.allocate({"로맨스": -1.0, "분위기:잔잔한": 5.0, "SF": 1.0}, total=20)

    assert seats == {"SF": 20}


def test_자리를_받을_관심사가_없으면_빈_자리표다() -> None:
    assert il.allocate({"로맨스": -1.0}) == {}
    assert il.allocate({}) == {}


# --- 번갈아 놓기 ---


def test_자리_비율대로_번갈아_놓는다() -> None:
    lists = {"가": ["가1", "가2", "가3", "가4"], "나": ["나1", "나2"]}

    out = il.interleave(lists, {"가": 4, "나": 2}, total=6)

    assert [item for _, item in out] == ["가1", "나1", "가2", "가3", "나2", "가4"]


def test_같은_책은_처음_나온_자리에만_둔다() -> None:
    lists = {"가": ["책1", "책2"], "나": ["책1", "책3"]}

    out = il.interleave(lists, {"가": 2, "나": 2}, total=4)

    assert out == [("가", "책1"), ("나", "책3"), ("가", "책2")]


def test_책이_모자란_관심사의_자리는_다른_관심사가_채운다() -> None:
    lists = {"가": ["가1", "가2", "가3", "가4"], "나": ["나1"]}

    out = il.interleave(lists, {"가": 2, "나": 2}, total=4)

    assert [item for _, item in out] == ["가1", "나1", "가2", "가3"]


def test_자리_수를_넘게_놓지_않는다() -> None:
    lists = {"가": [f"가{n}" for n in range(30)]}

    assert len(il.interleave(lists, {"가": 10}, total=10)) == 10


# --- 한 작가 상한 ---


def _author(item: str) -> str | None:
    return item.split("-")[0] or None


def test_한_작가는_묶음마다_정한_수까지_두고_넘는_책은_다음_묶음으로_미룬다() -> None:
    items = ["김-1", "김-2", "김-3", "이-1", "박-1", "김-4"]

    out = il.cap_authors(items, _author, block=4, per_block=2)

    assert out == ["김-1", "김-2", "이-1", "박-1", "김-3", "김-4"]


def test_작가를_모르는_책은_세지_않는다() -> None:
    items = ["-1", "-2", "-3"]

    assert il.cap_authors(items, _author, block=2, per_block=1) == items


@pytest.mark.parametrize("count", [1, 7, 30])
def test_한_작가의_책만_남아도_모두_놓는다(count: int) -> None:
    items = [f"김-{n}" for n in range(count)]

    out = il.cap_authors(items, _author, block=20, per_block=5)

    assert sorted(out) == sorted(items)


# --- 조건 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("300쪽 이하", il.Condition("pages", 300, upper=True)),
        ("500쪽 이상", il.Condition("pages", 500, upper=False)),
        ("1만 원 이하", il.Condition("price", 10000, upper=True)),
        ("1만 5천 원 이하", il.Condition("price", 15000, upper=True)),
        ("9,900원 이하", il.Condition("price", 9900, upper=True)),
        ("2024년 이후", il.Condition("year", 2024, upper=False)),
        ("2000년 이전", il.Condition("year", 2000, upper=True)),
        ("번역서", None),  # 책 정보로 가를 수 없는 것
    ],
)
def test_정해진_꼴의_조건을_읽는다(text: str, expected: il.Condition | None) -> None:
    assert il.parse_condition(text) == expected


def test_책이_조건을_모두_지키는지_본다() -> None:
    conditions = [il.Condition("pages", 300, True), il.Condition("year", 2024, False)]

    assert il.meets_all(conditions, {"pages": 280, "year": 2025})
    assert not il.meets_all(conditions, {"pages": 280, "year": 2023})
    # 쪽수를 모르면 지키지 않는 것으로 본다
    assert not il.meets_all(conditions, {"pages": None, "year": 2025})
    # 조건이 없으면 앞으로 옮길 책도 없다
    assert not il.meets_all([], {"pages": 280})


# --- 조립 (#452) ---


def _pick(
    book_id: int, part: float = 0.5, title: str | None = None, author: str | None = None
) -> Pick:
    return Pick(
        book_id, None, title or f"책{book_id}", author or f"저자{book_id}", None, part
    )


def test_거른_책을_빼고_자리_비율대로_번갈아_놓는다() -> None:
    picks = {"가": [_pick(1), _pick(2), _pick(3)], "나": [_pick(4), _pick(5)]}

    placed = il.assemble(
        picks, {"가": 2, "나": 1}, keep=lambda pick: pick.book_id != 2, total=3
    )

    assert [(p.book_id, p.interest) for p in placed] == [
        (1, "가"),
        (4, "나"),
        (3, "가"),
    ]


def test_같은_책의_판본은_처음_놓인_자리에만_두고_유사도_항은_높은_쪽을_쓴다() -> None:
    picks = {
        "가": [_pick(1, part=0.2, title="해리 포터", author="J.K. 롤링, 강동혁")],
        "나": [_pick(2, part=0.9, title="해리포터", author="J.K. 롤링 지음"), _pick(3)],
    }

    placed = il.assemble(picks, {"가": 1, "나": 1}, total=2)

    assert [p.book_id for p in placed] == [1, 3]
    # 유사도 항은 두 관심사 가운데 높은 0.9, 몫 항은 1 → 0.6*0.9 + 0.25 = 0.79
    assert placed[0].match_score == 79


def test_조건을_지키는_책을_관심사_안에서_순서를_지킨_채_앞으로_옮긴다() -> None:
    picks = {"가": [_pick(1), _pick(2), _pick(3), _pick(4)]}

    placed = il.assemble(picks, {"가": 4}, meets=lambda pick: pick.book_id > 2)

    assert [p.book_id for p in placed] == [3, 4, 1, 2]


def test_몫_항은_놓인_관심사가_받은_자리로_매긴다() -> None:
    picks = {"가": [_pick(1, part=1.0)], "나": [_pick(2, part=1.0)]}

    placed = il.assemble(picks, {"가": 4, "나": 1}, total=2)

    # 가는 자리를 가장 많이 받아 몫 항 1(85점), 나는 1/4 → 0.6 + 0.25*0.25 = 0.6625
    assert {p.book_id: p.match_score for p in placed} == {1: 85, 2: 66}


def test_한_작가의_책은_20권마다_5권까지_두고_넘는_책은_뒤로_미룬다() -> None:
    by_one = [_pick(n, author="김영하") for n in range(1, 8)]
    others = [_pick(n) for n in range(8, 25)]

    placed = il.assemble({"가": by_one + others}, {"가": 24}, total=24)

    first_block = [p.book_id for p in placed[: il.AUTHOR_BLOCK]]
    assert sum(book_id <= 7 for book_id in first_block) == il.AUTHOR_PER_BLOCK
    assert len(placed) == 24
