"""③ 1단계 — 대화 중 새 요청이 시작되면 앞 턴의 조건을 코드가 비운다(#513, #384 문제 5).

1단계 LLM 은 "바뀌는 값만 담은 patch"를 돌려주고 서버가 앞 spec 위에 겹쳐 쓴다. 그래서 주제가 바뀌었는데
모델이 가격·제목을 null 로 지우지 않으면 앞 조건이 남는다(dev 16턴, 평가 104·105번).

새 요청인지는 둘이 모두 맞아야 한다. 모델이 이전 요청과 이번 메시지만 보고 "새 책 찾기"라고 판정하고, 조건도
앞 대화도 없이 같은 메시지로 다시 뽑은 patch 가 스스로 주제 낱말을 담고 있어야 한다(app/chat/topic_switch.py).
그러면 그 patch 를 앞 조건을 비운 spec 위에 쓴다.

LLM 과 DB 는 가짜로 끼운다. 한 요청에서 LLM 이 "새 요청 판정", "1단계(spec 갱신)", "다시 뽑기", "카드 생성"으로
불리는데, 판정과 1단계는 나란히 나가 순서가 정해져 있지 않다. 그래서 가짜 모델은 프롬프트를 보고 어느 호출인지
가려 그 호출 몫의 답을 순서대로 돌려준다.
"""

import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import topic_switch
from app.chat.schemas import Spec, SpecExact
from app.main import app
from app.routers import chat
from app.search.schemas import SearchFilters

client = TestClient(app)

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}


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


# --- 주제가 있는 patch인지 ----------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        {"semantic": "데미안과 비슷한 느낌의 책"},
        {"semantic": "주식 입문서"},
        {"intent": "exact", "exact": {"title": "해리포터"}},
        {"exact": {"author": "한강"}},
        {"semantic": "토지"},
        {"semantic": "데미안이랑"},
    ],
)
def test_주제_낱말을_담은_patch는_주제가_있다(patch: dict) -> None:
    assert topic_switch.has_topic(patch) is True


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"filters": {"price_max": None}},
        {"filters": {"price_max": 30000}},
        {"semantic": "다른 책"},
        {"semantic": "책 추천해줘"},
        {"semantic": None},
        {"semantic": 123},
        {"exact": "해리포터"},
        {"exact": {"title": None, "publisher": "민음사"}},
    ],
)
def test_조건이나_요청_말뿐인_patch는_주제가_없다(patch: dict) -> None:
    assert topic_switch.has_topic(patch) is False


# --- 앞 요청 한 줄 ------------------------------------------------------------


def test_앞_요청은_찾던_주제와_걸린_조건을_한_줄로_적는다() -> None:
    spec = _spec(
        semantic="가벼운 파이썬 입문서",
        filters=SearchFilters(price_max=30000, pub_year_from=2020, category="IT"),
    )

    assert topic_switch.previous_request(spec) == (
        "찾는 것: 가벼운 파이썬 입문서 / 조건: 가격 30000원 이하, 2020년 이후 출간, 분야 IT"
    )


def test_앞_요청이_제목_찾기였으면_제목을_적는다() -> None:
    spec = _spec(intent="exact", exact=SpecExact(title="데미안", author="헤세"))

    assert topic_switch.previous_request(spec) == "찾는 것: 데미안 헤세"


def test_찾던_주제가_없으면_앞_요청이_없다() -> None:
    # 가격만 걸어 두고 되물은 뒤 주제를 말한 경우("2만원 이하 책 추천해줘" → "파이썬 입문서")에 가격을 지우면 안 된다
    assert topic_switch.previous_request(_spec()) is None
    assert (
        topic_switch.previous_request(_spec(filters=SearchFilters(price_max=20000)))
        is None
    )
    assert topic_switch.previous_request(_spec(exclude=[1, 2])) is None


