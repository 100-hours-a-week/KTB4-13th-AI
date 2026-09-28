"""④ 개인화·rule-only 목록의 쪽 넘김 테스트 — 둘이 같이 쓰는 personalized.rank. DB 없이 돈다."""

import pytest

from app.feed import personalized
from app.feed.cursor import Page
from app.feed.schemas import parse_query


def _row(book_id: int, score: int) -> dict:
    return {
        "book_id": book_id,
        "title": "책",
        "author": "저자",
        "price": 10000,
        "cover_url": None,
        "in_stock": True,
        "popularity": 0,
        "pub_year": 2020,
        # 채점 함수가 이 값을 그대로 점수로 쓴다.
        "score": score,
    }


# 점수순으로 줄 세우면 1, 2, 4, 5, 3번이다. 3번은 점수 하한(50)에 걸린다.
_ROWS = [_row(1, 90), _row(2, 80), _row(3, 10), _row(4, 70), _row(5, 60)]


def _page(offset: int, size: int, **params) -> tuple[list[int], bool]:
    req = parse_query(
        [
            ("user_id", "1"),
            ("surface", "recommend_more"),
            ("size", str(size)),
            *params.items(),
        ]
    )
    items, has_more = personalized.rank(
        _ROWS, req, Page(offset=offset), lambda row: row["score"]
    )
    return [item["book_id"] for item in items], has_more


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, ([1, 2], True)),
        (2, ([4, 5], True)),
        (4, ([3], False)),
    ],
)
def test_커서의_자리부터_size_만큼_주고_더_있는지_알린다(
    offset: int, expected: tuple[list[int], bool]
) -> None:
    assert _page(offset, 2) == expected


def test_남은_책이_딱_size_만큼이면_더_없다고_알린다() -> None:
    # 끝은 next_cursor 가 null 인 것으로만 판정한다(명세 ④). 빈 페이지를 한 번 더 부르게 하지 않는다.
    assert _page(3, 2) == ([5, 3], False)


def test_점수_하한으로_뺀_뒤에_자리를_센다() -> None:
    # 3번을 빼면 남은 책은 넷이라 둘째 페이지가 마지막이다. 빼기 전 권수로 세면 빈 페이지를 한 번 더 부른다.
    assert _page(2, 2, match_score_min="50") == ([4, 5], False)
