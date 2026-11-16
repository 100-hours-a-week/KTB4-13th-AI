import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
from langchain_core.load import dumpd
from langchain_core.prompts import ChatPromptTemplate
from langsmith import AsyncClient, Client

from app.agent import langsmith as monitor
from app.agent.fake_llm import ScriptedModel, ai_text
from app.core import langsmith as runtime
from app.core.config import get_settings
from app.main import app
from app.routers import agent
from evals.agent import langsmith_export, run


def _commit(version: str, text: str):
    template = ChatPromptTemplate.from_messages(
        [("system", text)], template_format="mustache"
    )
    return SimpleNamespace(commit_hash=version, manifest=dumpd(template))


def test_production_prompt_refresh_and_online_scores(monkeypatch) -> None:
    settings = get_settings().model_copy(
        update={
            "langsmith_agent_prompt": "shopping",
            "langsmith_agent_project": "online",
            "langsmith_agent_prompt_refresh_seconds": 5,
        }
    )
    sdk = AsyncMock(spec=AsyncClient)
    first = monitor.LOCAL_PROMPT.text + "\n첫 번째 버전"
    second = monitor.LOCAL_PROMPT.text + "\n두 번째 버전"
    sdk.pull_prompt_commit.side_effect = [
        _commit("first", first),
        _commit("second", second),
        _commit("invalid", "필수 구절이 빠졌다."),
    ]
    project_id = uuid4()
    sdk.read_project.return_value = SimpleNamespace(id=project_id)
    model = ScriptedModel(
        [
            ai_text("어느 책인지 알려 주세요."),
            ai_text("담았어요."),
            ai_text("어느 책인지 알려 주세요."),
        ]
    )
    clock = Mock(return_value=1.0)
    monkeypatch.setattr(runtime, "monotonic", clock)
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    async def scenario():
        service = runtime.LangSmith(settings, sdk)
        monkeypatch.setattr(runtime, "service", service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {
                "user_id": 234567,
                "message": "전송하면 안 되는 실제 발화",
                "user_context": {"logged_in": True},
                "idempotency_key": "langsmith-test",
            }
            response = await client.post("/agent/act", json=body)
            assert response.status_code == 200
            assert (await monitor.current_prompt()).version == "first"
            assert sdk.pull_prompt_commit.await_count == 1

            clock.return_value = 6.0
            response = await client.post("/agent/act", json=body)
            assert response.status_code == 422
            clock.return_value = 11.0
            response = await client.post("/agent/act", json=body)
            assert response.status_code == 200
        await service.close()

    asyncio.run(scenario())

    assert [call[0].content for call in model.calls] == [first, second, second]
    assert all(
        call.args == ("shopping:production",) and call.kwargs == {"skip_cache": True}
        for call in sdk.pull_prompt_commit.await_args_list
    )
    traces = {call.kwargs["id"]: call.kwargs for call in sdk.create_run.await_args_list}
    assert sdk.create_feedback.await_count == 3
    for call in sdk.create_feedback.await_args_list:
        trace = traces[call.args[0]]
        assert trace["inputs"] == {}
        assert call.args[1] == monitor.ONLINE_SCORE
        assert call.kwargs["session_id"] == project_id
        assert call.kwargs["score"] == (100 if trace["outputs"]["status"] == 200 else 0)
    assert [
        trace["extra"]["metadata"]["prompt_version"] for trace in traces.values()
    ] == ["first", "second", "second"]
    assert "전송하면 안 되는 실제 발화" not in repr(traces)
    assert "234567" not in repr([trace["outputs"] for trace in traces.values()])


def test_offline_eval_pins_candidate_and_publishes_case_scores(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_AGENT_PROMPT", "shopping")
    candidate = monitor.LOCAL_PROMPT.text + "\n평가 후보"
    sdk = AsyncMock(spec=AsyncClient)
    sdk.__aenter__.return_value = sdk
    sdk.pull_prompt_commit.return_value = _commit("candidate-commit", candidate)
    monkeypatch.setattr(langsmith_export, "AsyncClient", Mock(return_value=sdk))

    publisher = Mock(spec=Client)
    publisher.read_dataset.return_value = SimpleNamespace(id=uuid4())
    publisher.list_examples.return_value = []
    project_id = uuid4()
    publisher.create_project.return_value = SimpleNamespace(id=project_id, url=None)
    monkeypatch.setattr(langsmith_export, "Client", Mock(return_value=publisher))
    model = ScriptedModel([ai_text("어느 책인지 알려 주세요."), ai_text("담았어요.")])
    monkeypatch.setattr(run, "get_chat_model", lambda **kwargs: model)
    output = tmp_path / "scores.json"

    run.main(
        [
            "--langsmith",
            "--prompt-ref",
            "staging",
            "--ids",
            "ask-05",
            "--repeat",
            "2",
            "--concurrency",
            "1",
            "--out",
            str(output),
        ]
    )

    sdk.pull_prompt_commit.assert_awaited_once_with("shopping:staging", skip_cache=True)
    assert [call[0].content for call in model.calls] == [candidate, candidate]
    summary = json.loads(output.read_text(encoding="utf-8"))["summary"]
    assert summary["prompt_version"] == "candidate-commit"
    assert summary["passed"] == 1
    assert summary["total"] == 2
    traces = publisher.create_run.call_args_list
    assert len(traces) == 2
    assert (
        traces[0].kwargs["reference_example_id"]
        == traces[1].kwargs["reference_example_id"]
    )
    feedback = publisher.create_feedback.call_args_list
    assert sorted(call.kwargs["score"] for call in feedback) == [0, 100]
    assert all(call.args[1] == langsmith_export.OFFLINE_SCORE for call in feedback)
    assert all(call.kwargs["session_id"] == project_id for call in feedback)


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


def test_trace_puts_the_graph_under_one_turn_with_text(monkeypatch) -> None:
    """원문 트레이스를 켜면 ⑦ 한 턴이 루트 하나로 가고, 그래프가 그 아래 붙고, 점수가 그 루트에 붙는다."""
    settings = get_settings().model_copy(
        update={"langsmith_agent_project": "online", "langsmith_trace": True}
    )
    sdk = AsyncMock(spec=AsyncClient)
    sdk.read_project.return_value = SimpleNamespace(id=uuid4())
    model = ScriptedModel([ai_text("어느 책인지 알려 주세요.")])
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    sink = _Sink()

    async def scenario():
        service = runtime.LangSmith(settings, sdk, sink)
        monkeypatch.setattr(runtime, "service", service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/agent/act",
                json={
                    "user_id": 234567,
                    "message": "그 책 담아 줘",
                    "user_context": {"logged_in": True},
                    "idempotency_key": "langsmith-trace-test",
                },
            )
            assert response.status_code == 200
        await service.close()

    asyncio.run(scenario())
    root = next(run for run in sink.created if run["name"] == "shopping-agent")
    closed = next(run for run in sink.updated if run["id"] == root["id"])
    assert closed["inputs"]["message"] == "그 책 담아 줘"
    assert "user_id" not in closed["inputs"]
    assert closed["outputs"]["status"] == 200
    assert closed["outputs"]["data"]["reply"]
    graph = next(run for run in sink.created if run["name"] == "agent.graph")
    assert graph["parent_run_id"] == root["id"]
    sdk.create_run.assert_not_awaited()
    assert sdk.create_feedback.await_args.args[:2] == (
        root["id"],
        monitor.ONLINE_SCORE,
    )