def test_새_요청이면_조건을_비우되_이미_뺀_책은_남긴다() -> None:
    current = _spec(
        intent="exact",
        exact=SpecExact(title="토지", author="박경리", publisher="은행나무"),
        filters=SearchFilters(price_max=30000, category="소설"),
        semantic="옛 문장",
        anchor_book=77,
        exclude=[5, 6],
    )

    assert topic_switch.fresh_spec(current) == _spec(exclude=[5, 6])


def test_판정_프롬프트는_중괄호가_깨지지_않고_채워진다() -> None:
    text = topic_switch.RELATION_PROMPT.format_messages(
        previous="찾는 것: 파이썬 입문서", message="데미안 같은 책"
    )[0].content

    assert "찾는 것: 파이썬 입문서" in text
    assert '"데미안 같은 책"' in text
    assert '{"relation": "same"}' in text
    assert '{"relation": "new"}' in text


# --- patch 병합 ---------------------------------------------------------------


def test_exclude에_숫자가_아닌_값이_오면_버리고_숫자만_더한다() -> None:
    # 92번: 모델이 "첫 번째 책"의 id 를 몰라 제목 같은 글자를 넣으면 Spec 검증이 깨져 degraded 가 됐다(#507에서 관찰)
    current = _spec(exclude=[1])

    merged = chat._merge_spec_patch(
        current, {"exclude": ["데미안", 2, True, 2.5, None, 3]}
    )

    assert merged["exclude"] == [1, 2, 3]


def test_분위기_요청으로_바뀌면_앞_턴의_제목과_저자는_남기지_않는다() -> None:
    # 104번 7턴: intent 는 semantic 인데 6턴의 exact.title 이 그대로 남았다. 출판사는 분위기 요청에도 걸 수 있다.
    current = _spec(
        intent="exact",
        exact=SpecExact(title="해리포터", author="롤링", publisher="문학수첩"),
    )

    merged = chat._merge_spec_patch(
        current, {"intent": "semantic", "semantic": "개발자 면접 책"}
    )

    assert merged["intent"] == "semantic"
    assert merged["exact"] == {"title": None, "author": None, "publisher": "문학수첩"}


def test_분위기_요청이어도_이번_patch가_고른_제목은_그대로_둔다() -> None:
    # 기존 계약(test_chat_spec_merge): intent 를 직접 준 경우는 LLM 판단을 따른다
    merged = chat._merge_spec_patch(
        _spec(),
        {"intent": "semantic", "exact": {"title": "해리포터"}, "semantic": "마법"},
    )

    assert merged["exact"]["title"] == "해리포터"


def test_intent가_exact인_채로_이어지면_제목을_안_지운다() -> None:
    current = _spec(intent="exact", exact=SpecExact(title="데미안"))

    merged = chat._merge_spec_patch(current, {"exclude": [9]})

    assert merged["exact"]["title"] == "데미안"


# --- 서버 전체 경로(가짜 LLM) ---------------------------------------------------


def _reply(**patch) -> str:
    return json.dumps(patch, ensure_ascii=False)


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": "데미안이랑 비슷한 느낌의 책 있어?",
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
    }
    body.update(overrides)
    return body


def _stale_spec(**overrides) -> dict:
    """가격 3만원이 걸린 채 회사 일이 힘들다는 주제가 남은 상태(104번 5턴 뒤)."""
    return {
        **INITIAL_SPEC,
        "filters": {"price_max": 30000},
        "semantic": "요즘 회사 일이 너무 힘들고 아무것도 하기 싫을 때 읽을 책",
        **overrides,
    }


CARDS = '{"cards": []}'
CONNECTION_ERROR = openai.APIConnectionError(
    request=httpx.Request("POST", "http://localhost:11434/v1")
)


