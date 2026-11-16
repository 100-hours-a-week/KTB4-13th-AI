"""채점 결과를 LangSmith 데이터셋과 실험으로 저장한다.

평가 사례는 만든 질문이라 원문을 같이 올린다. 사례의 질문(Case.inputs)은 데이터셋 예제에, 실행마다 받은 답
(Row.outputs)은 실험 실행에 실린다. 그래야 LangSmith 실험 비교 화면에서 프롬프트 버전별 답을 나란히 본다.
"""

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4, uuid5

from langsmith import Client
from langsmith.utils import LangSmithConflictError, LangSmithNotFoundError


@dataclass(frozen=True)
class Case:
    id: str
    kind: str
    split: str | None = None
    # 사례의 질문. 데이터셋 예제의 입력에 사례 번호와 함께 실린다.
    inputs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Row:
    case_id: str
    seconds: float
    metrics: dict
    scores: dict[str, int | float]
    # 이 실행에서 받은 답과 실패 사유. 실행의 출력에 숫자와 함께 실린다.
    outputs: dict = field(default_factory=dict)


def fingerprint(data: object) -> str:
    return sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


def _inputs(case: Case) -> dict:
    return {"case_id": case.id, "kind": case.kind, **case.inputs}


def _dataset(client: Client, name: str, cases: list[Case]):
    try:
        dataset = client.read_dataset(dataset_name=name)
    except LangSmithNotFoundError:
        try:
            dataset = client.create_dataset(name)
        except LangSmithConflictError:
            dataset = client.read_dataset(dataset_name=name)

    example_ids = {case.id: uuid5(dataset.id, case.id) for case in cases}
    existing = {example.id for example in client.list_examples(dataset_id=dataset.id)}
    examples = [
        {
            "id": example_ids[case.id],
            "inputs": _inputs(case),
            "split": case.split,
        }
        for case in cases
        if example_ids[case.id] not in existing
    ]
    if examples:
        client.create_examples(dataset_id=dataset.id, examples=examples)
    return dataset.id, example_ids


def publish(
    client: Client,
    task: str,
    dataset_key: str,
    cases: list[Case],
    rows: list[Row],
    metadata: dict,
    *,
    label: str,
) -> str:
    if not rows:
        raise ValueError("업로드할 채점 결과가 없습니다")
    cases_by_id = {case.id: case for case in cases}
    repeats = Counter(row.case_id for row in rows)
    if not repeats.keys() <= cases_by_id.keys():
        raise ValueError("평가 결과에 데이터셋에 없는 사례가 있습니다")

    dataset_id, example_ids = _dataset(client, f"bookjeok-{task}-{dataset_key}", cases)
    name = f"bookjeok-{task}-offline-{label}-{uuid4().hex[:8]}"
    common_scores = set.intersection(*(set(row.scores) for row in rows))
    repetitions = set(repeats.values())
    project = client.create_project(
        name,
        reference_dataset_id=dataset_id,
        num_examples=len(repeats) if len(repetitions) == 1 else None,
        num_repetitions=repetitions.pop() if len(repetitions) == 1 else None,
        evaluator_keys=sorted(common_scores) or None,
        metadata=metadata,
    )

    def upload(row: Row) -> None:
        case = cases_by_id[row.case_id]
        end = datetime.now(UTC)
        start = end - timedelta(seconds=row.seconds)
        run_id = uuid4()
        client.create_run(
            f"{task}-offline",
            inputs=_inputs(case),
            outputs={**row.metrics, **row.outputs},
            run_type="chain",
            id=run_id,
            project_name=name,
            reference_example_id=example_ids[case.id],
            start_time=start,
            end_time=end,
            extra={"metadata": {**metadata, "task": task, "split": case.split}},
        )
        for key, score in row.scores.items():
            client.create_feedback(
                run_id,
                key,
                score=score,
                session_id=project.id,
                start_time=start,
                stop_after_attempt=2,
            )

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(upload, rows))
    client.update_project(project.id, end_time=datetime.now(UTC))
    return project.url or name
