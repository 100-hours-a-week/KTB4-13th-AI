"""⑤ 평가 묶음의 코드 채점(#341). 대화 하나의 기대값과 API 응답을 견줘 통과·실패를 매긴다.

코드로 볼 수 있는 것만 본다(#341 의 기준 1–3 과 4 의 핵심어 부분).

- 응답의 모양이 명세 ⑤와 같은가
- 뽑을 것이 없는 대화에서 뽑지 않았는가
- 뽑아야 하는 취향마다 핵심어가 든 문장이 있는가
- 넣으면 안 되는 말이 뽑은 문장에 없는가

뜻을 봐야 하는 것은 채점하지 않고 표시만 한다. 종류가 맞는지, 뽑으면 안 되는 취향을 다른 말로
뽑았는지, 더 뽑은 취향에 근거가 있는지가 그렇다. 이것은 LLM 채점(뒤 단계)의 몫이다.
"""

from dataclasses import dataclass
from typing import Any

from app.extraction.rules import (
    MAX_VALUE_CHARS,
    MIN_CONFIDENCE,
    PREFERENCE_TYPES,
    text_key,
)

_ITEM_KEYS = {"type", "value", "confidence", "vector", "dim", "source_conversation_id"}

# 실패 이유. 결과 표와 파일에 이 글자 그대로 남는다.
ERROR = "error"  # 200 이 아니다(LLM 장애, 시간 초과, 서버 오류)
FORMAT = "format"  # 응답의 모양이 명세와 다르다
EXTRACTED = "extracted"  # 뽑으면 안 되는 것을 뽑았다
MISSED = "missed"  # 뽑아야 하는 취향의 핵심어가 든 문장이 없다
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
    matched: int  # 그중 핵심어가 든 문장이 있는 것
    type_matched: int  # 그중 종류까지 같은 것(참고용, 통과에는 안 쓴다)
    extracted: int  # 응답에 나온 취향 수
    extra: int  # 어느 기대와도 맞지 않는 취향 수(실패가 아니다)
    needs_llm: bool  # 코드로 다 볼 수 없는 대화다(뽑으면 안 되는 취향이 적혀 있거나 더 뽑은 것이 있음)
    status: int
    seconds: float
    values: tuple[str, ...]  # 뽑은 문장(결과를 사람이 읽을 때 쓴다)


def _squash(text: str) -> str:
    """띄어쓰기와 대소문자를 보지 않고 견주려고 줄인 글."""
    return "".join(text.casefold().split())


def _well_formed(item: Any, conversation_id: str) -> bool:
    if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
        return False
    value, confidence, vector = item["value"], item["confidence"], item["vector"]
    return (
        item["type"] in PREFERENCE_TYPES
        and isinstance(value, str)
        and 0 < len(value) <= MAX_VALUE_CHARS
        and isinstance(confidence, int | float)
        and not isinstance(confidence, bool)
        and MIN_CONFIDENCE <= confidence <= 1
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
            "extracted": 0,
            "extra": 0,
            "needs_llm": False,
            "values": (),
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
    got = [
        (item.get("type"), item["value"])
        for item in items
        if isinstance(item, dict) and isinstance(item.get("value"), str)
    ]
    squashed = [_squash(value) for _type, value in got]

    hit: set[int] = set()  # 어느 기대와 맞은 문장의 위치
    matched = type_matched = 0
    for want in wanted:
        keys = [_squash(keyword) for keyword in want["keywords"]]
        found = [i for i, text in enumerate(squashed) if any(k in text for k in keys)]
        if found:
            matched += 1
            hit.update(found)
            type_matched += any(got[i][0] == want["type"] for i in found)
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
        extracted=len(got),
        extra=extra,
        needs_llm=bool(expect["must_not_extract"]) or (bool(wanted) and extra > 0),
        values=tuple(value for _type, value in got),
    )
