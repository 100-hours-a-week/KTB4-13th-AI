"""① 검색 평가 묶음의 채점(#495). 검색어 하나의 결과 위 N권을 사례의 종류에 맞게 잰다.

통과·실패는 아직 매기지 않고 숫자만 낸다. 몇이면 통과인지는 기준선을 본 뒤에 정한다.

- 제목(`title`, `title-variant`): 정답 제목과 같은 책이 처음 나온 등수. 제목은 띄어쓰기·문장부호·영문
  대소문자를 빼고 견준다(`editions.title_key`). 저자는 보지 않는다. 검색어에 저자가 없으니 제목이 같은
  책이면 어느 책이 1등이어도 검색은 맞게 한 것이다. 같은 작품도 판본마다 저자 표기가 달라 저자까지 보면
  맞게 찾은 것을 틀렸다고 센다.
- 저자(`author`): 위 10권 가운데 저자 칸에 그 이름이 낱말로 통째로 든 책의 비율(`has_author`).
- 필터(`filter`): 요청에 건 조건을 어긴 책의 수. 0이어야 한다.
- 없는 말(`nonsense`): 돌아온 권수만 센다.
"""

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core import categories, editions
from app.feed.interest_picks import has_author

TITLE_KINDS = ("title", "title-variant")
# 정답이 이 등수 안에 나온 사례의 비율을 낸다. 마지막 값은 묶음의 top 과 같다.
HIT_AT = (1, 5, 20)
# 저자 검색은 위 10권을 본다. 결과가 10권보다 적어도 10으로 나눈다.
AUTHOR_TOP = 10


@dataclass(frozen=True)
class Book:
    """책 한 권에 대해 채점이 아는 것. 값이 없는 칸은 None 이다."""

    book_id: int
    isbn: str | None
    title: str
    author: str | None
    category: str | None  # 책의 분류(v_books.category)
    pub_year: int | None
    price: int | None  # 상품 표의 판매가. 검색의 가격 필터와 같은 값이다


@dataclass(frozen=True)
class Response:
    """검색어 하나를 /search 에 넣은 결과. 책은 응답 순서대로의 book_id 다."""

    status: int
    degraded: str | None  # X-Degraded 헤더. 온전히 답했으면 None
    book_ids: tuple[int, ...]
    seconds: float


@dataclass(frozen=True)
class Score:
    """사례 하나의 채점 결과. 종류에 맞는 칸만 채운다."""

    id: str
    kind: str
    change: str | None  # 표기를 어떻게 바꿨는지. title-variant 만 있다
    ok: bool  # 200 으로 온전히 답했는가. 아니면 평균에서 빼고 따로 센다
    returned: int
    seconds: float
    rank: int | None = None  # 제목: 정답이 처음 나온 등수(1부터). 위 N권에 없으면 None
    author_share: float | None = None  # 저자: 위 10권 중 그 저자 책의 비율
    violations: int | None = None  # 필터: 조건을 어긴 책의 수


def title_rank(case: Mapping[str, Any], books: Sequence[Book | None]) -> int | None:
    """정답 제목(`expect.titles` 중 하나)과 같은 책이 처음 나온 등수. 없으면 None."""
    wanted = {editions.title_key(title) for title in case["expect"]["titles"]}
    wanted.discard("")
    for rank, book in enumerate(books, start=1):
        if book is not None and editions.title_key(book.title) in wanted:
            return rank
    return None


def author_share(case: Mapping[str, Any], books: Sequence[Book | None]) -> float:
    """위 10권 가운데 저자 칸에 그 이름이 통째로 든 책의 비율. 한강을 찾는데 한강준의 책은 세지 않는다."""
    name = case["expect"]["author"]
    hits = sum(
        book is not None and has_author(book.author, name)
        for book in books[:AUTHOR_TOP]
    )
    return hits / AUTHOR_TOP


def breaks(filters: Mapping[str, Any], book: Book | None) -> bool:
    """책이 요청의 조건을 어겼는가. 책 표에서 찾지 못한 책은 지켰는지 알 수 없어 어긴 것으로 센다."""
    if book is None:
        return True
    category = filters.get("category")
    if category is not None:
        # 검색과 같은 대응표로 가른다. 빈 튜플(어린이)은 거르지 않는다는 뜻이다(filters.build_where).
        allowed = categories.filter_categories(category)
        if allowed is None:
            allowed = (category,)
        if allowed and book.category not in allowed:
            return True
    bounds = (
        (book.pub_year, filters.get("pub_year_from"), filters.get("pub_year_to")),
        (book.price, filters.get("price_min"), filters.get("price_max")),
    )
    for value, low, high in bounds:
        if low is None and high is None:
            continue
        # 값이 없는 책은 조건을 걸면 빠져야 한다(NULL 비교는 참이 아니다).
        if value is None or (low is not None and value < low):
            return True
        if high is not None and value > high:
            return True
    return False


def score(
    case: Mapping[str, Any], response: Response, shelf: Mapping[int, Book], top: int
) -> Score:
    """사례 하나를 채점한다. `shelf` 는 받은 책의 book_id → 책 정보다."""
    books = [shelf.get(book_id) for book_id in response.book_ids[:top]]
    kind = case["kind"]
    extra: dict[str, Any] = {}
    if kind in TITLE_KINDS:
        extra["rank"] = title_rank(case, books)
    elif kind == "author":
        extra["author_share"] = author_share(case, books)
    elif kind == "filter":
        extra["violations"] = sum(breaks(case["filters"], book) for book in books)
    return Score(
        id=case["id"],
        kind=kind,
        change=case.get("change"),
        ok=response.status == 200 and response.degraded is None,
        returned=len(response.book_ids),
        seconds=response.seconds,
        **extra,
    )


def _mean(values: Sequence[float], digits: int = 3) -> float | None:
    return round(statistics.fmean(values), digits) if values else None


def _row(items: Sequence[Score]) -> dict[str, Any]:
    measured = [s for s in items if s.ok]
    row: dict[str, Any] = {
        "cases": len(items),
        "measured": len(measured),
        "returned_mean": _mean([s.returned for s in measured], 1),
        "seconds_median": (
            round(statistics.median([s.seconds for s in measured]), 3)
            if measured
            else None
        ),
    }
    kind = items[0].kind
    if kind in TITLE_KINDS:
        ranks = [s.rank for s in measured]
        for k in HIT_AT:
            row[f"hit_{k}"] = _mean([r is not None and r <= k for r in ranks])
        row["mrr"] = _mean([1 / r if r else 0.0 for r in ranks])
        row["missed"] = sum(r is None for r in ranks)
    elif kind == "author":
        row["author_share_mean"] = _mean([s.author_share for s in measured], 2)
    elif kind == "filter":
        row["violations"] = sum(s.violations for s in measured)
        row["violating_cases"] = sum(s.violations > 0 for s in measured)
    return row


def summarize(scores: Sequence[Score]) -> dict[str, Any]:
    """종류별로 모은다. 표기 변형은 바꾼 방법별로도 낸다. 종류를 섞은 전체 숫자는 내지 않는다.

    재지 못한 사례(200 이 아니거나 줄여 답함)는 평균에서 빼고, `cases` 와 `measured` 의 차이로 드러낸다.
    """
    kinds: dict[str, list[Score]] = {}
    changes: dict[str, list[Score]] = {}
    for s in scores:
        kinds.setdefault(s.kind, []).append(s)
        if s.change:
            changes.setdefault(s.change, []).append(s)
    return {
        "kinds": {kind: _row(items) for kind, items in kinds.items()},
        "changes": {change: _row(items) for change, items in changes.items()},
    }
