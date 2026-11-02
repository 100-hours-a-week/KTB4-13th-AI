"""③ 폴링 전송 테스트 — 맡기기(POST /recommendations/chat/jobs)와 묻기(GET …/{job_id}).

작업은 요청을 받은 이벤트 루프에서 돈다. TestClient 는 `with` 없이 쓰면 요청마다 루프를 새로 만들어 맡긴 작업이
다음 요청 전에 사라지므로, 한 루프에서 여러 요청을 보내는 httpx.ASGITransport 를 쓴다.
"""

import asyncio
import json

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.chat import jobs
from app.core import responses
from app.main import app
from app.routers import chat

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}

LLM_REPLY = "비 오는 날엔 『달러구트 꿈 백화점』이 잘 어울려요."
CARD_JSON = json.dumps(
    {
        "reply": LLM_REPLY,
        "cards": [
            {
                "book_id": 1088,
                "reason_short": "잔잔한 판타지예요.",
                "reason_long": "비 오는 날 차분히 읽기 좋은 꿈 이야기입니다.",
            }
        ],
    },
    ensure_ascii=False,
)


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": "비 오는 날 읽을 책 추천해줘",
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
    }
    body.update(overrides)
    return body


def _model(*replies: str) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter(AIMessage(content=r) for r in replies))


@pytest.fixture(autouse=True)
def _empty_store() -> None:
    jobs._jobs.clear()


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


def _run(scenario) -> None:
    """한 이벤트 루프에서 같은 앱에 여러 요청을 보낸다."""

    async def main() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai") as ac:
            await scenario(ac)

    asyncio.run(main())


async def _wait_done(ac: httpx.AsyncClient, job_id: str) -> httpx.Response:
    for _ in range(100):
        res = await ac.get(f"/recommendations/chat/jobs/{job_id}")
        if res.status_code != 202:
            return res
        await asyncio.sleep(0.01)
    raise AssertionError("작업이 끝나지 않았다")


# --- 맡기기·묻기 ---


def test_맡기면_202와_job_id를_주고_끝나면_chat과_같은_응답을_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(ac: httpx.AsyncClient) -> None:
        job_model = _model("{}", CARD_JSON)
        monkeypatch.setattr(chat, "get_chat_model", lambda: job_model)
        accepted = await ac.post("/recommendations/chat/jobs", json=_request())

        assert accepted.status_code == 202
        assert accepted.json()["message"] == "job_accepted"
        job_id = accepted.json()["data"]["job_id"]
        assert accepted.json()["data"]["status"] == "pending"

        done = await _wait_done(ac, job_id)

        json_model = _model("{}", CARD_JSON)
        monkeypatch.setattr(chat, "get_chat_model", lambda: json_model)
        expected = await ac.post("/recommendations/chat", json=_request())

        assert done.status_code == 200
        assert done.json() == expected.json()
        assert done.json()["data"]["reply"] == LLM_REPLY

    _run(scenario)


def test_끝나기_전엔_202와_지금까지_쓴_말풍선을_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def _slow_respond(req, payload, on_reply):
        await on_reply("비 오는 날엔")
        await on_reply(" 이 책이요.")
        await release.wait()
        return responses.success("recommend_success", {"reply": "끝"})

    monkeypatch.setattr(chat, "_respond", _slow_respond)

    async def scenario(ac: httpx.AsyncClient) -> None:
        accepted = await ac.post("/recommendations/chat/jobs", json=_request())
        job_id = accepted.json()["data"]["job_id"]
        await asyncio.sleep(0)

        pending = await ac.get(f"/recommendations/chat/jobs/{job_id}")

        assert pending.status_code == 202
        assert pending.json() == {
            "message": "job_pending",
            "data": {
                "job_id": job_id,
                "status": "pending",
                "reply": "비 오는 날엔 이 책이요.",
            },
        }

        release.set()
        done = await _wait_done(ac, job_id)
        assert done.status_code == 200
        assert done.json()["data"]["reply"] == "끝"

    _run(scenario)


def test_끝난_결과는_다시_물어도_같다(monkeypatch: pytest.MonkeyPatch) -> None:
    # BE 가 응답을 놓쳐 다시 물어도 결과를 잃지 않는다(GET 은 부수효과가 없다)
    async def scenario(ac: httpx.AsyncClient) -> None:
        model = _model("{}", CARD_JSON)
        monkeypatch.setattr(chat, "get_chat_model", lambda: model)
        accepted = await ac.post("/recommendations/chat/jobs", json=_request())
        job_id = accepted.json()["data"]["job_id"]

        first = await _wait_done(ac, job_id)
        second = await ac.get(f"/recommendations/chat/jobs/{job_id}")

        assert second.status_code == first.status_code == 200
        assert second.json() == first.json()

    _run(scenario)


def test_요청_검사_오류는_맡기지_않고_바로_상태_코드로_답한다() -> None:
    async def scenario(ac: httpx.AsyncClient) -> None:
        res = await ac.post("/recommendations/chat/jobs", json=_request(message=""))

        assert res.status_code == 400
        assert res.json() == {"message": "invalid_request", "data": None}
        assert jobs._jobs == {}

    _run(scenario)


def test_모르는_작업은_404다() -> None:
    async def scenario(ac: httpx.AsyncClient) -> None:
        res = await ac.get("/recommendations/chat/jobs/없는-작업")

        assert res.status_code == 404
        assert res.json() == {"message": "job_not_found", "data": None}

    _run(scenario)


def test_후보_검색이_실패하면_chat처럼_500을_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _broken(*args, **kwargs):
        raise RuntimeError("DB 끊김")

    monkeypatch.setattr(chat, "get_candidates", _broken)

    async def scenario(ac: httpx.AsyncClient) -> None:
        model = _model("{}")
        monkeypatch.setattr(chat, "get_chat_model", lambda: model)
        accepted = await ac.post("/recommendations/chat/jobs", json=_request())

        done = await _wait_done(ac, accepted.json()["data"]["job_id"])

        assert done.status_code == 500
        assert done.json() == {"message": "internal_server_error", "data": None}

    _run(scenario)


def test_처리하지_못한_예외는_로그를_남기고_500을_준다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _crash(*args, **kwargs):
        raise RuntimeError("버그")

    monkeypatch.setattr(chat, "_respond", _crash)

    async def scenario(ac: httpx.AsyncClient) -> None:
        accepted = await ac.post("/recommendations/chat/jobs", json=_request())

        done = await _wait_done(ac, accepted.json()["data"]["job_id"])

        assert done.status_code == 500
        assert done.json() == {"message": "internal_server_error", "data": None}

    _run(scenario)
    assert "폴링 작업 실패" in caplog.text


# --- 저장소 ---


def test_오래된_작업은_다음_맡기기_때_취소하고_지운다() -> None:
    async def _never(on_reply):
        await asyncio.sleep(10)

    async def _now(on_reply):
        return responses.success("recommend_success", {})

    async def scenario() -> None:
        old = jobs.submit(_never)
        old_task = jobs._jobs[old].task
        jobs._jobs[old].created -= jobs.KEEP_SECONDS + 1

        jobs.submit(_now)
        await asyncio.sleep(0)

        assert old not in jobs._jobs
        assert old_task.cancelled()

    asyncio.run(scenario())


def test_지난_작업은_물어도_없다() -> None:
    async def _now(on_reply):
        return responses.success("recommend_success", {})

    async def scenario() -> None:
        job_id = jobs.submit(_now)
        await asyncio.sleep(0)
        jobs._jobs[job_id].created -= jobs.KEEP_SECONDS + 1

        assert jobs.get(job_id) is None

    asyncio.run(scenario())