class _Llm:
    """가짜 모델. 프롬프트로 호출 종류를 가려 종류마다 준비한 답을 순서대로 돌려준다."""

    def __init__(
        self,
        *,
        relation: str | Exception = "same",
        specs: tuple[str | Exception, ...] = (),
    ) -> None:
        self.prompts: dict[str, list[str]] = {"relation": [], "spec": [], "cards": []}
        self._relation = (
            relation
            if isinstance(relation, Exception)
            else json.dumps({"relation": relation})
        )
        self._specs = iter(specs)

    @staticmethod
    def _kind(text: str) -> str:
        if "대화 분석기" in text:
            return "relation"
        if "조건(spec) 갱신기" in text:
            return "spec"
        return "cards"

    def model(self) -> RunnableLambda:
        def _answer(prompt) -> AIMessage:
            text = prompt.to_string()
            kind = self._kind(text)
            self.prompts[kind].append(text)
            if kind == "relation":
                reply = self._relation
            elif kind == "spec":
                reply = next(self._specs)
            else:
                reply = CARDS
            if isinstance(reply, Exception):
                raise reply
            return AIMessage(content=reply)

        return RunnableLambda(_answer)


@pytest.fixture(autouse=True)
def fake_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(spec, exclude_book_ids, user_id, owned_book_ids=None) -> list[dict]:
        return [
            {
                "book_id": 1088,
                "title": "달러구트 꿈 백화점",
                "author": "이미예",
                "description": "잠든 사이 꿈을 사고파는 상점 이야기.",
                "match_score": 25,
                "popularity": 3.0,
            }
        ]

    monkeypatch.setattr(chat, "get_candidates", _fake)


def _post(monkeypatch: pytest.MonkeyPatch, llm: _Llm, **request) -> dict:
    model = llm.model()
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)
    res = client.post("/recommendations/chat", json=_request(**request))
    assert res.status_code == 200
    return res.json()["data"]


def test_새_요청이면_앞_조건이_풀리고_빈_조건에서_다시_뽑은_답을_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """105번: 2만원 이하 파이썬 입문서 → 데미안이랑 비슷한 느낌의 책. 가격이 17500으로 남던 것."""
    llm = _Llm(
        relation="new",
        specs=(
            _reply(
                semantic="요즘 힘든 일상에 읽을 가벼운 책"
            ),  # 옛 spec 에 끌려 베낀 첫 답
            _reply(semantic="데미안과 비슷한 느낌의 책"),
        ),
    )
    request_spec = {
        **INITIAL_SPEC,
        "filters": {"price_max": 17500},
        "semantic": "가벼운 파이썬 입문서",
        "exclude": [7],
    }

    data = _post(monkeypatch, llm, spec=request_spec)

    assert data["degraded"] is False
    assert data["spec"]["filters"]["price_max"] is None
    assert data["spec"]["semantic"] == "데미안과 비슷한 느낌의 책"
    assert data["spec"]["exclude"] == [7]  # 이미 뺀 책은 주제가 바뀌어도 남는다
    # 판정 1, 1단계 1, 다시 뽑기 1: 1단계가 두 번 불린다
    assert len(llm.prompts["relation"]) == 1
    assert len(llm.prompts["spec"]) == 2
    assert "17500" in llm.prompts["spec"][0]  # 첫 호출은 지금까지의 조건을 본다
    assert "17500" not in llm.prompts["spec"][1]  # 다시 뽑을 때는 보지 않는다
    assert "가벼운 파이썬 입문서" not in llm.prompts["spec"][1]
    assert "(최근 대화 없음)" in llm.prompts["spec"][1]
    # 판정은 이전 요청과 이번 메시지만 본다
    assert "가격 17500원 이하" in llm.prompts["relation"][0]
    assert "데미안이랑 비슷한" in llm.prompts["relation"][0]


