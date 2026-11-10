"""③ 판단 호출 — 세 갈래에서 모은 한 목록을 보고 카드·질문·한 번 더 찾기 중 하나를 고른다(CHAT_AGENT).

지금의 카드 생성 호출(CARD_PROMPT)을 대신한다. 이 호출은 턴마다 한 번이고, 한 번 더 찾을 때만 두 번이다. 그 앞에 1단계와,
1단계와 나란히 가는 떠올린 제목 호출(app/chat/recall.py)이 있다.

- 카드: 사용자 말에 맞는 책만 최대 3장. 셋을 채우려고 맞지 않는 책을 넣지 않는다. 말풍선 마지막 문장은 고른 책의
  함께 읽은 책(옆으로 넓히기)·마니아 추천(같은 분야 더 보기)에서 두 갈래를 제안한다. 이 문장은 코드가 쓴다(proposal) —
  모델에게 맡기니 4o 는 제안 없이 한 문장으로 끝냈다(11/10 점검). 제안은 카드로 내지 않는다 — 카드에는 "제안" 표시가
  없고, 넓히기 책은 매칭 % 가 낮아 엉뚱한 추천처럼 보인다(11/10 결정)
- 질문: 후보가 서로 다른 방향으로 갈려 고를 수 없을 때, 목록에서 나온 두 갈래로 묻는다
- 한 번 더 찾기: 사용자 말의 조건 하나를 어느 후보도 맞히지 못하면, 놓친 조건과 새 검색어를 낸다. 턴마다 한 번만

화면에 쓰는 이유는 확인된 것만이다. 모델이 고를 때는 자기 지식을 써도 되지만, 이유 칸은 소개글로만 쓴다. 소개글이 없는
책과 그래프에서 온 책의 이유는 코드가 규칙으로 쓴다(아래 함수들).

정보나루의 함께 대출 목록은 사용자에게 "함께 읽은 책"이라고 말한다(11/10 사용자 결정: 질문도 답도 빌린 책이 아니라 함께
읽은 책). 같이 읽는 책일 뿐 비슷한 책이 아니고, 몇 명이 읽었는지·어떤 순서로 읽었는지 모른다. 그래서 "비슷한 책",
"많이 읽은", "다음에 읽은", "더 깊은" 같은 말은 쓰지 않고, 쓴 문장은 뺀다. 출처(도서관 정보나루)는 그대로 밝힌다.
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.prompts import ChatPromptTemplate

from app.chat.lanes import display_title
from app.core import voice

Action = Literal["cards", "ask", "search_again"]

# 목록에 넣는 소개글 길이. 후보가 30권 안팎이라 소개 전체를 넣으면 프롬프트가 너무 길어진다. 책소개 중앙값은 148자다
LISTING_DESCRIPTION_CHARS = 400
MAX_QUERY_CHARS = 50
MAX_REASON_SHORT_CHARS = 80
# 제안 문장에 짚는 제목 길이. 말풍선 200자 안에 모델 문장도 남게 줄인다
PROPOSAL_TITLE_CHARS = 25
SOURCE_NOTE = "도서관 정보나루"

_SOURCE_LABEL = {
    "search": "검색",
    "title": "떠올린 제목",
}

# 함께 읽은 책·마니아 목록으로는 알 수 없는 것(몇 명이, 어떤 순서로)을 말하는 낱말. 이 말이 든 문장은 뺀다.
# "많은 이들에게 깊은 울림"이 빠져나갔다(11/10 실험 2). "많은 이야기"는 걸리지 않게 "이" 뒤에 조사가 올 때만 본다
_OVERCLAIM = re.compile(
    r"많이\s?(빌|읽)|자주\s?(빌|읽)|가장\s?많이|많은\s?(사람|독자|분|이들|이(?=[에가의]))|다음에\s?읽|이어서\s?읽"
    r"|그다음|더\s?깊"
)
_SIMILAR = re.compile(r"비슷")
# 숫자로 끝나는 제목은 읽는 소리로 받침을 본다. 『AI 2041』(이천사십일)에 "를"을 붙였다(11/10 실험 2)
_DIGIT_HAS_FINAL = frozenset("013678")


def _object_particle(word: str) -> str:
    """받침이 있으면 "을", 없으면 "를". 숫자는 읽는 소리(영·일·삼·육·칠·팔은 받침)로 보고, 그 밖의 글자는 "를"."""
    last = word.rstrip()[-1:]
    if "가" <= last <= "힣" and (ord(last) - ord("가")) % 28 != 0:
        return "을"
    if last in _DIGIT_HAS_FINAL:
        return "을"
    return "를"


def _clip(title: str, room: int) -> str:
    return title if len(title) <= room else title[: max(room - 1, 1)].rstrip() + "…"


def relation_reason(kind: str, anchor: str) -> str:
    """그래프에서 온 책의 한 줄 이유. 관계 사실만 쓴다. 80자 안에 들게 기준 책 제목을 줄인다."""
    if kind == "co":
        frame = "『{t}』{p} 읽은 사람들이 함께 읽은 책이에요(" + SOURCE_NOTE + ")."
    else:
        frame = "『{t}』의 정보나루 마니아 추천 목록에 있는 책이에요."
    room = MAX_REASON_SHORT_CHARS - len(frame.format(t="", p="을"))
    title = _clip(anchor, room)
    return frame.format(t=title, p=_object_particle(title))


def relation_basis(kind: str, anchor: str) -> dict:
    """match_basis 한 칸. 출처를 같이 적는다."""
    if kind == "co":
        return {
            "label": "함께 읽은 책",
            "detail": f"『{anchor}』{_object_particle(anchor)} 읽은 사람들이 함께 읽은 책({SOURCE_NOTE})",
        }
    return {
        "label": "마니아 추천",
        "detail": f"『{anchor}』의 마니아 추천 목록에 있는 책({SOURCE_NOTE})",
    }


def confirmed_reason(book: dict) -> str:
    """소개글이 없는 떠올린 책의 한 줄 이유. 확인된 칸(저자·출간연도·분류)만 쓴다.

    "찾으신 X의 『Y』예요"(#86 규칙)는 사용자가 제목을 콕 집어 찾은 책에 맞는 말이라, 모델이 떠올린 책에는 틀리다.
    """
    author = _first_author(book.get("author"))
    facts = " ".join(
        part
        for part in (
            f"{book['pub_year']}년" if book.get("pub_year") else "",
            (book.get("category") or "").strip(),
        )
        if part
    )
    head = f"{author}의 " if author else f"『{_clip(book['title'], 40)}』, "
    reason = f"{head}{facts} 책이에요." if facts else f"{head}책이에요."
    if len(reason) <= MAX_REASON_SHORT_CHARS:
        return reason
    return _clip(reason, MAX_REASON_SHORT_CHARS)


_AUTHOR_ROLE = re.compile(
    r"^.*[:：]\s*|\s*\([^)]*\)|\s+(지음|옮김|엮음|그림|글|저|역|편)\s*$"
)


def _first_author(author: str | None) -> str:
    first = re.split(r"[;,]", author or "")[0]
    return _AUTHOR_ROLE.sub("", first).strip()


_TITLE_SPAN = re.compile(r"[『《][^』》]*[』》]")
_HIDDEN_TITLE = re.compile(r"\x00(\d+)\x00")


def sentences(text: str) -> list[str]:
    """말풍선을 문장으로 나눈다. 『제목』 안의 문장부호에서는 자르지 않는다.

    voice.sentences_of 는 "!"·"?" 뒤 띄어쓰기마다 자른다. 제목에 "! "가 든 책을 제안한 문장이 반으로 갈려, 카드에 없는
    책 문장을 빼는 규칙이 앞 반쪽만 지우고 "…DIY하다!』를 제안해요."를 남겼다(11/10 점검).
    """
    titles: list[str] = []

    def hide(match: re.Match) -> str:
        titles.append(match.group(0))
        return f"\x00{len(titles) - 1}\x00"

    masked = _TITLE_SPAN.sub(hide, text.strip())
    return [
        _HIDDEN_TITLE.sub(lambda m: titles[int(m.group(1))], part)
        for part in re.split(r"(?<=[.!?])\s+", masked)
        if part
    ]


def proposal(picked: list[dict]) -> str | None:
    """말풍선 마지막 문장: 고른 책의 이웃 줄에서 옆으로 넓히기(함께 읽은 책)와 같은 분야 더 보기(마니아 추천)를 한 권씩.

    앞 카드부터 줄이 있는 책을 찾는다. 카드에 이미 나온 책은 짚지 않는다. 함께 읽은 책은 함께 읽었다는 사실로만 말하고,
    마니아 추천은 방향으로만 말한다 — 목록으로는 몇 명이 읽었는지, 무엇으로 골랐는지 모른다. 줄이 없으면 None.

    같은 책이 함께 대출과 마니아 목록에 다 들어 있으면 두 갈래가 같은 책을 짚는다("…『더 커밍 웨이브』도 함께 읽었고, 같은
    분야를 더 보려면 『더 커밍 웨이브』도 있어요", 11/10 실험 2·3에서 두 번). 마니아 쪽은 함께 대출에서 짚은 책을 건너뛴다.
    이웃 줄의 제목은 부제를 뗀 제목이라 카드 제목도 둘 다 견준다.
    """
    on_cards = {b["title"] for b in picked} | {
        display_title(b["title"]) for b in picked
    }

    def first(kind: str, taken: set[str]) -> tuple[str, str] | None:
        for book in picked:
            for title in (book.get("neighbors") or {}).get(kind, []):
                if title not in taken:
                    return display_title(book["title"]), title
        return None

    co = first("co", on_cards)
    mania = first("mania", on_cards | ({co[1]} if co else set()))
    room = PROPOSAL_TITLE_CHARS
    other = (
        f"같은 분야를 더 보려면 『{_clip(mania[1], room)}』도 있어요."
        if mania
        else None
    )
    if co is None:
        return other
    card, title = _clip(co[0], room), _clip(co[1], room)
    readers = f"『{card}』{_object_particle(card)} 읽은 분들은 『{title}』도 함께 읽었"
    if other is None:
        return readers + "어요."
    return f"{readers}고, {other}"


def with_proposal(
    reply: str | None, proposal_text: str | None, max_chars: int
) -> str | None:
    """말풍선 끝에 제안 문장을 붙인다. 길이를 넘으면 모델이 쓴 뒷 문장부터 덜어 낸다 — 제안 문장을 남긴다."""
    if not proposal_text:
        return reply
    kept = sentences(reply or "")
    while kept and len(" ".join([*kept, proposal_text])) > max_chars:
        kept = kept[:-1]
    return " ".join([*kept, proposal_text])


def overclaims(text: str) -> bool:
    """함께 대출·마니아 목록으로는 알 수 없는 것을 말하는가."""
    return bool(_OVERCLAIM.search(text))


def without_overclaims(reply: str, co_titles: set[str]) -> str | None:
    """말풍선에서 목록으로 알 수 없는 것을 말한 문장, 함께 읽은 책을 "비슷한" 책이라 한 문장을 뺀다. 다 빠지면 None."""
    kept = []
    for sentence in sentences(reply):
        if overclaims(sentence):
            continue
        named = set(re.findall(r"[『《]([^』》]+)[』》]", sentence))
        if _SIMILAR.search(sentence) and named & co_titles:
            continue
        kept.append(sentence)
    return " ".join(kept) or None


# 형식 예시를 그대로 베낀 근거 칸. mini 가 판단 호출 3턴 중 1턴꼴로 label 에 "근거 이름"을 그대로 썼다(11/10 점검)
_PLACEHOLDER_BASIS = {"근거 이름", "그 근거의 구체적 내용"}


def without_placeholder_basis(basis: list[dict]) -> list[dict]:
    return [
        b
        for b in basis
        if b["label"].strip() not in _PLACEHOLDER_BASIS
        and b["detail"].strip() not in _PLACEHOLDER_BASIS
    ]


def _sources_text(book: dict) -> str:
    labels = []
    for lane, anchor in book.get("sources") or [("search", None)]:
        if lane in _SOURCE_LABEL:
            labels.append(_SOURCE_LABEL[lane])
        elif lane == "co":
            labels.append(f"『{anchor}』 함께 읽은 책")
        else:
            labels.append(f"『{anchor}』 마니아 추천")
    return ", ".join(dict.fromkeys(labels))


def _confirmed_facts(book: dict) -> str:
    parts = [
        book.get("publisher") or "",
        f"{book['pub_year']}년" if book.get("pub_year") else "",
        book.get("category") or "",
    ]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def format_listing(candidates: list[dict]) -> str:
    """판단 호출에 넣는 한 목록. 책마다 출처 꼬리표, 소개(없으면 확인된 정보), 함께 읽은 책·마니아 이웃 줄.

    소개글이 없다고 적지 않는다. "소개글 없음"을 적으면 모델이 그 책을 아예 안 고른다(#86, 평가 33번 5회 중 4회 0장).
    소개 자리에 확인된 정보(출판사·출간연도·분류)를 둔다.
    """
    lines = []
    for c in candidates:
        lines.append(
            f"- book_id {c['book_id']}: {c['title']} - {c.get('author') or ''} [{_sources_text(c)}]"
        )
        about = " ".join((c.get("description") or "").split())
        if about:
            lines.append(f"  소개: {about[:LISTING_DESCRIPTION_CHARS]}")
        elif facts := _confirmed_facts(c):
            lines.append(f"  확인된 정보: {facts}")
        neighbors = c.get("neighbors") or {}
        parts = []
        if neighbors.get("co"):
            parts.append(
                "함께 읽은 책 " + ", ".join(f"『{t}』" for t in neighbors["co"])
            )
        if neighbors.get("mania"):
            parts.append(
                "마니아 추천 " + ", ".join(f"『{t}』" for t in neighbors["mania"])
            )
        if parts:
            lines.append("  이 책의 " + " / ".join(parts))
    return "\n".join(lines)


_ASK_ALLOWED = (
    "고를 만한 책들이 서로 다른 두 방향으로 갈리고 사용자의 말만으로는 어느 쪽인지 알 수 없어 한 권도 자신 있게 "
    "고를 수 없을 때만 고른다. question 에 그 두 방향을 목록의 소개나 출처에서 나온 말로 직접 말하며 어느 쪽인지 "
    "묻는 한 문장을 쓴다. reply 는 이번 메시지에 짧게 반응하는 한 문장이다. 막연한 요청이어도 맞는 책이 있으면 "
    "묻지 말고 cards 를 고른다."
)
_ASK_BLOCKED = "이번에는 고르지 마라."
_SEARCH_ALLOWED = (
    "사용자 말의 중요한 조건 하나를 목록의 어느 책도 맞히지 못할 때만 고른다. missed 에 놓친 조건을, query 에 그 "
    "조건을 담은 새 검색어(30자 이내, 책 제목 말고 주제 낱말)를 쓴다. 맞는 책이 한 권이라도 있으면 cards 를 고른다."
)
_SEARCH_BLOCKED = "이번에는 고르지 마라. 이미 한 번 다시 찾았다."
_PROPOSE = (
    "고른 책 아래에 함께 읽은 책·마니아 추천 줄이 있으면 말풍선에서 다른 책을 제안하지 마라. 다음에 읽을 방향을 "
    "권하는 문장은 따로 붙는다. 줄이 없으면 사용자가 말한 것에 가장 맞는 한 권을 "
    "『제목』으로 짚고 그 까닭을 책 소개에 있는 것으로 한마디 하라. "
)
_PROPOSE_NO_QUESTION = "마지막 문장은 묻는 말이 아니라 권하는 말로 끝내라. "

# JSON 은 action → 고른 책(picks) → 말풍선(reply) → 카드 이유(cards) 순서다. 카드 생성 호출(#645)과 같은 까닭이다 — 책을
# 먼저 골라야 말풍선이 고른 책과 어긋나지 않고, 말풍선을 카드 이유 앞에 두어야 SSE 가 말풍선을 이유보다 먼저 보낸다.
# cards 가 reply 앞이면 에이전트를 켰을 때 카드 턴 말풍선이 이유를 다 쓴 뒤에야 나왔다. 화면 차례는 picks 를 따른다.
JUDGE_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇이다. 사용자와 대화하며 원하는 책을 함께 좁혀 간다.\n\n"
    "최근 대화:\n{recent_turns}\n\n"
    '이번 메시지: "{message}"\n\n'
    "{intro}\n\n"
    "아래 책 목록은 이 서점에서 파는 책을 세 곳에서 모은 것이다. 책마다 [ ] 안에 출처가 있다.\n"
    "- 검색: 사용자의 말로 서점을 검색한 결과\n"
    "- 떠올린 제목: 이 요청에 맞을 만한 책으로 떠올린 제목을 서점에서 찾은 것\n"
    "- 『X』 함께 읽은 책: 도서관 기록에서 『X』를 읽은 사람들이 함께 읽은 책\n"
    "- 『X』 마니아 추천: 도서관 기록에서 『X』의 마니아 추천 목록에 있는 책\n"
    '책 아래의 "이 책의 함께 읽은 책 / 마니아 추천" 줄도 같은 도서관 기록이다. 함께 읽은 책은 같이 읽는 책이지 '
    "비슷한 책이 아니다. 마니아 추천을 무엇으로 골랐는지는 모른다. 둘 다 몇 명이 읽었는지, 어떤 순서로 읽었는지 "
    "모른다.\n\n"
    "셋 중 하나를 골라 action 에 적어라.\n"
    '1. "cards": 사용자의 말에 맞는 책을 출처와 상관없이 맞는 순서로 최대 {limit}권 고른다. 맞지 않는 책으로 칸을 '
    "채우지 마라. {relation_rule}\n"
    '2. "ask": {ask_rule}\n'
    '3. "search_again": {search_rule}\n\n'
    "cards 를 고르면:\n"
    "반드시 한국어로만 답하라. book_id 는 목록에 적힌 값을 그대로 써라. 순서 번호가 아니다. picks 에 적은 차례가 "
    "화면의 1번, 2번, 3번이다. cards 에는 picks 의 책을 같은 차례로 적어라.\n"
    "reason_short 는 80자 이내 한 줄 이유, reason_long 은 사용자가 찾은 것과 이 책 소개의 어떤 점이 맞는지 잇는 "
    "2-4문장(최대 300자)이다. 책 소개 문장을 옮기거나 줄여 쓰지 마라. 근거는 그 책의 소개에 있는 것만 쓴다. "
    "match_basis 는 reason_short·reason_long 과 같은 근거를 label(예: 분위기, 장르, 가격)과 detail(그 근거의 구체적 "
    "내용) 짝으로 1~3개 적어라 — 새로운 근거를 지어내지 말고 두 이유 문장에 이미 쓴 근거만 옮겨 적어라.\n"
    "reply 는 말풍선에 보일 1-2문장이다(최대 120자). 먼저 이번 메시지에 반응하라. 요청을 되풀이하며 시작하지 "
    "마라. 카드 제목을 늘어놓지 말고 고른 책들이 어떤 결인지 한마디로 말하라. 책마다의 내용 설명은 뒤의 cards 이유에 "
    "쓰니 말풍선에 늘어놓지 마라. {propose}\n"
    '"비슷한 책"(함께 읽은 책), "더 깊은 책"(마니아 추천 책), "많이 읽은", "다음에 읽은" 같은 말은 쓰지 마라.\n'
    + voice.RULES
    + "다음 JSON 형식으로만, 이 칸 순서대로 답하라. action 을 맨 앞에 쓴다. 고른 action 에 쓰지 않는 칸은 넣지 마라: "
    '{{"action": "cards 또는 ask 또는 search_again", '
    '"picks": [고른 책의 book_id 정수], "reply": "말풍선", '
    '"cards": [{{"book_id": 정수, "reason_short": "한 줄 이유", "reason_long": "긴 이유", '
    '"match_basis": [{{"label": "근거 이름", "detail": "그 근거의 구체적 내용"}}]}}], '
    '"question": "ask 일 때 묻는 말", '
    '"missed": "search_again 일 때 놓친 조건", "query": "search_again 일 때 새 검색어"}}\n\n'
    "책 목록:\n{listing}"
)

# 관계를 물은 턴은 목록에 든 책이 곧 답이라 맞는 책만 고르라는 규칙보다 이것을 따르게 한다. 그 규칙만 두었더니 함께 읽은 책
# 후보 8~11권 중 1권만 고른 턴이 둘, 마니아 추천 20권 중 1~2권만 고른 턴이 셋이었다(11/10 실험 2). 사용자가 말한 다른 조건에
# 어긋나는 책은 그대로 뺀다.
_RELATION_RULE = {
    "co": "사용자가 기준 책을 읽은 사람들이 함께 읽은 책을 물었다. 함께 읽은 책에서 고른다. 목록의 함께 읽은 책은 모두 이 "
    "물음의 답이니, 사용자가 말한 다른 조건에 어긋나는 책만 빼고 {limit}권을 채워라.",
    "similar": "사용자가 기준 책과 비슷한 책을 더 원한다. 마니아 추천 책을 먼저 보고, 같은 저자 책은 한 권까지만 고른다. "
    "마니아 추천 책은 기준 책을 읽은 사람에게 권하는 목록이니, 사용자가 말한 다른 조건에 어긋나는 책만 빼고 {limit}권을 "
    "채워라.",
}


def prompt_inputs(
    *,
    recent_turns: str,
    message: str,
    intro: str,
    listing: str,
    limit: int,
    may_ask: bool,
    may_search: bool,
    relation: str | None,
) -> dict[str, Any]:
    return {
        "recent_turns": recent_turns,
        "message": message,
        "intro": intro,
        "listing": listing,
        "limit": limit,
        "ask_rule": _ASK_ALLOWED if may_ask else _ASK_BLOCKED,
        "search_rule": _SEARCH_ALLOWED if may_search else _SEARCH_BLOCKED,
        "propose": _PROPOSE + ("" if may_ask else _PROPOSE_NO_QUESTION),
        "relation_rule": _RELATION_RULE.get(relation or "", "").format(limit=limit),
    }


@dataclass(frozen=True)
class Decision:
    action: Action
    cards: list[Any]
    reply: str | None
    question: str | None = None
    missed: str | None = None
    query: str | None = None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse(answer: dict, *, may_ask: bool, may_search: bool) -> Decision:
    """판단 호출의 답을 읽는다. 쓸 수 없는 action 이면 cards 로 본다(카드가 없으면 호출부가 못 찾음 안내를 낸다)."""
    raw_cards = answer.get("cards")
    cards = (
        order_by_picks(raw_cards, answer.get("picks"))
        if isinstance(raw_cards, list)
        else []
    )
    reply = _text(answer.get("reply"))
    action = answer.get("action")
    if action == "search_again" and may_search:
        query = _text(answer.get("query"))
        if query:
            return Decision(
                "search_again",
                [],
                None,
                missed=_text(answer.get("missed")),
                query=query[:MAX_QUERY_CHARS],
            )
    if action == "ask" and may_ask and not cards:
        question = _text(answer.get("question"))
        if question:
            return Decision("ask", [], reply, question=question)
    return Decision("cards", cards, reply)


def order_by_picks(raw_cards: list, picks: object) -> list:
    """카드를 picks 차례로 세운다. 화면 차례는 말풍선을 쓰기 전에 고른 picks 다(카드 생성 호출 #645·판단 호출 같음).

    picks 가 정수 목록이 아니면 cards 차례를 그대로 쓴다. picks 에 없는 카드는 picks 의 카드 뒤에 둔다 —
    모델이 picks 를 빠뜨렸다고 카드를 버리면 카드가 0장이 될 수 있다. 카드가 없는 pick 은 건너뛴다.
    """
    if not isinstance(picks, list):
        return raw_cards
    order = {}
    for book_id in picks:
        if isinstance(book_id, int) and not isinstance(book_id, bool):
            order.setdefault(book_id, len(order))
    if not order:
        return raw_cards

    def rank(item: object) -> int:
        book_id = item.get("book_id") if isinstance(item, dict) else None
        return (
            order.get(book_id, len(order)) if isinstance(book_id, int) else len(order)
        )

    return sorted(raw_cards, key=rank)


_ACTION = re.compile(r'"action"\s*:\s*"(\w+)"')


def streams_reply(text: str) -> bool | None:
    """모델이 지금까지 쓴 글로 보아 말풍선을 먼저 보내도 되나. 아직 action 을 안 썼으면 None.

    한 번 더 찾을 때는 이 호출의 말풍선이 쓰이지 않는다.
    """
    match = _ACTION.search(text)
    if match is None:
        return None
    return match.group(1) != "search_again"
