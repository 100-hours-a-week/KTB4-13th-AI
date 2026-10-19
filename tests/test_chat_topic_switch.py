"""③ 1단계 — 대화 중 주제가 바뀌면 앞 턴의 조건을 지운다(#513, #384 문제 5).

1단계 LLM 은 "바뀌는 값만 담은 patch"를 돌려주고 서버가 앞 spec 위에 겹쳐 쓴다. 그래서 주제가 바뀌었는데
모델이 가격·제목을 null 로 지우지 않으면 앞 조건이 남는다(dev 16턴, 평가 104·105번). 주제가 바뀌었는지는
코드가 가른다: patch 가 내놓은 주제가 앞 주제와 겹치는 낱말이 하나도 없으면 새 요청이다(app/chat/topic_switch.py).

LLM 과 DB 는 가짜로 끼운다. 여기서는 갈라내는 규칙과 그 규칙이 spec·호출 횟수에 미치는 영향만 본다.
"""

import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import topic_switch as topic
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


def _after_python_intro() -> Spec:
    """104번 3턴 뒤 상태: 가격이 걸린 파이썬 입문서 요청."""
    return _spec(
        filters=SearchFilters(price_max=30000), semantic="가벼운 파이썬 입문서"
    )


# --- 새 요청인지 가르는 규칙 -------------------------------------------------


@pytest.mark.parametrize(
    ("current", "patch"),
    [
        # 104·105번: 파이썬 입문서 → 데미안 같은 책
        (_after_python_intro(), {"semantic": "데미안과 비슷한 느낌의 책"}),
        # 104번 5턴: 데미안 → 회사 일이 힘들다
        (
            _spec(semantic="데미안과 비슷한 느낌의 책"),
            {"semantic": "요즘 회사 일이 너무 힘들고 사람도 싫을 때 읽을 책"},
        ),
        # 104번 6턴: 앞 대화와 상관없는 제목 찾기
        (
            _spec(semantic="회사 일이 힘들고 아무것도 하기 싫을 때 읽을 책"),
            {"intent": "exact", "exact": {"title": "해리포터"}},
        ),
        # 57번: 위로되는 소설 → 주식 입문서
        (_spec(semantic="위로되는 소설"), {"semantic": "주식 입문서"}),
        # 제목 찾기 뒤에 분위기 요청
        (
            _spec(intent="exact", exact=SpecExact(title="토지")),
            {"intent": "semantic", "semantic": "개발자 면접 준비할 때 읽을 책"},
        ),
        # 저자 찾기 뒤에 다른 저자 찾기
        (
            _spec(intent="exact", exact=SpecExact(author="한강")),
            {"exact": {"author": "김영하"}},
        ),
    ],
)
def test_앞_주제와_겹치는_낱말이_없는_주제를_내면_새_요청이다(
    current: Spec, patch: dict
) -> None:
    assert topic.starts_new_request(current, patch) is True


@pytest.mark.parametrize(
    ("current", "patch"),
    [
        # 톤 조정: 앞 문장을 바탕으로 다시 쓴다(53번)
        (
            _spec(semantic="비 오는 날 읽을 책"),
            {"semantic": "비 오는 날 읽을 무겁지 않은 책"},
        ),
        # 조건만 말한 메시지: 주제가 patch 에 없다(55·58번)
        (_after_python_intro(), {"filters": {"price_max": 20000}}),
        (_after_python_intro(), {}),
        # 앞에 주제가 없었다: 조건만 걸어 두고 되물은 뒤 주제를 말한 경우
        (_spec(filters=SearchFilters(price_max=20000)), {"semantic": "파이썬 입문서"}),
        # 같은 책을 다시 찾음
        (
            _spec(intent="exact", exact=SpecExact(title="데미안")),
            {"exact": {"title": "데미안"}},
        ),
        # 같은 주제를 좁힘(56번)
        (_spec(semantic="에세이"), {"semantic": "진지한 에세이"}),
        # 책·추천 같은 요청 말만 있으면 주제를 모르는 것이니 판단하지 않는다
        (_after_python_intro(), {"semantic": "다른 책 추천"}),
    ],
)
def test_겹치는_낱말이_있거나_주제를_안_냈으면_새_요청이_아니다(
    current: Spec, patch: dict
) -> None:
    assert topic.starts_new_request(current, patch) is False


def test_조사가_붙어도_같은_낱말로_본다() -> None:
    # "데미안" / "데미안과" / "데미안이랑"
    current = _spec(semantic="데미안과 비슷한 느낌의 책")

    assert (
        topic.starts_new_request(current, {"semantic": "데미안이랑 같은 소설"}) is False
    )


def test_새_요청이면_조건을_비우되_이미_뺀_책은_남긴다() -> None:
    current = _spec(
        intent="exact",
        exact=SpecExact(title="토지", author="박경리", publisher="은행나무"),
        filters=SearchFilters(price_max=30000, category="소설"),
        semantic="옛 문장",
        anchor_book=77,
        exclude=[5, 6],
    )

    fresh = topic.fresh_spec(current)

    assert fresh == _spec(exclude=[5, 6])


