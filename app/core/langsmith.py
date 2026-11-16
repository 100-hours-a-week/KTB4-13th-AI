"""태스크별 프롬프트 조회와 LangSmith 운영 점수 전송.

기본은 점수와 숫자만 보낸다. LANGSMITH_TRACE 를 켜면 표본 요청을 원문 트레이스로 보내고 점수를 그 트레이스에
붙인다. 그래야 LangSmith 화면의 규칙(판정 LLM 채점, 낮은 점수 턴을 검토 대기열·데이터셋에 넣기)이 턴을 읽는다.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from random import random
from time import monotonic
from uuid import UUID, uuid4

from langchain_core.load import dumpd, load
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    PromptTemplate,
    SystemMessagePromptTemplate,
)
from langsmith import AsyncClient, Client, tracing_context
from langsmith.run_trees import RunTree
from langsmith.utils import LangSmithConflictError, LangSmithNotFoundError

from app.core.config import Settings
from app.core.request_log import current_request_id

logger = logging.getLogger(__name__)

EVAL_PROMPTS_HEADER = "X-Bookjeok-Eval-Prompts"
PROMPT_TIMEOUT_SECONDS = 1.0
EXPORT_TIMEOUT_SECONDS = 5.0
MAX_PENDING_SCORES = 32

Number = bool | int | float


@dataclass(frozen=True)
class PromptRevision:
    template: ChatPromptTemplate
    version: str


@dataclass
class _Cache:
    revision: PromptRevision | None = None
    refresh_at: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def client_options(settings: Settings) -> dict:
    return {
        "api_key": settings.langsmith_api_key.get_secret_value(),
        "api_url": settings.langsmith_endpoint,
        "workspace_id": settings.langsmith_workspace_id or None,
    }


def _failure(exc: BaseException, endpoint: str) -> str:
    """경고 로그에 남길 실패 요약. 응답 본문은 남기지 않는다.

    키를 만든 리전과 주소가 다르면 401·403 이 나는데 SDK 예외 이름만으로는 알 수 없어 상태와 주소를 붙인다.
    """
    response = getattr(exc.__cause__, "response", None)
    status = getattr(response, "status_code", None)
    if status in (401, 403):
        return (
            f"{type(exc).__name__} {status}, 주소 {endpoint}: "
            "키를 만든 계정의 리전 주소인지 확인"
        )
    return type(exc).__name__


def text_template(text: str, role: str = "system") -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages([(role, text)], template_format="mustache")


def plain_text(template: ChatPromptTemplate) -> str:
    text = template.format_messages()[0].content
    if not isinstance(text, str) or not text.strip():
        raise ValueError("텍스트 메시지가 필요합니다")
    return text


def validate_text(template: ChatPromptTemplate) -> None:
    plain_text(template)


def header_versions(header: str | None) -> dict[str, str]:
    """평가 서버 응답에서 프롬프트 버전만 읽는다."""
    if not header:
        return {}
    versions = json.loads(header)
    if not isinstance(versions, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in versions.items()
    ):
        raise ValueError("프롬프트 버전 헤더 형식 오류")
    return versions


def _signature(template: ChatPromptTemplate) -> tuple:
    roles = {
        SystemMessage: "system",
        SystemMessagePromptTemplate: "system",
        HumanMessage: "human",
        HumanMessagePromptTemplate: "human",
    }
    return (
        frozenset(template.input_variables),
        frozenset(template.optional_variables),
        frozenset(template.partial_variables),
        tuple(roles[type(message)] for message in template.messages),
    )


async def pull_template(
    client: AsyncClient,
    identifier: str,
    local: ChatPromptTemplate,
    validate: Callable[[ChatPromptTemplate], None] | None = None,
) -> PromptRevision:
    commit = await client.pull_prompt_commit(identifier, skip_cache=True)
    # 프롬프트 템플릿만 역직렬화한다. 원격 모델이나 도구 설정은 사용하지 않는다.
    template = load(
        commit.manifest,
        allowed_objects=[
            ChatPromptTemplate,
            HumanMessagePromptTemplate,
            SystemMessagePromptTemplate,
            PromptTemplate,
            HumanMessage,
            SystemMessage,
        ],
    )
    if (
        not isinstance(template, ChatPromptTemplate)
        or _signature(template) != _signature(local)
        or not commit.commit_hash
    ):
        raise ValueError("프롬프트의 메시지 역할이나 입력 변수가 코드와 다릅니다")
    if validate is not None:
        validate(template)
    return PromptRevision(template, commit.commit_hash)


@dataclass
class Observation:
    task: str
    sampled: bool
    eval_mode: bool
    started: datetime = field(default_factory=lambda: datetime.now(UTC))
    versions: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Number] = field(default_factory=dict)
    resolved: dict[str, PromptRevision] = field(default_factory=dict)
    # 원문 트레이스를 켰을 때 이 요청의 루트 실행. 점수를 따로 만든 빈 실행이 아니라 여기에 붙인다.
    run: RunTree | None = None
    result: tuple[dict[str, Number], float | None] | None = None

    def record(self, metrics: dict[str, Number], score: float | None) -> None:
        """채점 결과를 담아 두고, 요청이 끝날 때(observe 를 나갈 때) 한 번 보낸다."""
        if self.sampled:
            self.result = ({**self.facts, **metrics}, score)

    def trace_inputs(self, inputs: object) -> None:
        """트레이스에 실을 요청 본문. 원문 트레이스를 켠 요청에서만 실린다."""
        if self.run is not None:
            self.run.inputs = request_inputs(inputs)

    def trace_outputs(self, outputs: dict) -> None:
        """트레이스에 실을 응답. 원문 트레이스를 켠 요청에서만 실린다."""
        if self.run is not None:
            self.run.add_outputs(outputs)


_active: ContextVar[Observation | None] = ContextVar("evaluation", default=None)

# 원문 트레이스를 보내는 태스크. LLM 프롬프트가 있는 태스크만 보낸다. 검색·피드·임베딩·프로필은 LLM 이 없어
# 트레이스로 볼 것이 적고, 요청이 많아 무료 한도(월 5천 트레이스)를 먼저 쓴다.
TRACED_TASKS = frozenset({"chat", "agent", "extraction", "photo"})
# 원문을 실어도 사용자 번호는 싣지 않는다. 트레이스에서 사람을 찾을 일이 없고, 지울 때 찾을 일만 생긴다.
_ID_FIELDS = frozenset({"user_id"})


def request_inputs(payload: object) -> dict:
    """트레이스 입력으로 실을 요청 본문. 사용자 번호만 뺀다."""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        return {"body": payload}
    return {k: v for k, v in payload.items() if k not in _ID_FIELDS}


@contextmanager
def observe(task: str, inputs: object = None) -> Iterator[Observation | None]:
    """샘플링과 프롬프트 버전 수집을 요청 단위로 묶는다.

    LANGSMITH_TRACE 를 켜면 표본으로 뽑힌 요청을 원문 트레이스 하나로 보낸다. 끄면 이 경로의 트레이스를
    막고 숫자만 보낸다. 점수는 요청이 끝난 뒤 보낸다.
    """
    monitor = service
    if monitor is None:
        yield None
        return
    observed = Observation(
        task, monitor.sample(task), monitor.settings.langsmith_eval_mode
    )
    token = _active.set(observed)
    try:
        with monitor.tracing(observed, inputs):
            yield observed
    finally:
        _active.reset(token)
        monitor.send(observed)


def fact(key: str, value: Number) -> None:
    if (observed := _active.get()) and observed.sampled:
        observed.facts[key] = value


def trace_inputs(payload: object) -> None:
    """본문을 처리 중에 읽는 경로가 부른다. 원문 트레이스를 켠 표본 요청에서만 실린다."""
    if observed := _active.get():
        observed.trace_inputs(payload)


def tracing_request() -> bool:
    """이 요청이 원문 트레이스로 가는가. 트레이스에만 실을 것을 따로 만들 때 먼저 묻는다."""
    observed = _active.get()
    return observed is not None and observed.run is not None


def trace_outputs(outputs: dict, *, tags: list[str] | None = None) -> None:
    """트레이스 출력에 칸을 더하고 꼬리표를 단다. 원문 트레이스를 켠 표본 요청에서만 실린다."""
    observed = _active.get()
    if observed is None or observed.run is None:
        return
    observed.trace_outputs(outputs)
    if tags:
        known = observed.run.tags or []
        observed.run.tags = known + [tag for tag in tags if tag not in known]


async def prompt(
    key: str,
    local: ChatPromptTemplate,
    *,
    validate: Callable[[ChatPromptTemplate], None] | None = None,
    local_version: str | None = None,
) -> PromptRevision:
    monitor = service
    observed = _active.get()
    if monitor is None:
        return PromptRevision(local, local_version or "local")
    if observed is not None and key in observed.resolved:
        return observed.resolved[key]
    revision = await monitor.resolve(
        key,
        local,
        validate=validate,
        local_version=local_version,
        track=observed is not None and (observed.sampled or observed.eval_mode),
    )
    if observed is not None:
        revision = observed.resolved.setdefault(key, revision)
        if observed.sampled or observed.eval_mode:
            observed.versions[key] = revision.version
    return revision


async def template(key: str, local: ChatPromptTemplate) -> ChatPromptTemplate:
    return (await prompt(key, local)).template


class LangSmith:
    def __init__(
        self,
        settings: Settings,
        client: AsyncClient | None,
        tracer: Client | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        # 원문 트레이스용 동기 클라이언트. 실행을 뒤에서 묶어 보내고, LangChain 자식 실행도 이것으로 간다.
        self.tracer = tracer
        self._prompts: dict[str, _Cache] = {}
        self._projects: dict[str, UUID] = {}
        self._project_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()

    def _project(self, task: str) -> str:
        if self.client is None or self.settings.langsmith_eval_mode:
            return ""
        return self.settings.langsmith_project or (
            self.settings.langsmith_agent_project if task == "agent" else ""
        )

    def sample(self, task: str) -> bool:
        rate = (
            self.settings.langsmith_agent_sample_rate
            if task == "agent" and not self.settings.langsmith_project
            else self.settings.langsmith_sample_rate
        )
        return bool(self._project(task)) and random() < rate

    async def resolve(
        self,
        key: str,
        local: ChatPromptTemplate,
        *,
        validate: Callable[[ChatPromptTemplate], None] | None,
        local_version: str | None,
        track: bool,
    ) -> PromptRevision:
        eval_mode = self.settings.langsmith_eval_mode
        name = self.settings.langsmith_prompts.get(key)
        legacy = not name and key == "agent"
        if legacy:
            name = self.settings.langsmith_agent_prompt
        identifier = (name if ":" in name else f"{name}:production") if name else ""
        if identifier and self.client is not None:
            if key not in self._prompts:
                self._prompts[key] = _Cache()
            cached = self._prompts[key]
            if eval_mode or not cached.lock.locked():
                async with cached.lock:
                    if monotonic() >= cached.refresh_at and not (
                        eval_mode and cached.revision is not None
                    ):
                        ttl = (
                            self.settings.langsmith_agent_prompt_refresh_seconds
                            if legacy
                            else self.settings.langsmith_prompt_refresh_seconds
                        )
                        try:
                            async with asyncio.timeout(
                                5 if eval_mode else PROMPT_TIMEOUT_SECONDS
                            ):
                                cached.revision = await pull_template(
                                    self.client, identifier, local, validate
                                )
                        except Exception as exc:  # noqa: BLE001 - 운영에서는 마지막 정상 버전으로 응답
                            if eval_mode:
                                raise RuntimeError(
                                    f"평가 프롬프트 {key} 조회 실패"
                                ) from None
                            logger.warning(
                                "LangSmith 프롬프트 %s 조회 실패(%s)",
                                key,
                                _failure(exc, self.settings.langsmith_endpoint),
                            )
                            ttl = max(5, ttl)
                        cached.refresh_at = monotonic() + ttl
            if cached.revision is not None:
                return cached.revision
        elif identifier and eval_mode:
            raise RuntimeError("관리 프롬프트 평가에는 LANGSMITH_API_KEY가 필요합니다")
        if track and local_version is None:
            serialized = json.dumps(dumpd(local), sort_keys=True, ensure_ascii=False)
            local_version = "local-" + sha256(serialized.encode()).hexdigest()[:12]
        return PromptRevision(local, local_version or "local")

    def _metadata(self, task: str, versions: dict[str, str]) -> dict:
        metadata = {
            "task": task,
            "release_sha": self.settings.release_sha,
            "prompt_versions": versions,
        }
        # CloudWatch 로그·BE 로그와 같은 요청 번호. 낮은 점수 턴을 서버 로그에서 찾을 때 쓴다.
        if request_id := current_request_id():
            metadata["request_id"] = request_id
        if task == "agent":
            metadata["prompt_version"] = versions.get("agent")
            metadata["model"] = (
                self.settings.agent_llm_model_id or self.settings.llm_model_id
            )
        elif task == "chat":
            metadata["model"] = self.settings.llm_model_id
        elif task == "extraction":
            metadata["model"] = (
                self.settings.extraction_llm_model_id or self.settings.llm_model_id
            )
        elif task == "photo":
            metadata["model"] = (
                self.settings.photo_llm_model_id or self.settings.llm_model_id
            )
        return metadata

    @contextmanager
    def tracing(self, observed: Observation, inputs: object) -> Iterator[None]:
        """원문 트레이스를 켠 표본 요청이면 루트 실행을 열고, 아니면 이 경로의 트레이스를 막는다.

        루트 안에서 부른 LangChain 체인·LangGraph 그래프는 이 루트 아래 자식으로 붙는다.
        """
        if not (
            self.tracer is not None
            and observed.sampled
            and observed.task in TRACED_TASKS
        ):
            # 전역 추적 설정(LANGSMITH_TRACING)과 관계없이 이 경로의 입력·출력 원문을 수집하지 않는다.
            with tracing_context(enabled=False):
                yield
            return
        project = self._project(observed.task)
        run = RunTree(
            name=_run_name(observed.task),
            run_type="chain",
            inputs=request_inputs(inputs),
            project_name=project,
            client=self.tracer,
            extra={"metadata": self._metadata(observed.task, {})},
        )
        observed.run = run
        run.post()
        try:
            with tracing_context(
                enabled=True, client=self.tracer, project_name=project, parent=run
            ):
                yield
        except BaseException as exc:
            run.end(error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            # 프롬프트 버전과 채점 숫자는 요청이 끝나야 정해진다. 루트를 닫기 전에 싣는다.
            extra: dict = {"prompt_versions": dict(observed.versions)}
            if observed.task == "agent":
                extra["prompt_version"] = observed.versions.get("agent")
            if observed.result is not None:
                extra["online_eval"] = observed.result[0]
            run.add_metadata(extra)
            if run.end_time is None:
                run.end()
            # 요청 본문을 처리 중에 읽는 경로(취향 추출·사진)는 입력이 시작한 뒤에 정해진다. 닫을 때 입력도 다시 보낸다.
            run.patch(exclude_inputs=False)

    def send(self, observed: Observation) -> None:
        """요청 하나의 점수를 보낸다. 원문 트레이스가 있으면 그 루트에, 없으면 숫자만 담은 실행에 붙인다."""
        if observed.result is None:
            return
        project = self._project(observed.task)
        if not project or len(self._pending) >= MAX_PENDING_SCORES:
            return
        metrics, score = observed.result
        task = asyncio.create_task(
            self._export(
                project,
                observed.task,
                observed.started,
                datetime.now(UTC),
                self._metadata(observed.task, dict(observed.versions)),
                metrics,
                score,
                observed.run.id if observed.run is not None else None,
            )
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _project_id(self, client: AsyncClient, project: str) -> UUID:
        if project not in self._projects:
            async with self._project_lock:
                if project not in self._projects:
                    try:
                        session = await client.read_project(project)
                    except LangSmithNotFoundError:
                        try:
                            session = await client.create_project(project)
                        except LangSmithConflictError:
                            session = await client.read_project(project)
                    self._projects[project] = session.id
        return self._projects[project]

    async def _export(
        self,
        project: str,
        task: str,
        started: datetime,
        ended: datetime,
        metadata: dict,
        metrics: dict[str, Number],
        score: float | None,
        traced_run: UUID | None = None,
    ) -> None:
        client = self.client
        if client is None:
            return
        run_id = traced_run or uuid4()
        try:
            async with asyncio.timeout(EXPORT_TIMEOUT_SECONDS):
                project_id = await self._project_id(client, project)
                if traced_run is None:
                    await client.create_run(
                        _run_name(task),
                        inputs={},
                        outputs=metrics,
                        run_type="chain",
                        id=run_id,
                        trace_id=run_id,
                        project_name=project,
                        start_time=started,
                        end_time=ended,
                        extra={"metadata": metadata},
                    )
                if score is not None:
                    await client.create_feedback(
                        run_id,
                        f"{task}_online_score",
                        score=score,
                        trace_id=run_id,
                        session_id=project_id,
                        start_time=started,
                        extend_trace_retention=False,
                        stop_after_attempt=2,
                    )
        except Exception as exc:  # noqa: BLE001 - 계측 실패가 서비스 응답에 영향을 주지 않는다
            logger.warning(
                "LangSmith %s 운영 점수 전송 실패(%s)",
                task,
                _failure(exc, self.settings.langsmith_endpoint),
            )

    async def close(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending)
        if self.tracer is not None:
            # 트레이스는 SDK가 뒤에서 묶어 보낸다. 꺼지기 전에 남은 것을 보낸다.
            await asyncio.to_thread(self.tracer.flush)
            self.tracer.close()
        if self.client is not None:
            await self.client.aclose()


def _run_name(task: str) -> str:
    return "shopping-agent" if task == "agent" else task


service: LangSmith | None = None


def start(settings: Settings) -> None:
    global service
    enabled = (
        settings.langsmith_prompts
        or settings.langsmith_project
        or settings.langsmith_agent_prompt
        or settings.langsmith_agent_project
    )
    if not (enabled or settings.langsmith_eval_mode):
        return
    client = None
    if enabled:
        if not settings.langsmith_api_key.get_secret_value():
            logger.warning("LangSmith를 쓰려면 LANGSMITH_API_KEY가 필요합니다")
        else:
            try:
                client = AsyncClient(
                    **client_options(settings),
                    disable_prompt_cache=True,
                    timeout_ms=1500,
                )
            except Exception as exc:  # noqa: BLE001 - 선택 기능의 오류로 서버 기동을 막지 않는다
                logger.warning("LangSmith 연결 설정 실패(%s)", type(exc).__name__)
    tracer = None
    if client is not None and settings.langsmith_trace:
        try:
            tracer = Client(**client_options(settings), auto_batch_tracing=True)
        except Exception as exc:  # noqa: BLE001 - 선택 기능의 오류로 서버 기동을 막지 않는다
            logger.warning("LangSmith 트레이스 설정 실패(%s)", type(exc).__name__)
    if client is not None or settings.langsmith_eval_mode:
        service = LangSmith(settings, client, tracer)


async def stop() -> None:
    global service
    monitor, service = service, None
    if monitor is not None:
        await monitor.close()
