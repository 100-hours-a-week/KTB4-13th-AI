"""④ 관심사별 목록의 자리 나누기·번갈아 놓기·한 작가 상한(app/feed/interest_list.py) 테스트(#447)."""

import pytest

from app.feed import interest_list as il

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
