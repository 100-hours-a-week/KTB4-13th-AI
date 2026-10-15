"""⑤ 평가 묶음의 코드 채점(#341). 대화 하나의 기대값과 API 응답을 견줘 통과·실패를 매긴다.

코드로 볼 수 있는 것만 본다(#341 의 기준 1–3 과, 기준 4 가운데 코드로 볼 수 있는 부분).

- 응답의 모양이 명세 ⑤와 같은가
- 뽑을 것이 없는 대화에서 뽑지 않았는가
- 뽑아야 하는 취향마다 찾는 말(`keywords`)이 든 문장이 있는가
- 넣으면 안 되는 말이 뽑은 문장에 없는가

뜻을 봐야 하는 것은 채점하지 않고 표시만 한다. 종류가 맞는지, 뽑으면 안 되는 취향을 다른 말로
뽑았는지, 더 뽑은 취향에 근거가 있는지가 그렇다. 이것은 LLM 채점(뒤 단계)의 몫이다.

세기·어디에·핵심어(#424)는 통과·실패에 쓰지 않고, 찾은 취향마다 맞았는지만 센다. 기대에 이 칸을 적어 둔
취향만 센다. 찾은 문장이 여럿이면 하나라도 맞으면 맞은 것이다.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.core import categories
from app.extraction.rules import (
    MAX_APPLIES_TO,
    MAX_KEYWORD_CHARS,
    MAX_STRENGTH,
    MAX_VALUE_CHARS,
    MIN_CONDITION_STRENGTH,
    PREFERENCE_TYPES,
    REQUEST_TO_AVOID,
    condition_keyword,
    text_key,
)

_ITEM_KEYS = {
    "type",
    "value",
    "strength",
    "applies_to",
    "keyword",
    "vector",
    "dim",
    "source_conversation_id",
}

# 실패 이유. 결과 표와 파일에 이 글자 그대로 남는다.
ERROR = "error"  # 200 이 아니다(LLM 장애, 시간 초과, 서버 오류)
FORMAT = "format"  # 응답의 모양이 명세와 다르다
EXTRACTED = "extracted"  # 뽑으면 안 되는 것을 뽑았다
MISSED = "missed"  # 뽑아야 하는 취향의 찾는 말이 든 문장이 없다
FORBIDDEN = "forbidden"  # 넣으면 안 되는 말이 뽑은 문장에 있다


@dataclass(frozen=True)
class Outcome:
    """API 를 한 번 부른 결과."""

    status: int
    data: dict[str, Any] | None  # 200 이면 응답의 data, 아니면 None
    seconds: float


@dataclass(frozen=True)
class Score:
    case_id: str
    kind: str
    passed: bool  # 코드로 볼 수 있는 것을 모두 지켰다
    failures: tuple[str, ...]
    expected: int  # 뽑아야 하는 취향 수
    matched: int  # 그중 찾는 말이 든 문장이 있는 것
    type_matched: int  # 그중 종류까지 같은 것(참고용, 통과에는 안 쓴다)
    labeled: int  # 찾은 것 가운데 세기와 핵심어를 적어 둔 것
    sign_matched: int  # 그중 세기의 부호가 맞은 것
    strength_matched: int  # 그중 세기가 크기까지 맞은 것
    keyword_matched: int  # 그중 핵심어가 맞은 것
    where_labeled: int  # 찾은 것 가운데 어디에를 적어 둔 것(주제만)
    where_matched: int  # 그중 어디에가 맞은 것
    extracted: int  # 응답에 나온 취향 수
    extra: int  # 어느 기대와도 맞지 않는 취향 수(실패가 아니다)
    needs_llm: bool  # 코드로 다 볼 수 없는 대화다(뽑으면 안 되는 취향이 적혀 있거나 더 뽑은 것이 있음)
    status: int
    seconds: float
    values: tuple[str, ...]  # 뽑은 문장(결과를 사람이 읽을 때 쓴다)
    # 뽑은 취향의 종류·세기·어디에·핵심어. 세 칸이 왜 틀렸는지 사람이 볼 때 쓴다
    got: tuple[dict[str, Any], ...]


def _squash(text: str) -> str:
    """띄어쓰기와 대소문자를 보지 않고 견주려고 줄인 글."""
    return "".join(text.casefold().split())


def _sign(strength: Any) -> int | None:
    """세기의 부호. 정수가 아니면 None."""
    if isinstance(strength, bool) or not isinstance(strength, int):
        return None
    return (strength > 0) - (strength < 0)


def _where_ok(names: Any, allowed: list[str]) -> bool:
    """고른 이름이 모두 받아들이는 이름 안에 있는가. 받아들이는 이름이 없으면 아무것도 고르지 않아야 한다."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return False
    if not allowed:
        return not names
    return bool(names) and set(names) <= set(allowed)


