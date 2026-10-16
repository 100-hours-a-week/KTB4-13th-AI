"""⑤ LLM 이 낸 취향 후보를 거르는 규칙(app/extraction/rules.py) 테스트(#367). LLM 없이 돈다.

여기서 지키려는 것: LLM 이 무엇을 내든 응답에 나갈 수 있는 것은 명세 ⑤의 모양을 지킨 후보뿐이고,
틀린 항목 하나 때문에 맞는 항목까지 버려지지 않는다.
"""

import pytest

from app.extraction.rules import (
    MAX_EXTRACTIONS,
    MAX_KEYWORD_CHARS,
    MAX_VALUE_CHARS,
    Candidate,
    drop_reason,
    screen,
    text_key,
)


def _item(
    type_="mood", value="잔잔한 소설을 찾음", strength=1, keyword="잔잔한", **extra
) -> dict:
    return {
        "type": type_,
        "value": value,
        "strength": strength,
        "keyword": keyword,
        **extra,
    }


def _out(*items) -> dict:
    return {"extractions": list(items)}


def _c(
    type_="mood",
    value="잔잔한 소설을 찾음",
    strength=1,
    applies_to=(),
    keyword="잔잔한",
) -> Candidate:
    return Candidate(type_, value, strength, applies_to, keyword)


# --- 통과 ---


def test_모양이_맞는_후보는_그대로_나온다() -> None:
    assert screen(_out(_item())) == [_c()]


@pytest.mark.parametrize("type_", ["mood", "topic", "author", "condition"])
def test_종류는_명세의_네_가지를_받는다(type_) -> None:
    assert [c.type for c in screen(_out(_item(type_=type_)))] == [type_]


def test_모르는_칸이_더_있어도_후보는_받는다() -> None:
    # LLM 이 근거 문장 같은 칸을 덧붙여도 쓰는 칸만 쓴다
    kept = screen(_out(_item(reason="사용자가 두 번 말함")))

    assert kept == [_c()]


def test_문장과_핵심어의_앞뒤_공백과_줄바꿈은_정리한다() -> None:
    # 취향 카드에 그대로 보이는 글이라 한 줄로 만든다
    kept = screen(
        _out(_item(value="  잔잔한\n소설을   찾음 ", keyword=" 잔잔한\n 소설 "))
    )

    assert [(c.value, c.keyword) for c in kept] == [
        ("잔잔한 소설을 찾음", "잔잔한 소설")
    ]


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


def _without(key: str) -> dict:
    item = _item()
    del item[key]
    return item


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("잔잔한 소설", id="항목이_글자"),
        pytest.param(None, id="항목이_null"),
        pytest.param(_without("type"), id="종류_없음"),
        pytest.param(_item(type_="genre"), id="모르는_종류"),
        pytest.param(_item(type_="Mood"), id="대문자_종류"),
        pytest.param(_item(type_=1), id="숫자_종류"),
        pytest.param(_without("value"), id="문장_없음"),
        pytest.param(_item(value=""), id="빈_문장"),
        pytest.param(_item(value="  \n "), id="공백뿐인_문장"),
        pytest.param(_item(value=["잔잔한 소설"]), id="문장이_목록"),
        pytest.param(_item(value="가" * (MAX_VALUE_CHARS + 1)), id="문장이_너무_김"),
        pytest.param(_item(value="잔잔한\x00소설"), id="문장에_NUL"),
        pytest.param(_item(value="잔잔한 \ud800"), id="문장에_깨진_글자"),
        pytest.param(_without("strength"), id="세기_없음"),
        pytest.param(_item(strength="1"), id="세기가_글자"),
        pytest.param(_item(strength=True), id="세기가_참거짓"),
        pytest.param(_item(strength=None), id="세기가_null"),
        pytest.param(_item(strength=1.0), id="세기가_소수"),
        pytest.param(_item(strength=0), id="세기가_0"),
        pytest.param(_item(strength=4), id="세기가_3_초과"),
        pytest.param(_item(strength=-4), id="세기가_-3_미만"),
        pytest.param(_without("keyword"), id="핵심어_없음"),
        pytest.param(_item(keyword=""), id="빈_핵심어"),
        pytest.param(_item(keyword="  "), id="공백뿐인_핵심어"),
        pytest.param(_item(keyword=["잔잔한"]), id="핵심어가_목록"),
        pytest.param(
            _item(keyword="가" * (MAX_KEYWORD_CHARS + 1)), id="핵심어가_너무_김"
        ),
        pytest.param(_item(keyword="잔잔\x00"), id="핵심어에_NUL"),
    ],
)
def test_깨진_항목만_버리고_나머지는_살린다(bad) -> None:
    good = _item(
        type_="topic",
        value="기후 위기를 다룬 책을 찾음",
        strength=2,
        keyword="기후 위기",
    )

    kept = screen(_out(bad, good))

    assert kept == [_c("topic", "기후 위기를 다룬 책을 찾음", 2, (), "기후 위기")]


