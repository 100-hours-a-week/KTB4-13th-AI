"""③ 1단계 — 대화 중 새 요청이 시작됐을 때 앞 조건을 코드가 비운다(#513, #384 문제 5).

1단계 LLM 은 "이번 메시지로 바뀌는 값만 담은 patch"를 돌려주고, 서버가 앞 spec 위에 겹쳐 쓴다. patch 에 없는
키는 앞 값이 그대로 남는다. 그래서 주제가 바뀌었는데 모델이 가격 상한이나 제목을 null 로 지우지 않으면 앞 조건이
다음 턴까지 이어진다(dev 16턴, 평가 104·105번). 프롬프트는 "없애 달라는 말이 있으면 null 을 명시하라"까지만
가르치고, 주제가 바뀌었을 때 무엇을 비울지는 모델에 맡기지 않았다. 그 판단을 코드가 한다.

앞 spec 이 프롬프트에 있으면 모델의 해석도 흔들린다. 같은 "해리포터 가튼 판타지 소설"을 옛 spec 과 함께 주면
제목 찾기로 보고(8번 중 8번), 빈 spec 으로 주면 분위기 요청으로 본다(0번). 옛 조건 문장을 새 요청에 베껴 쓰는
일도 있다. 그래서 새 요청이면 patch 를 앞 spec 위에 겹쳐 쓰지 않고, 조건도 앞 대화도 없이 같은 메시지로 다시
뽑은 patch 를 빈 spec(이미 뺀 책만 남김) 위에 쓴다.

새 요청인지는 둘이 모두 맞아야 한다.
1. 모델이 이전 요청과 이번 메시지만 보고 새 책 찾기인지 판정한다(RELATION_PROMPT). 짧은 말("또 다른 거", "2만원
   이하 아니어도 돼", 인사)을 새 요청이라고 잘못 보는 일이 있어 이것만으로는 못 믿는다.
2. 빈 spec 으로 다시 뽑은 patch 가 스스로 주제 낱말을 담고 있다(has_topic). 주제 없이 조건·요청 말뿐이면 앞
   요청을 이어가는 말이다.
앞 요청에 주제가 없었으면(조건만 걸어 두고 되물은 경우) 판정하지 않는다. 모르는 쪽은 앞 조건을 남긴다(고치기
전과 같은 동작).
"""

import re

from langchain_core.prompts import ChatPromptTemplate

from app.chat.schemas import Spec, SpecExact
from app.search.schemas import SearchFilters

# {previous}·{message}가 채워지는 자리다. JSON 예시의 중괄호는 {{ }}로 겹쳐 쓴다.
#
# 처음에는 "이어가는 말 vs 새 책 찾기" 둘을 같은 무게로 물었더니 이어가는 말 4개를 새 요청으로 봤다("다른 거 보여줘",
# "다 아니야. 모든 게 다 귀찮아", 인사 등). 기본값을 same 으로 두고 new 는 "새 주제 낱말이 메시지에 있어야 한다"로
# 좁혔더니 "다른 거"는 맞았지만 "2만원 이하 아니어도 돼"(5번 중 5번), "또 다른 거"(4번), 인사(2번)가 남았다.
# 이 말들은 새 주제 낱말이 없으니 2번 조건(has_topic)이 거른다.
#
# 빈 spec 으로 다시 뽑으면 앞 대화가 없어 "자바 공부 책" 다음의 "그럼 스프링은?"이 "스프링은 따뜻한 느낌의 책"이
# 된다(평가 98번, 3회 중 2회 엉뚱한 책). 앞 주제를 한 줄 알려 줘 봐도 낫지 않았고 제목 찾기로 되돌아갔다(해리포터
# 6번 중 3번). 그래서 이런 짧은 후속 질문은 same 으로 두라고 "그럼 JPA는?"을 예로 들었다. 평가 질문("스프링")을
# 예로 쓰면 그 질문만 맞히게 되어 다른 낱말로 골랐다. 효과는 "그럼 스프링은?" 5번 중 4번 same 이다("그러면 파이썬은
# 어때?", "그럼 에세이는?"은 여전히 new 로 본다. 이쪽은 새 주제를 스스로 말하니 빈 spec 으로 뽑아도 맞다).
RELATION_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇의 대화 분석기다. 사용자의 이전 요청과 이번 메시지를 보고, 이번 메시지가 새로운 책 "
    "찾기를 시작하는 말인지 판단하라.\n\n"
    "이전 요청: {previous}\n"
    '이번 메시지: "{message}"\n\n'
    "- new: 이전 요청과 상관없는 다른 주제·책 제목·저자·분야를 이번 메시지가 스스로 말하며 새로 찾는다. "
    '또는 "처음부터 다시"라고 한다. 이전 요청에 없던 새 주제 낱말이 있고, 이전 요청을 몰라도 이 메시지만으로 '
    "무엇을 찾는지 알 수 있어야 한다.\n"
    "- same: 그 밖의 모든 경우. 이전 요청을 이어간다(가격·출간연도 조건을 더하거나 바꿈, 분위기 조정, "
    '"다른 거"·"또 다른 거"처럼 같은 요청의 다른 책, 앞에 보여 준 책을 가리킴, 이전 요청에 이유나 사정을 '
    '덧붙이거나 바로잡음, "그럼 JPA는?"처럼 이전 요청을 알아야 이해되는 짧은 후속 질문), 또는 책 찾기와 '
    "상관없는 말(인사, 질문)이다. 새 주제가 없으면 same이다.\n\n"
    '답은 JSON 한 줄로만 하라: {{"relation": "same"}} 또는 {{"relation": "new"}}'
)