def test_patch가_제목이나_저자를_골랐을_때만_책을_콕_집은_것으로_본다() -> None:
    assert topic.names_a_book({"exact": {"title": "해리포터"}}) is True
    assert topic.names_a_book({"exact": {"author": "한강"}}) is True
    assert topic.names_a_book({"exact": {"publisher": "민음사"}}) is False
    assert topic.names_a_book({"exact": {"title": None}}) is False
    assert topic.names_a_book({"semantic": "비 오는 날"}) is False
    assert topic.names_a_book({"exact": "해리포터"}) is False


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


def _spec_reply(**patch) -> str:
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


class _Calls:
    """가짜 모델이 받은 프롬프트와 호출 횟수. 호출마다 준비한 답을 순서대로 돌려준다."""

    def __init__(self, *replies: str | Exception) -> None:
        self.prompts: list[str] = []
        self._replies = iter(replies)

    def model(self) -> RunnableLambda:
        def _answer(prompt) -> AIMessage:
            self.prompts.append(prompt.to_string())
            reply = next(self._replies)
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


def _post(monkeypatch: pytest.MonkeyPatch, calls: _Calls, **request) -> dict:
    model = calls.model()
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)
    res = client.post("/recommendations/chat", json=_request(**request))
    assert res.status_code == 200
    return res.json()["data"]


CARDS = '{"cards": []}'


def test_주제가_바뀌면_가격이_풀리고_새_주제로_바뀐다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """105번: 2만원 이하 파이썬 입문서 → 데미안이랑 비슷한 느낌의 책. 가격이 20000으로 남던 것."""
    calls = _Calls(_spec_reply(semantic="데미안과 비슷한 느낌의 책"), CARDS)
    request_spec = {
        **INITIAL_SPEC,
        "filters": {"price_max": 20000},
        "semantic": "가벼운 파이썬 입문서",
    }

    data = _post(monkeypatch, calls, spec=request_spec)

    assert data["degraded"] is False
    assert data["spec"]["filters"]["price_max"] is None
    assert data["spec"]["semantic"] == "데미안과 비슷한 느낌의 책"
    # 분위기로 바꾼 것은 1단계를 다시 부르지 않는다: 1단계, 카드 생성 2번
    assert len(calls.prompts) == 2


def test_앞_주제와_이어지는_말이면_가격을_그대로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _Calls(_spec_reply(), CARDS)
    request_spec = {**INITIAL_SPEC, "filters": {"price_max": 20000}, "semantic": "소설"}

    data = _post(monkeypatch, calls, spec=request_spec, message="다른 거 보여줘")

    assert data["spec"]["filters"]["price_max"] == 20000
    assert data["spec"]["semantic"] == "소설"


def test_톤을_바꾸는_말은_앞_조건을_그대로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _Calls(_spec_reply(semantic="퇴근길에 읽을 무겁지 않은 소설"), CARDS)
    request_spec = {
        **INITIAL_SPEC,
        "filters": {"price_max": 20000},
        "semantic": "퇴근길에 읽을 소설",
    }

    data = _post(monkeypatch, calls, spec=request_spec, message="너무 무겁지 않은 걸로")

    assert data["spec"]["filters"]["price_max"] == 20000
    assert data["spec"]["semantic"] == "퇴근길에 읽을 무겁지 않은 소설"


def test_가격만_새로_말하면_앞_주제를_그대로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """104번 3턴: 가격은 3만원까지 괜찮아."""
    calls = _Calls(_spec_reply(filters={"price_max": 30000}), CARDS)
    request_spec = {
        **INITIAL_SPEC,
        "filters": {"price_max": 20000},
        "semantic": "가벼운 파이썬 입문서",
    }

    data = _post(
        monkeypatch, calls, spec=request_spec, message="가격은 3만원까지 괜찮아"
    )

    assert data["spec"]["filters"]["price_max"] == 30000
    assert data["spec"]["semantic"] == "가벼운 파이썬 입문서"


def test_새_주제를_메시지가_같이_말한_가격은_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _Calls(_spec_reply(semantic="요리책", filters={"price_max": 15000}), CARDS)

    data = _post(
        monkeypatch,
        calls,
        spec=_stale_spec(),
        message="만오천원 이하 요리책 추천해줘",
    )

    assert data["spec"]["semantic"] == "요리책"
    assert data["spec"]["filters"]["price_max"] == 15000


