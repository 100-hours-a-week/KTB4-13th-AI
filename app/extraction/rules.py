"""⑤ LLM 이 낸 취향 후보를 거르는 규칙(#367). LLM 을 부르지 않는다.

LLM 의 답은 믿을 수 있는 입력이 아니다. 종류를 지어내거나, 문장을 길게 쓰거나, 같은 취향을 두 번
내거나, 수십 개를 낼 수 있다. 응답에 나가는 것은 여기를 통과한 후보뿐이게 해서, 프롬프트가 바뀌어도
명세 ⑤의 모양이 지켜지게 한다. 틀린 항목은 그 항목만 버린다.

한 대화에서 글자가 사실상 같은 취향은 하나로 합친다. 이미 저장된 취향과는 견주지 않는다. 같은 취향을
다른 날 또 말하면 또 내서, 자주 말한 취향이 무거워지게 한다(#425).
"""

import re
from collections.abc import Sequence
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
# LLM 이 취향마다 같이 적는 세 칸(#378). 프롬프트로 "하지 마라"고만 하면 지키지 않아서, LLM 에게는
# 가려서 적게만 하고 버리는 것은 코드가 한다.
OWNER_USER = "user"  # owner: 사용자 본인의 취향이다. 남의 취향("other")은 버린다
BASIS_STATED = "stated"  # basis: 좋아한다·싫어한다·늘 그렇게 한다고 직접 말했다
BASIS_REQUESTED = "requested"  # basis: 찾거나 추천을 부탁하기만 했다
# evidence(근거로 든 사용자의 말)가 이보다 짧으면 근거로 보지 않는다. "응", "소설" 같은 말은 어느
# 대화에나 있어서 무엇이든 뒷받침하는 것처럼 보인다.
MIN_EVIDENCE_CHARS = 4
# 좋아한다고 쓴 문장. "기분이 좋아지는"은 해당하지 않는다.
_SAYS_LIKE = re.compile(r"좋아(함|하|해)")

# 문장 끝에서 무시하는 문장부호. 글자 속의 기호는 건드리지 않는다 — "C#"과 "C", "3.5"와 "35"는 다르다.
_TRAILING_PUNCTUATION = ".!?~…。"
# 명세에 없는 값이다(#367). 대화 하나에서 취향 카드가 한꺼번에 수십 장 생기지 않게 한다. 평가 묶음
# (evals/extraction)의 기대값은 대화 하나에 많아야 2개다.
MAX_EXTRACTIONS = 5


@dataclass(frozen=True)
class Candidate:
    """거르기를 통과한 취향 후보. 벡터는 아직 없다."""

    type: str
    value: str
    confidence: float


def text_key(value: str) -> str:
    """글자가 사실상 같은 문장이면 같은 값이 나온다. 대소문자, 띄어쓰기, 끝의 문장부호를 무시한다.

    뜻이 같은지는 보지 않는다. "조용한 산문"과 "차분한 산문"은 다른 값이다.
    """
    return "".join(value.casefold().split()).rstrip(_TRAILING_PUNCTUATION)


def _letters(text: str) -> str:
    """글자와 숫자만 남긴다. 띄어쓰기, 문장부호, 대소문자를 보지 않고 견주려는 것이다."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _quoted_from(evidence: Any, user_turns: Sequence[str]) -> bool:
    """근거로 든 말이 사용자가 한 말 하나에서 옮긴 것인가.

    글자 그대로일 필요는 없다. 모델은 가운데를 줄여서 옮기므로, 근거의 낱말이 사용자의 말 하나에
    모두 들어 있으면 된다. 챗봇의 말이나 지어낸 말은 낱말이 맞지 않아 걸린다.
    """
    if not isinstance(evidence, str):
        return False
    words = [word for word in map(_letters, evidence.split()) if word]
    if sum(len(word) for word in words) < MIN_EVIDENCE_CHARS:
        return False
    said = [_letters(turn) for turn in user_turns]
    return any(all(word in turn for word in words) for turn in said)


def drop_reason(item: dict[str, Any], user_turns: Sequence[str]) -> str | None:
    """근거·주인·직접 말했는지 때문에 버려야 하면 그 이유, 남겨도 되면 None.

    - owner: 사용자 본인의 취향이 아니다
    - basis: 직접 말했는지 부탁했는지를 적지 않았다
    - evidence: 근거로 든 말이 사용자가 한 말에서 옮긴 것이 아니다(챗봇이 한 말이거나 지어낸 말이다)
    - unsaid_like: 찾기만 했는데 좋아한다고 썼다
    """
    if item.get("owner") != OWNER_USER:
        return "owner"
    basis = item.get("basis")
    if basis not in (BASIS_STATED, BASIS_REQUESTED):
        return "basis"
    if not _quoted_from(item.get("evidence"), user_turns):
        return "evidence"
    value = item.get("value")
    if basis == BASIS_REQUESTED and isinstance(value, str) and _SAYS_LIKE.search(value):
        return "unsaid_like"
    return None


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


def screen(llm_output: Any, user_turns: Sequence[str] | None = None) -> list[Candidate]:
    """LLM 의 답(`{"extractions": [...]}`)에서 쓸 수 있는 후보만 신뢰도가 높은 순으로 돌려준다.

    한 대화에서 같은 취향이 두 번 나오면 하나로 합친다. 표기는 먼저 나온 것, 신뢰도는 높은 값이다.
    개수 상한은 합친 뒤에 센다.
    사용자가 한 말(user_turns)을 주면 근거·주인·직접 말했는지도 본다(drop_reason).
    """
    items = llm_output.get("extractions") if isinstance(llm_output, dict) else None
    if not isinstance(items, list):
        return []

    merged: dict[tuple[str, str], Candidate] = {}
    for item in items:
        candidate = _candidate(item)
        if candidate is None:
            continue
        if user_turns is not None and drop_reason(item, user_turns) is not None:
            continue
        key = (candidate.type, text_key(candidate.value))
        seen = merged.get(key)
        if seen is None:
            merged[key] = candidate
        elif candidate.confidence > seen.confidence:
            merged[key] = replace(seen, confidence=candidate.confidence)

    # sorted 는 순서를 지키는 정렬이라 신뢰도가 같으면 LLM 이 낸 순서가 남는다
    ranked = sorted(merged.values(), key=lambda c: c.confidence, reverse=True)
    return ranked[:MAX_EXTRACTIONS]
