"""복제한 사용자 표(구매·나의 도서관·리뷰)를 앱 코드가 부르지 않는다(#307). DB 없이 돈다.

이 표들은 복제를 멈추고 지운다. 앱 어딘가에 표 이름이 남아 있으면 지운 뒤에 그 길에서 오류가 난다.
"""

from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "app"
_TABLES = ("v_user_purchases", "v_user_library", "v_user_reviews")


@pytest.mark.parametrize("table", _TABLES)
def test_앱_코드는_복제한_사용자_표를_부르지_않는다(table: str) -> None:
    used = [
        str(path.relative_to(_APP.parent))
        for path in sorted(_APP.rglob("*.py"))
        if table in path.read_text(encoding="utf-8")
    ]

    assert used == []
