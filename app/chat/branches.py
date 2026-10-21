"""③ 갈래 질문 — 만들고, 직전 질문을 다시 읽고, 사용자가 고른 갈래를 찾는다(#530).

넓은 요청엔 갈래마다 한 권씩 보여 주고 "…(1번), …(2번) 중 어느 쪽이 끌리세요?"라고 묻는다. 모델은 카드마다 갈래
이름만 내고 질문 문장은 코드가 만든다. 꼴이 정해져 있으니 다음 턴에 직전 챗봇 말에서 번호표를 다시 읽을 수 있다.

번호표를 다시 읽는 이유: 사용자가 "3번"이라고 답해도 1단계가 그것을 갈래 이름으로 바꿔 검색어에 넣는 일이 드물었다
(#530 평가: 갈래 질문 다음 턴 42번 중 11번). 그러면 새 카드는 처음의 넓은 검색에서 다시 뽑은 책이라 좁혀지지
않는다. 그래서 고른 갈래가 하나로 정해지면 코드가 검색어에 붙인다.
"""

import re
from collections.abc import Sequence
from typing import Protocol

# 갈래 이름의 최대 글자 수. 질문이 길면 말풍선 200자 안에 못 붙인다
NAME_MAX = 15
# 갈래 질문 끝에 붙이는 "다른 쪽" 길. 보여 준 갈래에 원하는 것이 없으면 사용자가 거기서 벗어나 엉뚱한 쪽을 고르게
# 된다(#530 평가: "과학책"에 우주 갈래가 없어 가짜 사용자가 원하던 책에서 멀어졌다)
_NONE_OF_THEM = {2: "둘 다", 3: "셋 다"}
_ASK = "중 어느 쪽이 끌리세요?"
# 이름 안에 있으면 번호표를 다시 읽을 때 갈래가 갈라진다
_SEPARATORS = re.compile(r"[,()]")
_OPTION = re.compile(r"([^,()]+?)\((\d)번\)")

# 고른 번호: "3번", "세 번째", "셋째"
_DIGIT = re.compile(r"(?<!\d)(\d)\s*번")
_NATIVE = {"첫": 1, "두": 2, "세": 3}
_NATIVE_ORDINAL = re.compile(r"(첫|두|세)\s*번\s*째")
_SHORT_ORDINAL = {"첫째": 1, "둘째": 2, "셋째": 3}
# 고른 것이 아닌 말. 싫다는 말("3번은 별로")과 그 책을 묻는 말("1번은 초보도 볼 수 있어?")은 갈래 고르기가 아니다
_REJECT = re.compile(r"별로|싫|아니|말고|빼고|아쉬|안\s*(?:끌|맞|당기)")
_ASKING = re.compile(r"\?|(?:나요|까요|어때|있어|인가|일까|건가)\s*[.!~]*\s*$")


class _Turn(Protocol):
    role: str
    text: str


def question(names: list[str]) -> str:
    """갈래 질문. 꼴을 코드가 정한다. 모델에게 쓰게 했더니 40번 중 22번이 "어느 쪽" 꼴을 벗어나 문지기가 갈래
    질문인지 알아보지 못했고, 그 답("1번이 좋아 보여")이 ⑦로 갔다(#530 평가)."""
    options = ", ".join(f"{name}({n}번)" for n, name in enumerate(names, 1))
    rest = _NONE_OF_THEM.get(len(names), "모두")
    return f"{options} {_ASK} {rest} 아니면 원하는 쪽을 말씀해 주세요."


def parse_names(raw: object, card_count: int) -> list[str] | None:
    """카드마다 하나씩, 서로 다른 갈래 이름이면 그 목록. 아니면 None(질문하지 않는다)."""
    if not isinstance(raw, list) or len(raw) != card_count or card_count < 2:
        return None
    names = [item.strip() if isinstance(item, str) else "" for item in raw]
    if not all(0 < len(name) <= NAME_MAX for name in names):
        return None
    if any(_SEPARATORS.search(name) for name in names):
        return None
    if len(set(names)) != len(names):
        return None
    return names


def asked(recent_turns: Sequence[_Turn]) -> list[str] | None:
    """직전 챗봇 말이 갈래 질문이면 번호 차례의 갈래 이름들. 아니면 None."""
    for turn in reversed(recent_turns):
        if turn.role == "user":
            return None
        if turn.role == "assistant":
            return _names_in(turn.text)
    return None


def ever_asked(recent_turns: Sequence[_Turn]) -> bool:
    """최근 대화에서 갈래 질문을 한 적이 있나."""
    return any(
        turn.role == "assistant" and _names_in(turn.text) for turn in recent_turns
    )


def _names_in(text: str) -> list[str] | None:
    head, found, _ = text.rpartition(_ASK)
    if not found:
        return None
    # 질문은 말풍선 끝 문단에 붙는다. 앞 문단의 괄호는 번호표가 아니다
    head = head.rsplit("\n", 1)[-1]
    options = _OPTION.findall(head)
    names = [name.strip() for name, _ in options]
    if len(names) < 2 or [int(n) for _, n in options] != list(range(1, len(names) + 1)):
        return None
    return names


def chosen(message: str, names: list[str]) -> str | None:
    """사용자가 고른 갈래 하나. 번호("3번", "세 번째")나 그 갈래만 가진 낱말("감성 쪽")로 고른다.

    싫다는 말, 책을 묻는 말, 둘 이상을 고른 말은 None 이다. 이때는 1단계에 맡긴다.
    """
    text = message.strip()
    if _REJECT.search(text) or _ASKING.search(text):
        return None
    picked = {int(n) for n in _DIGIT.findall(text)}
    picked |= {_NATIVE[w] for w in _NATIVE_ORDINAL.findall(text)}
    picked |= {n for word, n in _SHORT_ORDINAL.items() if word in text}
    picked |= {n for n, name in enumerate(names, 1) if _says_name(text, name, names)}
    picked &= set(range(1, len(names) + 1))
    if len(picked) != 1:
        return None
    return names[picked.pop() - 1]


def _says_name(text: str, name: str, names: list[str]) -> bool:
    """이 갈래만 가진 낱말(두 글자 이상)을 말했나. "에세이"처럼 여러 갈래에 있는 낱말은 세지 않는다."""
    others = " ".join(other for other in names if other != name)
    return any(
        len(word) >= 2 and word in text and word not in others for word in name.split()
    )
