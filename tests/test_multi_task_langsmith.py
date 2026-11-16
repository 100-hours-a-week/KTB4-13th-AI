import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from langchain_core.load import dumpd
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langsmith import AsyncClient, Client, tracing_context
from langsmith.run_trees import RunTree
from langsmith.utils import LangSmithError
from pydantic import SecretStr

from app.core import langsmith, responses
from app.core.config import Settings, get_settings
from app.extraction.prompts import EXTRACTION_PROMPT
from app.gateway.llm import invoke_chain
from app.main import app
from app.routers import chat, extractions
from evals import langsmith as evaluation
from evals.extraction.run import load_cases

_CHAT_TEMPLATE = ChatPromptTemplate.from_template("이전 안내 {message}")


def _commit(template, revision):
    return SimpleNamespace(manifest=dumpd(template), commit_hash=revision)


def _wire(monkeypatch, *, eval_mode=False):
    settings = get_settings().model_copy(
        update={
            "langsmith_project": "shared",
            "langsmith_prompts": {
                "chat.pick": "recommendation:staging"
                if eval_mode
                else "recommendation",
                "extraction": "preferences",
            },
            "langsmith_prompt_refresh_seconds": 0,
            "langsmith_eval_mode": eval_mode,
        }
    )
    sdk = AsyncMock(spec=AsyncClient)
    sdk.read_project.return_value = SimpleNamespace(id=uuid4())
    candidate = ChatPromptTemplate.from_template("후보 안내 {message}")

    async def pull(identifier, **_):
        if identifier.startswith("recommendation:"):
            return _commit(candidate, "chat-commit")
        return _commit(EXTRACTION_PROMPT, "extraction-commit")

    sdk.pull_prompt_commit.side_effect = pull

    async def chat_result(*_):
        prompt = await langsmith.template("chat.pick", _CHAT_TEMPLATE)
        return responses.success(
            "recommend_success",
            {
                "reply": prompt.format_messages(message="보내지 않을 발화")[0].content,
                "cards": [],
                "degraded": False,
            },
        )

    async def extract(_):
        prompt = await langsmith.template("extraction", EXTRACTION_PROMPT)
        assert prompt.input_variables == EXTRACTION_PROMPT.input_variables
        return []

    monkeypatch.setattr(chat, "_handle", chat_result)
    monkeypatch.setattr(extractions.service, "extract", extract)
    monitor = langsmith.LangSmith(settings, sdk)
    monkeypatch.setattr(langsmith, "service", monitor)
    return monitor, sdk


