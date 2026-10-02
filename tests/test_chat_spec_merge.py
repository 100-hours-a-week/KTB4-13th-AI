"""③ 1단계 spec 갱신 — patch 병합의 intent 보정과 SPEC_PROMPT 모양(#281).

LLM은 부르지 않는다. _merge_spec_patch는 patch(dict)만 받는 순수 함수고, 프롬프트는
채워 보는 것만으로 중괄호 이스케이프가 깨졌는지 알 수 있다.
"""

from app.chat.schemas import Spec, SpecExact
from app.routers import chat
from app.search.schemas import SearchFilters


def _spec(**overrides) -> Spec:
    base = {
        "intent": "semantic",
        "exact": SpecExact(title=None, author=None, publisher=None),
        "filters": SearchFilters(),
        "semantic": None,
        "anchor_book": None,
        "exclude": [],
    }
    base.update(overrides)
    return Spec(**base)


def test_patch가_저자만_채우고_intent가_없으면_exact로_본다() -> None:
    merged = chat._merge_spec_patch(_spec(), {"exact": {"author": "김영하"}})

    assert merged["intent"] == "exact"
    assert merged["exact"]["author"] == "김영하"


def test_patch가_제목만_채우고_intent가_없으면_exact로_본다() -> None:
    merged = chat._merge_spec_patch(_spec(), {"exact": {"title": "어린왕자"}})

    assert merged["intent"] == "exact"


def test_patch가_intent를_직접_줬으면_LLM_판단을_따른다() -> None:
    patch = {"intent": "semantic", "exact": {"title": "해리포터"}, "semantic": "마법"}

    merged = chat._merge_spec_patch(_spec(), patch)

    assert merged["intent"] == "semantic"


def test_출판사만_채우면_intent를_안_바꾼다() -> None:
    merged = chat._merge_spec_patch(_spec(), {"exact": {"publisher": "민음사"}})

    assert merged["intent"] == "semantic"


def test_제목을_null로_지우는_patch는_exact로_보지_않는다() -> None:
    current = _spec(
        intent="semantic",
        exact=SpecExact(title="옛 제목", author=None, publisher=None),
    )

    merged = chat._merge_spec_patch(current, {"exact": {"title": None}})

    assert merged["intent"] == "semantic"


def test_이전_턴의_제목이_남아_있어도_이번_patch가_안_건드리면_intent를_안_바꾼다() -> (
    None
):
    # 보정은 "이번 patch가 제목·저자를 채웠을 때"만이다. 지난 턴 값 때문에 분위기 요청이 exact로 뒤집히면 안 된다.
    current = _spec(
        intent="semantic",
        exact=SpecExact(title="옛 제목", author=None, publisher=None),
        semantic="잔잔한 책",
    )

    merged = chat._merge_spec_patch(current, {"semantic": "더 가벼운 책"})

    assert merged["intent"] == "semantic"


def test_SPEC_PROMPT는_중괄호가_깨지지_않고_채워지며_exact_규칙이_들어_있다() -> None:
    messages = chat.SPEC_PROMPT.format_messages(
        recent_turns="(없음)", spec_json="{}", message="어린왕자"
    )
    text = messages[0].content

    assert '{"intent": "exact", "exact": {"title": "데미안"}}' in text
    assert '{"intent": "exact", "exact": {"author": "박경리"}}' in text
    assert "exact.title" in text
    assert "exact.author" in text
    assert "절대 덧붙이지 마라" in text
