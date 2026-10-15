"""④ 관심사별 목록 — 관심사마다 그 관심사의 도서관 분류 안에서 검색어와 가까운 책을 뽑는다(#449, 설계 #395).

검색어는 그 관심사의 책 벡터 평균이고, 책이 없으면 이름 벡터다. 뽑은 책 가운데 세부 분류 번호가 이 관심사로
이어지는 책(AI 책 분류 표)을 앞에 둔다. 말한 "요리"도 594 책부터 나온다.
작가·키워드 관심사는 #450, 시간 예산과 대체 자리는 #451 이 맡는다.
"""

from collections.abc import Callable, Sequence

from app.core import categories

# 세부 카테고리 → 속한 카테고리. 어느 카테고리에도 속하지 않는 세부 카테고리는 None 이다
_PARENT = {
    name: parent for parent, group in categories.SUBCATEGORIES.items() for name in group
}


def classes_of(name: str) -> tuple[str, ...] | None:
    """관심사를 뽑을 도서관 분류. 분류로 좁힐 수 없으면 None 이고, 그때는 벡터로만 뽑는다.

    카테고리는 그 카테고리의 분류다. 세부 카테고리는 지금의 세부 카테고리 → 도서관 분류 대응표를 쓰고,
    없으면 속한 카테고리의 분류를 쓴다(추리/스릴러는 소설의 분류 안에서). 어린이와 그 밑의 세부
    카테고리, 어느 카테고리에도 속하지 않는 세부 카테고리(의학 등)는 None 이다.
    """
    if name in categories.TAG_TO_CATALOG:
        return categories.TAG_TO_CATALOG[name] or None
    match = categories.ONBOARDING_TO_CATALOG.get(
        name if name in categories.ONBOARDING_TO_CATALOG else _PARENT.get(name)
    )
    return match.core if match else None


def numbered_first[T](
    name: str, items: Sequence[T], class_no: Callable[[T], str | None]
) -> list[T]:
    """세부 분류 번호가 이 관심사로 이어지는 책을 앞으로. 앞뒤 안의 순서는 그대로다."""
    return sorted(
        items, key=lambda item: categories.class_interest(class_no(item)) != name
    )