def _count_labels(want: dict[str, Any], found: list[dict], counts: Counter) -> None:
    """찾은 취향 하나의 세기·어디에·핵심어가 맞았는지 센다."""
    if "strength" not in want:
        return
    strengths = [item.get("strength") for item in found]
    keywords = {_squash(k) for k in want["keyword"]}
    counts["labeled"] += 1
    counts["sign_matched"] += any(
        _sign(s) == _sign(want["strength"]) for s in strengths
    )
    counts["strength_matched"] += any(
        _sign(s) is not None and s == want["strength"] for s in strengths
    )
    counts["keyword_matched"] += any(
        isinstance(item.get("keyword"), str) and _squash(item["keyword"]) in keywords
        for item in found
    )
    if "applies_to" in want:
        counts["where_labeled"] += 1
        counts["where_matched"] += any(
            _where_ok(item.get("applies_to"), want["applies_to"]) for item in found
        )


def _well_formed(item: Any, conversation_id: str) -> bool:
    if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
        return False
    value, strength, keyword = item["value"], item["strength"], item["keyword"]
    names, vector = item["applies_to"], item["vector"]
    return (
        item["type"] in PREFERENCE_TYPES
        and isinstance(value, str)
        and 0 < len(value) <= MAX_VALUE_CHARS
        # 0 과 피함의 부탁(-1)은 응답에 나오지 않는다
        and bool(_sign(strength))
        and abs(strength) <= MAX_STRENGTH
        and strength != REQUEST_TO_AVOID
        and isinstance(keyword, str)
        and 0 < len(keyword) <= MAX_KEYWORD_CHARS
        # 어디에는 주제에만 있고, 서버 안 목록의 이름 3개 이하다
        and isinstance(names, list)
        and len(names) <= MAX_APPLIES_TO
        and all(name in categories.NAME_TEXTS for name in names)
        and (item["type"] == "topic" or not names)
        # 조건은 정해진 꼴이고, 늘 지킨다고 말한 것만 나온다
        and (
            item["type"] != "condition"
            or (
                condition_keyword(keyword) == keyword
                and strength >= MIN_CONDITION_STRENGTH
            )
        )
        and isinstance(vector, list)
        and len(vector) > 0
        and item["dim"] == len(vector)
        and item["source_conversation_id"] == conversation_id
    )


def score(case: dict[str, Any], outcome: Outcome) -> Score:
    """대화 하나를 한 번 돌린 결과를 채점한다."""
    expect = case["expect"]
    wanted = expect["extractions"]

    def result(failures: list[str], **counts: Any) -> Score:
        filled = {
            "matched": 0,
            "type_matched": 0,
            "labeled": 0,
            "sign_matched": 0,
            "strength_matched": 0,
            "keyword_matched": 0,
            "where_labeled": 0,
            "where_matched": 0,
            "extracted": 0,
            "extra": 0,
            "needs_llm": False,
            "values": (),
            "got": (),
        }
        filled.update(counts)
        return Score(
            case_id=case["id"],
            kind=case["kind"],
            passed=not failures,
            failures=tuple(failures),
            expected=len(wanted),
            status=outcome.status,
            seconds=outcome.seconds,
            **filled,
        )

    if outcome.status != 200 or not isinstance(outcome.data, dict):
        return result([ERROR])

    items = outcome.data.get("extractions")
    if not isinstance(items, list):
        return result([FORMAT])

    failures: list[str] = []
    conversation_id = case["request"]["conversation_id"]
    shape_ok = outcome.data.get("nothing_found") is (not items) and all(
        _well_formed(item, conversation_id) for item in items
    )
    if not shape_ok:
        failures.append(FORMAT)

    # 모양이 깨진 항목에서도 문장은 읽어 나머지 채점을 이어 간다
    kept = [
        item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("value"), str)
    ]
    got = [(item.get("type"), item["value"]) for item in kept]
    squashed = [_squash(value) for _type, value in got]

    hit: set[int] = set()  # 어느 기대와 맞은 문장의 위치
    matched = type_matched = 0
    labels: Counter = Counter()
    for want in wanted:
        keys = [_squash(keyword) for keyword in want["keywords"]]
        found = [i for i, text in enumerate(squashed) if any(k in text for k in keys)]
        if found:
            matched += 1
            hit.update(found)
            type_matched += any(got[i][0] == want["type"] for i in found)
            _count_labels(want, [kept[i] for i in found], labels)
    extra = len(got) - len(hit)

    if not wanted and got:
        failures.append(EXTRACTED)
    # 뽑으면 안 되는 취향은 뜻으로 견줘야 한다. 글자가 사실상 같을 때만 코드가 잡는다
    banned = {text_key(sentence) for sentence in expect["must_not_extract"]}
    if wanted and any(text_key(value) in banned for _type, value in got):
        failures.append(EXTRACTED)
    if matched < len(wanted):
        failures.append(MISSED)
    words = [_squash(word) for word in expect["forbidden"]]
    if any(word in text for word in words for text in squashed):
        failures.append(FORBIDDEN)

    return result(
        failures,
        matched=matched,
        type_matched=type_matched,
        **labels,
        extracted=len(got),
        extra=extra,
        needs_llm=bool(expect["must_not_extract"]) or (bool(wanted) and extra > 0),
        values=tuple(value for _type, value in got),
        got=tuple(
            {
                key: item.get(key)
                for key in ("type", "strength", "applies_to", "keyword")
            }
            for item in kept
        ),
    )