def test_two_tasks_record_only_metrics_in_one_project(monkeypatch):
    async def scenario():
        monitor, sdk = _wire(monkeypatch)
        result = await chat._respond(None, {}, None)
        assert "후보 안내" in json.loads(result.body)["data"]["reply"]
        body = next(
            case["request"] for case in load_cases() if case["id"] == "request-01"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/preferences/extractions", json=body)
            assert response.status_code == 200
            assert langsmith.EVAL_PROMPTS_HEADER not in response.headers
        await monitor.close()
        return sdk

    sdk = asyncio.run(scenario())
    sdk.read_project.assert_awaited_once_with("shared")
    traces = [call.kwargs for call in sdk.create_run.await_args_list]
    assert {trace["extra"]["metadata"]["task"] for trace in traces} == {
        "chat",
        "extraction",
    }
    assert all(
        trace["project_name"] == "shared" and trace["inputs"] == {} for trace in traces
    )
    assert {
        trace["extra"]["metadata"]["task"]: trace["extra"]["metadata"][
            "prompt_versions"
        ]
        for trace in traces
    } == {
        "chat": {"chat.pick": "chat-commit"},
        "extraction": {"extraction": "extraction-commit"},
    }
    assert "보내지 않을 발화" not in repr(traces)
    assert {call.args[1] for call in sdk.create_feedback.await_args_list} == {
        "chat_online_score",
        "extraction_online_score",
    }


def test_offline_server_pins_versions_and_sends_no_online_scores(monkeypatch):
    async def scenario():
        monitor, sdk = _wire(monkeypatch, eval_mode=True)
        for _ in range(2):
            response = await chat._respond(None, {}, None)
            assert langsmith.header_versions(
                response.headers[langsmith.EVAL_PROMPTS_HEADER]
            ) == {"chat.pick": "chat-commit"}
        await monitor.close()
        return sdk

    sdk = asyncio.run(scenario())
    sdk.pull_prompt_commit.assert_awaited_once_with(
        "recommendation:staging", skip_cache=True
    )
    sdk.create_run.assert_not_awaited()


def test_search_offline_keeps_ranking_scores_and_excludes_degraded(tmp_path):
    bundle = evaluation._case_bundle("search", None)
    case = next(case for case in bundle["cases"] if case["kind"] == "descriptive")
    row = {
        "id": case["id"],
        "kind": "descriptive",
        "ndcg": 0.45,
        "seconds": 0.2,
        "unlabeled": 2,
        "book_ids": [1, 2],
        "query": "검색어 원문",
    }
    path = tmp_path / "search.json"
    path.write_text(
        json.dumps(
            {
                "bundle_version": bundle["version"],
                "scores": [{**row, "ok": True}, {**row, "ok": False}],
            }
        ),
        encoding="utf-8",
    )

    _, _, rows, _ = evaluation.load_report("search", path)
    assert rows[0].scores == {"search_offline_valid": 100, "search_ndcg_10": 45}
    assert rows[1].scores == {"search_offline_valid": 0}
    assert "검색어 원문" not in repr(rows)
    assert "book_ids" not in rows[0].metrics


def test_report_rejects_mixed_prompt_versions():
    with pytest.raises(ValueError, match="chat.pick"):
        evaluation._versions([{"chat.pick": "first"}, {"chat.pick": "second"}])


def test_chat_report_uses_existing_grader_and_keeps_actual_prompt_revision(tmp_path):
    case_file = tmp_path / "cases.json"
    case_file.write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {
                        "no": 1,
                        "kind": "clarify",
                        "turns": ["평가 발화"],
                        "expect": {"action": "clarify"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "chat.json"
    report.write_text(
        json.dumps(
            {
                "meta": {"cases_version": 1},
                "runs": [
                    {
                        "1": [
                            {
                                "status": 200,
                                "sec": 0.1,
                                "cards": [],
                                "followup": "어떤 책을 원하세요?",
                                "prompt_versions": {"chat.spec": "tested-commit"},
                            }
                        ]
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _, cases, rows, metadata = evaluation.load_report("chat", report, case_file)
    assert metadata["prompt_versions"] == {"chat.spec": "tested-commit"}
    assert rows[0].scores == {"chat_offline_score": 100}
    # 평가 사례는 만든 질문이라 질문과 답을 같이 올린다. 실험 화면에서 버전별 답을 견준다.
    assert cases[0].inputs == {"turns": ["평가 발화"]}
    assert rows[0].outputs["answers"][0]["followup"] == "어떤 책을 원하세요?"
    assert rows[0].outputs["why"] == []


def test_default_endpoint_is_the_team_account_region():
    assert Settings(_env_file=None).langsmith_endpoint == (
        "https://apac.api.smith.langchain.com"
    )


def test_wrong_region_key_falls_back_to_code_prompt_and_names_the_endpoint(
    monkeypatch, caplog
):
    """다른 리전 키는 403 이 난다. 답은 코드 프롬프트로 하고, 경고에 상태와 주소를 남긴다."""
    monitor, sdk = _wire(monkeypatch)
    request = httpx.Request("GET", "https://apac.api.smith.langchain.com/commits")

    def forbidden(*_, **__):
        try:
            raise httpx.HTTPStatusError(
                "forbidden", request=request, response=httpx.Response(403)
            )
        except httpx.HTTPStatusError as error:
            raise LangSmithError("Failed to GET /commits") from error

    sdk.pull_prompt_commit.side_effect = forbidden
    revision = asyncio.run(langsmith.prompt("chat.pick", _CHAT_TEMPLATE))
    assert revision.template is _CHAT_TEMPLATE
    warning = next(
        record.getMessage()
        for record in caplog.records
        if "chat.pick 조회 실패" in record.getMessage()
    )
    assert "403" in warning
    assert monitor.settings.langsmith_endpoint in warning


class _Sink(Client):
    """원문 트레이스용 동기 클라이언트. 보내려던 실행을 보내지 않고 모은다."""

    def __init__(self):
        super().__init__(
            api_url="https://apac.api.smith.langchain.com",
            api_key="test",
            auto_batch_tracing=False,
        )
        self.created = []
        self.updated = []

    def create_run(self, *args, **kwargs):
        self.created.append({"name": args[0], **kwargs} if args else kwargs)

    def update_run(self, run_id=None, **kwargs):
        self.updated.append({"id": run_id, **kwargs})


def test_trace_sends_the_turn_with_text_and_puts_the_score_on_it(monkeypatch):
    """원문 트레이스를 켜면 턴 하나가 루트 하나로 가고, LLM 단계가 그 아래 붙고, 점수가 그 루트에 붙는다."""
    monitor, sdk = _wire(monkeypatch)
    sink = _Sink()
    monitor.tracer = sink

    async def handle(req, payload, on_reply):
        prompt = await langsmith.template("chat.pick", _CHAT_TEMPLATE)
        chain = prompt | RunnableLambda(lambda value: value.to_string())
        reply = await invoke_chain(
            chain, {"message": payload["message"]}, name="chat.pick"
        )
        return responses.success(
            "recommend_success", {"reply": reply, "cards": [], "degraded": False}
        )

    monkeypatch.setattr(chat, "_handle", handle)

    async def scenario():
        await chat._respond(
            None, {"user_id": 7, "message": "추리 소설 추천해 줘"}, None
        )
        await monitor.close()

    asyncio.run(scenario())
    roots = [run for run in sink.created if run["name"] == "chat"]
    assert len(roots) == 1
    root_id = roots[0]["id"]
    closed = next(run for run in sink.updated if run["id"] == root_id)
    # 요청 본문은 처리 중에 실리므로 닫을 때 다시 보낸다. 사용자 번호는 싣지 않는다.
    assert closed["inputs"] == {"message": "추리 소설 추천해 줘"}
    assert "추리 소설 추천해 줘" in closed["outputs"]["data"]["reply"]
    metadata = closed["extra"]["metadata"]
    assert metadata["prompt_versions"] == {"chat.pick": "chat-commit"}
    assert metadata["online_eval"]["status"] == 200
    step = next(run for run in sink.created if run["name"] == "chat.pick")
    assert step["parent_run_id"] == root_id
    assert step["trace_id"] == root_id
    # 숫자만 담은 실행을 따로 만들지 않고, 점수를 그 턴의 루트에 붙인다.
    sdk.create_run.assert_not_awaited()
    feedback = sdk.create_feedback.await_args
    assert feedback.args[:2] == (root_id, "chat_online_score")
    assert feedback.kwargs["trace_id"] == root_id
    assert feedback.kwargs["score"] == 100


def test_without_trace_no_run_reaches_the_tracer_even_if_global_tracing_is_on(
    monkeypatch,
):
    """원문 트레이스를 끄면 LANGSMITH_TRACING 이 켜져 있어도 이 경로의 LLM 단계는 나가지 않는다."""
    monitor, sdk = _wire(monkeypatch)
    sink = _Sink()

    async def handle(req, payload, on_reply):
        chain = RunnableLambda(lambda value: value["message"])
        reply = await invoke_chain(
            chain, {"message": payload["message"]}, name="chat.pick"
        )
        return responses.success(
            "recommend_success", {"reply": reply, "cards": [], "degraded": False}
        )

    monkeypatch.setattr(chat, "_handle", handle)

    async def scenario():
        with tracing_context(enabled=True, client=sink):
            await chat._respond(None, {"message": "보내지 않을 발화"}, None)
        await monitor.close()

    asyncio.run(scenario())
    assert sink.created == []
    assert sdk.create_run.await_args.kwargs["inputs"] == {}


def test_start_opens_the_tracer_only_when_trace_is_on(monkeypatch):
    base = get_settings().model_copy(
        update={
            "langsmith_api_key": SecretStr("test"),
            "langsmith_project": "shared",
        }
    )
    for trace_on in (False, True):
        langsmith.start(base.model_copy(update={"langsmith_trace": trace_on}))
        try:
            assert (langsmith.service.tracer is not None) is trace_on
        finally:
            asyncio.run(langsmith.stop())


def test_photo_trace_drops_the_url_signature():
    observed = langsmith.Observation("photo", sampled=True, eval_mode=False)
    observed.run = RunTree(name="photo", inputs={})
    observed.trace_inputs({"image_ref": "https://s3.test/a.jpg", "user_id": 3})
    assert observed.run.inputs == {"image_ref": "https://s3.test/a.jpg"}


def test_trace_carries_card_books_for_the_judge(monkeypatch):
    """카드가 있는 표본 턴은 판정 LLM 이 읽을 책 정보(제목·저자·소개 앞부분)와 has_cards 꼬리표를 트레이스에 싣는다."""
    monitor, _ = _wire(monkeypatch)
    sink = _Sink()
    monitor.tracer = sink

    async def handle(req, payload, on_reply):
        return responses.success(
            "recommend_success",
            {
                "reply": "골랐어요",
                "cards": [{"rank": 1, "isbn": "9788900000001", "title": "책"}],
                "degraded": False,
            },
        )

    async def fetch(cards):
        assert cards == [(1, "9788900000001")]
        return [chat.shown.ShownBook(1, 7, "책", "저자", "소개 " * 200)]

    monkeypatch.setattr(chat, "_handle", handle)
    monkeypatch.setattr(chat.shown, "fetch", fetch)

    async def scenario():
        await chat._respond(None, {"message": "가벼운 에세이"}, None)
        await monitor.close()

    asyncio.run(scenario())
    root = next(run for run in sink.created if run["name"] == "chat")
    closed = next(run for run in sink.updated if run["id"] == root["id"])
    books = closed["outputs"]["judge_books"]
    assert books.startswith("1번: 『책』 - 저자 - 소개")
    assert len(books) < 340
    assert "has_cards" in closed["tags"]
