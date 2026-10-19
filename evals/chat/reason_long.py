"""③ 평가의 긴 이유 지표(#503) — grade.py 가 공통 지표에 붙인다. DB·LLM 없이 돈다.

긴 이유(reason_long)는 도서 상세 페이지에 책 소개와 같이 보인다. 책 소개를 옮기면 같은 글이 두 번 보이고,
명세(AI-1 ③, 부록 예시 3)의 긴 이유는 사용자가 찾은 것과 책의 근거를 잇는 글이다. 그래서 넷을 센다.

- 책 소개를 옮김: 띄어쓰기·기호를 지우고 긴 이유의 이어진 8글자 묶음 중 책 소개에도 있는 것이 50% 이상
  (app/chat/copying.py — 카드 생성이 옮긴 글을 버릴 때와 같은 잣대)
- 요청 낱말을 짚음: 조건 문장(spec.semantic)의 낱말이 긴 이유에 있음
- 책 소개에 없는 요청 낱말을 짚음: 위 낱말 중 책 소개에 없는 것(사용자의 상황)만. 책 소개를 옮기다 우연히
  겹친 것을 빼고 본다
- 말하지 않은 분위기를 붙임: 사용자가 말하지 않았고 책 소개에도 없는 분위기 낱말이 긴 이유에 있음. 1단계가
  조건 문장에 지어 넣은 분위기를 긴 이유가 "…를 찾는 분께 어울린다"고 되받으면 여기 걸린다

모두 어림이라 통과/실패에는 넣지 않고 전후를 견주는 데만 쓴다. 책 소개가 있는 책의 카드만 본다(책 소개가 없는
책의 긴 이유는 #407 로 늘 비어 있다).
"""

import re
import statistics
from collections.abc import Iterable
from typing import Any

from app.chat.copying import COPIED_SHARE_MIN, copied_share

# 조건 문장에서 낱말을 뽑을 때 끝에서 떼는 조사. 떼고 나서 앞 두 글자로 견준다 — 한국어는 활용으로 끝이 바뀐다
# ("퇴근길에"·"퇴근 후"). 어림이다. "가볍게"와 "가벼운"처럼 앞 두 글자가 바뀌는 활용은 놓치고, 흔한 말이 우연히
# 겹치면 짚은 것으로 센다.
_PARTICLES = (
    "으로", "에서", "에게", "까지", "부터", "처럼", "이나",
    "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만",
)  # fmt: skip
# 어느 요청에나 붙는 말. 이 말이 이유에 있다고 요청을 짚었다고 볼 수 없다(앞 두 글자로 적는다).
_NOT_REQUEST_WORDS = frozenset(
    {
        "추천", "관련", "읽을", "읽기", "읽고", "읽는", "읽히", "좋은", "좋고", "좋을", "만한",
        "대한", "위한", "있는", "있을", "하는", "되는", "도움", "내용", "주제", "분야", "다룬",
        "다루", "알려", "보여", "싶어", "싶은", "같은", "이런", "그런", "어떤", "정도",
    }
)  # fmt: skip


def request_words(semantic: str | None) -> list[str]:
    """조건 문장에서 요청의 핵심어를 뽑는다(앞 두 글자, 영문은 낱말 통째로). 책·도서와 흔한 말은 뺀다."""
    words = []
    for token in re.findall(r"\w+", (semantic or "").lower()):
        if token.isascii():
            if len(token) >= 2:
                words.append(token)
            continue
        if token.startswith(("책", "도서")):
            continue
        for particle in _PARTICLES:
            if token.endswith(particle) and len(token) - len(particle) >= 2:
                token = token[: -len(particle)]
                break
        if len(token) >= 2 and token[:2] not in _NOT_REQUEST_WORDS:
            words.append(token[:2])
    return list(dict.fromkeys(words))


