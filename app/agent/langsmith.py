"""쇼핑 에이전트의 기본 프롬프트와 운영 채점 규칙."""

from dataclasses import dataclass
from hashlib import sha256

from langchain_core.prompts import ChatPromptTemplate
from langsmith import AsyncClient

from app.agent import prompts
from app.agent.errors import ToolErrorCode
from app.agent.graph import AgentRun
from app.agent.guard import WRITE_TOOLS
from app.agent.schemas import ToolName
from app.core import langsmith

ONLINE_SCORE = "agent_online_score"
_WRITE_TOOLS = WRITE_TOOLS | {ToolName.ORDER_OPEN}
_GUARD_DENIALS = (
    ToolErrorCode.TOOL_NOT_ALLOWED,
    ToolErrorCode.LOGIN_REQUIRED,
    ToolErrorCode.BOOK_NOT_IN_CARDS,
    ToolErrorCode.BOOK_NOT_IN_CART,
    ToolErrorCode.CART_NOT_LOADED,
    ToolErrorCode.NO_FOCUSED_BOOK,
    ToolErrorCode.AMBIGUOUS_BOOK,
    ToolErrorCode.REFERENCE_NOT_STATED,
    ToolErrorCode.CONFIRMATION_REQUIRED,
)


@dataclass(frozen=True)
class AgentPrompt:
    text: str
    version: str


LOCAL_PROMPT = AgentPrompt(
    prompts.SYSTEM_PROMPT,
    "local-" + sha256(prompts.SYSTEM_PROMPT.encode()).hexdigest()[:12],
)
BASE_TEMPLATE = langsmith.text_template(LOCAL_PROMPT.text)


def _validate(template: ChatPromptTemplate) -> None:
    prompts.validate_base_prompt(langsmith.plain_text(template))


def _agent_prompt(revision: langsmith.PromptRevision) -> AgentPrompt:
    if revision.template is BASE_TEMPLATE:
        return LOCAL_PROMPT
    return AgentPrompt(langsmith.plain_text(revision.template), revision.version)


async def pull_prompt(client: AsyncClient, name: str, ref: str) -> AgentPrompt:
    return _agent_prompt(
        await langsmith.pull_template(client, f"{name}:{ref}", BASE_TEMPLATE, _validate)
    )


async def current_prompt() -> AgentPrompt:
    return _agent_prompt(
        await langsmith.prompt(
            "agent",
            BASE_TEMPLATE,
            validate=_validate,
            local_version=LOCAL_PROMPT.version,
        )
    )


def record(
    observed: langsmith.Observation,
    status: int,
    run: AgentRun | None,
    removed: list[str],
) -> None:
    # 정상적으로 반복해 말한 성공 문장은 응답 조립 단계에서 중복만 제거한다.
    grounding_issues = sum(reason != "ACTION_RESTATED" for reason in removed)
    blocked_writes = sum(
        call.name in _WRITE_TOOLS and call.result.get("error") in _GUARD_DENIALS
        for call in (run.tool_calls if run else ())
    )
    observed.record(
        {
            "status": status,
            "grounding_issues": grounding_issues,
            "blocked_writes": blocked_writes,
            "llm_calls": run.llm_calls if run else 0,
        },
        100 if status == 200 and grounding_issues == blocked_writes == 0 else 0,
    )
