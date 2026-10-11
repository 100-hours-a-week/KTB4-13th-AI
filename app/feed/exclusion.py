"""④ 목록에서 뺄 책의 조건 — 세 목록(개인화, 규칙 점수, 개인화 끔)의 조회가 같은 조건을 쓴다(#330).

조건마다 넘기는 값의 개수가 다르다. 그래서 조회문에 `$1` 같은 자리 번호를 박아 두지 않고, 조건이
쓰는 값의 개수에 맞춰 조회가 번호를 매긴다. 조건을 바꿔 끼워도 뒤따르는 번호가 어긋나지 않는다.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core import history


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


# 이미 산 책, 나의 도서관에 담은 책, 2.0점 이하 리뷰를 단 책은 뺀다(명세 ④).
# 좋은 리뷰만 단 책(사지도 담지도 않음)은 명세의 제외 대상이 아니라 남긴다.
_REPLICATED = """
NOT EXISTS (
        SELECT 1 FROM v_user_purchases x
        WHERE x.user_id = {0} AND x.book_id = b.book_id AND x.purchased_at <= {2})
  AND NOT EXISTS (
        SELECT 1 FROM v_user_library x
        WHERE x.user_id = {0} AND x.book_id = b.book_id AND x.added_at <= {2})
  AND NOT EXISTS (
        SELECT 1 FROM v_user_reviews x
        WHERE x.user_id = {0} AND x.book_id = b.book_id AND x.rating <= {1}
          AND x.created_at <= {2})
"""


def replicated(user_id: int, issued_at: datetime | None) -> Exclusion:
    """복제한 사용자 표의 이력으로 뺀다. 커서를 받은 시각 뒤에 생긴 이력은 빼고 본다.

    카드를 보고 돌아와 산 책이 다음 페이지에서 사라지면 목록이 한 칸씩 밀린다(명세 ④).
    """
    return Exclusion(_REPLICATED, (user_id, history.DISLIKED_MAX_RATING, issued_at))
