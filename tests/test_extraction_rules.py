"""⑤ LLM 이 낸 취향 후보를 거르는 규칙(app/extraction/rules.py) 테스트(#367). LLM 없이 돈다.

여기서 지키려는 것: LLM 이 무엇을 내든 응답에 나갈 수 있는 것은 명세 ⑤의 모양을 지킨 후보뿐이고,
틀린 항목 하나 때문에 맞는 항목까지 버려지지 않는다.
"""

import math

import pytest

from app.extraction.rules import (
    MAX_EXTRACTIONS,
    MAX_VALUE_CHARS,
    MIN_CONFIDENCE,
    Candidate,
    screen,
)


def _item(type_="mood", value="잔잔한 소설을 찾음", confidence=0.8, **extra) -> dict:
    return {"type": type_, "value": value, "confidence": confidence, **extra}


def _out(*items) -> dict:
    return {"extractions": list(items)}


# --- 통과 ---


def test_모양이_맞는_후보는_그대로_나온다() -> None:
    assert screen(_out(_item())) == [Candidate("mood", "잔잔한 소설을 찾음", 0.8)]


@pytest.mark.parametrize("type_", ["mood", "topic", "author", "condition"])
def test_종류는_명세의_네_가지를_받는다(type_) -> None:
    assert [c.type for c in screen(_out(_item(type_=type_)))] == [type_]


def test_정수_신뢰도_1도_받는다() -> None:
    assert screen(_out(_item(confidence=1))) == [
        Candidate("mood", "잔잔한 소설을 찾음", 1.0)
    ]


def test_모르는_칸이_더_있어도_후보는_받는다() -> None:
    # LLM 이 근거 문장 같은 칸을 덧붙여도 세 칸만 쓴다
    kept = screen(_out(_item(reason="사용자가 두 번 말함")))

    assert kept == [Candidate("mood", "잔잔한 소설을 찾음", 0.8)]


def test_문장의_앞뒤_공백과_줄바꿈은_정리한다() -> None:
    # 취향 카드에 그대로 보이는 글이라 한 줄로 만든다
    kept = screen(_out(_item(value="  잔잔한\n소설을   찾음 ")))

    assert [c.value for c in kept] == ["잔잔한 소설을 찾음"]


# --- LLM 의 답 전체가 깨진 경우 ---


@pytest.mark.parametrize(
    "llm_output",
    [
        pytest.param(None, id="null"),
        pytest.param([], id="목록"),
        pytest.param("mood", id="글자"),
        pytest.param({}, id="extractions_없음"),
        pytest.param({"extractions": None}, id="extractions_null"),
        pytest.param({"extractions": {"type": "mood"}}, id="extractions_가_객체"),
        pytest.param({"extractions": []}, id="빈_목록"),
    ],
)
def test_답의_모양이_아니면_뽑은_것이_없다(llm_output) -> None:
    assert screen(llm_output) == []


# --- 항목 하나가 깨진 경우 ---


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("잔잔한 소설", id="항목이_글자"),
        pytest.param(None, id="항목이_null"),
        pytest.param({"value": "잔잔한 소설", "confidence": 0.8}, id="종류_없음"),
        pytest.param(_item(type_="genre"), id="모르는_종류"),
        pytest.param(_item(type_="Mood"), id="대문자_종류"),
        pytest.param(_item(type_=1), id="숫자_종류"),
        pytest.param({"type": "mood", "confidence": 0.8}, id="문장_없음"),
        pytest.param(_item(value=""), id="빈_문장"),
        pytest.param(_item(value="  \n "), id="공백뿐인_문장"),
        pytest.param(_item(value=["잔잔한 소설"]), id="문장이_목록"),
        pytest.param(_item(value="가" * (MAX_VALUE_CHARS + 1)), id="문장이_너무_김"),
        pytest.param(_item(value="잔잔한\x00소설"), id="문장에_NUL"),
        pytest.param(_item(value="잔잔한 \ud800"), id="문장에_깨진_글자"),
        pytest.param({"type": "mood", "value": "잔잔한 소설"}, id="신뢰도_없음"),
        pytest.param(_item(confidence="0.8"), id="신뢰도가_글자"),
        pytest.param(_item(confidence=True), id="신뢰도가_참거짓"),
        pytest.param(_item(confidence=None), id="신뢰도가_null"),
        pytest.param(_item(confidence=math.nan), id="신뢰도가_NaN"),
        pytest.param(_item(confidence=math.inf), id="신뢰도가_무한"),
        pytest.param(_item(confidence=1.01), id="신뢰도가_1_초과"),
        pytest.param(_item(confidence=80), id="신뢰도가_백분율"),
        pytest.param(_item(confidence=-0.1), id="신뢰도가_음수"),
    ],
)
def test_깨진_항목만_버리고_나머지는_살린다(bad) -> None:
    good = _item(type_="topic", value="기후 위기를 다룬 책을 찾음", confidence=0.9)

    kept = screen(_out(bad, good))

    assert kept == [Candidate("topic", "기후 위기를 다룬 책을 찾음", 0.9)]


