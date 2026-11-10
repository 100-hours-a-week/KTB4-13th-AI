"""④ 목록에서 뺄 책의 조건 — 세 목록(개인화, 규칙 점수, 개인화 끔)의 조회가 같은 조건을 쓴다(#330).

조건마다 넘기는 값의 개수가 다르다. 그래서 조회문에 `$1` 같은 자리 번호를 박아 두지 않고, 조건이
쓰는 값의 개수에 맞춰 조회가 번호를 매긴다. 조건을 바꿔 끼워도 뒤따르는 번호가 어긋나지 않는다.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Exclusion:
    # 조건식. 값이 들어갈 자리는 {0}, {1} … 이고, 책 표는 b 로 부른다.
    template: str
    # 조건식의 자리에 차례로 들어갈 값.
    params: tuple[Any, ...]

    def sql(self, first_param: int) -> str:
        """자리를 `$first_param` 부터 이어지는 번호로 채운 조건식."""
        return self.template.format(
            *(f"${first_param + i}" for i in range(len(self.params)))
        )


def owned(book_ids: list[int]) -> Exclusion:
    """요청에 실려 온 가진 책(산 책·나의 도서관 책)으로 뺀다(#330). 안 실려 오면 빈 목록이다.

    별점이 낮은 책은 따로 받지 않는다. 리뷰는 산 책에만 달 수 있어서 산 책에 들어 있다(#307).
    요청에는 이력마다의 시각이 없어서, 스크롤 중에 산 책은 다음 페이지에서 바로 빠진다.
    """
    if not book_ids:
        # 가진 책이 없는 사용자가 대부분이다. 조건을 아예 붙이지 않는다.
        return Exclusion("true", ())
    return Exclusion("b.book_id <> ALL({0}::int[])", (sorted(set(book_ids)),))
