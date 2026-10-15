"""⑤ LLM 이 낸 취향 후보를 거르는 규칙(#367). LLM 을 부르지 않는다.

LLM 의 답은 믿을 수 있는 입력이 아니다. 종류를 지어내거나, 문장을 길게 쓰거나, 같은 취향을 두 번
내거나, 수십 개를 낼 수 있다. 응답에 나가는 것은 여기를 통과한 후보뿐이게 해서, 프롬프트가 바뀌어도
명세 ⑤의 모양이 지켜지게 한다. 틀린 항목은 그 항목만 버린다.

세기·어디에·핵심어(#426)도 여기서 본다. 범위 밖의 세기는 버리고, 피함의 부탁(-1)은 응답에서 뺀다.
어디에는 서버 안 목록(app/core/categories.py)에 있는 이름만 남긴다.

한 대화에서 종류와 핵심어가 같은 취향은 하나로 합친다. 이미 저장된 취향과는 견주지 않는다. 같은 취향을
다른 날 또 말하면 또 내서, 자주 말한 취향이 무거워지게 한다(#425).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.core import categories
from app.core.validation import sanitize_string

# 명세 ⑤ 의 extractions[].type
PREFERENCE_TYPES = frozenset({"mood", "topic", "author", "condition"})
# 명세 ⑤: value 는 200자 이하(#299). 넘으면 자르지 않고 버린다 — 잘린 문장이 취향 카드로 보이면
# 뜻이 달라진다.
MAX_VALUE_CHARS = 200
# 명세 ⑤: 세기는 -3 이상 3 이하이고 0 은 없다. 크기는 부탁만 했으면 1, 직접 말했으면 2, 강하게 말했으면 3
MAX_STRENGTH = 3
# 싫은 쪽을 부탁하기만 한 것("이번엔 ○○ 말고")의 세기. 응답에서 뺀다. 추천을 바꾸지 않는 줄이 마이페이지에
# 보이면 헷갈린다(#395)
REQUEST_TO_AVOID = -1
# 명세 ⑤: 어디에는 3개 이하, 핵심어는 1자 이상 100자 이하
MAX_APPLIES_TO = 3
MAX_KEYWORD_CHARS = 100
# 핵심어를 감싼 제목 괄호. 『』가 아니면 『』로 바꾼다. 제목만 말한 취향은 핵심어를 『』로 감싼다(명세 ⑤)
_TITLE_BRACKETS = {"「": "」", "《": "》", "〈": "〉", "<": ">"}
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
    strength: int
    applies_to: tuple[str, ...]
    keyword: str


def text_key(value: str) -> str:
    """글자가 사실상 같은 문장이면 같은 값이 나온다. 대소문자, 띄어쓰기, 끝의 문장부호를 무시한다.

    뜻이 같은지는 보지 않는다. "조용한 산문"과 "차분한 산문"은 다른 값이다.
    """
    return "".join(value.casefold().split()).rstrip(_TRAILING_PUNCTUATION)


def _letters(text: str) -> str:
    """글자와 숫자만 남긴다. 띄어쓰기, 문장부호, 대소문자를 보지 않고 견주려는 것이다."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


# 어디에로 받는 이름. 띄어쓰기와 문장부호가 조금 달라도("추리 / 스릴러") 목록의 이름으로 바꾼다
_NAMES = {_letters(name): name for name in categories.NAME_TEXTS}


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


def _strength(raw: Any, basis: Any) -> int | None:
    """세기. 정수가 아니거나 범위 밖이거나 0 이면 None.

    크기는 직접 말했는지(basis)에 맞춘다. 부탁만 했으면 1, 직접 말했으면 2 이상이다. LLM 이 둘을 어긋나게
    적어도 같은 말에 같은 크기가 나오게 하려는 것이다.
    """
    # bool 은 int 의 하위 타입이라 따로 거른다. 2.0 같은 소수는 짐작해서 고치지 않는다
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw == 0 or abs(raw) > MAX_STRENGTH:
        return None
    sign = 1 if raw > 0 else -1
    if basis == BASIS_REQUESTED:
        return sign
    if basis == BASIS_STATED:
        return sign * max(2, abs(raw))
    return raw


def _keyword(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    keyword = " ".join(raw.split())
    closing = _TITLE_BRACKETS.get(keyword[:1])
    if closing and len(keyword) > 2 and keyword.endswith(closing):
        keyword = f"『{keyword[1:-1].strip()}』"
    if not keyword or len(keyword) > MAX_KEYWORD_CHARS:
        return None
    try:
        return sanitize_string(keyword)
    except ValueError:
        return None


def _is_title(keyword: str) -> bool:
    """책 제목만 말한 취향의 핵심어인가."""
    return keyword.startswith("『") and keyword.endswith("』")


def _applies_to(raw: Any, type_: str, keyword: str) -> tuple[str, ...]:
    """어디에. 주제가 아니거나 제목만 말했으면 비운다. 목록에 없는 이름은 빼고 앞의 3개만 남긴다.

    이름이 틀려도 취향 자체는 맞을 수 있어 항목은 버리지 않는다. 비면 그 취향은 핵심어로 찾는다(#395).
    """
    if type_ != "topic" or _is_title(keyword) or not isinstance(raw, list):
        return ()
    names = [_NAMES.get(_letters(name)) for name in raw if isinstance(name, str)]
    return tuple(dict.fromkeys(name for name in names if name))[:MAX_APPLIES_TO]


def _candidate(item: Any) -> Candidate | None:
    if not isinstance(item, dict):
        return None
    type_ = item.get("type")
    if not isinstance(type_, str) or type_ not in PREFERENCE_TYPES:
        return None
    value = _value(item.get("value"))
    strength = _strength(item.get("strength"), item.get("basis"))
    keyword = _keyword(item.get("keyword"))
    if value is None or strength is None or keyword is None:
        return None
    applies_to = _applies_to(item.get("applies_to"), type_, keyword)
    return Candidate(type_, value, strength, applies_to, keyword)


def screen(llm_output: Any, user_turns: Sequence[str] | None = None) -> list[Candidate]:
    """LLM 의 답(`{"extractions": [...]}`)에서 쓸 수 있는 후보만 세기의 크기가 큰 순으로 돌려준다.

    피함의 부탁(세기 -1)은 뺀다. 한 대화에서 종류와 핵심어가 같은 취향이 두 번 나오면 하나로 합친다.
    문장은 먼저 나온 것, 세기는 크기가 큰 쪽이다. 개수 상한은 합친 뒤에 센다.
    사용자가 한 말(user_turns)을 주면 근거·주인·직접 말했는지도 본다(drop_reason).
    """
    items = llm_output.get("extractions") if isinstance(llm_output, dict) else None
    if not isinstance(items, list):
        return []

    merged: dict[tuple[str, str], Candidate] = {}
    for item in items:
        candidate = _candidate(item)
        if candidate is None or candidate.strength == REQUEST_TO_AVOID:
            continue
        if user_turns is not None and drop_reason(item, user_turns) is not None:
            continue
        key = (candidate.type, text_key(candidate.keyword))
        seen = merged.get(key)
        if seen is None:
            merged[key] = candidate
        elif abs(candidate.strength) > abs(seen.strength):
            merged[key] = replace(seen, strength=candidate.strength)

    # sorted 는 순서를 지키는 정렬이라 세기의 크기가 같으면 LLM 이 낸 순서가 남는다
    ranked = sorted(merged.values(), key=lambda c: abs(c.strength), reverse=True)
    return ranked[:MAX_EXTRACTIONS]