def test_문장은_딱_200자까지_받는다() -> None:
    value = "가" * MAX_VALUE_CHARS

    assert [c.value for c in screen(_out(_item(value=value)))] == [value]


def test_긴_문장은_잘라서_받지_않는다() -> None:
    # 잘린 문장이 취향 카드로 보이면 뜻이 달라진다
    assert screen(_out(_item(value="가" * (MAX_VALUE_CHARS + 1)))) == []


# --- 신뢰도 ---


def test_신뢰도_기준은_명세의_0_5다() -> None:
    assert MIN_CONFIDENCE == 0.5


def test_신뢰도가_기준과_같으면_받고_낮으면_버린다() -> None:
    kept = screen(
        _out(
            _item(value="딱 기준", confidence=MIN_CONFIDENCE),
            _item(value="기준 아래", confidence=MIN_CONFIDENCE - 0.01),
        )
    )

    assert [c.value for c in kept] == ["딱 기준"]


# --- 한 대화 안의 중복 ---


def test_같은_종류_같은_문장은_하나로_합치고_높은_신뢰도를_남긴다() -> None:
    kept = screen(
        _out(
            _item(value="잔잔한 소설을 찾음", confidence=0.6),
            _item(value="잔잔한 소설을 찾음", confidence=0.9),
        )
    )

    assert kept == [Candidate("mood", "잔잔한 소설을 찾음", 0.9)]


def test_공백과_대소문자만_다른_문장도_같은_것으로_본다() -> None:
    kept = screen(
        _out(
            _item(type_="topic", value="SF 소설을 좋아함", confidence=0.9),
            _item(type_="topic", value="sf  소설을 좋아함", confidence=0.7),
        )
    )

    # 먼저 나온 표기를 남긴다
    assert kept == [Candidate("topic", "SF 소설을 좋아함", 0.9)]


def test_합칠_때_표기는_먼저_나온_것을_쓰고_신뢰도만_높은_값으로_한다() -> None:
    kept = screen(
        _out(
            _item(type_="topic", value="SF 소설을 좋아함", confidence=0.6),
            _item(type_="topic", value="sf 소설을 좋아함", confidence=0.9),
        )
    )

    assert kept == [Candidate("topic", "SF 소설을 좋아함", 0.9)]


def test_문장이_같아도_종류가_다르면_따로_둔다() -> None:
    kept = screen(
        _out(
            _item(type_="topic", value="김초엽", confidence=0.7),
            _item(type_="author", value="김초엽", confidence=0.9),
        )
    )

    assert {(c.type, c.value) for c in kept} == {
        ("topic", "김초엽"),
        ("author", "김초엽"),
    }


# --- 개수 상한과 순서 ---


def test_신뢰도가_높은_순으로_나온다() -> None:
    kept = screen(
        _out(
            _item(value="가", confidence=0.6),
            _item(value="나", confidence=0.9),
            _item(value="다", confidence=0.7),
        )
    )

    assert [c.value for c in kept] == ["나", "다", "가"]


def test_신뢰도가_같으면_LLM이_낸_순서를_지킨다() -> None:
    kept = screen(_out(*[_item(value=v, confidence=0.8) for v in "가나다"]))

    assert [c.value for c in kept] == ["가", "나", "다"]


def test_상한을_넘으면_신뢰도가_높은_것만_남긴다() -> None:
    items = [
        _item(value=f"취향 {n}", confidence=0.5 + n / 100)
        for n in range(MAX_EXTRACTIONS + 3)
    ]

    kept = screen(_out(*items))

    assert len(kept) == MAX_EXTRACTIONS
    assert min(c.confidence for c in kept) > 0.5 + 2 / 100


def test_버려진_항목은_상한_자리를_차지하지_않는다() -> None:
    # 신뢰도가 낮거나 깨진 항목이 앞에 많아도, 맞는 항목이 상한 때문에 밀리지 않는다
    junk = [_item(value=f"낮음 {n}", confidence=0.1) for n in range(MAX_EXTRACTIONS)]
    good = [_item(value=f"맞음 {n}", confidence=0.8) for n in range(MAX_EXTRACTIONS)]

    kept = screen(_out(*junk, *good))

    assert [c.value for c in kept] == [f"맞음 {n}" for n in range(MAX_EXTRACTIONS)]


def test_중복은_합친_뒤에_상한을_센다() -> None:
    same = [_item(value="잔잔한 소설을 찾음", confidence=0.9)] * MAX_EXTRACTIONS
    other = _item(type_="topic", value="기후 위기를 다룬 책을 찾음", confidence=0.6)

    kept = screen(_out(*same, other))

    assert [c.type for c in kept] == ["mood", "topic"]