def test_문장은_딱_200자까지_받는다() -> None:
    value = "가" * MAX_VALUE_CHARS

    assert [c.value for c in screen(_out(_item(value=value)))] == [value]


def test_긴_문장은_잘라서_받지_않는다() -> None:
    # 잘린 문장이 취향 카드로 보이면 뜻이 달라진다
    assert screen(_out(_item(value="가" * (MAX_VALUE_CHARS + 1)))) == []


def test_핵심어는_딱_100자까지_받는다() -> None:
    keyword = "가" * MAX_KEYWORD_CHARS

    assert [c.keyword for c in screen(_out(_item(keyword=keyword)))] == [keyword]


# --- 세기 (#426) ---


@pytest.mark.parametrize("strength", [-3, -2, 1, 2, 3])
def test_세기는_명세의_범위에서_0을_뺀_정수를_받는다(strength) -> None:
    assert [c.strength for c in screen(_out(_item(strength=strength)))] == [strength]


def test_피함의_부탁_세기_마이너스_1은_응답에서_뺀다() -> None:
    # "이번엔 ○○ 말고"는 싫다는 말이 아니다. 추천을 바꾸지 않는 줄이 화면에 보이면 헷갈린다(#395)
    kept = screen(
        _out(
            _item(
                type_="topic", value="로맨스는 빼고 찾음", strength=-1, keyword="로맨스"
            ),
            _item(),
        )
    )

    assert kept == [_c()]


@pytest.mark.parametrize(
    ("strength", "basis", "expected"),
    [
        (3, "requested", 1),  # 부탁만 했으면 1
        (-2, "requested", None),  # 싫은 쪽을 부탁만 했으면 -1 이라 뺀다
        (1, "stated", 2),  # 직접 말했으면 2 이상
        (-1, "stated", -2),
        (3, "stated", 3),
        (2, None, 2),  # 말한 방식을 모르면 LLM 이 적은 그대로
    ],
)
def test_세기의_크기는_직접_말했는지에_맞춘다(strength, basis, expected) -> None:
    kept = screen(_out(_item(strength=strength, basis=basis)))

    assert [c.strength for c in kept] == ([] if expected is None else [expected])


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("나는 무협 진짜 좋아해", 3),
        ("그 작가 팬이라서 다 읽어", 3),
        ("무협 좋아해", 2),
        ("팬데믹 이야기를 좋아해", 2),  # "팬데믹"의 "팬"은 팬이 아니다
    ],
)
def test_직접_말한_취향의_근거에_세기를_높이는_말이_있으면_세기는_3이다(
    evidence, expected
) -> None:
    kept = screen(_out(_item(strength=2, basis="stated", evidence=evidence)))

    assert [c.strength for c in kept] == [expected]


# --- 어디에 (#426) ---


def _topic(applies_to, keyword="추리 소설") -> dict:
    return _item(
        type_="topic",
        value="추리 소설을 찾음",
        keyword=keyword,
        applies_to=applies_to,
    )


def test_어디에는_목록의_이름만_남기고_앞의_세_개까지다() -> None:
    names = ["추리/스릴러", "없는 이름", "추리/스릴러", "소설", "SF", "판타지"]

    kept = screen(_out(_topic(names)))

    assert [c.applies_to for c in kept] == [("추리/스릴러", "소설", "SF")]


def test_어디에는_띄어쓰기와_문장부호가_조금_달라도_목록의_이름으로_바꾼다() -> None:
    kept = screen(_out(_topic(["추리 / 스릴러", "sf", "만화, 라이트노벨"])))

    assert [c.applies_to for c in kept] == [("추리/스릴러", "SF", "만화·라이트노벨")]


@pytest.mark.parametrize(
    "applies_to",
    [
        pytest.param("소설", id="목록이_아님"),
        pytest.param(None, id="null"),
        pytest.param([1, None], id="이름이_글자가_아님"),
        pytest.param(["없는 이름"], id="모두_목록_밖"),
    ],
)
def test_어디에가_틀려도_취향은_남기고_어디에만_비운다(applies_to) -> None:
    # 이름이 틀려도 취향 자체는 맞을 수 있다. 비면 핵심어로 찾는다(#395)
    assert [c.applies_to for c in screen(_out(_topic(applies_to)))] == [()]


def test_주제가_아니면_어디에를_비운다() -> None:
    kept = screen(_out(_item(applies_to=["소설"])))

    assert [c.applies_to for c in kept] == [()]


def test_제목만_말한_취향은_어디에를_비운다() -> None:
    # 제목만으로 장르를 짐작하지 않는다(명세 ⑤)
    kept = screen(_out(_topic(["소설"], keyword="『데미안』")))

    assert [c.applies_to for c in kept] == [()]


# --- 핵심어 (#426) ---


