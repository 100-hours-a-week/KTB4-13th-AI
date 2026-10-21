"""③ 넓은 첫 요청에 한 번 묻기(#530) — 1단계에 줄 규칙, 묻는 턴의 꼴, 다음 턴에 직전 챗봇 말이 그 질문이었는지 알아보기.

넓은 첫 요청("○○ 책 추천해줘")에 바로 카드를 내면, 사용자가 카드 이야기만 하다가 원하는 핵심을 끝내 말하지 않았다
(#530 대화 평가, main 실패 16개 중 6개: "개발 공부" → 원한 건 "파이썬 처음", "돈 공부" → "주식 처음"). 그래서 첫
요청이 넓으면 카드 없이 열린 질문 하나를 먼저 한다. 명세상 카드 0장 + followup 이 되묻기의 기본 꼴이라 계약은 그대로다.

이것은 갈래 메뉴("…(1번), …(2번) 중 어느 쪽?")를 대신한다. 메뉴는 넓은 검색으로 뽑은 10권에서 보기를 만들어, 원하는
방향이 보기에 없으면 고를 수 없었다(평가: "과학책" 보기에 우주가 없음, "판타지" 보기에서 다크 판타지를 골라 마법에서
멀어짐). 열린 질문이면 사용자가 자기 말로 답하고, 그 말이 그대로 검색어가 된다.

묻고 말지는 1단계 모델이 정한다. 호출을 늘리지 않으려고 판단과 질문을 1단계 호출에 얹는다. 질문 끝에는 코드가 고정
문장(TAIL)을 붙인다. 서버는 대화를 저장하지 않아서, 다음 턴에 최근 대화 글자만 보고 "직전 챗봇 말이 이 질문이었다"를
알아야 하기 때문이다. 문지기는 이것으로 그 답을 ⑦에 들르지 않고 ③으로 보낸다.
"""

import re
from collections.abc import Sequence
from typing import Literal, Protocol

# none: 묻지 않는다 / may_ask: 넓으면 물어도 된다(대화의 첫 요청) / answered: 이번 말은 직전 질문의 답이다
Stage = Literal["none", "may_ask", "answered"]

# 질문 끝에 코드가 붙이는 문장. 답하기 싫은 사람에게 건너뛸 길을 알리고, 다음 턴에 이 질문을 알아보는 표시가 된다.
# 최근 대화는 줄마다 뒤쪽 200자만 실리므로(명세 ③ 입력 ³) 끝에 둔다.
TAIL = '바로 보고 싶으시면 "아무거나"라고 해 주세요.'
_TAIL_MARK = re.compile(r"아무거나[\"”']?\s*라고\s*해\s*주세요")

REPLY_FALLBACK = "딱 맞는 책을 찾으려고 하나만 여쭤볼게요."
REPLY_MAX_CHARS = 60
QUESTION_MAX_CHARS = 100

# 1단계 프롬프트의 {ask_rule} 자리에 들어간다. 값으로 채워지므로 중괄호를 겹쳐 쓰지 않는다.
# 예시는 평가 시나리오(evals/chat_dialog/scenarios.json)와 겹치지 않는 주제로 고른다. 평가 질문을 예로 쓰면 그 질문만 맞힌다.
MAY_ASK_RULE = (
    "이번 메시지는 대화의 첫 요청이다. 찾는 범위가 넓어 어떤 책이 맞을지 사람마다 크게 갈리면 — 분야나 큰 "
    "주제만 말하고, 왜 읽는지·어떤 상황인지·어느 수준인지는 말하지 않았으면 — 바로 찾지 말고 한 번 물어라. "
    '그때도 메시지에서 알 수 있는 조건은 평소처럼 넣고, "turn": "ask"와 "reply", "question"을 같이 넣어라. '
    f"reply는 이번 메시지에 짧게 반응하는 한 문장(최대 {REPLY_MAX_CHARS}자)이다. question은 원하는 것을 "
    f"묻는 열린 질문 한두 문장(최대 {QUESTION_MAX_CHARS - 20}자)이다. 상황이나 목적을 먼저 묻고, 고를 보기를 "
    "늘어놓지 마라. 제목·저자를 말했거나 원하는 것이 이미 구체적이면 묻지 말고 평소처럼 답하라.\n"
    '묻기 예시(형식일 뿐 실제 값 아님) — 메시지가 "여행 책 추천해줘"면:\n'
    '{"semantic": "여행 책", "turn": "ask", "reply": "여행 책을 찾으시는군요.", '
    '"question": "어떤 여행을 생각하세요? 떠나기 전에 준비하려는 건지, 여행 이야기를 읽고 싶은 건지 알려 주세요."}\n'
    '묻지 않는 예시(형식일 뿐 실제 값 아님) — 메시지가 "제주도 가족여행 가이드북"이면:\n'
    '{"semantic": "제주도 가족여행 가이드북"}\n\n'
)
ANSWERED_RULE = (
    "직전 챗봇 말은 사용자가 원하는 것을 물은 질문이고, 이번 메시지는 그 답이다. 지금 semantic 에 답에서 알게 된 "
    "것(상황, 목적, 수준, 장르, 분위기)을 더해 찾을 것을 semantic 에 통째로 다시 써라. 싫다고 한 것은 semantic 에 "
    '적지 마라 — 검색어에 들어가면 오히려 그 책이 찾아진다. 답이 "아무거나"처럼 더 말할 게 없다는 뜻이면 '
    "빈 객체 {}로 답하라.\n\n"
)


class _Turn(Protocol):
    role: str
    text: str


def rule(stage: Stage) -> str:
    """1단계 프롬프트에 넣을 규칙. 묻지 않는 턴은 빈 문자열이라 프롬프트가 이 기능 전과 같다."""
    if stage == "may_ask":
        return MAY_ASK_RULE
    if stage == "answered":
        return ANSWERED_RULE
    return ""


def asked(recent_turns: Sequence[_Turn]) -> bool:
    """직전 챗봇 말이 이 모듈의 질문이었나."""
    for turn in reversed(recent_turns):
        if turn.role == "user":
            return False
        if turn.role == "assistant":
            return bool(_TAIL_MARK.search(turn.text))
    return False


def parse(patch: dict) -> tuple[str, str] | None:
    """1단계 patch 에 묻기가 있으면 (reply, question). 질문이 없거나 비었으면 None — 묻지 않고 평소처럼 찾는다."""
    if patch.get("turn") != "ask":
        return None
    question = patch.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    reply = patch.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = REPLY_FALLBACK
    return _fit(reply.strip(), REPLY_MAX_CHARS), _fit(
        question.strip(), QUESTION_MAX_CHARS
    )


def followup(question: str) -> str:
    """묻는 턴의 followup. 질문 끝에 고정 문장을 붙인다."""
    return f"{question} {TAIL}"


def _fit(text: str, limit: int) -> str:
    """limit 자 안으로. 넘으면 뒷 문장부터 덜어 내고, 한 문장만으로도 넘으면 자른다."""
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    while len(sentences) > 1 and len(" ".join(sentences)) > limit:
        sentences = sentences[:-1]
    fitted = " ".join(sentences)
    return fitted if len(fitted) <= limit else fitted[: limit - 1] + "…"
