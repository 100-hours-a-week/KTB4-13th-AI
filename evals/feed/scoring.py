"""④⑥ 평가 묶음의 채점(#392). 사용자 한 명의 홈 추천 위 N권에서 관심사별로 맞는 책을 센다.

통과·실패는 매기지 않고 숫자만 낸다. 몇 권이면 통과인지는 지금 구조의 기준선을 본 뒤에 정한다(#390).

책이 관심사에 맞는지는 묶음의 `interests` 가 정한 기준으로 가른다. 맞다, 아니다, 알 수 없다의 셋이다.
알 수 없는 책(세부 분류 번호가 없는 책 등)을 아닌 책에 넣으면 숫자가 부풀려지므로 따로 센다.
"""

import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from app.core import categories


@dataclass(frozen=True)
class Book:
    """책 한 권에 대해 채점이 아는 것. 값이 없는 칸은 None 이다."""

    isbn: str
    title: str
    author: str | None
    category: str | None  # 책의 분류(v_books.category)
    kdc: str | None  # 도서관 세부 분류 번호(예: 594.5)
    reader: str | None  # 독자 대상 부호(7 = 어린이)
    pages: int | None
    pub_year: int | None
    price: int | None
    text: str  # 제목과 소개글. 낱말로 가르는 기준에 쓴다


@dataclass(frozen=True)
class Response:
    """사용자 한 명을 프로필 API 와 홈 추천 API 에 넣은 결과."""

    profile_status: int
    feed_status: int
    cold_start: bool | None  # 개인화하지 않은 목록이면 True
    degraded: str | None  # 기능을 줄여 답했으면 그 이름(X-Degraded)
    isbns: tuple[str, ...]  # 위 N권, 받은 순서대로
    seconds: float  # 홈 추천 한 번에 걸린 시간


@dataclass(frozen=True)
class Score:
    case_id: str
    kind: str
    # 두 API 가 200 이고, 벡터를 쓴 목록이고, 개인화 여부가 기대와 같다. 아니면 재려던 것을 잰 것이 아니다.
    ok: bool
    returned: int
    show: dict[str, int]  # 나와야 하는 관심사별 맞는 책 수
    unknown: dict[str, int]  # 그 관심사에 맞는지 알 수 없는 책 수
    least: int | None  # 나와야 하는 관심사 중 가장 적게 나온 것의 권수
    missing: int  # 한 권도 안 나온 관심사 수
    none: int  # 나와야 하는 관심사 어느 것에도 맞지 않는 책
    unsure: int  # 맞는 관심사는 없는데 알 수 없는 것이 있는 책
    avoid: dict[str, int]  # 늘면 안 되는 관심사별 책 수
    toward: dict[str, int]  # 기울어야 하는 것(분위기, 조건)별 책 수
    twin_show: dict[str, int] | None  # 짝의 목록에서 센 같은 관심사
    twin_avoid: dict[str, int] | None
    twin_toward: dict[str, int] | None
    same_as_twin: int | None  # 짝의 목록과 겹치는 책
    owned: int | None  # 가진 책이 나온 권수
    editions: int | None  # 가진 책과 제목이 같은 다른 판본
    pile: int | None  # 좋아한 책의 권수(책이 쌓인 사용자)
    seconds: float
    isbns: tuple[str, ...]
    titles: tuple[str, ...]


def matches(interest: Mapping[str, Any], book: Book) -> bool | None:
    """책이 관심사에 맞는가. 가를 값이 책에 없으면 None(알 수 없음)."""
    by = interest["by"]
    if by == "field":
        core = categories.ONBOARDING_TO_CATALOG[interest["field"]].core
        return book.category in core
    if by == "kdc":
        if book.kdc is None:
            return None
        return book.kdc.startswith(tuple(interest["kdc"]))
    if by == "words":
        text = book.text.casefold()
        return any(word.casefold() in text for word in interest["words"])
    if by == "author":
        # 한글 이름이 다른 이름의 일부인 경우(한강 / 한강희, 김한강)는 다른 사람이다.
        name = re.escape(interest["author"])
        return bool(re.search(f"(^|[^가-힣]){name}([^가-힣]|$)", book.author or ""))
    if by == "reader":
        return None if book.reader is None else book.reader in interest["reader"]
    if by == "pages":
        return None if book.pages is None else book.pages <= interest["max"]
    if by == "pub_year":
        return None if book.pub_year is None else book.pub_year >= interest["min"]
    if by == "price":
        return None if book.price is None else book.price <= interest["max"]
    raise ValueError(f"모르는 기준: {by}")


