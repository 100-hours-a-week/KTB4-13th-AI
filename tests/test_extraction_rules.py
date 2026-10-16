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
    drop_reason,
    screen,
    text_key,
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


# --- 글자가 사실상 같은 문장 (#369) ---


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param("SF 소설을 좋아함", "sf 소설을 좋아함", id="대소문자"),
        pytest.param("SF 소설을 좋아함", "SF소설을  좋아함", id="띄어쓰기"),
        pytest.param("잔잔한 소설을 찾음", "잔잔한 소설을 찾음.", id="끝의_마침표"),
        pytest.param("잔잔한 소설을 찾음!", "잔잔한 소설을 찾음…", id="끝의_문장부호"),
    ],
)
def test_대소문자_띄어쓰기_끝의_문장부호만_다르면_같은_글자다(a, b) -> None:
    assert text_key(a) == text_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param("추리 소설을 좋아함", "추리 소설은 읽지 않음", id="뜻이_반대"),
        pytest.param("조용한 산문을 좋아함", "차분한 산문을 좋아함", id="뜻만_같음"),
        pytest.param("C# 입문서를 찾음", "C 입문서를 찾음", id="글자_속_기호"),
        pytest.param("배우고 싶은 언어는 C#", "배우고 싶은 언어는 C", id="끝의_기호"),
        pytest.param("평점 3.5 이상만 읽음", "평점 35 이상만 읽음", id="숫자_속_점"),
    ],
)
def test_글자가_다르면_뜻이_가까워도_다른_글자다(a, b) -> None:
    # 뜻이 같은지는 여기서 보지 않는다. 문장 벡터로도 가를 수 없어 LLM 에게 맡긴다(#369)
    assert text_key(a) != text_key(b)


def test_한_대화_안의_중복도_띄어쓰기와_끝의_문장부호를_무시하고_합친다() -> None:
    kept = screen(
        _out(
            _item(value="잔잔한 소설을 찾음", confidence=0.6),
            _item(value="잔잔한소설을 찾음.", confidence=0.9),
        )
    )

    assert kept == [Candidate("mood", "잔잔한 소설을 찾음", 0.9)]


# --- 근거, 누구의 취향인지, 직접 말했는지 (#378) ---
#
# LLM 에게 취향마다 세 칸을 더 적게 하고 코드가 본다. 프롬프트로 "하지 마라"고만 하면 지키지 않아서다.

_SAID = ["잔잔한 소설 추천해줘", "나는 원래 시집을 좋아해"]


def _claim(**over) -> dict:
    item = {
        "type": "mood",
        "value": "잔잔한 소설을 찾음",
        "confidence": 0.7,
        "evidence": "잔잔한 소설 추천해줘",
        "owner": "user",
        "basis": "requested",
    }
    item.update(over)
    return item


def _kept(*items, said=_SAID) -> list[str]:
    return [c.value for c in screen(_out(*items), user_turns=said)]


def test_사용자가_한_말을_근거로_든_사용자의_취향은_남는다() -> None:
    assert _kept(_claim()) == ["잔잔한 소설을 찾음"]


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param("잔잔한 소설", id="일부만_옮김"),
        pytest.param("잔잔한 추천해줘", id="가운데를_줄여_옮김"),
        pytest.param("잔잔한 소설 추천해줘.", id="문장부호를_붙임"),
        pytest.param("  잔잔한   소설  ", id="띄어쓰기가_다름"),
        pytest.param("시집을 좋아해", id="다른_말에서_옮김"),
    ],
)
def test_근거는_사용자가_한_말_하나에_낱말이_모두_들어_있으면_된다(evidence) -> None:
    # 모델은 사용자의 말을 조금 줄여서 옮긴다. 글자 그대로만 받으면 맞는 취향까지 버린다
    # (실측: "쿠버네티스 처음 배우는데 입문서 추천해줘"를 "쿠버네티스 입문서 추천해줘"로 옮김)
    assert _kept(_claim(evidence=evidence)) == ["잔잔한 소설을 찾음"]


def test_근거의_낱말이_사용자의_여러_말에_흩어져_있으면_근거가_아니다() -> None:
    # "잔잔한"은 첫 말에, "시집을"은 둘째 말에 있다. 한 말에서 옮긴 것이 아니다
    assert _kept(_claim(evidence="잔잔한 시집을")) == []


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param("힐링 소설을 즐겨 읽는 분들께 인기가 많아요", id="챗봇이_한_말"),
        pytest.param("잔잔한 소설을 추천해 달라고 함", id="고쳐_쓴_말"),
        pytest.param("", id="빈_근거"),
        pytest.param("소설", id="너무_짧은_근거"),
        pytest.param(None, id="근거_없음"),
        pytest.param(["잔잔한 소설 추천해줘"], id="근거가_목록"),
    ],
)
def test_사용자가_한_말에_없는_근거를_든_취향은_버린다(evidence) -> None:
    assert _kept(_claim(evidence=evidence)) == []


def test_근거_칸이_아예_없어도_버린다() -> None:
    item = _claim()
    del item["evidence"]

    assert _kept(item) == []


@pytest.mark.parametrize("owner", ["other", "friend", "", None])
def test_사용자_본인의_취향이_아니면_버린다(owner) -> None:
    assert _kept(_claim(owner=owner)) == []


@pytest.mark.parametrize("basis", ["guessed", "", None])
def test_직접_말했는지를_적지_않았으면_버린다(basis) -> None:
    assert _kept(_claim(basis=basis)) == []


@pytest.mark.parametrize(
    "value", ["잔잔한 소설을 좋아함", "잔잔한 소설을 좋아하는 편임"]
)
def test_찾기만_했는데_좋아한다고_쓴_취향은_버린다(value) -> None:
    assert _kept(_claim(value=value, basis="requested")) == []


def test_좋아한다고_직접_말한_것은_좋아한다고_써도_남는다() -> None:
    kept = _kept(
        _claim(
            type="topic",
            value="시집을 좋아함",
            evidence="나는 원래 시집을 좋아해",
            basis="stated",
        )
    )

    assert kept == ["시집을 좋아함"]


def test_찾기만_한_취향의_문장에_좋아지다_같은_말이_있는_것은_괜찮다() -> None:
    # "좋아함"만 본다. "기분이 좋아지는"은 좋아한다는 말이 아니다
    kept = _kept(
        _claim(value="기분이 좋아지는 소설을 찾음", evidence="잔잔한 소설 추천해줘")
    )

    assert kept == ["기분이 좋아지는 소설을 찾음"]


def test_한_항목이_걸려도_나머지는_남는다() -> None:
    kept = _kept(
        _claim(value="힐링 소설을 즐겨 읽음", evidence="힐링 소설을 즐겨 읽는 분들께"),
        _claim(),
    )

    assert kept == ["잔잔한 소설을 찾음"]


def test_사용자가_한_말을_주지_않으면_세_칸을_보지_않는다() -> None:
    # 세 칸을 보는 것은 대화가 있을 때다. 형식만 거르는 쓰임(평가 묶음 검사)은 그대로다
    assert [c.value for c in screen(_out(_item()))] == ["잔잔한 소설을 찾음"]


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({}, None),
        ({"owner": "other"}, "owner"),
        ({"basis": "guessed"}, "basis"),
        ({"evidence": "챗봇이 한 말"}, "evidence"),
        ({"value": "잔잔한 소설을 좋아함"}, "unsaid_like"),
    ],
)
def test_버리는_이유를_알려_준다(over, reason) -> None:
    item = _claim(**over)

    assert drop_reason(item, _SAID) == reason
