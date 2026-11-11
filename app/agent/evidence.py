"""⑦ 사용자의 말에 그 책을 가리키는 근거가 있는지 본다(#387).

"그거 담아줘"처럼 어느 책인지 알 수 없는 말에 LLM 이 카드 1번을 골라 담는 것(짐작해서 실행)을 막으려는
검사다. 프롬프트에 "짐작하지 마"를 쓰는 것으로는 막히지 않았다(7단계 측정). 그래서 LLM 이 고른 책을 사용자의
말과 코드가 맞춰 보고, 근거가 없으면 장바구니를 바꾸는 도구를 실행하지 않는다.

LLM 이 고른 책(card n / cart n)마다 아래 중 하나가 사용자의 말에 있으면 근거가 있다고 본다.
- 그 책의 번호: "1번", "두 번째", "맨 위"(장바구니와 카드의 1번), "맨 아래"(마지막 번호). 숫자만 말한 번호는
  카드 번호이고, 장바구니 번호는 "장바구니 2번째"처럼 장바구니를 말했거나 같은 번호의 카드가 없을 때만 본다(#528)
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
from app.agent.schemas import MAX_QUANTITY, ContextCard

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

# 장바구니를 말한 번호(#528). "장바구니 2번째", "장바구니에서 두 번째", "장바구니에 있는 거 3번",
# "장바구니 두 번째랑 세 번째". 숫자만 말한 번호("2번")는 프롬프트의 약속대로 추천 카드 번호다(지킬 것 1).
# "1번 장바구니에 담아줘"처럼 번호가 장바구니 앞에 오면 카드 1번을 장바구니에 담으라는 말이라 장바구니 번호가 아니다
_ORDINAL_WORD = (
    r"(?:\d+\s*(?:번째|번|째)"
    r"|(?:첫|한|두|세|석|네|넉|다섯|여섯|일곱|여덟|아홉|열)\s*(?:번째|째)"
    r"|첫째|둘째|셋째|넷째)"
)
_CART_NUMBER = re.compile(
    r"장바구니\s*(?:에서|의|에\s*(?:있는|담긴))?\s*(?:거|것|책)?\s*(?:중(?:에서?)?)?\s*"
    rf"({_ORDINAL_WORD}(?:\s*(?:이랑|랑|하고|와|과|및|,)\s*{_ORDINAL_WORD})*)"
)
_CARD_MENTION = re.compile(r"카드\s*(\d+)\s*번")

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
# "둘 다요", "셋 다요"처럼 존댓말 끝(요)이 붙어도 전체다
_COUNT_ALL = re.compile(r"(?<![가-힣])([둘셋넷])\s*다(?:요)?(?![가-힣])")
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


def numbers(message: str) -> tuple[set[int], set[int]]:
    """말에 나온 번호를 (장바구니를 말한 번호, 숫자만 말한 번호)로 나눈다(#528)."""
    in_cart: set[int] = set()
    for match in _CART_NUMBER.finditer(message):
        in_cart |= ordinals(match.group(1))
    return in_cart, ordinals(_CART_NUMBER.sub(" ", message))


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
    if ref.source == "search":
        return {book.isbn: book.title for book in ctx.search_results}
    return {
        card.isbn: ctx.titles[card.isbn]
        for card in ctx.cards
        if card.isbn in ctx.titles
    }


def _list_size(ctx: TurnContext, ref: BookRef) -> int:
    if ref.source == "cart":
        return len(ctx.cart_snapshot or [])
    if ref.source == "search":
        return len({book.isbn for book in ctx.search_results})
    return len({card.isbn for card in ctx.cards})


def _ends(ctx: TurnContext, ref: BookRef) -> tuple[int, int]:
    """목록의 첫 번호와 마지막 번호. 카드는 rank 가 이어지지 않을 수 있어 가장 작은·큰 rank 를 쓴다."""
    if ref.source == "cart":
        return 1, len(ctx.cart_snapshot or [])
    if ref.source == "search":
        return 1, len(ctx.search_results)
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


def _card_book(ctx: TurnContext, rank: int) -> str | None:
    """그 번호 카드의 책. 그 번호의 카드가 없으면 None."""
    return next((card.isbn for card in ctx.cards if card.rank == rank), None)


def _says_number(ctx: TurnContext, ref: BookRef, isbn: str, message: str) -> bool:
    """이 책의 번호를 말했는가. 숫자만 말한 번호는 추천 카드 번호다(프롬프트 지킬 것 1, #528).

    장바구니 번호는 "장바구니 2번째"처럼 장바구니를 말했을 때 근거다. 숫자만 말했어도 같은 번호의 카드가
    없으면(카드가 없거나 그 번호까지 없으면) 장바구니 번호일 수밖에 없어 근거로 본다. 같은 번호의 카드가 같은
    책이면 어느 목록으로 읽든 같은 책이라 근거다. 이 규칙이 없을 때는 카드가 보이는 대화의 "2번 빼줘"가 장바구니
    2번째 줄의 근거로 통과해 말하지 않은 책이 빠졌다(dev QA AGENT-005).
    """
    in_cart, bare = numbers(message)
    if ref.source == "card":
        return ref.n in bare
    return ref.n in in_cart or (
        ref.n in bare and _card_book(ctx, ref.n) in (None, isbn)
    )


def _cart_book(ctx: TurnContext, position: int) -> str | None:
    """장바구니 그 번째 줄의 책. 그 줄이 없거나 장바구니를 모르면 None."""
    snapshot = ctx.cart_snapshot or []
    return snapshot[position - 1].isbn if 1 <= position <= len(snapshot) else None


# 장바구니 답변에서 책 앞에 붙는 번호("1번째는 《…》", "2번: 《…》", "3. 《…》").
_LISTED_POSITION = re.compile(
    r"(\d+)\s*(?:번째|번|째)(?:는|은)?(?:\s+|:\s*|(?=[《「]))"
)
_LISTED_ITEM = re.compile(r"^\s*(\d+)[.)]\s+", re.MULTILINE)


def listed_cart(ctx: TurnContext) -> bool:
    """직전 에이전트의 말이 장바구니를 번호로 두 줄 이상 늘어놓았나.

    카드 제목을 상태에 보여 준 뒤로(graph.prefetch_card_titles) 모델이 숫자만 말한 번호를 카드로 읽는다. "장바구니
    보여줘" 다음의 "2번은 빼 주세요"는 방금 본 장바구니 목록의 번호일 수 있어서, 이때는 카드로 읽은 것도 근거로 보지
    않고 두 책을 짚어 묻는다. 예전(카드 번호만 보여 줄 때)에도 이 대화는 되물었다(모델이 장바구니로 읽고 코드가 막음).
    """
    if not ctx.turns or ctx.turns[-1].role != "assistant":
        return False
    text = normalize(ctx.turns[-1].text)
    if "장바구니" not in text:
        return False
    positions = {int(n) for n in _LISTED_POSITION.findall(_CARD_MENTION.sub(" ", text))}
    positions.update(int(n) for n in _LISTED_ITEM.findall(text))
    return len(positions) >= 2


def _names_card_number(ctx: TurnContext, number: int | None) -> bool:
    """사용자가 숫자만 쓰지 않고 '카드 2번'처럼 어느 목록인지 말했나."""
    return ctx.message is not None and any(
        int(n) == number for n in _CARD_MENTION.findall(normalize(ctx.message))
    )


def cart_for_bare_number(ctx: TurnContext, ref: BookRef, isbn: str) -> int | None:
    """숫자만 말한 번호를 모델이 카드로 읽었는데, 직전에 장바구니를 번호로 늘어놓았고 그 번째 줄이 다른 책이면 그
    번째(listed_cart). card_for_bare_number 의 반대 방향이다."""
    if (
        ctx.message is None
        or ref.source != "card"
        or not listed_cart(ctx)
        or _names_card_number(ctx, ref.n)
    ):
        return None
    in_cart, bare = numbers(normalize(ctx.message))
    if ref.n in in_cart or ref.n not in bare:
        return None
    other = _cart_book(ctx, ref.n)
    return ref.n if other not in (None, isbn) else None


def card_for_bare_number(
    ctx: TurnContext, ref: BookRef, isbn: str
) -> ContextCard | None:
    """숫자만 말한 번호를 모델이 장바구니 줄로 읽었는데 같은 번호의 카드가 다른 책이면 그 카드(#528).

    사용자가 장바구니 목록을 보다가 "2번"이라고 했을 수도 있어서, 코드가 카드로 바꿔 실행하지 않는다. 바꿔 실행하면
    반대 방향으로 같은 사고가 난다. 두 책을 짚어 어느 책인지 묻게 한다(tools._unstated). 빼기에서 그 카드 책이
    장바구니에 있고 직전에 장바구니를 번호로 보여 주지 않았으면 카드로 바꿔 뺀다(removal_card).
    """
    if ctx.message is None or ref.source != "cart":
        return None
    in_cart, bare = numbers(normalize(ctx.message))
    if ref.n in in_cart or ref.n not in bare:
        return None
    return next(
        (card for card in ctx.cards if card.rank == ref.n and card.isbn != isbn), None
    )


def removal_card(ctx: TurnContext, ref: BookRef) -> BookRef | None:
    """빼기에서 숫자만 말한 "2번"을 모델이 장바구니 2번째로 읽었을 때, 카드 2번으로 바꿀 그 카드. 바꾸지 않으면 None.

    카드가 보이면 숫자만 말한 번호는 카드 번호다(프롬프트 지킬 것 1, #528). 모델은 빼기를 장바구니 일로 보고 장바구니
    줄로 읽는 일이 잦아서, 코드가 두 책을 짚어 되물었다(11/10 평가 remove-13 3번 다, 카드 제목을 보여 준 뒤에도). 카드
    2번 책이 장바구니에 있으면 "카드 2번 책을 빼라"는 말로 읽을 수 있으므로 그 책을 뺀다. 아래면 바꾸지 않는다(되묻는다).
    - 직전에 에이전트가 장바구니를 번호로 늘어놓았고 사용자는 카드라고 말하지 않았다
    - 카드 2번 책이 장바구니에 없다. 뺄 수 없으니 바꿀 까닭이 없다
    - 사용자가 장바구니의 책을 번호, 제목, 위치로 따로 가리켰다
    """
    if ctx.message is None or ref.source != "cart":
        return None
    if listed_cart(ctx) and not _names_card_number(ctx, ref.n):
        return None
    cart_isbn = _cart_book(ctx, ref.n)
    if cart_isbn is None:
        return None
    card = card_for_bare_number(ctx, ref, cart_isbn)
    if card is None or card.isbn not in {line.isbn for line in ctx.cart_snapshot or []}:
        return None
    message = normalize(ctx.message)
    if _says_position(message, ctx, ref) or _says_title(ctx, ref, cart_isbn, message):
        return None
    return BookRef(source="card", n=card.rank)


def _explicit(ctx: TurnContext, ref: BookRef, isbn: str, message: str) -> bool:
    """이번 말이 스스로 이 책을 가리키는가: 번호, 위치, 전체, 제목 낱말. 이력은 보지 않는다."""
    return (
        _says_number(ctx, ref, isbn, message)
        or _says_position(message, ctx, ref)
        or _says_all(message, _list_size(ctx, ref))
        or _says_title(ctx, ref, isbn, message)
    )


def has_evidence(ctx: TurnContext, ref: BookRef, isbn: str) -> bool:
    """사용자의 말에 이 책을 가리키는 근거가 있는가. 말을 모르면(message 가 None) 검사하지 않는다."""
    if ctx.message is None or ref.source == "focused":
        return True
    # 번호가 두 목록에서 서로 다른 책을 가리키면 한 목록에 책이 하나뿐이어도 되묻는다.
    if cart_for_bare_number(ctx, ref, isbn) is not None or (
        ref.source == "cart"
        and listed_cart(ctx)
        and card_for_bare_number(ctx, ref, isbn) is not None
    ):
        return False
    message = normalize(ctx.message)
    # 가리킬 수 있는 책이 하나뿐이면 짐작이 아니다
    if _list_size(ctx, ref) <= 1:
        return True
    if ref.source == "search":
        return _search_evidence(ctx, isbn, message)

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
    r"^\s*(?:네|넵|넹|예|응|웅|그래|그래요|좋아|좋아요|맞아|맞아요|그걸로|ㅇㅇ|ㅇㅋ|ㄱㄱ|고고|오케이|ok|okay)(?![가-힣])",
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
# 쿠폰·포인트를 쓰지 말라는 꼴("포인트 적용하지 말고", "쿠폰은 안 쓸래", "포인트 없이"). 책이 아니라 할인을 거절하는
# 말이라 책 부정(_NEGATION)으로 보지 않는다. "이거 주문해줘 포인트 적용하지 말고"가 "말고" 때문에 지시어의 근거를 잃어
# "지금 보고 있는 책이 없어요"로 끝났다(11/10 dev). 주문 화면에 채울 값에서도 그 할인을 뺀다(declines_discount)
_DISCOUNT_DECLINE = re.compile(
    r"(쿠폰|포인트|적립금|할인)\s*(?:은|는|도|을|를|이|가)?\s*(?:적용|사용|쓰|써)?\s*(?:하|해)?\s*"
    r"(?:지\s*(?:말(?:고|아|아요|구)?|마(?:세요|요)?|않(?:고|게|을|아|아요)?)|않(?:고|게|을)?|없이|말고|빼고|빼\s*(?:줘|주|고|서)"
    r"|안\s*(?:쓰|써|쓸|하|할|함|적용|사용))"
)


def _without_discount_decline(message: str) -> str:
    """책 부정을 볼 때 할인을 거절하는 말은 지운다."""
    return _DISCOUNT_DECLINE.sub(" ", message)


# 동의 뒤에 길게 이어지는 말은 다른 요청이 섞인 것이라 동의로만 보지 않는다
_MAX_AGREEMENT_CHARS = 30
_CART_MENTION = re.compile(r"장바구니\s*(\d+)\s*번째")


def _known_titles(ctx: TurnContext) -> dict[str, str]:
    """제목을 아는 책(장바구니 줄과 조회한 카드)의 ISBN → 제목."""
    return {line.isbn: line.title for line in ctx.cart_snapshot or []} | ctx.titles


def _numbered_books(ctx: TurnContext, text: str) -> set[str]:
    """에이전트의 말이 번호("카드 1번", "2번", "장바구니 1번째")로 짚은 책들."""
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
    return named


def _books_named(ctx: TurnContext, text: str) -> set[str]:
    """에이전트의 말이 짚은 책들. 번호("카드 1번", "2번")와 제목 전체로 찾는다."""
    named = _numbered_books(ctx, text)
    spoken = _flat(normalize(text))
    known = _known_titles(ctx)
    # 번호로 짚은 책의 제목 안에 든 제목은 그 책을 말한 것이다. 권마다 ISBN 이 다른 시리즈처럼 카드 여러 장의 제목이
    # 같으면("카드 1번 《한강》을 담을까요?") 제목 일치로 다른 《한강》까지 세어 한 권을 짚은 말이 아니게 되고, "네"가
    # 근거가 못 되어 같은 질문만 되풀이했다(11/11 dev)
    numbered = [_flat(known[isbn]) for isbn in named if isbn in known]
    named |= {
        isbn
        for isbn, title in known.items()
        if _flat(title) in spoken
        and not any(_flat(title) in numbered_title for numbered_title in numbered)
    }
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
    named = _books_named(ctx, last)
    if len(named) >= 2 and isbn in named:
        if _only_by_shared_title(ctx, named, last, isbn):
            return False
        return _for_named_books(ctx, named, last, message)
    if named != {isbn}:
        return False
    if _NEGATION.search(_without_discount_decline(message)) or _names_other_book(
        ctx, isbn, message
    ):
        return False
    if _DEICTIC.search(message):
        return True
    return (
        bool(_AGREE.match(message))
        and len(message) <= _MAX_AGREEMENT_CHARS
        and bool(_PROPOSAL.search(last))
    )


def _only_by_shared_title(
    ctx: TurnContext, named: set[str], text: str, isbn: str
) -> bool:
    """이 책을 번호 없이 제목으로만 짚었고, 같이 짚힌 다른 책도 제목이 같은가.

    "《한강》을 담을까요?"는 같은 제목 카드 여러 장을 다 짚은 말로 세어진다. 그 말에 대한 "네"는 여러 권 모두에 대한
    동의가 아니라 어느 《한강》인지 모르는 말이다(#663). 여러 권 동의(_for_named_books)로 넘기면 같은 제목 카드를 모두
    담았다.
    """
    if isbn in _numbered_books(ctx, text):
        return False
    known = _known_titles(ctx)
    title = known.get(isbn)
    if title is None:
        return False
    return any(
        other != isbn and other in known and _flat(known[other]) == _flat(title)
        for other in named
    )


# 직전 에이전트의 말이 여러 권을 짚었을 때. "카드 1번 《A》과 카드 2번 《B》를 담을까요?"에 "네"라고 하면 두 권 다에 대한
# 동의이고, "A과 B 중 어느 책을 담을까요?"에 "둘 다"라고 하면 짚은 책 전부다. 예전에는 한 권을 짚었을 때만 이력을 봐서
# "네" → "어느 책을 담을까요?" → "둘다" → 같은 물음이 되풀이됐다(11/10 dev). "둘 다"는 짚은 책의 수와 같아야 한다
# (카드가 셋이어도 직전 말이 두 권을 짚었으면 그 두 권이다). 고르라는 물음("중 어느")에 대한 "네"는 어느 책인지 모른다.
_CHOICE_QUESTION = re.compile(r"중\s*(?:어느|어떤|무엇|뭘|하나)|어느\s*(?:책|쪽|것|거)")


def _for_named_books(
    ctx: TurnContext, named: set[str], last: str, message: str
) -> bool:
    """직전 말이 짚은 여러 권 중 하나에 대해, 이번 말이 그 전부를 가리키거나 전부에 동의하는가.
    짚지 않은 책을 스스로 가리키면("네 근데 3번으로요") 아니다."""
    if _NEGATION.search(_without_discount_decline(message)):
        return False
    others = [
        (BookRef(source="card", n=card.rank), card.isbn)
        for card in ctx.cards
        if card.isbn not in named
    ]
    if any(_explicit(ctx, ref, other, message) for ref, other in others):
        return False
    if _says_all(message, len(named)):
        return True
    return (
        bool(_AGREE.match(message))
        and len(message) <= _MAX_AGREEMENT_CHARS
        and bool(_PROPOSAL.search(last))
        and not _CHOICE_QUESTION.search(normalize(last))
    )


# --- 대화에서 이야기한 책 (#418) ---
# 실제 모델로 재 보니 "1번 얼마야?" → "15,000원이에요" → "응 담아"나, 한 책의 가격·재고·난이도를 차례로 물은 뒤의
# "그럼 두 권 담아줘"가 막혔다. 모델은 맞는 책을 골랐다. 이번 말이 책을 다시 말하지 않아도 앞 대화가 그 책 한 권만
# 이야기했다면 어느 책인지는 정해져 있다. 모델에게 보여 준 대화(최근 PROMPT_TURNS 줄)만 본다.
# 이번 말이 다른 책을 찾으면("다른 거", "딴 책") 이야기한 책을 뜻하지 않는다.
#
# 이번 말이 행동을 스스로 말할 때만 쓴다. "네", "어?"처럼 행동을 말하지 않은 짧은 말은 직전에 한 권을 짚어
# 제안한 말에 대한 동의일 때만 근거다(_from_history, #397). 알림("담았어요") 뒤의 "네"는 시킨 것이 아니다.
# 행동 낱말은 states_action 과 같다(담기, 빼기, 수량 바꾸기, 사기, "하나 주세요"처럼 달라는 말). 11/10 까지는 담기·빼기·수량
# 낱말만 봤다. "주세요"는 수량이나 지시어가 붙은 꼴(_GIVE)만 세서 "알려 주세요"는 아니다.
# 책을 다른 말로 꾸며 가리키면("예쁜 거", "제일 싼 거") 대화의 책이 아니라 그 말이 가리키는 책이다. 코드는 그
# 뜻을 확인할 수 없으므로 이 길로 풀지 않는다(모델이 그 책을 짚어 묻는다). "그거", "이 책" 같은 지시어는 꾸밈이 아니다.
_OTHER_BOOK = re.compile(r"다른|딴\s*(?:거|것|책)")
# "담"과 "추가"는 행동이 아닌 말에도 들어 있다: "담담하게", "잘 담겨 있어", "일상을 담고 있어", "삶을 담아낸",
# "추가로 궁금한 게"(#530 대화 평가에서 이 말들이 담기의 근거가 됐다). 담기를 시키는 꼴만 센다
_CART_ACTION = re.compile(
    r"담(?:아(?!\s*내|낸)|어|을|으|자|기|겠)|담고(?!\s*있)|넣|추가(?!로|적)"
    r"|빼|뺄|지워|지우|지울|삭제|제거|취소|바꿔|바꾸|바꿀|늘려|늘리|줄여|줄이|변경"
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


def topic_book(ctx: TurnContext) -> str | None:
    """최근 대화(모델에게 보여 준 줄)에서 사용자와 에이전트가 짚은 책이 한 권뿐이면 그 책의 ISBN. 아니면 None.

    카드 제목을 다 모르면 정하지 않는다(needs_card_titles). 제목으로 이야기한 카드를 못 알아보고 한 권뿐이라고 잘못
    볼 수 있다."""
    if not ctx.turns or needs_card_titles(ctx):
        return None
    named: set[str] = set()
    for turn in ctx.turns[-PROMPT_TURNS:]:
        named |= _turn_books(ctx, turn.text)
    return next(iter(named)) if len(named) == 1 else None


def topic_name(ctx: TurnContext) -> str | None:
    """topic_book 을 LLM 에게 보여 줄 이름. 카드에 있으면 "카드 2번 《제목》", 아니면 "장바구니 1번째 《제목》"."""
    isbn = topic_book(ctx)
    if isbn is None:
        return None
    card = next((c for c in ctx.cards if c.isbn == isbn), None)
    if card is not None:
        return f"카드 {card.rank}번 《{ctx.titles[isbn]}》"
    for position, line in enumerate(ctx.cart_snapshot or [], start=1):
        if line.isbn == isbn:
            return f"장바구니 {position}번째 《{line.title}》"
    return None


def _from_topic(ctx: TurnContext, isbn: str, message: str) -> bool:
    """최근 대화에서 사용자와 에이전트가 짚은 책이 이 책 한 권뿐이고, 이번 말이 그 책에 할 행동을 말하며 다른 책을
    가리키지 않는가.

    행동은 states_action 과 같은 낱말로 본다(담기·빼기·수량 바꾸기에 더해 "살게요", "하나 주세요", "두 권"). 예전에는
    담기·빼기 낱말만 봐서 한 책을 물은 뒤의 "좋네요 하나 주세요", "그럼 하나 살게요"가 3번 다 막혔다(11/10 평가
    multiturn-04·07·08). "하나 주세요"의 "주세요"는 앞에 수량이나 지시어가 붙은 꼴(_GIVE)만 세서 "알려 주세요"는 아니다.
    """
    if not _says_action(ctx, message, quantity=False) or _DESCRIBED.search(
        _DEICTIC.sub(" ", message)
    ):
        return False
    if topic_book(ctx) != isbn:
        return False
    return not (
        _NEGATION.search(_without_discount_decline(message))
        or _OTHER_BOOK.search(message)
        or _names_other_book(ctx, isbn, message)
    )


# --- 수량 (#489) ---
# 담기는 "요청 수량으로 바꾸기"라서, 이미 담긴 책에 사용자가 말하지 않은 수량을 LLM 이 채우면 3권이 1권으로 준다.
# 그래서 사용자의 이번 말에 수량("2권", "두 개")이 있을 때만 그 수량으로 바꾼다. 단위가 없는 숫자("1번")는 번호다.
_QUANTITY = re.compile(
    r"(?:\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:권|개|부)"
)


def states_quantity(ctx: TurnContext) -> bool:
    """사용자의 이번 말이 수량을 말했는가. 말을 모르면(message 가 None) 말하지 않은 것으로 본다."""
    return ctx.message is not None and bool(_QUANTITY.search(normalize(ctx.message)))


def said_quantity_lately(ctx: TurnContext) -> bool:
    """사용자가 이번 말이나 모델에게 보여 준 대화의 자기 말에서 수량을 말했는가("두 권 담아줘" → "어느 책?" → "1번이요").
    말을 모르면(message 가 None) 검사하지 않는다(말한 것으로 본다)."""
    if ctx.message is None:
        return True
    users = [t.text for t in ctx.turns[-PROMPT_TURNS:] if t.role == "user"]
    return any(_QUANTITY.search(normalize(text)) for text in [ctx.message, *users])


# 권 번호가 있는 책(『토지 1』, 『삼국지 3』, 상·중·하)에 "그거 말고 2권으로"라고 하면 다른 권을 원하는 말일 수 있다.
# ⑦은 "2권"을 수량으로만 읽어 『토지 1』을 2권으로 바꿨다(11/11 dev 3번 중 3번, #695). 이런 말이면 바꾸지 않고 묻는다.
# 묻는 것은 지시어("그거·이거·그 책")로만 짚은 말뿐이다. 번호("3번 말고 1번을 2권으로")나 제목("토지 1 말고 토지 3 두
# 권")으로 어느 책인지 짚은 말의 "N권"은 그 책의 수량이다(#700 리뷰). "그건·이건 말고"도 지시어로 짚은 말이다
_DEICTIC_INSTEAD = re.compile(
    r"(?:" + _DEICTIC.pattern + r"|(?<![가-힣])(?:그건|이건))"
    r"\s*(?:은|는)?\s*(?:말고|대신|아니고|아니라)"
)
_VOLUME_TITLE = re.compile(r"^(?P<base>.*\S)\s+(?P<volume>\d{1,2}|상|중|하)(?:\s?권)?$")


def swaps_volume(ctx: TurnContext) -> bool:
    """이번 말이 지시어로만 책을 짚고 "말고·대신"과 권수를 같이 말했는가("아 그거 말고 2권으로"). 다른 권을 원하는
    말일 수 있다. 번호나 아는 책의 제목을 말했으면 그 책을 짚은 말이라 아니다."""
    if ctx.message is None:
        return False
    message = normalize(ctx.message)
    if not (_DEICTIC_INSTEAD.search(message) and _QUANTITY.search(message)):
        return False
    spoken = _flat(message)
    titles = [_flat(title) for title in _known_titles(ctx).values()]
    return not ordinals(message) and not any(
        len(title) >= 2 and title in spoken for title in titles
    )


def volume_title(title: str | None) -> tuple[str, str] | None:
    """제목 끝의 권 번호. 『토지 1』 → ("토지", "1"), 『태백산맥 상』 → ("태백산맥", "상"). 없으면 None.
    번호 앞에 띄어쓰기가 있어야 한다(『1984』는 권 번호가 아니다)."""
    found = _VOLUME_TITLE.match((title or "").strip())
    return (found["base"], found["volume"]) if found else None


# --- 행동 (#531) ---
# 위의 근거 검사는 어느 책인지만 본다. "1번이 마음에 들어"에는 "1번"이 있어서 책은 정해지지만 담으라는 말은 없다.
# 실제 모델로 재 보니(#530 대화 평가) 모델이 이런 관심 표현을 담기로 읽어, 대화 30개 중 23개에서 시키지 않은 책을
# 담았다. 그래서 장바구니를 바꾸는 도구는 사용자가 행동을 말했을 때만 실행한다. 행동을 말한 것으로 보는 것은 셋이다.
# - 이번 말의 행동 낱말(담기, 빼기, 수량 바꾸기, 사기)이나 "하나 주세요", "이거 줘" 같은 달라는 말
# - 이번 말의 수량("3권으로 해줘"). 수량을 말한 것은 그 수량으로 담거나 바꾸라는 말이다
# - 직전 에이전트의 말이 행동을 물은 문장("어느 책을 담을까요?", "카드 1번 《…》을 뺄까요?")이고 이번 말이 그 답인 것.
#   알림 뒤의 물음("담았어요. 다른 도와드릴 일이 있으신가요?")은 행동을 묻지 않았으므로 문장 단위로 본다.
#   되묻는 말("마음에 든다! 더 궁금한 내용 있어?")은 답이 아니다
# 어느 행동인지(담기인지 빼기인지)는 맞춰 보지 않는다. 시킨 적이 없는 행동을 막는 것만 한다.
# "1번으로 할게요"처럼 행동이 애매한 말은 받지 않는다. 막히면 그 책을 짚어 "담을까요?"라고 한 번 묻는다.
_BUY = re.compile(r"살게|살래|사\s*갈게|사고\s*싶|(?:구매|구입|주문)\s*(?:할|하고|해)")
# "사 줘"(#631). 카드 밖 책을 찾아 담는 ⑦(AGENT_CHECKOUT)이 켜졌을 때만 장바구니를 바꾸라는 말로 본다.
# 꺼져 있으면 _BUY 만 본다(main 과 같다)
_BUY_REQUEST = re.compile(r"(?<![가-힣])사\s*(?:줘|주세요|줄래|주라)")
# 수량 뒤의 달라는 말은 "한 권"만이 아니다("두 권 주세요", "3권 줘")
_GIVE = re.compile(
    r"(?:하나|(?:\d+|한|두|세|네|다섯)\s*(?:권|개)|거|걸로|것|번|책)\s*(?:주세요|줘|주실래요|줄래|주시겠어요)"
)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")


def _says_action(ctx: TurnContext, message: str, *, quantity: bool = True) -> bool:
    """이번 말에 장바구니를 바꾸라는 낱말이 있는가(normalize 한 말). 이력은 보지 않는다.

    quantity 가 거짓이면 수량만 말한 것("두 권")은 세지 않는다. 대화의 책(_from_topic)에서는 수량만 말한 답이 담으라는
    말이 아니다(#418 short-04: 재고를 물은 뒤 "두 권"). "두 권 주세요"처럼 달라는 말이 붙으면 _GIVE 로 센다.
    """
    patterns = [_CART_ACTION, _BUY, _GIVE] + [_QUANTITY] * quantity
    if ctx.checkout_enabled:
        # 주문하겠다는 말(wants_order 와 같은 낱말)도 담으라는 말이다. 주문 화면은 담은 책으로 연다. 이 낱말이 빠져
        # "파이썬 책 결제해 줘"가 주문 말로는 읽히고 담기는 ACTION_NOT_STATED 로 막혔다(11/10 평가 checkout-m09)
        patterns += [_BUY_REQUEST, _ORDER]
    return any(pattern.search(message) for pattern in patterns)


def _asks_action(text: str) -> bool:
    """에이전트의 말에 장바구니 행동을 묻는 문장이 있는가."""
    return any(
        _PROPOSAL.search(sentence) and _CART_ACTION.search(sentence)
        for sentence in _SENTENCE.findall(normalize(text))
    )


def states_action(ctx: TurnContext) -> bool:
    """사용자가 장바구니를 바꾸라고 말했는가. 말을 모르면(message 가 None) 검사하지 않는다."""
    if ctx.message is None:
        return True
    message = normalize(ctx.message)
    if _says_action(ctx, message):
        return True
    return (
        "?" not in message
        and bool(ctx.turns)
        and ctx.turns[-1].role == "assistant"
        and _asks_action(ctx.turns[-1].text)
    )


# 금액을 말한 꼴: "3만원", "4만 원", "2만7천원", "37,000원", "만 원", "삼만원". 금액 없이 "예산 안에서"만 말하면 아니다.
# 놓치면(금액을 말했는데 못 알아보면) 고르기가 막히고, 잘못 잡으면 예전처럼 모델이 정한 예산으로 고른다. 그래서 넓게 잡는다
_MONEY = re.compile(
    r"\d[\d,]*\s*(?:만|천|원)"
    r"|[이삼사오육칠팔구십]\s*만|[이삼사오육칠팔구]\s*천\s*원"
    r"|(?<![가-힣\d])(?:만|천)\s*원"
)


def states_budget(ctx: TurnContext) -> bool:
    """사용자가 이번 말이나 앞선 자기 말에서 금액을 말했는가. 말을 모르면(message 가 None) 검사하지 않는다.

    예산 고르기(selection.pick)는 금액이 있어야 한다. 실제 모델이 금액 없는 말("1번 하나 주세요", "리눅스 명령어 사전
    두 권 사 주세요")에 예산 칸을 1원·0원으로 지어내 "조합이 없다"고 답했다(11/10 평가 add-11·checkout-m08·06).
    프롬프트에는 이미 "예산을 지어내지 않는다"가 있어서 코드가 막는다. 앞선 말도 보는 것은 "4만원 안에서 골라 줘" →
    "몇 권 고를까요?" → "두 권이요"처럼 금액이 앞 차례에 있을 수 있어서다.
    """
    if ctx.message is None:
        return True
    texts = [ctx.message, *(turn.text for turn in ctx.turns if turn.role == "user")]
    return any(_MONEY.search(text) for text in texts)


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


# --- 고르기를 맡긴 말과 주문하겠다는 말 (#631) ---
# 찾은 책(search n)은 사용자가 아직 본 적이 없는 책이다. 사용자가 보인 책을 "그거", "1번"으로 가리키지 않고 원하는
# 책을 말로 그렸다면("파이썬 입문서 하나 담아 줘") 어느 책을 고를지 맡긴 것이라, 모델이 찾은 책 중에서 고른 것은
# 짐작이 아니다. 보인 책을 가리키는 말이면 찾은 책은 그 책이 아니라서 근거가 없다(카드나 장바구니로 풀어야 한다).
_CHOOSE = re.compile(
    r"골라|고르|골라서|알아서|아무거나|아무\s*책|아무\s*거|추천해서|추천해\s*서"
)
# 담기를 시키는 꼴. _CART_ACTION 에서 빼기·바꾸기를 뺀 것이다
_ADD = re.compile(
    r"담(?:아(?!\s*내|낸)|어|을|으|자|기|겠)|담고(?!\s*있)|넣|추가(?!로|적)"
)
# 주문하겠다는 말. 주문 화면을 연다(주문서를 만들거나 결제하지는 않는다). "주문 내역", "주문한 책", "주문했어?"는
# 이미 한 주문을 묻는 말이라 들지 않는다. "주문을 해주라고", "주문시켜줘", 말끝의 "~ 주문"도 주문이다(#664, 11/11 dev).
# 주문 화면을 열지는 모델이 order.open 으로 정하고, 이 목록은 모델이 부르지 않았을 때의 보조다
_ORDER = re.compile(
    r"(?:주문|결제|구매|구입)\s*(?:을|를|좀)?\s*(?:해|하고|할|하기|하려|하자|까지|부탁|시켜|진행)"
    r"|(?:주문|결제|구매|구입)\s*[.!~]*$"
    r"|바로\s*(?:주문|구매|결제)|(?<![가-힣])사\s*(?:줘|주세요|줄래|주라)|살게|살래|사고\s*싶"
    # 말이 "주문"으로 끝나는 꼴("1번 주문"). 주문해 달라는 말을 줄인 것이다. "주문 내역", "주문했어?"는 끝이 아니다
    r"|(?:주문|구매|결제)\s*[.!~]*$"
)
# 낱말 하나로 주문하겠다고 한 말("주문", "주문이요", "결제할게요"). 앞 대화에 이어서 하는 말이다. 이 꼴이 없으면 "주문"
# 한 마디가 쇼핑이 아닌 말로 ③에 가서 추천 카드가 나왔다(11/10 dev)
_ORDER_ALONE = re.compile(
    r"^\s*(?:주문|결제|구매)\s*(?:요|이요|할게요?|할래요?|하기)?\s*[.!~]*\s*$"
)
_COUPON = re.compile(r"쿠폰|포인트|할인")


def _search_evidence(ctx: TurnContext, isbn: str, message: str) -> bool:
    """찾은 책의 근거. 사용자가 찾은 책 중 어느 책의 제목을 말했으면("테스트 주도 개발 책") 그 책만 근거가 있다.
    그 책이 품절이라고 제목이 다른 책을 대신 담지 않는다(평가 checkout-04). 제목을 말하지 않고 주제·종류로 그렸으면
    ("파이썬 입문서 하나") 찾은 책 어느 것이든 고르기를 맡긴 것이다.

    한 턴에 여러 번 찾았으면 검색마다 따로 본다. "개발 입문서 하나랑 파이썬 입문서 담아 줘"에서 두 번째 검색의 책
    제목을 말했다고 첫 번째 검색(주제로 그린 책)의 책까지 막지 않는다."""
    same = ctx.search_round.get(isbn)
    named = {
        book.isbn
        for n, book in enumerate(ctx.search_results, start=1)
        if ctx.search_round.get(book.isbn) == same
        and _says_title(ctx, BookRef(source="search", n=n), book.isbn, message)
    }
    if named:
        return isbn in named
    return _describes(message)


def _describes(message: str) -> bool:
    """보인 책을 가리키지 않고 원하는 책을 말로 그렸는가. 지시어나 번호가 있으면 보인 책을 가리킨 것이다."""
    in_cart, bare = numbers(message)
    return not _DEICTIC.search(message) and not in_cart and not bare


def delegates_pick(ctx: TurnContext) -> bool:
    """골라 달라면서 담거나 주문하라고 했는가("3만원 안으로 두 권 골라서 담아 줘"). 그러면 코드가 고른 책을 확인
    버튼 없이 담는다. "골라줘"만 하면 고르기만 맡긴 것이라 지금처럼 확인 버튼으로 담는다."""
    if ctx.message is None:
        return False
    message = normalize(ctx.message)
    ordered = bool(_ORDER.search(message))
    if ordered and _MONEY.search(message):
        # "3만원내로 개발도서 주문": 금액을 말하고 주문하라면 고르기와 주문을 함께 맡긴 것이다(#664). 확인받을 조합으로
        # 돌려보내면 찾은 책이 든 조합을 BE 가 받지 않아 턴이 실패했다(11/11 dev E502)
        return True
    return bool(_CHOOSE.search(message)) and bool(
        _ADD.search(message) or _BUY.search(message) or ordered
    )


def wants_order(ctx: TurnContext) -> bool:
    """주문하겠다고 했는가. 그러면 담은 뒤 주문 화면으로 보낸다."""
    if ctx.message is None:
        return False
    message = normalize(ctx.message)
    return bool(_ORDER.search(message) or _ORDER_ALONE.match(message))


def orders_alone(ctx: TurnContext) -> bool:
    """주문·결제 낱말만으로 된 말인가("주문", "결제할게요"). "선물로 한 권 사고 싶은데 뭐가 좋을까요"처럼 주문 낱말이
    든 추천 요청은 들지 않는다."""
    return ctx.message is not None and bool(_ORDER_ALONE.match(normalize(ctx.message)))


_EACH = re.compile(r"씩|각각|각\s*\d|각\s*(?:한|두|세|네)")


def states_each(ctx: TurnContext) -> bool:
    """책마다의 수량을 말했는가("두 권씩", "각각 2권"). 없으면 "두 권"은 서로 다른 책의 수다."""
    return ctx.message is not None and bool(_EACH.search(normalize(ctx.message)))


def states_copies(ctx: TurnContext, isbn: str) -> bool:
    """찾은 책 하나의 제목을 말하고 권수를 말했는가("AI 모델 서빙 실전 3권 주문해 줘"). 그러면 "3권"은 서로 다른
    책의 수가 아니라 그 책의 수량이다(평가 checkout-h05). 고르기를 맡긴 말("두 권 골라서")의 권수는 그대로 서로
    다른 책의 수다."""
    if ctx.message is None or not states_quantity(ctx):
        return False
    message = normalize(ctx.message)
    if _CHOOSE.search(message):
        return False
    return any(
        book.isbn == isbn
        and _says_title(ctx, BookRef(source="search", n=n), isbn, message)
        for n, book in enumerate(ctx.search_results, start=1)
    )


# 수량을 말한 꼴의 수. _QUANTITY 와 같은 꼴이다("두 권", "3권", "두 개")
_COPY_COUNT = re.compile(
    r"(?<![가-힣\d])(\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:권|개|부)"
)


def copies_of(ctx: TurnContext, isbn: str) -> int | None:
    """찾은 책의 제목과 권수를 말했으면(states_copies) 그 권수. 수를 하나만 말했을 때만 준다.

    "리눅스 명령어 사전 두 권 사 주세요"에 실제 모델이 수량을 1로 채워 5번 중 3번 한 권만 담았다(11/10 평가
    checkout-m08). 수량은 사용자 말에서 오는 값이라 코드가 채운다. "A 2권이랑 B 3권"처럼 수가 여럿이면 어느 책의
    수인지 낱말로 모르니 None(모델이 채운 값을 쓴다).
    """
    if not states_copies(ctx, isbn) or ctx.message is None:
        return None
    counts = {
        int(word) if word.isdigit() else _NATIVE[word]
        for word in _COPY_COUNT.findall(normalize(ctx.message))
    }
    if len(counts) != 1:
        return None
    (count,) = counts
    return count if 1 <= count <= MAX_QUANTITY else None


def mentions_coupon(ctx: TurnContext) -> bool:
    """쿠폰이나 포인트, 할인을 말했는가. 주문하지 않는 말이면 쿠폰은 주문 화면에서 쓴다고 알려 준다."""
    return ctx.message is not None and bool(_COUPON.search(normalize(ctx.message)))


def declines_discount(ctx: TurnContext, kind: str) -> bool:
    """사용자가 쿠폰(kind="쿠폰")이나 포인트(kind="포인트")를 쓰지 말라고 했는가. 그러면 주문 화면에 채우지 않는다.

    이번 말에 더해 모델에게 보여 준 대화의 자기 말도 본다. "1번 주문해 줘 포인트 적용하지 말고" 다음의 "이 책 2개
    주문해 줘"에 다시 포인트를 채우면 앞에서 한 말을 잊은 것이다(11/10 dev). 이번 말이 그 할인을 거절하지 않고
    말했으면("포인트 써 줘") 이번 말을 따른다. "적립금"은 포인트로 본다.
    """
    if ctx.message is None:
        return False

    def said(text: str) -> bool | None:
        """이 말이 그 할인을 거절했으면 True, 거절 없이 말했으면 False, 말하지 않았으면 None."""
        text = normalize(text)
        names = ("포인트", "적립금") if kind == "포인트" else (kind,)
        declined = [m for m in _DISCOUNT_DECLINE.finditer(text) if m.group(1) in names]
        if declined:
            return True
        return False if any(name in text for name in names) else None

    for text in [
        ctx.message,
        *(t.text for t in reversed(ctx.turns[-PROMPT_TURNS:]) if t.role == "user"),
    ]:
        verdict = said(text)
        if verdict is not None:
            return verdict
    return False