def _percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def metrics(
    turns: Iterable[tuple[dict, str]], books: dict, mood_words: Iterable[str]
) -> dict[str, Any]:
    """turns 는 LLM 을 부른 (턴, 그 턴까지 사용자가 한 말) 목록이다. mood_words 는 grade.MOOD_WORDS.

    원본에 책 소개가 없으면(run.py 가 책 소개를 저장하기 전의 원본) 재지 않는다 — `run.py --refill-books`로 채운다.
    """
    if not books or any("description" not in b for b in books.values()):
        return {"reason_long_measured": False}
    mood_words = tuple(mood_words)
    cards = missing = copied = unsaid_mood = 0
    asked = mentioned = own_asked = own_mentioned = 0
    shares: list[float] = []
    lengths: list[int] = []
    for turn, user_said in turns:
        words = request_words((turn.get("spec") or {}).get("semantic"))
        for card in turn["cards"]:
            book = books.get(str(card.get("book_id"))) or {}
            description = book.get("description") or ""
            if not description.strip():
                continue
            reason = card.get("reason_long")
            if not reason:
                missing += 1
                continue
            cards += 1
            lengths.append(len(reason))
            share = copied_share(reason, description)
            shares.append(share)
            copied += share >= COPIED_SHARE_MIN
            unsaid_mood += any(
                w in reason and w not in description and w not in user_said
                for w in mood_words
            )
            lowered, origin = reason.lower(), description.lower()
            if words:
                asked += 1
                mentioned += any(w in lowered for w in words)
            own = [w for w in words if w not in origin]
            if own:
                own_asked += 1
                own_mentioned += any(w in lowered for w in own)
    return {
        "reason_long_measured": True,
        "reason_long_cards": cards,
        "reason_long_missing": missing,
        "reason_long_copied": copied,
        "reason_long_copied_rate": _percent(copied, cards),
        "reason_long_copy_share_median": round(statistics.median(shares), 2)
        if shares
        else None,
        "reason_long_request_cards": asked,
        "reason_long_mentions_request": mentioned,
        "reason_long_mentions_request_rate": _percent(mentioned, asked),
        "reason_long_own_word_cards": own_asked,
        "reason_long_mentions_own_word": own_mentioned,
        "reason_long_mentions_own_word_rate": _percent(own_mentioned, own_asked),
        "reason_long_unsaid_mood": unsaid_mood,
        "reason_long_unsaid_mood_rate": _percent(unsaid_mood, cards),
        "reason_long_chars_median": statistics.median(lengths) if lengths else None,
        "reason_long_chars_max": max(lengths) if lengths else None,
    }


def render(c: dict) -> list[str]:
    """요약의 공통 지표(c)에서 긴 이유 줄을 만든다."""
    if not c.get("reason_long_measured"):
        return ["긴 이유: 원본에 책 소개가 없어 못 잼(run.py --refill-books 로 채운다)"]
    copied = f"{c['reason_long_copied']} ({c['reason_long_copied_rate']}%)"
    mentioned = (
        f"{c['reason_long_mentions_request']}/{c['reason_long_request_cards']}"
        f" ({c['reason_long_mentions_request_rate']}%)"
    )
    own = (
        f"{c['reason_long_mentions_own_word']}/{c['reason_long_own_word_cards']}"
        f" ({c['reason_long_mentions_own_word_rate']}%)"
    )
    return [
        f"긴 이유(책 소개가 있는 책의 카드, LLM 을 부른 턴) {c['reason_long_cards']}장",
        f"  책 소개를 옮김(8글자 묶음의 {COPIED_SHARE_MIN:.0%} 이상) {copied}"
        + f" · 옮긴 몫 중앙 {c['reason_long_copy_share_median']}",
        f"  요청 낱말을 짚음 {mentioned} · 책 소개에 없는 요청 낱말을 짚음 {own}",
        f"  말하지 않았고 책 소개에도 없는 분위기를 붙임 {c['reason_long_unsaid_mood']}"
        + f" ({c['reason_long_unsaid_mood_rate']}%)",
        f"  긴 이유가 빈 카드 {c['reason_long_missing']}"
        + f" · 길이 중앙 {c['reason_long_chars_median']}자"
        + f" · 가장 긴 것 {c['reason_long_chars_max']}자(명세 상한 300자)",
    ]


def compare_rows(before: dict, after: dict) -> list[tuple[str, Any, Any]]:
    """두 요약의 공통 지표를 (이름, 전, 후) 줄로."""
    b, a = before, after
    if not (b.get("reason_long_measured") and a.get("reason_long_measured")):
        return [("긴 이유 지표", "원본에 책 소개가 없어 못 잼", "")]
    return [
        (
            "긴 이유가 책 소개를 옮김",
            f"{b['reason_long_copied']}/{b['reason_long_cards']} ({b['reason_long_copied_rate']}%)",
            f"{a['reason_long_copied']}/{a['reason_long_cards']} ({a['reason_long_copied_rate']}%)",
        ),
        (
            "옮긴 몫 중앙",
            b["reason_long_copy_share_median"],
            a["reason_long_copy_share_median"],
        ),
        (
            "긴 이유가 요청 낱말을 짚음",
            f"{b['reason_long_mentions_request_rate']}%",
            f"{a['reason_long_mentions_request_rate']}%",
        ),
        (
            "  책 소개에 없는 요청 낱말을 짚음",
            f"{b['reason_long_mentions_own_word_rate']}%",
            f"{a['reason_long_mentions_own_word_rate']}%",
        ),
        (
            "긴 이유에 말하지 않은 분위기(책 소개에도 없음)",
            f"{b['reason_long_unsaid_mood_rate']}%",
            f"{a['reason_long_unsaid_mood_rate']}%",
        ),
        (
            "책 소개가 있는데 긴 이유가 빈 카드",
            b["reason_long_missing"],
            a["reason_long_missing"],
        ),
    ]