def test_앞_대화_탓에_제목_찾기로_잘못_본_말은_빈_조건에서_분위기_요청으로_다시_뽑는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """104번 6턴: 옛 spec 이 프롬프트에 있으면 "해리포터 가튼 판타지 소설"을 제목 찾기로 봤다(8/8). 빈 spec 이면 0/8."""
    llm = _Llm(
        relation="new",
        specs=(
            _reply(intent="exact", exact={"title": "해리포터"}),
            _reply(semantic="해리포터 같은 판타지 소설"),
        ),
    )
    recent = [
        {"role": "user", "text": "가격은 3만원까지 괜찮아"},
        {"role": "assistant", "text": "골라봤어요."},
    ]

    data = _post(
        monkeypatch,
        llm,
        spec=_stale_spec(),
        message="해리포터 가튼 판타지 소설",
        recent_turns=recent,
    )

    spec = data["spec"]
    assert spec["intent"] == "semantic"
    assert spec["exact"]["title"] is None
    assert spec["semantic"] == "해리포터 같은 판타지 소설"
    assert spec["filters"]["price_max"] is None
    assert "3만원" not in llm.prompts["spec"][1]  # 최근 대화도 안 본다


def test_새_요청이_같이_말한_가격은_남긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _Llm(
        relation="new",
        specs=(
            _reply(semantic="요리책"),
            _reply(semantic="요리책", filters={"price_max": 15000}),
        ),
    )

    data = _post(
        monkeypatch, llm, spec=_stale_spec(), message="만오천원 이하 요리책 추천해줘"
    )

    assert data["spec"]["semantic"] == "요리책"
    assert data["spec"]["filters"]["price_max"] == 15000


def test_새_요청으로_판정해도_다시_뽑은_답에_주제가_없으면_이어가는_말이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "2만원 이하 아니어도 돼"를 새 요청으로 본 판정(5번 중 5번)이 주제를 지우면 안 된다."""
    llm = _Llm(
        relation="new",
        specs=(
            _reply(filters={"price_max": None}),
            _reply(semantic="다른 책"),
        ),
    )
    request_spec = {
        **INITIAL_SPEC,
        "filters": {"price_max": 20000},
        "semantic": "퇴근길에 가볍게 읽을 소설",
    }

    data = _post(monkeypatch, llm, spec=request_spec, message="2만원 이하 아니어도 돼")

    assert data["spec"]["semantic"] == "퇴근길에 가볍게 읽을 소설"
    assert (
        data["spec"]["filters"]["price_max"] is None
    )  # 첫 답이 가격을 푼 것은 반영된다


def test_이어가는_말이면_가격을_그대로_두고_다시_뽑지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _Llm(relation="same", specs=(_reply(),))
    request_spec = {**INITIAL_SPEC, "filters": {"price_max": 20000}, "semantic": "소설"}

    data = _post(monkeypatch, llm, spec=request_spec, message="다른 거 보여줘")

    assert data["spec"]["filters"]["price_max"] == 20000
    assert data["spec"]["semantic"] == "소설"
    assert len(llm.prompts["spec"]) == 1


def test_톤을_바꾸는_말은_앞_조건을_그대로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _Llm(
        relation="same", specs=(_reply(semantic="퇴근길에 읽을 무겁지 않은 소설"),)
    )
    request_spec = {
        **INITIAL_SPEC,
        "filters": {"price_max": 20000},
        "semantic": "퇴근길에 읽을 소설",
    }

    data = _post(monkeypatch, llm, spec=request_spec, message="너무 무겁지 않은 걸로")

    assert data["spec"]["filters"]["price_max"] == 20000
    assert data["spec"]["semantic"] == "퇴근길에 읽을 무겁지 않은 소설"


def test_가격만_새로_말하면_앞_주제를_그대로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """104번 3턴: 가격은 3만원까지 괜찮아."""
    llm = _Llm(relation="same", specs=(_reply(filters={"price_max": 30000}),))
    request_spec = {
        **INITIAL_SPEC,
        "filters": {"price_max": 20000},
        "semantic": "가벼운 파이썬 입문서",
    }

    data = _post(monkeypatch, llm, spec=request_spec, message="가격은 3만원까지 괜찮아")

    assert data["spec"]["filters"]["price_max"] == 30000
    assert data["spec"]["semantic"] == "가벼운 파이썬 입문서"


def test_앞_요청에_주제가_없으면_판정을_부르지_않고_가격을_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "2만원 이하 책 추천해줘"에 되물은 뒤 "파이썬 입문서"라고 하면 가격을 지우면 안 된다."""
    llm = _Llm(specs=(_reply(semantic="파이썬 입문서"),))
    request_spec = {**INITIAL_SPEC, "filters": {"price_max": 20000}}

    data = _post(monkeypatch, llm, spec=request_spec, message="파이썬 입문서")

    assert data["spec"]["filters"]["price_max"] == 20000
    assert data["spec"]["semantic"] == "파이썬 입문서"
    assert llm.prompts["relation"] == []


