"""③ 검색 문장에서 사용자가 말하지 않은 분위기 낱말을 뺀다(#543).

1단계(조건 정리)가 사용자가 하지 않은 분위기 말을 검색 문장(semantic)에 지어 넣는다. "에세이 추천해줘"가 "가벼운
에세이"가 되고, 말풍선은 그 문장을 보고 "가벼운 에세이를 찾으신다면"이라고 답한다. 사용자에게는 챗봇이 하지 않은 말을
했다고 우기는 것으로 보이고, 검색도 가벼운 책 쪽으로 쏠린다. "요즘 회사 일이 너무 힘들어"(대화 평가 10번, 원한 것은
번아웃 회복 책)가 "힘들어도 가벼운 분위기의 책"이 되는 식이다.

지어 넣는 낱말은 1단계 프롬프트의 예시 문구("가벼운 걸로", "무겁지 않은", "더 재밌는 걸로", "잔잔한")와 거의 같다.
카드 프롬프트에서 예시 낱말을 베꼈던 #410과 같은 모양이다. 프롬프트에 "메시지에 없는 분위기를 덧붙이지 마라"가 이미
있는데도 나왔고, 예시를 바꾸면 무엇이 달라지는지는 평가를 다시 돌려야 안다. 그래서 먼저 코드로 막는다.

정해 둔 분위기 낱말 묶음만 본다. 검색 문장에 그 낱말이 있는데 근거가 없으면 뺀다. 근거는 같은 뜻의 말도 받는다("덜
무거운 걸로"면 "가벼운"을 남긴다). 근거로 보는 글은 넷이다.
- 이번 말과 최근 대화의 사용자 줄. 질문 아래 보기를 눌러 보낸 말도 사용자 말로 들어온다.
- 요청에 실려 온 앞 검색 문장. 그 낱말은 들어온 턴에 이미 이 검사를 거쳤다. 최근 대화는 20턴까지만 오고 안 싣는
  호출자도 있어서, 앞 문장을 근거로 보지 않으면 사용자가 오래전에 말한 분위기를 지운다.
- 카드를 가리킨 말("2번 같은 거")이면 가리킨 책의 소개. 1단계가 그 소개를 보고 검색 문장을 다시 쓴다.
챗봇 말은 근거로 보지 않는다. 지어낸 낱말을 말풍선이 되읊으므로, 챗봇 말을 근거로 보면 한 번 지어낸 낱말이 다음 턴에도
남는다.

낱말에 딸린 말("분위기의", "느낌의", "걸로", 앞의 "너무·좀·더")도 같이 뺀다. 빼고 나서 주제 말이 하나도 남지 않으면
("더 재밌는 걸로 추천해줘") 손대지 않는다. 비우면 대화 한가운데서 "어떤 분야가 좋으세요?"로 처음부터 되묻게 되는데,
그보다는 고치기 전처럼 찾는 쪽이 낫다.

10/21 평가 원본(gpt-4o-mini, 대화 평가·점검 대화의 검색 문장 612개)에 대어 보면 27%에서 낱말을 뺀다. 많이 빼는 낱말은
"가벼운" 90번, "재미있는·재밌는" 24번, "흥미로운" 11번, "무겁지 않은" 10번이다. 운영 모델(4o)은 덜 지어낼 수 있다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.chat.shown import ShownBook


@dataclass(frozen=True)
class _Mood:
    # 검색 문장 낱말이 이것으로 시작하면 이 묶음이다
    heads: tuple[str, ...]
    # 사용자 말에 이 중 하나가 있으면 말한 것으로 본다. 띄어쓰기를 지운 글끼리 견준다
    support: tuple[str, ...]


# 무거운 쪽을 싫다고 한 말("2번은 좀 비극적이야", "덜 슬프게")도 가벼운 쪽의 근거로 본다
_LIGHT = (
    "가벼", "가볍", "무겁지않", "덜무거", "안무거", "부담없", "부담스럽지않",
    "밝", "비극", "슬프지않", "덜슬프", "우울하지않",
)  # fmt: skip
_FUN = (
    "재미", "재밌", "흥미", "웃기", "웃긴", "유쾌", "유머", "코믹", "즐거", "즐겁", "신나", "지루하지않",
)  # fmt: skip
_MOODS = (
    _Mood(("가벼", "가볍"), _LIGHT),
    _Mood(("재미", "재밌"), _FUN),
    _Mood(("흥미",), _FUN + ("몰입",)),
    _Mood(("따뜻", "따듯"), ("따뜻", "따듯", "훈훈", "포근", "위로", "힐링")),
    _Mood(
        ("편안", "편하게", "편한"), ("편안", "편하", "편한", "힐링", "휴식") + _LIGHT
    ),
    _Mood(("잔잔",), ("잔잔", "차분", "조용")),
)
# "무겁지 않은"처럼 두 낱말로 된 분위기 말. 이 낱말 다음 낱말이 "않"으로 시작하면 둘을 한 말로 본다
# "무거"로 견주면 "아무거나"에 걸린다
_NOT_HEAVY = _Mood(
    ("무겁지", "부담스럽지"), _LIGHT + ("무겁", "무거운", "무거워", "무거우", "부담")
)

# 분위기 낱말 바로 뒤에 붙어 그 말에 딸린 말("가벼운 분위기의 책", "무겁지 않은 걸로")
_TRAILING = frozenset(
    {
        "분위기", "분위기의", "분위기로", "분위기를", "분위기가", "느낌", "느낌의", "느낌으로", "느낌을", "느낌이",
        "걸로", "거로", "것으로", "쪽으로", "편으로",
    }
)  # fmt: skip
# 분위기 낱말 바로 앞에서 그 말의 정도를 정하는 말("조금 더 가벼운")
_DEGREE = frozenset(
    {"너무", "좀", "조금", "더", "덜", "아주", "약간", "살짝", "좀더", "조금더"}
)
# 이것만 남으면 찾을 주제가 없다
_FILLER = frozenset(
    {
        "책",
        "책들",
        "도서",
        "읽을",
        "읽기",
        "볼",
        "만한",
        "좋은",
        "수",
        "있는",
        "것",
        "거",
    }
)
# 요청하는 말의 끝. app/routers/chat.py 의 _REQUEST_WORD 와 같은 꼴에 "필요해"를 더했다
_REQUEST = re.compile(
    r"(줘|주세요|줄래|있어요?|있나요|있을까요?|싶어요?|필요해요?)[?!.~]*$"
)

_PUNCTUATION = ".,!?~\"'()[]“”‘’"
_TRAILING_PARTICLE = re.compile(r"(을|를|은|는|이|가|으로|로|의|도)$")
_SPACE = re.compile(r"\s+")
# 카드를 가리키는 말. 번호를 말했으면 그 책만, 지시어면 보여 준 책 전부를 근거로 본다
_CARD_NUMBER = re.compile(r"(\d+)\s*번")
_CARD_POINTER = re.compile(r"이거|그거|저거|이 책|그 책|저 책|[첫두세네]\s?번째")


class _Turn(Protocol):
    role: str
    text: str


def evidence(
    message: str,
    recent_turns: Sequence[_Turn],
    previous_semantic: str | None,
    shown_books: Sequence[ShownBook],
) -> str:
    """근거로 볼 글. 이번 말, 최근 대화의 사용자 줄, 앞 검색 문장, 이번 말이 가리킨 카드의 책 소개를 잇는다."""
    parts = [message, *(turn.text for turn in recent_turns if turn.role == "user")]
    parts.append(previous_semantic or "")
    ranks = {int(n) for n in _CARD_NUMBER.findall(message)}
    pointed = [
        book
        for book in shown_books
        if book.rank in ranks or (not ranks and _CARD_POINTER.search(message))
    ]
    parts.extend(book.description or "" for book in pointed)
    return "\n".join(parts)


def strip(semantic: str | None, grounds: str) -> tuple[str | None, int]:
    """(고친 검색 문장, 뺀 분위기 말 수). 뺄 것이 없거나 빼면 주제가 안 남으면 받은 문장을 그대로 돌려준다."""
    if not semantic:
        return semantic, 0
    squashed = _SPACE.sub("", grounds)
    words = semantic.split()
    drop: set[int] = set()
    removed = 0
    for index, word in enumerate(words):
        if index in drop:
            continue
        span = _invented_span(words, index, squashed)
        if not span:
            continue
        removed += 1
        start, end = span
        while start > 0 and words[start - 1].strip(_PUNCTUATION) in _DEGREE:
            start -= 1
        while end < len(words) and words[end].strip(_PUNCTUATION) in _TRAILING:
            end += 1
        drop.update(range(start, end))
    if not removed:
        return semantic, 0
    kept = [word for index, word in enumerate(words) if index not in drop]
    if not any(_is_topic(word) for word in kept):
        return semantic, 0
    # "추천해 줘, 너무 무겁지 않은 걸로"처럼 뒤를 빼면 쉼표가 끝에 남는다
    return " ".join(kept).strip(" ,"), removed


def _invented_span(
    words: list[str], index: int, squashed: str
) -> tuple[int, int] | None:
    """index 낱말이 사용자가 말하지 않은 분위기 말이면 그 말이 차지하는 [시작, 끝). 아니면 None."""
    word = words[index].strip(_PUNCTUATION)
    following = words[index + 1].strip(_PUNCTUATION) if index + 1 < len(words) else ""
    if word.startswith(_NOT_HEAVY.heads) and following.startswith("않"):
        return (index, index + 2) if _unsupported(_NOT_HEAVY, squashed) else None
    for mood in _MOODS:
        if word.startswith(mood.heads):
            return (index, index + 1) if _unsupported(mood, squashed) else None
    return None


def _unsupported(mood: _Mood, squashed: str) -> bool:
    return not any(stem in squashed for stem in mood.support)


def _is_topic(word: str) -> bool:
    bare = word.strip(_PUNCTUATION)
    if not bare or bare.startswith("추천") or _REQUEST.search(bare):
        return False
    return bare not in _FILLER and _TRAILING_PARTICLE.sub("", bare) not in _FILLER
