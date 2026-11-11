"""③ 판단 호출 — 세 갈래에서 모은 한 목록을 보고 카드·질문·한 번 더 찾기 중 하나를 고른다(CHAT_AGENT).

지금의 카드 생성 호출(CARD_PROMPT)을 대신한다. 이 호출은 턴마다 한 번이고, 한 번 더 찾을 때만 두 번이다. 그 앞에 1단계와,
1단계와 나란히 가는 떠올린 제목 호출(app/chat/recall.py)이 있다.

- 카드: 사용자 말에 맞는 책만 최대 3장. 셋을 채우려고 맞지 않는 책을 넣지 않는다. 고른 책에 함께 읽은 책(옆으로
  넓히기)·마니아 추천(같은 분야 더 보기) 줄이 있으면 그 두 갈래를 답 보기로 제안한다(proposal_options). 코드가 쓴다 —
  모델에게 맡기니 4o 는 제안 없이 한 문장으로 끝냈다(11/10 점검). 제안한 책을 카드로 내지 않는다 — 카드에는 "제안"
  표시가 없고, 넓히기 책은 매칭 % 가 낮아 엉뚱한 추천처럼 보인다(11/10 결정). 처음엔 말풍선 끝 문장으로 붙였는데
  줄글이라 읽기 어려워(11/10 사용자) followup 의 보기 줄로 옮겼다(#652). 누르면 그 갈래의 카드가 다음 턴에 나온다
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

from app.chat import options, reasons
from app.chat.lanes import display_title
from app.core import voice

Action = Literal["cards", "ask", "search_again"]

# 목록에 넣는 소개글 길이. 후보가 30권 안팎이라 소개 전체를 넣으면 프롬프트가 너무 길어진다. 책소개 중앙값은 148자다
LISTING_DESCRIPTION_CHARS = 400
MAX_QUERY_CHARS = 50
MAX_REASON_SHORT_CHARS = 80
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
# 숫자로 끝나는 제목은 읽는 소리로 받침을 본다(app/chat/options.has_object_final). 『AI 2041』(이천사십일)에 "를"을
# 붙였다(11/10 실험 2)


def _object_particle(word: str) -> str:
    """받침이 있으면 "을", 없으면 "를". 숫자는 읽는 소리(영·일·삼·육·칠·팔은 받침)로 보고, 그 밖의 글자는 "를"."""
    return "을" if options.has_object_final(word) else "를"


def _clip(title: str, room: int) -> str:
    return title if len(title) <= room else title[: max(room - 1, 1)].rstrip() + "…"


def graph_source(book: dict) -> tuple[str, str] | None:
    """책이 기준 책의 함께 읽은 책·마니아 추천에서 왔으면 (갈래, 기준 책 제목). 아니면 None."""
    return next(
        (
            (lane, anchor)
            for lane, anchor in book.get("sources") or []
            if lane in ("co", "mania")
        ),
        None,
    )


# 말풍선 끝에 붙이는 출처 문장의 기준 책 제목 상한. 말풍선이 120자 안팎이라 문장 하나가 너무 길지 않게 한다
_NOTE_ANCHOR_CHARS = 30
_NOTE_TITLE_CHARS = 20


def _topic_particle(word: str) -> str:
    return "은" if options.has_object_final(word) else "는"


def source_note(picked: list[dict]) -> str | None:
    """카드가 함께 읽은 책·마니아 추천에서 왔으면 어떻게 골랐는지 말풍선 끝에 붙일 한 문장. 아니면 None.

    전에는 카드 한 줄 이유를 "『X』의 정보나루 마니아 추천 목록에 있는 책이에요." 틀로 썼다. 기준 책 제목만 들어가는
    틀이라 같은 기준 책에서 온 카드 세 장이 같은 이유로 도배됐다(11/11 사용자). 그래서 출처는 말풍선에서 한 번 말하고,
    카드 이유는 책마다 다른 것(소개글, 없으면 저자·연도·분류)을 쓴다.

    카드 순서에서 처음 나오는 기준 책 갈래 하나만 말한다. 카드가 모두 그 갈래면 "…책으로 골랐어요", 일부면 그 책
    제목을 짚는다. 목록으로는 몇 명이 읽었는지·무엇으로 골랐는지 모르니(위 모듈 설명) 누가 읽었는지와 출처만 말한다.
    마니아 추천은 정보나루 이름("마니아를 위한 추천도서")대로 그 책을 좋아하는 독자에게 권하는 목록이라고 말한다.
    출처(도서관 정보나루)는 카드 근거 칸(relation_basis)이 적으니 이 문장에는 쓰지 않는다.
    """
    sources = [(book, graph_source(book)) for book in picked]
    first = next((s for _, s in sources if s is not None), None)
    if first is None:
        return None
    kind, anchor = first
    anchor = _clip(display_title(anchor), _NOTE_ANCHOR_CHARS)
    who = (
        f"『{anchor}』{_object_particle(anchor)} 읽은 분들이 함께 읽은"
        if kind == "co"
        else f"『{anchor}』{_object_particle(anchor)} 좋아하는 분께 권하는"
    )
    titles = [
        _clip(display_title(book["title"]), _NOTE_TITLE_CHARS)
        for book, s in sources
        if s == first
    ]
    if len(titles) == len(picked):
        return f"{who} 책으로 골랐어요."
    named = "·".join(f"『{t}』" for t in titles)
    return f"{named}{_topic_particle(titles[-1])} {who} 책이에요."


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


def proposal_options(picked: list[dict]) -> list[str]:
    """고른 책의 이웃 줄에서 옆으로 넓히기(함께 읽은 책)와 같은 분야 더 보기(마니아 추천)를 답 보기로 하나씩 낸다.

    앞 카드부터 카드에 없는 이웃이 있는 책을 기준 책으로 짚는다. 보기는 누르면 그대로 보낼 말이라 다음 턴의 해석이
    알아듣게 쓴다 — 기준 책 『제목』(카드 제목과 맞춰 카드를 짚는다)과 "함께 읽"(함께 읽은 책 갈래)·"비슷한 책
    더"(마니아 추천 갈래) 낱말(app/chat/lanes.py fill_plan). 제목은 자르지 않는다 — 잘린 제목은 카드와 맞지 않는다.
    목록으로는 몇 명이 읽었는지·무엇으로 골랐는지 모르니 보기도 방향만 말한다. 줄이 없으면 [].
    """
    on_cards = {b["title"] for b in picked} | {
        display_title(b["title"]) for b in picked
    }

    def anchor(kind: str, taken: set[str]) -> tuple[str, list[str]] | None:
        for book in picked:
            neighbors = (book.get("neighbors") or {}).get(kind, [])
            if any(title not in taken for title in neighbors):
                return display_title(book["title"]), neighbors
        return None

    found: list[str] = []
    co = anchor("co", on_cards)
    if co:
        found.append(f"『{co[0]}』 함께 읽은 책 보여줘")
    # 마니아 목록이 함께 읽은 책 보기로 나올 책뿐이면 두 보기가 같은 책을 낸다(11/10 실험 2·3, 『더 커밍 웨이브』)
    mania_found = anchor("mania", on_cards | set(co[1] if co else []))
    if mania_found:
        mania = mania_found[0]
        with_particle = "과" if options.has_object_final(mania) else "와"
        found.append(f"『{mania}』{with_particle} 비슷한 책 더 보여줘")
    return found


def overclaims(text: str) -> bool:
    """함께 대출·마니아 목록으로는 알 수 없는 것을 말하는가."""
    return bool(_OVERCLAIM.search(text))


# 판단 호출 목록의 출처 꼬리표("『X』 마니아 추천", "『X』 함께 읽은 책")를 한 줄 이유에 옮겨 적은 말. 같은 기준 책에서
# 온 카드마다 같은 말이 되어 이유가 책을 가르지 못한다(11/11 사용자). 출처는 근거 칸이 따로 말한다
_SOURCE_TAG = re.compile(r"마니아|함께\s?(읽|대출|빌)|정보나루")


def names_source(text: str) -> bool:
    """한 줄 이유가 책 내용 대신 출처 꼬리표를 말하는가."""
    return bool(_SOURCE_TAG.search(text))


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
    "묻는 한 문장을 쓴다. options 에는 사용자가 그 두 방향 중 하나를 고를 때 그대로 보낼 짧은 말을 방향마다 하나씩 "
    "2개 쓴다(각 20자 이내, 예: 그 방향의 책을 보여 달라는 말). reply 는 이번 메시지에 짧게 반응하는 한 문장이다. "
    "막연한 요청이어도 맞는 책이 있으면 묻지 말고 cards 를 고른다."
)
_ASK_BLOCKED = "이번에는 고르지 마라."
_SEARCH_ALLOWED = (
    "사용자 말의 중요한 조건 하나를 목록의 어느 책도 맞히지 못할 때만 고른다. missed 에 놓친 조건을, query 에 그 "
    "조건을 담은 새 검색어(30자 이내, 책 제목 말고 주제 낱말)를 쓴다. 맞는 책이 한 권이라도 있으면 cards 를 고른다."
)
_SEARCH_BLOCKED = "이번에는 고르지 마라. 이미 한 번 다시 찾았다."
_PROPOSE = (
    "고른 책 아래에 함께 읽은 책·마니아 추천 줄이 있으면 말풍선에서 다른 책을 제안하지 마라. 다음에 읽을 방향은 "
    "화면에 따로 보기로 붙는다. 줄이 없으면 사용자가 말한 것에 가장 맞는 한 권을 "
    "『제목』으로 짚고 그 까닭을 책 소개에 있는 것으로 한마디 하라. "
)
_PROPOSE_NO_QUESTION = "마지막 문장은 묻는 말이 아니라 권하는 말로 끝내라. "

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
    "반드시 한국어로만 답하라. book_id 는 목록에 적힌 값을 그대로 써라. 순서 번호가 아니다. cards 에 적은 차례가 "
    "화면의 1번, 2번, 3번이다.\n"
    + reasons.SHORT_RULE
    + "reason_long 은 사용자가 찾은 것과 이 책 소개의 어떤 점이 맞는지 잇는 "
    "2-4문장(최대 300자)이다. 책 소개 문장을 옮기거나 줄여 쓰지 마라. 근거는 그 책의 소개에 있는 것만 쓴다. "
    "match_basis 는 reason_short·reason_long 과 같은 근거를 label(예: 분위기, 장르, 가격)과 detail(그 근거의 구체적 "
    "내용) 짝으로 1~3개 적어라 — 새로운 근거를 지어내지 말고 두 이유 문장에 이미 쓴 근거만 옮겨 적어라.\n"
    "reply 는 말풍선에 보일 1-2문장이다(최대 120자). 먼저 이번 메시지에 반응하라. 요청을 되풀이하며 시작하지 "
    "마라. 카드 제목을 늘어놓지 말고 고른 책들이 어떤 결인지 한마디로 말하라. {propose}\n"
    '"비슷한 책"(함께 읽은 책), "더 깊은 책"(마니아 추천 책), "많이 읽은", "다음에 읽은" 같은 말은 쓰지 마라.\n'
    "책을 어느 출처(함께 읽은 책·마니아 추천)에서 데려왔는지는 화면이 말풍선 끝에 따로 붙인다. reply 와 reason_short "
    "에 출처를 쓰지 말고, reason_short 는 그 책 소개에서 이유를 찾아라.\n"
    + voice.RULES
    + "다음 JSON 형식으로만 답하라. action 을 맨 앞에 쓴다. 고른 action 에 쓰지 않는 칸은 넣지 마라: "
    '{{"action": "cards 또는 ask 또는 search_again", '
    '"cards": [{{"book_id": 정수, "reason_short": "다른 책과 갈리는 점(30자 이내)", "reason_long": "긴 이유", '
    '"match_basis": [{{"label": "근거 이름", "detail": "그 근거의 구체적 내용"}}]}}], '
    '"reply": "말풍선", "question": "ask 일 때 묻는 말", "options": ["ask 일 때 답 보기"], '
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
    # ask 일 때 질문 아래 답 보기(#652). 못 받았으면 비어 있고 질문만 한다
    options: tuple[str, ...] = ()
    missed: str | None = None
    query: str | None = None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse(answer: dict, *, may_ask: bool, may_search: bool) -> Decision:
    """판단 호출의 답을 읽는다. 쓸 수 없는 action 이면 cards 로 본다(카드가 없으면 호출부가 못 찾음 안내를 낸다)."""
    raw_cards = answer.get("cards")
    cards = raw_cards if isinstance(raw_cards, list) else []
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
            return Decision(
                "ask",
                [],
                reply,
                question=question,
                options=_ask_options(answer.get("options")),
            )
    return Decision("cards", cards, reply)


# 판단 호출이 물을 때의 답 보기 수와 길이. 질문이 두 방향을 묻는다. 길면 자르지 않고 버린다 — 잘린 말은 보낼 수 없다
MAX_ASK_OPTIONS = 2
MAX_ASK_OPTION_CHARS = 20


def _ask_options(raw: Any) -> tuple[str, ...]:
    """답 보기. 글자가 아니거나 길거나 겹친 것은 버린다. 둘이 안 되면 보기 없이 묻는다(한 방향만으로는 고르는 게 아니다)."""
    if not isinstance(raw, list):
        return ()
    found = [
        text
        for text in dict.fromkeys(_text(item) for item in raw if isinstance(item, str))
        if text and len(text) <= MAX_ASK_OPTION_CHARS
    ][:MAX_ASK_OPTIONS]
    return tuple(found) if len(found) == MAX_ASK_OPTIONS else ()


_ACTION = re.compile(r'"action"\s*:\s*"(\w+)"')


def streams_reply(text: str) -> bool | None:
    """모델이 지금까지 쓴 글로 보아 말풍선을 먼저 보내도 되나. 아직 action 을 안 썼으면 None.

    한 번 더 찾을 때는 이 호출의 말풍선이 쓰이지 않는다.
    """
    match = _ACTION.search(text)
    if match is None:
        return None
    return match.group(1) != "search_again"
