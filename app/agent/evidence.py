"""⑦ 사용자의 말에 그 책을 가리키는 근거가 있는지 본다(#387).

"그거 담아줘"처럼 어느 책인지 알 수 없는 말에 LLM 이 카드 1번을 골라 담는 것(짐작해서 실행)을 막으려는
검사다. 프롬프트에 "짐작하지 마"를 쓰는 것으로는 막히지 않았다(7단계 측정). 그래서 LLM 이 고른 책을 사용자의
말과 코드가 맞춰 보고, 근거가 없으면 장바구니를 바꾸는 도구를 실행하지 않는다.

LLM 이 고른 책(card n / cart n)마다 아래 중 하나가 사용자의 말에 있으면 근거가 있다고 본다.
- 그 책의 번호: "1번", "두 번째", "맨 위"(장바구니와 카드의 1번), "맨 아래"(마지막 번호)
- 그 책만 가리키는 제목 낱말: 같은 목록의 다른 책 제목에 없는 낱말
- 목록 전체를 가리키는 말: "전부", "모두", "다 빼줘", 목록 크기와 같은 "둘 다". 수를 말했는데 목록보다 적으면
  ("그 두 권 다 빼줘") 전체가 아니라 일부라서 근거가 아니다
- 목록에 책이 하나뿐이라 가리킬 수 있는 책이 그것뿐인 경우
지금 보고 있는 책(focused)은 화면 상태가 지목한 것이라 짐작이 아니므로 검사하지 않는다.

완벽한 말 풀이가 아니다. 근거를 못 찾으면 사용자에게 되묻는 쪽으로 틀리도록 만들었다(잘못 막으면 한 번
더 묻고, 잘못 통과시키면 장바구니가 엉뚱하게 바뀐다).
"""

import re

from app.agent.errors import RefError, ToolErrorCode
from app.agent.reference import BookRef, TurnContext

# 말로 쓴 순서. "두 번째", "첫 번째". "두 번 담아줘"(횟수)와 헷갈리지 않게 "번째"/"째"가 있어야 순서로 본다
_NATIVE = {
    "첫": 1,
    "한": 1,
    "두": 2,
    "세": 3,
    "석": 3,
    "네": 4,
    "넉": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}
_NATIVE_ORDINAL = re.compile(
    r"(첫|한|두|세|석|네|넉|다섯|여섯|일곱|여덟|아홉|열)\s*(?:번째|째)"
)
_SHORT_ORDINAL = {"첫째": 1, "둘째": 2, "셋째": 3, "넷째": 4}
# 숫자는 "3번"만으로 순서로 본다(카드 3번). "3권", "3개"는 수량이라 세지 않는다
_DIGIT_ORDINAL = re.compile(r"(\d+)\s*(?:번째|번|째)")

_TOP = re.compile(
    r"맨\s*위|제일\s*위|가장\s*위|맨\s*앞|가장\s*최근|최근에\s*담은|방금\s*담은"
)
_BOTTOM = re.compile(
    r"맨\s*아래|맨\s*밑|제일\s*아래|가장\s*아래|맨\s*뒤|처음\s*담은|맨\s*처음"
)
# 목록 전체를 말하는 낱말. "다 빼줘"는 수를 말하지 않았을 때만 전체로 본다("그 두 권 다 빼줘"는 일부다)
_ALL_WORDS = re.compile(r"전부|모두|모든")
_BARE_ALL = re.compile(r"다\s*(?:담|빼|지워|넣|삭제)")
_COUNT_ALL = re.compile(r"([둘셋넷])\s*다")
_COUNT_WORDS = {"둘": 2, "셋": 3, "넷": 4}
# 말한 수량("두 권", "2권", "둘"). 전체가 아니라 그 수만큼을 말한 것일 수 있다
_DIGIT_COUNT = re.compile(r"(\d+)\s*권")
_NATIVE_COUNT = re.compile(r"(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*권")
_BARE_COUNT = re.compile(r"([둘셋넷])(?![가-힣])")

# 제목에서 뽑은 낱말 가운데 위치를 말하는 낱말은 제목의 근거로 세지 않는다("처음 거 빼줘")
_POSITION_WORDS = frozenset({"처음", "마지막", "최근", "방금"})
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


def ordinals(message: str) -> set[int]:
    """말에 나온 순서(번호)들."""
    found = {int(m.group(1)) for m in _DIGIT_ORDINAL.finditer(message)}
    found |= {_NATIVE[m.group(1)] for m in _NATIVE_ORDINAL.finditer(message)}
    found |= {n for word, n in _SHORT_ORDINAL.items() if word in message}
    return found


