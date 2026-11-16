"""쇼핑 에이전트 평가와 LangSmith 프롬프트 연결."""

from langsmith import AsyncClient, Client

from app.agent.langsmith import BASE_TEMPLATE, LOCAL_PROMPT, AgentPrompt, pull_prompt
from app.core.config import Settings
from app.core.langsmith import client_options
from evals.langsmith import agent_answer, case_inputs
from evals.langsmith_publish import Case, Row, fingerprint, publish

OFFLINE_SCORE = "agent_offline_score"


def prompt_name(settings: Settings) -> str:
    return (
        settings.langsmith_prompts.get("agent") or settings.langsmith_agent_prompt
    ).partition(":")[0]


def seed_prompt(settings: Settings) -> str:
    client = Client(**client_options(settings), auto_batch_tracing=False)
    name = prompt_name(settings)
    try:
        if client.get_prompt(name) is not None:
            raise ValueError("이미 존재하는 프롬프트입니다")
        return client.push_prompt(name, object=BASE_TEMPLATE, is_public=False)
    finally:
        client.close()


async def evaluation_prompt(settings: Settings, ref: str) -> AgentPrompt:
    if ref == "local":
        return LOCAL_PROMPT
    async with AsyncClient(
        **client_options(settings), disable_prompt_cache=True, timeout_ms=5000
    ) as client:
        return await pull_prompt(client, prompt_name(settings), ref)


def export(
    settings: Settings, data: dict, results: list[dict], summary: dict, *, label: str
) -> str:
    client = Client(
        **client_options(settings),
        auto_batch_tracing=False,
        tracing_sampling_rate=1,
        timeout_ms=5000,
    )
    try:
        return publish(
            client,
            "agent",
            f"v{data['version']}-{fingerprint(data)}",
            [
                Case(
                    case["id"],
                    case["kind"],
                    case.get("split", "dev"),
                    case_inputs("agent", case),
                )
                for case in data["cases"]
            ],
            [
                Row(
                    case_id=result["id"],
                    seconds=result["record"]["ms"] / 1000,
                    metrics={
                        "status": result["record"]["status"],
                        "blocked_write": result["grade"]["blocked_write"],
                        "matched_outcome": result["grade"]["matched"],
                        "llm_calls": result["record"]["llm_calls"],
                        "repetition": result["run"],
                    },
                    scores={OFFLINE_SCORE: 100 if result["grade"]["passed"] else 0},
                    outputs=agent_answer(result),
                )
                for result in results
            ],
            {
                "model": summary["model"],
                "prompt_version": summary.get("prompt_version", "unknown"),
                "dataset_version": summary["version"],
                "book_info": summary.get("book_info"),
            },
            label=label,
        )
    finally:
        client.close()