_WORD = re.compile(r"[0-9a-z가-힣]+")
# 낱말 끝에 붙은 조사. 두 글자짜리를 먼저 뗀다("데미안이랑" → "데미안").
# 세 글자 이상인 낱말에서만 뗀다 — "오는"·"읽을" 같은 두 글자 낱말까지 깎으면 뜻이 사라진다.
_PARTICLE = re.compile(
    r"(이랑|으로|에서|처럼|까지|부터|보다|[은는이가을를의에도만와과로랑])$"
)
# 주제가 아니라 요청이나 느낌을 말하는 낱말. 이 낱말뿐인 문장은 주제가 있다고 보지 않는다.
_GENERIC = frozenset(
    {
        "책", "책들", "도서", "추천", "추천해줘", "보여줘", "알려줘", "읽을", "읽기", "읽고", "볼", "보기",
        "만한", "좋은", "같은", "가튼", "비슷한", "느낌", "분위기", "있는", "있어", "위한", "관한", "오는",
        "하는", "되는", "않은", "않는", "다른", "더", "좀", "너무", "그냥", "요즘", "때",
    }
)  # fmt: skip


def _has_topic_word(text: object) -> bool:
    if not isinstance(text, str):
        return False
    for word in _WORD.findall(text.casefold()):
        if len(word) >= 3:
            word = _PARTICLE.sub("", word)
        if len(word) >= 2 and word not in _GENERIC:
            return True
    return False


def has_topic(patch: dict) -> bool:
    """patch 가 주제 낱말을 담은 조건 문장·제목·저자를 냈는지. "다른 책", "책 추천" 같은 요청 말뿐이면 아니다."""
    exact = patch.get("exact")
    exact = exact if isinstance(exact, dict) else {}
    return any(
        _has_topic_word(text)
        for text in (patch.get("semantic"), exact.get("title"), exact.get("author"))
    )


def previous_request(spec: Spec) -> str | None:
    """앞 요청을 판정 프롬프트에 넣을 한 줄로. 찾던 주제(조건 문장·제목·저자)가 없으면 None.

    주제가 없으면 새 요청으로 갈 것이 없다. 가격만 걸어 두고 되물은 뒤 주제를 말한 경우("2만원 이하 책 추천해줘" →
    "파이썬 입문서")에 가격을 지우면 안 된다.
    """
    topic = " ".join(
        p for p in (spec.semantic, spec.exact.title, spec.exact.author) if p
    )
    if not topic.strip():
        return None
    lines = [f"찾는 것: {topic}"]
    filters = spec.filters
    conditions = []
    if filters.price_max is not None:
        conditions.append(f"가격 {filters.price_max}원 이하")
    if filters.price_min:
        conditions.append(f"가격 {filters.price_min}원 이상")
    if filters.pub_year_from is not None:
        conditions.append(f"{filters.pub_year_from}년 이후 출간")
    if filters.pub_year_to is not None:
        conditions.append(f"{filters.pub_year_to}년 이전 출간")
    if filters.category:
        conditions.append(f"분야 {filters.category}")
    if conditions:
        lines.append("조건: " + ", ".join(conditions))
    return " / ".join(lines)


def fresh_spec(current: Spec) -> Spec:
    """새 요청의 출발점. 가격·연도·분야·제목·저자·출판사·조건 문장·기준 책을 비운다.

    이미 뺀 책(exclude)은 사용자가 싫다고 한 것이라 주제가 바뀌어도 남긴다.
    """
    return Spec(
        intent="semantic",
        exact=SpecExact(),
        filters=SearchFilters(),
        semantic=None,
        anchor_book=None,
        exclude=list(current.exclude),
    )