def _flat(text: str) -> str:
    """띄어쓰기를 없애고 소문자로. "파이썬 책"과 "파이썬책"을 같게 본다."""
    return re.sub(r"\s+", "", text).lower()


def title_tokens(title: str) -> set[str]:
    """제목에서 낱말을 뽑는다. 한 글자와 위치를 말하는 낱말은 뺀다. 비교하기 쉽게 소문자로 둔다."""
    return {
        word.lower()
        for word in _TOKEN.findall(title)
        if len(word) >= 2 and word not in _POSITION_WORDS
    }


def _list_titles(ctx: TurnContext, ref: BookRef) -> dict[str, str]:
    """가리킨 목록에서 제목을 아는 책들(ISBN → 제목)."""
    if ref.source == "cart":
        return {line.isbn: line.title for line in ctx.cart_snapshot or []}
    return {
        card.isbn: ctx.titles[card.isbn]
        for card in ctx.cards
        if card.isbn in ctx.titles
    }


def _list_size(ctx: TurnContext, ref: BookRef) -> int:
    if ref.source == "cart":
        return len(ctx.cart_snapshot or [])
    return len({card.isbn for card in ctx.cards})


def _stated_counts(message: str) -> set[int]:
    """말에 나온 수량들."""
    counts = {int(n) for n in _DIGIT_COUNT.findall(message)}
    counts |= {_NATIVE[w] for w in _NATIVE_COUNT.findall(message)}
    counts |= {_COUNT_WORDS[w] for w in _BARE_COUNT.findall(message)}
    return counts


def _says_all(message: str, size: int) -> bool:
    """목록 전체를 말했는가. 수를 말했는데 목록 크기와 다르면 일부를 말한 것이라 전체가 아니다."""
    if _ALL_WORDS.search(message):
        return True
    counted = _COUNT_ALL.search(message)
    if counted:
        return _COUNT_WORDS[counted.group(1)] == size
    if _BARE_ALL.search(message):
        counts = _stated_counts(message)
        return not counts or size in counts
    return False


def has_evidence(ctx: TurnContext, ref: BookRef, isbn: str) -> bool:
    """사용자의 말에 이 책을 가리키는 근거가 있는가. 말을 모르면(message 가 None) 검사하지 않는다."""
    message = ctx.message
    if message is None or ref.source == "focused":
        return True
    # 가리킬 수 있는 책이 하나뿐이면 짐작이 아니다
    size = _list_size(ctx, ref)
    if size <= 1:
        return True
    n = ref.n
    if n in ordinals(message):
        return True
    if (n == 1 and _TOP.search(message)) or (n == size and _BOTTOM.search(message)):
        return True
    if _says_all(message, size):
        return True

    titles = _list_titles(ctx, ref)
    title = titles.get(isbn)
    if title is None:
        return False
    # 다른 책의 제목 글자 안에도 있는 낱말은 이 책만 가리키지 못한다("설계"는 두 제목에 다 들어 있다)
    others = [_flat(t) for other, t in titles.items() if other != isbn]
    spoken = _flat(message)
    return any(
        token in spoken and not any(token in other for other in others)
        for token in title_tokens(title)
    )


def natural_refs(ctx: TurnContext, isbn: str) -> list[BookRef]:
    """이 책을 LLM 이 가리킬 때 쓸 수 있는 말들(카드 번호, 장바구니 번호, 지금 보는 책)."""
    refs = [BookRef(source="card", n=card.rank) for card in ctx.cards if card.isbn == isbn]
    refs += [
        BookRef(source="cart", n=position)
        for position, line in enumerate(ctx.cart_snapshot or [], start=1)
        if line.isbn == isbn
    ]
    if ctx.focused_isbn == isbn:
        refs.append(BookRef(source="focused"))
    return refs


def stated(ctx: TurnContext, isbn: str) -> bool:
    """사용자가 이 책을 가리켰는가. 책을 조회했다고 사용자가 그 책을 가리킨 것은 아니다.

    "그거 담아줘"를 되묻으려고 카드 세 장의 제목을 조회해도 세 권이 "정해진 책"은 아니다
    (ResolvedReference: 하나로 정해지지 않으면 isbns 는 빈 목록).
    """
    return any(has_evidence(ctx, ref, isbn) for ref in natural_refs(ctx, isbn))


def require(ctx: TurnContext, ref: BookRef, isbn: str) -> None:
    """근거가 없으면 RefError. 쓰기 도구가 BE 를 부르기 전에 쓴다."""
    if not has_evidence(ctx, ref, isbn):
        raise RefError(ToolErrorCode.REFERENCE_NOT_STATED)