def test_첫_턴은_판정도_다시_뽑기도_없어_호출_수가_그대로다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _Llm(specs=(_reply(intent="exact", exact={"title": "데미안"}),))

    data = _post(monkeypatch, llm, message="데미안")

    assert data["spec"]["exact"]["title"] == "데미안"
    assert llm.prompts["relation"] == []
    assert len(llm.prompts["spec"]) == 1


def test_판정이_실패하면_이어가는_말로_보고_degraded가_되지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _Llm(relation=CONNECTION_ERROR, specs=(_reply(),))
    request_spec = {**INITIAL_SPEC, "filters": {"price_max": 20000}, "semantic": "소설"}

    data = _post(monkeypatch, llm, spec=request_spec, message="다른 거 보여줘")

    assert data["degraded"] is False
    assert data["spec"]["filters"]["price_max"] == 20000


def test_다시_뽑다_실패하면_첫_답을_앞_조건_위에_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 앞 조건을 남기는 쪽이 고치기 전과 같은 동작이다
    llm = _Llm(
        relation="new",
        specs=(_reply(semantic="데미안과 비슷한 느낌의 책"), CONNECTION_ERROR),
    )

    data = _post(monkeypatch, llm, spec=_stale_spec())

    assert data["degraded"] is False
    assert data["spec"]["semantic"] == "데미안과 비슷한 느낌의 책"
    assert data["spec"]["filters"]["price_max"] == 30000


def test_1단계가_실패하면_판정과_상관없이_degraded다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _Llm(relation="new", specs=(CONNECTION_ERROR,))

    data = _post(monkeypatch, llm, spec=_stale_spec())

    assert data["degraded"] is True
    assert data["spec"]["filters"]["price_max"] == 30000  # 요청의 spec 그대로


def test_앞_턴의_제목_찾기는_분위기_요청으로_바뀌면_남지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """104번 7턴. 6턴이 제목 찾기로 잘못 갔어도 7턴에서 제목이 남아 있으면 안 된다."""
    llm = _Llm(
        relation="same",
        specs=(_reply(intent="semantic", semantic="해리포터 같은 판타지"),),
    )
    request_spec = {
        **INITIAL_SPEC,
        "intent": "exact",
        "exact": {"title": "해리포터", "author": None, "publisher": None},
        "semantic": "해리포터 같은 판타지",
    }

    data = _post(monkeypatch, llm, spec=request_spec, message="비슷한 판타지로 보여줘")

    assert data["spec"]["intent"] == "semantic"
    assert data["spec"]["exact"]["title"] is None


def test_exclude에_제목이_섞여_와도_degraded가_되지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """92번: "두 번째는 읽었고 첫 번째는 별로야. 다른 거". 모델이 책 id 를 몰라 exclude 에 글자를 넣었다."""
    llm = _Llm(relation="same", specs=(_reply(exclude=["데미안", 3]),))
    request_spec = {**INITIAL_SPEC, "semantic": "자기계발 책", "exclude": [1]}

    data = _post(
        monkeypatch,
        llm,
        spec=request_spec,
        message="두 번째는 읽었고 첫 번째는 별로야. 다른 거",
        recent_turns=[
            {"role": "user", "text": "자기계발 책 추천해줘"},
            {"role": "assistant", "text": "골라봤어요."},
        ],
    )

    assert data["degraded"] is False
    assert data["spec"]["exclude"] == [1, 3]
