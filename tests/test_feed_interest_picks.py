"""④ 관심사마다 분류 안에서 책 뽑기(app/feed/interest_picks.py) 테스트(#449). DB 없이 돈다."""

import pytest

from app.feed import interest_picks

# --- 어느 분류에서 뽑나 ---


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("여행", ("지리",)),  # 카테고리는 그 카테고리의 분류
        ("우주", ("천문학",)),  # 세부 카테고리 → 도서관 분류 대응표
        ("요리", ("생활과학", "오락·스포츠")),  # 대응표에 없으면 속한 카테고리의 분류
        ("어린이", None),  # 분류로 좁힐 수 없는 카테고리
        ("그림책", None),  # 대응표가 "거르지 않음"인 세부 카테고리
        ("어린이 지식·교양", None),  # 속한 카테고리를 분류로 좁힐 수 없다
        ("의학", None),  # 어느 카테고리에도 속하지 않는다
    ],
)
def test_관심사를_뽑을_도서관_분류(name: str, expected: tuple[str, ...] | None) -> None:
    assert interest_picks.classes_of(name) == expected


def test_추리_스릴러는_소설의_분류_안에서_뽑는다() -> None:
    assert interest_picks.classes_of("추리/스릴러") == interest_picks.classes_of("소설")


def test_번호가_이_관심사로_이어지는_책을_앞에_둔다() -> None:
    books = [(1, "598"), (2, "594.5"), (3, None), (4, "594")]  # 육아, 요리, 모름, 요리

    ordered = interest_picks.numbered_first("요리", books, lambda book: book[1])

    assert [book_id for book_id, _ in ordered] == [2, 4, 1, 3]