@pytest.mark.parametrize(
    "keyword", ["「데미안」", "《데미안》", "〈데미안〉", "<데미안>"]
)
def test_다른_괄호로_감싼_제목은_겹낫표로_바꾸고_어디에를_비운다(keyword) -> None:
    kept = screen(_out(_topic(["소설"], keyword=keyword)))

    assert [(c.keyword, c.applies_to) for c in kept] == [("『데미안』", ())]


def test_괄호가_한쪽만_있으면_제목으로_보지_않는다() -> None:
    kept = screen(_out(_topic(["소설"], keyword="<데미안")))

    assert [(c.keyword, c.applies_to) for c in kept] == [("<데미안", ("소설",))]


# --- 한 대화 안의 중복 ---


def test_종류와_핵심어가_같으면_하나로_합치고_세기는_크기가_큰_쪽을_남긴다() -> None:
    kept = screen(
        _out(
            _item(value="잔잔한 소설을 찾음", strength=1),
            _item(value="잔잔한 소설을 좋아함", strength=2),
        )
    )

    # 문장은 먼저 나온 것을 남긴다
    assert kept == [_c(strength=2)]


def test_핵심어의_대소문자와_띄어쓰기만_달라도_같은_것으로_본다() -> None:
    kept = screen(
        _out(
            _item(type_="topic", value="SF 소설을 찾음", keyword="SF 소설"),
            _item(type_="topic", value="sf 소설을 또 찾음", keyword="sf소설"),
        )
    )

    assert [(c.value, c.keyword) for c in kept] == [("SF 소설을 찾음", "SF 소설")]


def test_핵심어가_같아도_종류가_다르면_따로_둔다() -> None:
    kept = screen(
        _out(
            _item(type_="topic", value="김초엽", keyword="김초엽"),
            _item(type_="author", value="김초엽", keyword="김초엽"),
        )
    )

    assert {(c.type, c.keyword) for c in kept} == {
        ("topic", "김초엽"),
        ("author", "김초엽"),
    }


# --- 개수 상한과 순서 ---


def test_세기의_크기가_큰_순으로_나온다() -> None:
    kept = screen(
        _out(
            _item(keyword="가", strength=1),
            _item(keyword="나", strength=-3),
            _item(keyword="다", strength=2),
        )
    )

    assert [c.keyword for c in kept] == ["나", "다", "가"]


def test_세기의_크기가_같으면_LLM이_낸_순서를_지킨다() -> None:
    kept = screen(_out(*[_item(keyword=k, strength=2) for k in "가나다"]))

    assert [c.keyword for c in kept] == ["가", "나", "다"]


def test_상한을_넘으면_세기의_크기가_큰_것만_남긴다() -> None:
    items = [
        _item(keyword=f"취향 {n}", strength=3 if n >= MAX_EXTRACTIONS else 1)
        for n in range(MAX_EXTRACTIONS + 3)
    ]

    kept = screen(_out(*items))

    assert len(kept) == MAX_EXTRACTIONS
    assert [c.strength for c in kept[:3]] == [3, 3, 3]


def test_버려진_항목은_상한_자리를_차지하지_않는다() -> None:
    # 세기가 틀렸거나 피함의 부탁이라 빠진 항목이 앞에 많아도, 맞는 항목이 상한 때문에 밀리지 않는다
    junk = [_item(keyword=f"빠짐 {n}", strength=-1) for n in range(MAX_EXTRACTIONS)]
    good = [_item(keyword=f"맞음 {n}") for n in range(MAX_EXTRACTIONS)]

    kept = screen(_out(*junk, *good))

    assert [c.keyword for c in kept] == [f"맞음 {n}" for n in range(MAX_EXTRACTIONS)]


def test_중복은_합친_뒤에_상한을_센다() -> None:
    same = [_item(strength=2)] * MAX_EXTRACTIONS
    other = _item(
        type_="topic", value="기후 위기를 다룬 책을 찾음", keyword="기후 위기"
    )

    kept = screen(_out(*same, other))

    assert [c.type for c in kept] == ["mood", "topic"]


# --- 글자가 사실상 같은 말 (#369) ---


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
    # 뜻이 같은지는 여기서 보지 않는다(#369)
    assert text_key(a) != text_key(b)


def test_한_대화_안의_중복도_띄어쓰기와_끝의_문장부호를_무시하고_합친다() -> None:
    kept = screen(
        _out(
            _item(keyword="잔잔한 소설", strength=1),
            _item(keyword="잔잔한소설.", strength=2),
        )
    )

    assert [(c.keyword, c.strength) for c in kept] == [("잔잔한 소설", 2)]


# --- 근거, 누구의 취향인지, 직접 말했는지 (#378) ---
#
# LLM 에게 취향마다 세 칸을 더 적게 하고 코드가 본다. 프롬프트로 "하지 마라"고만 하면 지키지 않아서다.

_SAID = ["잔잔한 소설 추천해줘", "나는 원래 시집을 좋아해"]


def _claim(**over) -> dict:
    item = {
        "type": "mood",
        "value": "잔잔한 소설을 찾음",
        "strength": 1,
        "keyword": "잔잔한",
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
