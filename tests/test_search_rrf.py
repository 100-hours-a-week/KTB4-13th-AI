"""순위 합치기(RRF) 테스트."""

from app.search import rrf


def test_두_목록에_다_나온_책이_한쪽에서만_1등인_책보다_위다() -> None:
    assert rrf.fuse([[1, 2, 3], [9, 2, 8]])[0] == 2


def test_한쪽에만_있는_책도_결과에_남는다() -> None:
    assert set(rrf.fuse([[1, 2], [3]])) == {1, 2, 3}


def test_점수가_같으면_book_id가_작은_쪽이_먼저라_결과가_고정된다() -> None:
    assert rrf.fuse([[7], [3]]) == [3, 7]


def test_비중을_주면_그_목록의_1등이_이긴다() -> None:
    assert rrf.fuse([[7], [3]], weights=[3.0, 1.0]) == [7, 3]


def test_빈_목록끼리_합치면_빈_목록이다() -> None:
    assert rrf.fuse([[], []]) == []
