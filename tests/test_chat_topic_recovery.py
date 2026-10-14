"""③ 1단계가 조건만 뽑고 주제를 빠뜨렸을 때(#359 원인 2) 메시지에서 주제를 되찾는다.

"2020년 이후에 나온 인공지능 책 추천해줘"에서 1단계 LLM 이 {"filters": {"pub_year_from": 2020}}만 내고
semantic 을 비우는 일이 있다(평가 38번: 같은 질문을 5번 돌려 1~2번, 프롬프트를 바꾼 실험에서는 4~5번).
검색 문장이 비면 후보를 찾지 않고 "어떤 분야가 좋으세요?"로 되물어 카드가 0장이 된다.

규칙: 이번 메시지에 조건(가격·출간연도)이 있고 검색 문장·제목·저자가 모두 비었으면, 메시지에서 조건과
요청 말을 뺀 나머지 낱말을 검색 문장으로 쓴다. 나머지가 없으면("2만원 이하 책 추천해줘") 그대로 되묻는다.
"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat.schemas import Spec
from app.main import app
from app.routers import chat

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
    body = {**INITIAL_SPEC, **overrides}
    return Spec.model_validate(body)


@pytest.mark.parametrize(
    ("message", "filters", "topic"),
    [
        (
            "2020년 이후에 나온 인공지능 책 추천해줘",
            {"pub_year_from": 2020},
            "인공지능",
        ),
        ("2만원 이하 파이썬 책 추천해줘", {"price_max": 20000}, "파이썬"),
        (
            "300쪽 안 넘는 자바 입문서 추천해줘",
            {"price_max": 300},
            "자바 입문서",
        ),
        ("1만원대로 살 수 있는 쿠버네티스 책", {"price_max": 19999}, "쿠버네티스"),
        ("3D프린터 책 2020년 이후로 보여줘", {"pub_year_from": 2020}, "3D프린터"),
        (
            "1984 같은 디스토피아 소설 2015년 이후",
            {"pub_year_from": 2015},
            "1984 같은 디스토피아 소설",
        ),
    ],
)
def test_조건만_있고_검색_문장이_비면_메시지에서_주제를_되찾는다(
    message: str, filters: dict, topic: str
) -> None:
    recovered = chat._recover_topic(_spec(filters=filters), message)

    assert recovered.semantic == topic
    assert recovered.filters == _spec(filters=filters).filters  # 조건은 그대로


@pytest.mark.parametrize(
    ("message", "filters"),
    [
        ("2만원 이하 책 추천해줘", {"price_max": 20000}),
        ("2020년 이후에 나온 책 추천해줘", {"pub_year_from": 2020}),
        ("300쪽 안 넘는 책 보여줘", {"price_max": 300}),
        ("저렴한 책 좀 추천해줘", {"price_max": 15000}),
    ],
)
def test_메시지에_주제가_남지_않으면_그대로_둔다(message: str, filters: dict) -> None:
    spec = _spec(filters=filters)

    assert chat._recover_topic(spec, message) == spec


def test_조건이_없으면_건드리지_않는다() -> None:
    # 조건 없이 주제를 빠뜨린 경우("책 추천해줘")는 되묻는 것이 맞다.
    spec = _spec()

    assert chat._recover_topic(spec, "인공지능 책 추천해줘") == spec


@pytest.mark.parametrize(
    "overrides",
    [
        {"semantic": "인공지능 책"},
        {"exact": {"title": "클린 코드", "author": None, "publisher": None}},
        {"exact": {"title": None, "author": "한강", "publisher": None}},
        {"intent": "exact"},
    ],
)
def test_이미_찾을_말이_있으면_건드리지_않는다(overrides: dict) -> None:
    spec = _spec(filters={"pub_year_from": 2020}, **overrides)

    assert chat._recover_topic(spec, "2020년 이후에 나온 인공지능 책 추천해줘") == spec


def _fake_model(patch: dict) -> RunnableLambda:
    return RunnableLambda(
        lambda _p: AIMessage(content=json.dumps(patch, ensure_ascii=False))
    )


def _request(message: str) -> dict:
    return {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": message,
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
    }


def _capture_candidates_spec(monkeypatch: pytest.MonkeyPatch) -> list[Spec]:
    seen: list[Spec] = []

    async def _fake(spec, exclude_book_ids, user_id, owned_book_ids=None) -> list[dict]:
        seen.append(spec)
        return []

    monkeypatch.setattr(chat, "get_candidates", _fake)
    return seen


def test_1단계가_주제를_빠뜨려도_되찾은_주제로_후보를_찾고_응답_spec에_싣는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _capture_candidates_spec(monkeypatch)
    model = _fake_model({"filters": {"pub_year_from": 2020}})
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post(
        "/recommendations/chat",
        json=_request("2020년 이후에 나온 인공지능 책 추천해줘"),
    )

    assert res.status_code == 200
    assert seen[0].semantic == "인공지능"
    assert seen[0].filters.pub_year_from == 2020
    data = res.json()["data"]
    assert data["spec"]["semantic"] == "인공지능"  # 다음 턴으로 이어진다
    # 이제 분야를 되묻지 않는다(후보를 찾았는데 없을 뿐이다).
    assert not (data["followup"] or "").startswith("어떤 분야가 좋으세요?")


def test_주제가_없는_조건_말은_계속_되묻는다(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_candidates_spec(monkeypatch)
    model = _fake_model({"filters": {"price_max": 20000}})
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request("2만원 이하 책 추천해줘"))

    assert res.status_code == 200
    assert seen[0].semantic is None
    assert res.json()["data"]["followup"].startswith("어떤 분야가 좋으세요?")