def _title_key(title: str) -> str:
    """띄어쓰기와 문장부호만 다른 제목을 같은 것으로 본다."""
    return "".join(ch for ch in title.casefold() if ch.isalnum())


def _count(
    keys: Sequence[str],
    isbns: Sequence[str],
    shelf: Mapping[str, Book],
    interests: Mapping[str, Any],
) -> tuple[dict[str, int], dict[str, int]]:
    """(관심사별 맞는 책 수, 관심사별 알 수 없는 책 수). 책 표에 없는 책은 알 수 없는 책이다."""
    hit = dict.fromkeys(keys, 0)
    unknown = dict.fromkeys(keys, 0)
    for isbn in isbns:
        book = shelf.get(isbn)
        for key in keys:
            verdict = None if book is None else matches(interests[key], book)
            if verdict is True:
                hit[key] += 1
            elif verdict is None:
                unknown[key] += 1
    return hit, unknown


def _measured(response: Response, wants_cold: bool) -> bool:
    """재려던 목록이 왔는가. API 가 실패했거나 기능을 줄여 답했거나, 개인화 여부가 기대와 다르면 아니다.

    취향이 있는 사용자에게 개인화하지 않은 목록이 왔다면 프로필이 쓰이지 않은 것이다.
    """
    return (
        response.profile_status == 200
        and response.feed_status == 200
        and response.degraded is None
        and response.cold_start is wants_cold
    )


def score(
    case: Mapping[str, Any],
    response: Response,
    shelf: Mapping[str, Book],
    interests: Mapping[str, Any],
    twin: Response | None = None,
    twin_wants_cold: bool = False,
) -> Score:
    """사용자 한 명을 채점한다. shelf 는 ISBN 으로 찾는 책 정보, twin 은 견줄 짝의 결과다.

    짝을 재지 못했으면 이 사용자도 재지 못한 것으로 친다. 짝의 목록이 비면 짝의 책이 0권으로 세어져,
    기억 하나를 더한 뒤가 오히려 나아진 것처럼 보이기 때문이다(#393 리뷰).
    """
    expect = case["expect"]
    show_keys = expect.get("show", [])
    avoid_keys = expect.get("avoid", [])
    toward_keys = expect.get("toward", [])
    isbns = response.isbns

    show, unknown = _count(show_keys, isbns, shelf, interests)
    avoid, _ = _count(avoid_keys, isbns, shelf, interests)
    toward, _ = _count(toward_keys, isbns, shelf, interests)

    none = unsure = 0
    if show_keys:
        for isbn in isbns:
            book = shelf.get(isbn)
            verdicts = [
                None if book is None else matches(interests[key], book)
                for key in show_keys
            ]
            if any(v is True for v in verdicts):
                continue
            if all(v is False for v in verdicts):
                none += 1
            else:
                unsure += 1

    ok = _measured(response, bool(expect.get("cold_start")))
    twin_ok = twin is not None and _measured(twin, twin_wants_cold)
    if twin is not None and not twin_ok:
        ok = False

    twin_show = twin_avoid = twin_toward = same_as_twin = None
    if twin is not None and twin_ok:
        twin_show, _ = _count(show_keys, twin.isbns, shelf, interests)
        twin_avoid, _ = _count(avoid_keys, twin.isbns, shelf, interests)
        twin_toward, _ = _count(toward_keys, twin.isbns, shelf, interests)
        same_as_twin = len(set(isbns) & set(twin.isbns))

    owned = editions = None
    if expect.get("never_owned"):
        history = case["user"]["history"]
        mine = {*history["purchased_isbns"], *history["library_isbns"]}
        owned = sum(isbn in mine for isbn in isbns)
        if expect.get("editions"):
            titles = {_title_key(shelf[isbn].title) for isbn in mine if isbn in shelf}
            editions = sum(
                isbn not in mine
                and isbn in shelf
                and _title_key(shelf[isbn].title) in titles
                for isbn in isbns
            )

    return Score(
        case_id=case["id"],
        kind=case["kind"],
        ok=ok,
        returned=len(isbns),
        show=show,
        unknown=unknown,
        least=min(show.values()) if show else None,
        missing=sum(count == 0 for count in show.values()),
        none=none,
        unsure=unsure,
        avoid=avoid,
        toward=toward,
        twin_show=twin_show,
        twin_avoid=twin_avoid,
        twin_toward=twin_toward,
        same_as_twin=same_as_twin,
        owned=owned,
        editions=editions,
        pile=expect.get("pile"),
        seconds=response.seconds,
        isbns=tuple(isbns),
        titles=tuple(shelf[isbn].title if isbn in shelf else "" for isbn in isbns),
    )


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def _common(scores: Sequence[Score]) -> dict[str, Any]:
    """종류에도 묶음에도 똑같이 내는 숫자. 재지 못한 사용자(ok 가 아님)는 평균에서 뺀다."""
    measured = [s for s in scores if s.ok]
    with_show = [s for s in measured if s.least is not None]
    return {
        "users": len(scores),
        "measured": len(measured),
        "least_mean": _mean([s.least for s in with_show]),
        "missing_users": sum(s.missing > 0 for s in with_show),
        "low_users": sum(s.least <= 2 for s in with_show),
        "none_mean": _mean([s.none for s in with_show]),
        "unsure_mean": _mean([s.unsure for s in with_show]),
    }


