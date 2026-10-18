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
- 최근 대화에서 이야기한 책이 그 한 권뿐인 경우(#418, 아래 _from_topic)
지금 보고 있는 책(focused)은 화면 상태가 지목한 것이라 짐작이 아니므로 검사하지 않는다. 최근 대화에서 이야기한
책이 한 권뿐인 것도 같은 이치로, 말투가 아니라 대화 상태가 정한 책이다.

완벽한 말 풀이가 아니다. 근거를 못 찾으면 사용자에게 되묻는 쪽으로 틀리도록 만들었다(잘못 막으면 한 번
더 묻고, 잘못 통과시키면 장바구니가 엉뚱하게 바뀐다).

그래서 이 규칙은 잘못 통과하는 구멍만 조인다. 말투를 더 받아 주는 방향(막혀서 되묻는 표현)으로는 늘리지
않는다. 위치 낱말 뒤에 한글이 붙은 표현("맨 위로 올려줘")처럼 정상인데 막히는 말은 한 번 되묻는 비용으로
받아들이고, 실제로 자주 막히는 말은 평가 사례로 모은 뒤에 다룬다(#398).
"""

import re

from app.agent.errors import RefError, ToolErrorCode
from app.agent.prompts import PROMPT_TURNS
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
# "열한 번째"의 "한"을 1번째로 읽지 않게, 한글 바로 뒤에 붙은 낱말은 순서로 세지 않는다
_NATIVE_ORDINAL = re.compile(
    r"(?<![가-힣])(첫|한|두|세|석|네|넉|다섯|여섯|일곱|여덟|아홉|열)\s*(?:번째|째)"
)
_SHORT_ORDINAL = {"첫째": 1, "둘째": 2, "셋째": 3, "넷째": 4}
# 숫자는 "3번"만으로 순서로 본다(카드 3번). "3권", "3개"는 수량이라 세지 않는다
_DIGIT_ORDINAL = re.compile(r"(\d+)\s*(?:번째|번|째)")

# 장바구니는 최근에 담은 책이 1번이라 "처음 담은 거"가 마지막 번호다. 카드는 보인 순서라 "맨 처음"이 1번이다
# 위치 낱말은 "맨/제일/가장" 뒤에 위치를 말하는 낱말이 오는 일반 규칙이다. 표현을 하나씩 늘리지 않는다(#397).
# 위치 낱말 뒤에 한글이 바로 붙으면 다른 낱말의 앞부분이라 위치로 세지 않는다("가장 위로가 되는", "제일 앞서가는",
# "가장 끝내주는", "가장 뒤늦게"). 이어 붙어도 위치로 읽는 것은 "에/에서/의/쪽"과 "거/것"뿐이다(#398)
_POSITION_END = r"(?:에서?|의|쪽)?(?:거|것)?(?![가-힣])"
_TOP = rf"(?:맨|제일|가장)\s*(?:위|앞){_POSITION_END}"
_BOTTOM = rf"(?:맨|제일|가장)\s*(?:아래|밑|뒤|끝){_POSITION_END}"
_CART_FIRST = re.compile(
    rf"{_TOP}|가장\s*최근|최근에\s*담은|방금\s*담은|방금\s*(?:거|것|책)"
)
_CART_LAST = re.compile(rf"{_BOTTOM}|처음\s*담은|맨\s*처음")
_CARD_FIRST = re.compile(rf"{_TOP}|맨\s*처음")
_CARD_LAST = re.compile(rf"{_BOTTOM}|마지막\s*(?:카드|거|것|책)")

# 평범한 말투 정규화(#397). 사람이 보면 분명한 말이 근거 없음으로 막히던 것을 줄인다.
# "이"는 "이번 거"(이번에 보인 것)와 겹쳐서 한글 숫자에서 뺀다.
_HANGUL_NUMBER = {
    "일": 1,
    "삼": 3,
    "사": 4,
    "오": 5,
    "육": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
    "십": 10,
}
# "일번이랑", "삼번이요"처럼 조사가 바로 붙어도 읽는다. 앞에 한글이 붙은 말("첫일번")은 읽지 않는다
_HANGUL_NUMBER_BEON = re.compile(r"(?<![가-힣])(일|삼|사|오|육|칠|팔|구|십)\s*번")
_JEL = re.compile(r"(?<![가-힣])젤(?=\s|$)")
_KKEO = re.compile(r"꺼(?![가-힣])")
# 목록 전체를 말하는 낱말. "다 빼줘"는 수를 말하지 않았을 때만 전체로 본다("그 두 권 다 빼줘"는 일부다)
# "다"는 앞뒤에 한글이 붙지 않은 낱말일 때만 센다. "좋겠다 담아줘"의 "다"는 어미다
_ALL_WORDS = re.compile(r"전부(?!터)|모두|모든")
_BARE_ALL = re.compile(r"(?<![가-힣])다\s*(?:담|빼|지워|넣|삭제)")
_COUNT_ALL = re.compile(r"(?<![가-힣])([둘셋넷])\s*다(?![가-힣])")
_COUNT_WORDS = {"둘": 2, "셋": 3, "넷": 4}
# 말한 수량("두 권", "2권", "둘"). 전체가 아니라 그 수만큼을 말한 것일 수 있다
_DIGIT_COUNT = re.compile(r"(\d+)\s*권")
_NATIVE_COUNT = re.compile(r"(한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*권")
_BARE_COUNT = re.compile(r"([둘셋넷])(?![가-힣])")

# 제목에서 뽑은 낱말 가운데 위치를 말하는 낱말은 제목의 근거로 세지 않는다("처음 거 빼줘")
_POSITION_WORDS = frozenset({"처음", "마지막", "최근", "방금"})
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
# 제목에서 맞춘 낱말의 글자 수 합이 이만큼은 돼야 근거로 본다. 두 글자 낱말 하나("나는", "그게")는 제목에 있어도
# 말버릇일 수 있다
_MIN_TITLE_MATCH = 3


def normalize(message: str) -> str:
    """구어체를 표준 표현으로 바꾼다: 삼번→3번, 젤→제일, 위에꺼→위에거. 뜻이 달라지는 말은 건드리지 않는다."""
    text = _HANGUL_NUMBER_BEON.sub(lambda m: f"{_HANGUL_NUMBER[m.group(1)]}번", message)
    text = _JEL.sub("제일", text)
    return _KKEO.sub("거", text)


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


def _ends(ctx: TurnContext, ref: BookRef) -> tuple[int, int]:
    """목록의 첫 번호와 마지막 번호. 카드는 rank 가 이어지지 않을 수 있어 가장 작은·큰 rank 를 쓴다."""
    if ref.source == "cart":
        return 1, len(ctx.cart_snapshot or [])
    ranks = [card.rank for card in ctx.cards]
    return min(ranks), max(ranks)


def _says_position(message: str, ctx: TurnContext, ref: BookRef) -> bool:
    """ "맨 위", "맨 아래" 같은 위치로 이 번호를 가리켰는가."""
    first, last = _ends(ctx, ref)
    if ref.source == "cart":
        starts, ends = _CART_FIRST, _CART_LAST
    else:
        starts, ends = _CARD_FIRST, _CARD_LAST
    return (ref.n == first and bool(starts.search(message))) or (
        ref.n == last and bool(ends.search(message))
    )


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


def _explicit(ctx: TurnContext, ref: BookRef, isbn: str, message: str) -> bool:
    """이번 말이 스스로 이 책을 가리키는가: 번호, 위치, 전체, 제목 낱말. 이력은 보지 않는다."""
    return (
        ref.n in ordinals(message)
        or _says_position(message, ctx, ref)
        or _says_all(message, _list_size(ctx, ref))
        or _says_title(ctx, ref, isbn, message)
    )


def has_evidence(ctx: TurnContext, ref: BookRef, isbn: str) -> bool:
    """사용자의 말에 이 책을 가리키는 근거가 있는가. 말을 모르면(message 가 None) 검사하지 않는다."""
    if ctx.message is None or ref.source == "focused":
        return True
    message = normalize(ctx.message)
    # 가리킬 수 있는 책이 하나뿐이면 짐작이 아니다
    if _list_size(ctx, ref) <= 1:
        return True

    return (
        _explicit(ctx, ref, isbn, message)
        or _from_history(ctx, isbn, message)
        or _from_topic(ctx, isbn, message)
    )


def _says_title(ctx: TurnContext, ref: BookRef, isbn: str, message: str) -> bool:
    """이 책만 가리키는 제목 낱말을 말했는가."""
    titles = _list_titles(ctx, ref)
    title = titles.get(isbn)
    if title is None:
        return False
    # 카드 전부의 제목을 모르면 "이 책만 가리키는 낱말"인지 알 수 없다. 카드 셋이 다 파이썬 책인데 하나만
    # 조회했다면 "파이썬 책"은 그 하나의 근거가 아니다
    if ref.source == "card" and len(titles) < _list_size(ctx, ref):
        return False
    # 다른 책의 제목 글자 안에도 있는 낱말은 이 책만 가리키지 못한다("설계"는 두 제목에 다 들어 있다)
    others = [_flat(t) for other, t in titles.items() if other != isbn]
    spoken = _flat(message)
    matched = [
        token
        for token in title_tokens(title)
        if token in spoken and not any(token in other for other in others)
    ]
    return sum(len(token) for token in matched) >= _MIN_TITLE_MATCH


# --- 이력에서 나오는 근거 (#397) ---
# 인정하는 것은 두 가지뿐이다. 직전 에이전트의 말이 책을 **정확히 한 권** 짚었고,
#  1) 에이전트가 그 책을 제안했고(…할까요?) 사용자가 동의했다("네", "좋아")
#  2) 사용자가 그 책을 "그거", "이거", "방금 거"로 가리켰다
# 선택지를 나열한 말에 대한 "네"는 어느 책인지 모르므로 근거가 아니다.
_AGREE = re.compile(
    r"^\s*(?:네|넵|예|응|그래|그래요|좋아|좋아요|맞아|맞아요|그걸로|ㅇㅇ|ㅇㅋ|오케이|ok|okay)(?![가-힣])",
    re.IGNORECASE,
)
_DEICTIC = re.compile(
    r"(?<![가-힣])(?:그거|그것|이거|이것|그\s*책|이\s*책|방금\s*거|방금\s*그거|아까\s*그거|아까\s*그\s*책)"
)
_PROPOSAL = re.compile(r"\?|까요|드릴까")
# "그거 말고 다른 책"의 "그거"는 그 책을 원하지 않는다는 말이다. "네? 아니요"의 "네"도 동의가 아니다.
# 부정이 있으면 지시어도 동의도 근거로 보지 않는다.
# "안 담을래", "담지 마", "넣지 않을래"처럼 행동을 거절하는 꼴도 부정이다(#419 리뷰). 대화의 책(_from_topic)은 행동
# 낱말만 있으면 들어오므로, 이 꼴을 보지 않으면 거절이 그 책에 대한 근거가 된다. "나쁘지 않네 담아줘"도 막히지만
# 막히면 그 책을 짚어 되묻는 쪽이라 잘못 담는 것보다 낫다
_NEGATION = re.compile(r"말고|아니|싫|그만|지\s*(?:마|말)|않|안\s*(?:담|넣|빼|사|살)")
# 동의 뒤에 길게 이어지는 말은 다른 요청이 섞인 것이라 동의로만 보지 않는다
_MAX_AGREEMENT_CHARS = 30
_CARD_MENTION = re.compile(r"카드\s*(\d+)\s*번")
_CART_MENTION = re.compile(r"장바구니\s*(\d+)\s*번째")


def _books_named(ctx: TurnContext, text: str) -> set[str]:
    """에이전트의 말이 짚은 책들. 번호("카드 1번", "2번")와 제목 전체로 찾는다."""
    text = normalize(text)
    named: set[str] = set()
    for match in _CART_MENTION.finditer(text):
        position = int(match.group(1))
        snapshot = ctx.cart_snapshot or []
        if 1 <= position <= len(snapshot):
            named.add(snapshot[position - 1].isbn)
    rest = _CART_MENTION.sub(" ", text)
    ranks = {int(m.group(1)) for m in _CARD_MENTION.finditer(rest)} | ordinals(rest)
    named |= {card.isbn for card in ctx.cards if card.rank in ranks}
    spoken = _flat(text)
    known = {line.isbn: line.title for line in ctx.cart_snapshot or []} | ctx.titles
    named |= {isbn for isbn, title in known.items() if _flat(title) in spoken}
    return named


def _names_other_book(ctx: TurnContext, isbn: str, message: str) -> bool:
    """이번 말이 이 책이 아닌 다른 책을 스스로 가리키는가(번호, 위치, 제목, 전체)."""
    others = [
        (BookRef(source="card", n=card.rank), card.isbn)
        for card in ctx.cards
        if card.isbn != isbn
    ] + [
        (BookRef(source="cart", n=position), line.isbn)
        for position, line in enumerate(ctx.cart_snapshot or [], start=1)
        if line.isbn != isbn
    ]
    return any(_explicit(ctx, ref, other, message) for ref, other in others)


def _from_history(ctx: TurnContext, isbn: str, message: str) -> bool:
    """직전 에이전트의 말이 정확히 한 권을 짚었고 이번 말이 그 책에 대한 동의나 지시어인가.

    이력은 이번 말이 책을 스스로 가리키지 않을 때만 쓴다. 부정이 있거나("네? 아니요", "그거 말고") 다른 책을
    가리키면("네 근데 2번으로요") 동의나 지시어가 제안한 책을 뜻하지 않는다.
    """
    if not ctx.turns or ctx.turns[-1].role != "assistant":
        return False
    last = ctx.turns[-1].text
    if _books_named(ctx, last) != {isbn}:
        return False
    if _NEGATION.search(message) or _names_other_book(ctx, isbn, message):
        return False
    if _DEICTIC.search(message):
        return True
    return (
        bool(_AGREE.match(message))
        and len(message) <= _MAX_AGREEMENT_CHARS
        and bool(_PROPOSAL.search(last))
    )


# --- 대화에서 이야기한 책 (#418) ---
# 실제 모델로 재 보니 "1번 얼마야?" → "15,000원이에요" → "응 담아"나, 한 책의 가격·재고·난이도를 차례로 물은 뒤의
# "그럼 두 권 담아줘"가 막혔다. 모델은 맞는 책을 골랐다. 이번 말이 책을 다시 말하지 않아도 앞 대화가 그 책 한 권만
# 이야기했다면 어느 책인지는 정해져 있다. 모델에게 보여 준 대화(최근 PROMPT_TURNS 줄)만 본다.
# 이번 말이 다른 책을 찾으면("다른 거", "딴 책") 이야기한 책을 뜻하지 않는다.
#
# 이번 말이 행동을 스스로 말할 때만 쓴다. "네", "어?"처럼 행동을 말하지 않은 짧은 말은 직전에 한 권을 짚어
# 제안한 말에 대한 동의일 때만 근거다(_from_history, #397). 알림("담았어요") 뒤의 "네"는 시킨 것이 아니다.
# 행동 낱말은 장바구니를 바꾸는 것뿐이다(담기, 빼기, 수량 바꾸기). "주세요"는 "알려 주세요"와 겹쳐 넣지 않았다.
# 책을 다른 말로 꾸며 가리키면("예쁜 거", "제일 싼 거") 대화의 책이 아니라 그 말이 가리키는 책이다. 코드는 그
# 뜻을 확인할 수 없으므로 이 길로 풀지 않는다(모델이 그 책을 짚어 묻는다). "그거", "이 책" 같은 지시어는 꾸밈이 아니다.
_OTHER_BOOK = re.compile(r"다른|딴\s*(?:거|것|책)")
_CART_ACTION = re.compile(
    r"담|넣|추가|빼|뺄|지워|지우|삭제|제거|취소|바꿔|바꾸|늘려|늘리|줄여|줄이|변경"
)
_DESCRIBED = re.compile(r"[가-힣]\s*(?:거|것|책)")


def needs_card_titles(ctx: TurnContext) -> bool:
    """대화에서 이야기한 책을 알아보려면 카드 제목이 다 있어야 한다. 에이전트는 책을 제목으로 말하기도 해서,
    제목을 모르는 카드가 있으면 그 카드를 이야기한 줄을 못 알아보고 한 권만 이야기했다고 잘못 볼 수 있다."""
    return bool(ctx.turns) and any(card.isbn not in ctx.titles for card in ctx.cards)


def _turn_books(ctx: TurnContext, text: str) -> set[str]:
    """대화 한 줄이 짚은 책들. 번호와 제목 전체에 더해, 그 책만 가리키는 제목 낱말도 센다("쿠버네티스 책은?").
    많이 세면 한 권으로 정해지지 않을 뿐이라(되묻는 쪽) 넉넉히 센다."""
    named = _books_named(ctx, text)
    spoken = normalize(text)
    named |= {
        card.isbn
        for card in ctx.cards
        if _says_title(ctx, BookRef(source="card", n=card.rank), card.isbn, spoken)
    }
    named |= {
        line.isbn
        for position, line in enumerate(ctx.cart_snapshot or [], start=1)
        if _says_title(ctx, BookRef(source="cart", n=position), line.isbn, spoken)
    }
    return named


def _from_topic(ctx: TurnContext, isbn: str, message: str) -> bool:
    """최근 대화에서 사용자와 에이전트가 짚은 책이 이 책 한 권뿐이고, 이번 말이 그 책에 할 행동을 말하며 다른 책을
    가리키지 않는가."""
    if not ctx.turns or needs_card_titles(ctx):
        return False
    if not _CART_ACTION.search(message) or _DESCRIBED.search(
        _DEICTIC.sub(" ", message)
    ):
        return False
    named: set[str] = set()
    for turn in ctx.turns[-PROMPT_TURNS:]:
        named |= _turn_books(ctx, turn.text)
    if named != {isbn}:
        return False
    return not (
        _NEGATION.search(message)
        or _OTHER_BOOK.search(message)
        or _names_other_book(ctx, isbn, message)
    )


def natural_refs(ctx: TurnContext, isbn: str) -> list[BookRef]:
    """이 책을 LLM 이 가리킬 때 쓸 수 있는 말들(카드 번호, 장바구니 번호, 지금 보는 책)."""
    refs = [
        BookRef(source="card", n=card.rank) for card in ctx.cards if card.isbn == isbn
    ]
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