def test_이미_뺀_책은_주제가_바뀌어도_남는다(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _Calls(_spec_reply(semantic="데미안과 비슷한 느낌의 책"), CARDS)

    data = _post(monkeypatch, calls, spec=_stale_spec(exclude=[7, 8]))

    assert data["spec"]["exclude"] == [7, 8]


def test_앞_대화_탓에_제목_찾기로_잘못_본_말은_빈_조건에서_다시_뽑는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """104번 6턴: 옛 spec 이 프롬프트에 있으면 "해리포터 가튼 판타지 소설"을 제목 찾기로 봤다(8/8).

    같은 말을 빈 spec 으로 물으면 분위기 요청으로 본다(0/8). 새 요청으로 판명되고 첫 답이 제목을 골랐으면,
    옛 조건도 앞 대화도 없이 다시 뽑는다.
    """
    calls = _Calls(
        _spec_reply(intent="exact", exact={"title": "해리포터"}),
        _spec_reply(semantic="해리포터 같은 판타지 소설"),
        CARDS,
    )
    recent = [
        {"role": "user", "text": "가격은 3만원까지 괜찮아"},
        {"role": "assistant", "text": "골라봤어요."},
    ]

    data = _post(
        monkeypatch,
        calls,
        spec=_stale_spec(),
        message="해리포터 가튼 판타지 소설",
        recent_turns=recent,
    )

    spec = data["spec"]
    assert spec["intent"] == "semantic"
    assert spec["exact"]["title"] is None
    assert spec["semantic"] == "해리포터 같은 판타지 소설"
    assert spec["filters"]["price_max"] is None
    assert len(calls.prompts) == 3
    assert "30000" in calls.prompts[0]  # 첫 호출은 지금까지의 조건을 본다
    assert "30000" not in calls.prompts[1]  # 다시 뽑을 때는 보지 않는다
    assert "3만원" not in calls.prompts[1]
    assert "(최근 대화 없음)" in calls.prompts[1]


def test_새_제목을_찾는_말이면_다시_뽑아도_제목_찾기로_남는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _Calls(
        _spec_reply(intent="exact", exact={"title": "토지"}),
        _spec_reply(intent="exact", exact={"title": "토지"}),
        CARDS,
    )

    data = _post(monkeypatch, calls, spec=_stale_spec(), message="토지 찾아줘")

    assert data["spec"]["intent"] == "exact"
    assert data["spec"]["exact"]["title"] == "토지"
    assert data["spec"]["filters"]["price_max"] is None


def test_다시_뽑다_실패하면_첫_답으로_이어가되_옛_조건은_비운다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_error = openai.APIConnectionError(
        request=httpx.Request("POST", "http://localhost:11434/v1")
    )
    calls = _Calls(
        _spec_reply(intent="exact", exact={"title": "해리포터"}),
        connection_error,
        CARDS,
    )

    data = _post(
        monkeypatch, calls, spec=_stale_spec(), message="해리포터 가튼 판타지 소설"
    )

    assert data["degraded"] is False
    assert data["spec"]["filters"]["price_max"] is None
    assert data["spec"]["exact"]["title"] == "해리포터"


def test_첫_턴은_앞_조건이_없으니_다시_뽑지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _Calls(_spec_reply(intent="exact", exact={"title": "데미안"}), CARDS)

    data = _post(monkeypatch, calls, message="데미안")

    assert data["spec"]["exact"]["title"] == "데미안"
    assert len(calls.prompts) == 2


def test_앞_턴의_제목_찾기는_분위기_요청으로_바뀌면_남지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """104번 7턴. 6턴이 제목 찾기로 잘못 갔어도 7턴에서 제목이 남아 있으면 안 된다."""
    calls = _Calls(
        _spec_reply(intent="semantic", semantic="개발자 면접 준비할 때 읽을 책"), CARDS
    )
    request_spec = {
        **INITIAL_SPEC,
        "intent": "exact",
        "exact": {"title": "해리포터", "author": None, "publisher": None},
        "semantic": "해리포터 같은 판타지",
    }

    data = _post(
        monkeypatch,
        calls,
        spec=request_spec,
        message="개발자 면접 준비할 때 볼 만한 책",
    )

    assert data["spec"]["intent"] == "semantic"
    assert data["spec"]["exact"]["title"] is None


def test_exclude에_제목이_섞여_와도_degraded가_되지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """92번: "두 번째는 읽었고 첫 번째는 별로야. 다른 거". 모델이 책 id 를 몰라 exclude 에 글자를 넣었다."""
    calls = _Calls(_spec_reply(exclude=["데미안", 3]), CARDS)
    request_spec = {**INITIAL_SPEC, "semantic": "자기계발 책", "exclude": [1]}

    data = _post(
        monkeypatch,
        calls,
        spec=request_spec,
        message="두 번째는 읽었고 첫 번째는 별로야. 다른 거",
        recent_turns=[
            {"role": "user", "text": "자기계발 책 추천해줘"},
            {"role": "assistant", "text": "골라봤어요."},
        ],
    )

    assert data["degraded"] is False
    assert data["spec"]["exclude"] == [1, 3]
