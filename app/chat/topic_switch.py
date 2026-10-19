"""③ 1단계 — 대화 중 새 요청이 시작됐는지를 코드가 가른다(#513, #384 문제 5).

1단계 LLM 은 "이번 메시지로 바뀌는 값만 담은 patch"를 돌려주고, 서버가 앞 spec 위에 겹쳐 쓴다. patch 에 없는
키는 앞 값이 그대로 남는다. 그래서 주제가 바뀌었는데 모델이 가격 상한이나 제목을 null 로 지우지 않으면 앞 조건이
다음 턴까지 이어진다(dev 16턴, 평가 104·105번). 프롬프트는 "없애 달라는 말이 있으면 null 을 명시하라"까지만
가르치고, 주제가 바뀌었을 때 무엇을 비울지는 모델에 맡기지 않았다. 그 판단을 코드가 한다.

규칙: patch 가 내놓은 주제(조건 문장·제목·저자)가 앞 주제와 겹치는 낱말이 하나도 없으면 새 요청이다. 톤 조정은
앞 문장을 바탕으로 다시 쓰므로("비 오는 날 읽을 책" → "비 오는 날 읽을 무겁지 않은 책") 겹치고, 가격만 말한
메시지나 "다른 거 보여줘"는 주제를 내지 않는다. 판단할 수 없으면(어느 쪽이든 주제가 비었거나 요청 말뿐) 새 요청이
아닌 것으로 본다. 모르면 앞 조건을 남기는 쪽이 고치기 전과 같은 동작이라 안전하다. 반대로 "파이썬 입문서"에서
"주식 입문서"로 가는 것처럼 낱말이 하나라도 겹치는 주제 전환은 못 잡는다.
"""

import re

from app.chat.schemas import Spec, SpecExact
from app.search.schemas import SearchFilters

_WORD = re.compile(r"[0-9a-z가-힣]+")
# 낱말 끝에 붙은 조사. 두 글자짜리를 먼저 뗀다("데미안이랑" → "데미안", "데미안과" → "데미안").
# 세 글자 이상인 낱말에서만 뗀다 — "에서"·"이랑"이 아닌 두 글자 낱말("오는", "읽을")까지 깎으면 뜻이 사라진다.
_PARTICLE = re.compile(
    r"(이랑|으로|에서|처럼|까지|부터|보다|[은는이가을를의에도만와과로랑])$"
)
# 주제가 아니라 요청이나 느낌을 말하는 낱말. 이 낱말만 겹친 것을 같은 주제로 보면 안 된다.
_GENERIC = frozenset(
    {
        "책", "책들", "도서", "추천", "추천해줘", "보여줘", "알려줘", "읽을", "읽기", "읽고", "볼", "보기",
        "만한", "좋은", "같은", "가튼", "비슷한", "느낌", "분위기", "있는", "있어", "위한", "관한", "오는",
        "하는", "되는", "않은", "않는", "다른", "더", "좀", "너무", "그냥", "요즘", "때",
    }
)  # fmt: skip


def _grams(text: str) -> set[str]:
    """주제 글에서 낱말마다 두 글자 조각을 모은다. 조사는 떼고, 요청·느낌 낱말과 한 글자 낱말은 버린다."""
    grams: set[str] = set()
    for word in _WORD.findall(text.casefold()):
        if len(word) >= 3:
            word = _PARTICLE.sub("", word)
        if len(word) < 2 or word in _GENERIC:
            continue
        grams.update(word[i : i + 2] for i in range(len(word) - 1))
    return grams


def _spec_topic(spec: Spec) -> str:
    return " ".join(
        p for p in (spec.semantic, spec.exact.title, spec.exact.author) if p
    )


def _patch_topic(patch: dict) -> str:
    exact = patch.get("exact")
    exact = exact if isinstance(exact, dict) else {}
    parts = (patch.get("semantic"), exact.get("title"), exact.get("author"))
    return " ".join(p for p in parts if isinstance(p, str))


def starts_new_request(current: Spec, patch: dict) -> bool:
    """이번 patch 가 앞 요청과 상관없는 새 요청의 주제를 냈는지."""
    before = _grams(_spec_topic(current))
    after = _grams(_patch_topic(patch))
    return bool(before) and bool(after) and before.isdisjoint(after)


def names_a_book(patch: dict) -> bool:
    """patch 가 제목이나 저자를 골랐는지. 제목 찾기로 본 것이다."""
    exact = patch.get("exact")
    if not isinstance(exact, dict):
        return False
    return any(
        isinstance(exact.get(key), str) and exact[key] for key in ("title", "author")
    )


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
