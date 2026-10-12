"""⑤ LLM 이 낸 취향 후보를 거르는 규칙(#367). LLM 을 부르지 않는다.

LLM 의 답은 믿을 수 있는 입력이 아니다. 종류를 지어내거나, 문장을 길게 쓰거나, 같은 취향을 두 번
내거나, 수십 개를 낼 수 있다. 응답에 나가는 것은 여기를 통과한 후보뿐이게 해서, 프롬프트가 바뀌어도
명세 ⑤의 모양이 지켜지게 한다. 틀린 항목은 그 항목만 버린다.

이미 저장된 취향과 겹치는지는 문장 벡터로 견줘야 해서 여기서 보지 않는다(#299 의 다음 단계).
"""

from dataclasses import dataclass, replace
from typing import Any

from app.core.validation import sanitize_string

# 명세 ⑤ 의 extractions[].type
PREFERENCE_TYPES = frozenset({"mood", "topic", "author", "condition"})
# 명세 ⑤: value 는 200자 이하(#299). 넘으면 자르지 않고 버린다 — 잘린 문장이 취향 카드로 보이면
# 뜻이 달라진다.
MAX_VALUE_CHARS = 200
# 명세 ⑤: 신뢰도가 이보다 낮으면 버린다.
MIN_CONFIDENCE = 0.5
# 명세에 없는 값이다(#367). 대화 하나에서 취향 카드가 한꺼번에 수십 장 생기지 않게 한다. 평가 묶음
# (evals/extraction)의 기대값은 대화 하나에 많아야 2개다.
MAX_EXTRACTIONS = 5


@dataclass(frozen=True)
class Candidate:
    """거르기를 통과한 취향 후보. 벡터는 아직 없다."""

    type: str
    value: str
    confidence: float


def _value(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    # 취향 카드에 그대로 보이는 글이라 줄바꿈과 겹친 공백을 한 칸으로 만든다
    value = " ".join(raw.split())
    if not value or len(value) > MAX_VALUE_CHARS:
        return None
    try:
        # NUL·짝 없는 서로게이트는 응답을 내보내거나 벡터로 바꿀 때 터진다(#201)
        return sanitize_string(value)
    except ValueError:
        return None


def _confidence(raw: Any) -> float | None:
    # bool 은 int 의 하위 타입이라 따로 거른다. 80 처럼 백분율로 온 값은 짐작해서 고치지 않는다.
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    # NaN 과 무한대도 이 범위 비교에서 걸린다(NaN 은 어떤 비교든 거짓이다)
    if not MIN_CONFIDENCE <= raw <= 1:
        return None
    return float(raw)


def _candidate(item: Any) -> Candidate | None:
    if not isinstance(item, dict):
        return None
    type_ = item.get("type")
    if not isinstance(type_, str) or type_ not in PREFERENCE_TYPES:
        return None
    value = _value(item.get("value"))
    confidence = _confidence(item.get("confidence"))
    if value is None or confidence is None:
        return None
    return Candidate(type_, value, confidence)


def screen(llm_output: Any) -> list[Candidate]:
    """LLM 의 답(`{"extractions": [...]}`)에서 쓸 수 있는 후보만 신뢰도가 높은 순으로 돌려준다.

    한 대화에서 같은 취향이 두 번 나오면 하나로 합친다. 표기는 먼저 나온 것, 신뢰도는 높은 값이다.
    """
    items = llm_output.get("extractions") if isinstance(llm_output, dict) else None
    if not isinstance(items, list):
        return []

    merged: dict[tuple[str, str], Candidate] = {}
    for item in items:
        candidate = _candidate(item)
        if candidate is None:
            continue
        key = (candidate.type, candidate.value.casefold())
        seen = merged.get(key)
        if seen is None:
            merged[key] = candidate
        elif candidate.confidence > seen.confidence:
            merged[key] = replace(seen, confidence=candidate.confidence)

    # sorted 는 순서를 지키는 정렬이라 신뢰도가 같으면 LLM 이 낸 순서가 남는다
    ranked = sorted(merged.values(), key=lambda c: c.confidence, reverse=True)
    return ranked[:MAX_EXTRACTIONS]