def _piles(measured: Sequence[Score]) -> dict[str, dict[str, Any]]:
    """좋아한 책의 권수가 같은 사용자끼리, 위 N권이 얼마나 겹치는가."""
    out: dict[str, dict[str, Any]] = {}
    for size in sorted({s.pile for s in measured if s.pile is not None}):
        same = [s for s in measured if s.pile == size]
        pairs = [len(set(a.isbns) & set(b.isbns)) for a, b in combinations(same, 2)]
        out[str(size)] = {
            "users": len(same),
            "overlap_mean": _mean(pairs),
            "distinct": len({isbn for s in same for isbn in s.isbns}),
        }
    return out


def summarize(
    scores: Sequence[Score],
    kinds: Mapping[str, Mapping[str, str]],
    interests: Mapping[str, Any],
) -> dict[str, Any]:
    """종류별, 묶음(typical·guard)별로 모은다. 전체를 합친 숫자는 내지 않는다.

    가상 사용자는 실제 사용자의 분포를 따르지 않아서, 종류를 섞은 평균은 뜻이 없다.
    """
    by_kind: dict[str, list[Score]] = {}
    for s in scores:
        by_kind.setdefault(s.kind, []).append(s)

    out_kinds: dict[str, Any] = {}
    by_group: dict[str, list[Score]] = {}
    for kind, items in by_kind.items():
        group = kinds[kind]["group"]
        by_group.setdefault(group, []).extend(items)
        measured = [s for s in items if s.ok]
        used = {key for s in items for key in (*s.show, *s.avoid, *s.toward)}
        row: dict[str, Any] = {
            "group": group,
            **_common(items),
            # 낱말로 가르는 관심사가 끼어 있으면 이 종류의 숫자는 방향만 본다.
            "rough": any(interests[key].get("rough", False) for key in used),
            "seconds_median": (
                round(statistics.median([s.seconds for s in measured]), 3)
                if measured
                else None
            ),
        }
        if any(s.avoid for s in measured):
            row["avoid_mean"] = _mean([sum(s.avoid.values()) for s in measured])
        if any(s.toward for s in measured):
            row["toward_mean"] = _mean([sum(s.toward.values()) for s in measured])
        twins = [s for s in measured if s.twin_show is not None]
        if twins:
            row["show_mean"] = _mean([sum(s.show.values()) for s in twins])
            row["twin_show_mean"] = _mean([sum(s.twin_show.values()) for s in twins])
            row["same_as_twin_mean"] = _mean([s.same_as_twin for s in twins])
            if any(s.avoid for s in twins):
                row["twin_avoid_mean"] = _mean(
                    [sum(s.twin_avoid.values()) for s in twins]
                )
            if any(s.toward for s in twins):
                row["twin_toward_mean"] = _mean(
                    [sum(s.twin_toward.values()) for s in twins]
                )
        owning = [s for s in measured if s.owned is not None]
        if owning:
            row["owned_users"] = sum(s.owned > 0 for s in owning)
            row["editions_mean"] = _mean(
                [s.editions for s in owning if s.editions is not None]
            )
        piles = _piles(measured)
        if piles:
            row["piles"] = piles
        out_kinds[kind] = row

    return {
        "kinds": out_kinds,
        "groups": {group: _common(items) for group, items in by_group.items()},
    }
