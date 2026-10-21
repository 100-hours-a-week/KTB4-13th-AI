"""③ 넓은 첫 요청에 한 번 묻기(#530) — 넓은지 판정하는 프롬프트, 묻는 턴의 꼴, 다음 턴에 직전 챗봇 말이 그 질문이었는지 알아보기.

넓은 첫 요청("○○ 책 추천해줘")에 바로 카드를 내면, 사용자가 카드 이야기만 하다가 원하는 핵심을 끝내 말하지 않았다
(#530 대화 평가, main 실패 16개 중 6개: "개발 공부" → 원한 건 "파이썬 처음", "돈 공부" → "주식 처음"). 그래서 첫
요청이 넓으면 카드 없이 열린 질문 하나를 먼저 한다. 명세상 카드 0장 + followup 이 되묻기의 기본 꼴이라 계약은 그대로다.

이것은 갈래 메뉴("…(1번), …(2번) 중 어느 쪽?")를 대신한다. 메뉴는 넓은 검색으로 뽑은 10권에서 보기를 만들어, 원하는
방향이 보기에 없으면 고를 수 없었다(평가: "과학책" 보기에 우주가 없음, "판타지" 보기에서 다크 판타지를 골라 마법에서
멀어짐). 열린 질문이면 사용자가 자기 말로 답하고, 그 말이 그대로 검색어가 된다.

넓은지는 1단계와 나란히 보내는 작은 판정 호출(SCOPE_PROMPT)이 정한다. 처음에는 판단과 질문을 1단계 프롬프트에 얹었는데,
규칙이 많은 그 프롬프트 안에서는 4o-mini 가 넓은지를 안정되게 가르지 못했다. 문구 네 벌을 같은 10개 첫 요청에 4번씩
돌려 보니 분야만 말한 요청에 묻는 비율이 0~4/4로 흔들렸고, 묻지 말라고 예로 든 "제주도 가족여행 가이드북"에도
4/4 물은 판이 있었다. 따로 판정하게 하니 넓은 요청 5개는 모두 4/4, 구체적인 요청 3개는 0~1/4였다(2026-10-21).

질문 끝에는 코드가 고정 문장(TAIL)을 붙인다. 서버는 대화를 저장하지 않아서, 다음 턴에 최근 대화 글자만 보고 "직전
챗봇 말이 이 질문이었다"를 알아야 하기 때문이다. 문지기는 이것으로 그 답을 ⑦에 들르지 않고 ③으로 보낸다.

질문과 함께 답 보기(options)도 만든다(#505). 사용자가 말한 주제를 더 좁히는 갈래 2~4개이고, 화면은 이것을 질문 아래
버튼으로 보여 주고 누르면 그 글자를 다음 메시지로 보낸다. 보기는 질문과 같은 판정 호출에서 같이 받는다 — 질문을
이해한 모델이 그 질문의 답을 같이 써야 둘이 어긋나지 않고, 호출도 늘지 않는다. 위의 갈래 메뉴와 다른 점은 둘이다.
보기는 검색한 10권이 아니라 사용자의 말에서 나오고, 보기에 없으면 지금처럼 자기 말로 답하면 된다(질문은 열린 질문
그대로다). 끝에는 코드가 "아무거나"를 붙인다. 고정 문장이 안내하는 말과 같아서 누르면 답 규칙이 그대로 받는다.

보기는 명세의 buttons 가 아니라 followup_options 칸에 싣는다. BE 는 ③ 응답의 buttons 에 navigate 밖의 버튼이 오면
턴 전체를 AI 장애로 처리한다(AgentChatExternalMapper). 새 칸은 BE 가 모르는 칸이라 무시되니, AI 가 먼저 배포되어도
챗봇이 깨지지 않는다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from langchain_core.prompts import ChatPromptTemplate

# none: 묻지 않는다 / may_ask: 넓으면 물어도 된다(대화의 첫 요청) / answered: 이번 말은 직전 질문의 답이다
Stage = Literal["none", "may_ask", "answered"]

# 질문 끝에 코드가 붙이는 문장. 답하기 싫은 사람에게 건너뛸 길을 알리고, 다음 턴에 이 질문을 알아보는 표시가 된다.
# 최근 대화는 줄마다 뒤쪽 200자만 실리므로(명세 ③ 입력 ³) 끝에 둔다.
TAIL = '바로 보고 싶으시면 "아무거나"라고 해 주세요.'
_TAIL_MARK = re.compile(r"아무거나[\"”']?\s*라고\s*해\s*주세요")

REPLY_FALLBACK = "딱 맞는 책을 찾으려고 하나만 여쭤볼게요."
REPLY_MAX_CHARS = 60
QUESTION_MAX_CHARS = 100
# 보기는 버튼 한 칸에 들어가고, 누르면 그대로 사용자 말이 된다. 길면 자르지 않고 버린다 — 잘린 글자를 보내면 말이 안 된다.
OPTION_MAX_CHARS = 20
MIN_OPTIONS = 2
MAX_OPTIONS = 4
# 보기 끝에 코드가 붙이는 건너뛰기 보기. TAIL 이 안내하는 말과 같다.
SKIP_OPTION = "아무거나"
# 모델이 건너뛰기 보기를 스스로 만들면 SKIP_OPTION 과 겹친다. 그런 보기는 버린다.
_SKIP_LIKE = re.compile(r"(아무거나|상관\s?없|다\s?좋아|잘\s?모르겠)")

# {message}가 채워지는 자리다. JSON 예시의 중괄호는 {{ }}로 겹쳐 쓴다.
# 예시는 평가 시나리오(evals/chat_dialog/scenarios.json)와 겹치지 않는 주제로 고른다. 평가 질문을 예로 쓰면 그 질문만 맞힌다.
SCOPE_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇이다. 사용자의 첫 요청을 보고, 바로 책을 찾아도 될 만큼 원하는 것이 분명한지 판단하라.\n\n"
    '첫 요청: "{message}"\n\n'
    '- broad: 분야나 큰 주제만 말했거나(예: "여행 책 추천해줘", "시집 읽고 싶어"), 기분이나 사정만 말했다'
    '(예: "요즘 잠이 안 와"). 무엇을 위해·어느 수준으로 읽을지에 따라 맞는 책이 크게 갈린다.\n'
    "- clear: 제목·저자를 말했거나, 주제에 더해 대상·수준·목적·형식·조건 중 하나 이상을 말했다"
    '(예: "제주도 가족여행 가이드북", "초등학생이 읽을 공룡 도감", "엑셀 처음 배우는 입문서", "2만원 이하 요리책").\n\n'
    f"broad 면 사용자에게 할 말도 써라. reply 는 요청에 짧게 반응하는 한 문장(최대 {REPLY_MAX_CHARS}자), "
    f"question 은 원하는 것을 묻는 열린 질문 한두 문장(최대 {QUESTION_MAX_CHARS - 20}자)이다. 상황이나 목적을 "
    "먼저 묻고, question 안에 보기를 늘어놓지 마라.\n"
    f"options 는 question 에 대한 답 보기 {MIN_OPTIONS}~{MAX_OPTIONS}개다. question 이 물은 것에 그대로 답하는 "
    "말이어야 한다(누구에게 줄지 물었으면 보기도 받는 사람이다). 사용자가 말한 주제 안에서 맞는 책이 크게 갈리는 "
    '갈래로 나누고, 서로 겹치지 않게 하라. "감정적 지지"처럼 뜻이 넓은 말 대신 서점에서 책을 고를 때 쓰는 구체적인 '
    f"말로, 사용자가 그대로 눌러 보낼 짧은 말(최대 {OPTION_MAX_CHARS}자)로 써라. 사용자가 이미 말한 것을 되묻는 "
    '보기, 첫 요청에 없는 주제로 넘어가는 보기, "상관없어요"·"아무거나" 같은 보기는 넣지 마라.\n'
    '예) 첫 요청 "여행 책 추천해줘" → question "어떤 여행을 준비하고 계세요?", '
    'options ["국내 여행 코스", "해외 배낭여행", "읽는 여행 에세이"]\n'
    '답은 JSON 한 줄로만 하라: {{"scope": "clear"}} 또는 '
    '{{"scope": "broad", "reply": "…", "question": "…", "options": ["…", "…"]}}'
)

# 1단계 프롬프트의 {ask_rule} 자리에 들어간다. 값으로 채워지므로 중괄호를 겹쳐 쓰지 않는다.
ANSWERED_RULE = (
    "직전 챗봇 말은 사용자가 원하는 것을 물은 질문이고, 이번 메시지는 그 답이다. 지금 semantic 에 답에서 알게 된 "
    "것(상황, 목적, 수준, 장르, 분위기)을 더해 찾을 것을 semantic 에 통째로 다시 써라. 싫다고 한 것은 semantic 에 "
    '적지 마라 — 검색어에 들어가면 오히려 그 책이 찾아진다. 답이 "아무거나"처럼 더 말할 게 없다는 뜻이면 '
    "빈 객체 {}로 답하라.\n\n"
)


@dataclass(frozen=True)
class Ask:
    """묻는 턴에 할 말. options 는 질문 아래 보일 답 보기이고, 못 만들었으면 비어 있다(질문만 한다)."""

    reply: str
    question: str
    options: tuple[str, ...] = ()


class _Turn(Protocol):
    role: str
    text: str


def rule(stage: Stage) -> str:
    """1단계 프롬프트에 넣을 규칙. 직전 질문의 답일 때만 있고, 그 밖에는 빈 문자열이라 프롬프트가 이 기능 전과 같다."""
    return ANSWERED_RULE if stage == "answered" else ""


def asked(recent_turns: Sequence[_Turn]) -> bool:
    """직전 챗봇 말이 이 모듈의 질문이었나."""
    for turn in reversed(recent_turns):
        if turn.role == "user":
            return False
        if turn.role == "assistant":
            return bool(_TAIL_MARK.search(turn.text))
    return False


def parse(answer: dict) -> Ask | None:
    """판정 답이 넓다(broad)면 물을 말. 분명하거나 질문이 비었으면 None — 묻지 않고 평소처럼 찾는다."""
    if answer.get("scope") != "broad":
        return None
    question = answer.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    reply = answer.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = REPLY_FALLBACK
    return Ask(
        _fit(reply.strip(), REPLY_MAX_CHARS),
        _fit(question.strip(), QUESTION_MAX_CHARS),
        _options(answer.get("options")),
    )


def _options(raw: object) -> tuple[str, ...]:
    """모델이 쓴 보기를 버튼에 실을 수 있게 거른다. 쓸 보기가 둘이 안 되면 고를 게 없으니 보기 없이 묻는다.

    목록이 아니거나, 글자가 아니거나, 비었거나, 너무 길거나, 앞 보기와 같거나, 건너뛰기 뜻인 보기는 버린다.
    남은 보기 끝에 SKIP_OPTION 을 붙인다.
    """
    if not isinstance(raw, list):
        return ()
    kept: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        option = " ".join(item.split())
        if (
            not option
            or len(option) > OPTION_MAX_CHARS
            or option in kept
            or _SKIP_LIKE.search(option)
        ):
            continue
        kept.append(option)
    if len(kept) < MIN_OPTIONS:
        return ()
    return (*kept[:MAX_OPTIONS], SKIP_OPTION)


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
