"""기존 태스크 평가를 실행하고 점수를 LangSmith 실험으로 저장한다."""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from xml.etree import ElementTree

from langsmith import Client
from langsmith.utils import LangSmithError

from app.core.config import Settings
from app.core.langsmith import client_options
from evals.langsmith_publish import Case, Row, fingerprint, publish

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = ("agent", "chat", "extraction", "search", "feed")
REGRESSIONS = {
    "photo": (
        "test_photo_read",
        "test_photo_match_cover",
        "test_photo_match_shelf",
        "test_photo_scan",
        "test_shelf_router",
    ),
    "embedding": ("test_embedding_gateway", "test_embeddings_router"),
    "profile": (
        "test_profile_compute",
        "test_profile_service",
        "test_profile_router",
        "test_profile_interests",
        "test_profile_kafka_consumer",
    ),
}
TASKS = (*RUNNERS, *REGRESSIONS)
PROMPTS = (
    "agent",
    "chat.spec",
    "chat.relation",
    "chat.scope",
    "chat.recall",
    "chat.cards",
    "chat.pick",
    "chat.lane",
    "chat.answer",
    "extraction",
    "photo.read",
)


def _case_bundle(task: str, path: Path | None) -> dict:
    return json.loads(
        (path or ROOT / "evals" / task / "cases.json").read_text(encoding="utf-8")
    )


def _versions(groups) -> dict[str, str]:
    values: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        for name, version in group.items():
            values[name].add(version)
    if mixed := sorted(name for name, versions in values.items() if len(versions) > 1):
        raise ValueError(
            f"한 실험에 여러 프롬프트 버전이 섞였습니다 ({', '.join(mixed)})"
        )
    return {name: next(iter(versions)) for name, versions in values.items()}


def _chat(raw: dict, bundle: dict) -> tuple[list[Row], dict]:
    from evals.chat.grade import OBSERVE, grade_case

    cases = {str(case["no"]): case for case in bundle["cases"]}
    rows = []
    versions = []
    for round_no, turn_map in enumerate(raw["runs"], start=1):
        for case_id, turns in turn_map.items():
            case = cases[case_id]
            if case["expect"]["action"] == OBSERVE:
                continue
            failures = grade_case(case, turns, raw.get("books") or {})
            versions.extend(turn.get("prompt_versions", {}) for turn in turns)
            rows.append(
                Row(
                    case_id,
                    sum(turn["sec"] for turn in turns),
                    {
                        "repetition": round_no,
                        "turns": len(turns),
                        "request_errors": sum(turn["status"] != 200 for turn in turns),
                        "degraded_turns": sum(
                            bool(turn.get("degraded")) for turn in turns
                        ),
                        "failures": len(failures),
                    },
                    {"chat_offline_score": 0 if failures else 100},
                    {
                        "answers": [_chat_answer(turn) for turn in turns],
                        "why": failures,
                    },
                )
            )
    return rows, {
        "model": raw["meta"].get("model"),
        "release_sha": raw["meta"].get("server_version"),
        "prompt_versions": _versions(versions),
    }


def _chat_answer(turn: dict) -> dict:
    """한 턴의 답. 카드는 제목·저자·한 줄 이유만 싣는다(가격·점수는 숫자 칸에 따로 있다)."""
    return {
        "message": turn.get("msg"),
        "reply": turn.get("reply"),
        "followup": turn.get("followup"),
        "cards": [
            {key: card.get(key) for key in ("title", "author", "reason_short")}
            for card in turn.get("cards") or []
        ],
        "error": turn.get("error"),
    }


def agent_answer(result: dict) -> dict:
    """⑦ 실행 하나의 답과 실패 사유. 오프라인 실험 화면에서 버전별로 견준다."""
    record = result["record"]
    return {
        "reply": record.get("reply"),
        "actions": (record.get("data") or {}).get("actions"),
        "removed": record.get("removed"),
        "error": record.get("error"),
        "why": result["grade"].get("failures", []),
    }


def case_inputs(task: str, case: dict) -> dict:
    """데이터셋 예제에 실을 사례의 질문. ③은 여러 턴의 말, ⑦은 요청의 말과 카드다."""
    if task == "chat":
        return {"turns": case["turns"]}
    if task == "agent":
        request = case["request"]
        return {
            "message": request.get("message"),
            "context_cards": request.get("context_cards"),
        }
    return {}


def _agent(raw: dict) -> tuple[list[Row], dict]:
    rows = [
        Row(
            result["id"],
            result["record"]["ms"] / 1000,
            {"status": result["record"]["status"], "repetition": result["run"]},
            {"agent_offline_score": 100 * result["grade"]["passed"]},
            agent_answer(result),
        )
        for result in raw["results"]
    ]
    summary = raw["summary"]
    return rows, {
        key: summary[key]
        for key in ("model", "prompt_version", "book_info")
        if key in summary
    }


def _extraction(raw: dict) -> tuple[list[Row], dict]:
    rows = [
        Row(
            row["case_id"],
            row["seconds"],
            {
                key: row[key]
                for key in ("status", "expected", "matched", "extracted", "extra")
            },
            {"extraction_offline_score": 100 * row["passed"]},
        )
        for row in raw["runs"]
    ]
    return rows, {
        "model": raw.get("model"),
        "prompt_version": raw.get("prompt_version"),
        "prompt_versions": _versions(
            row.get("prompt_versions", {}) for row in raw["runs"]
        ),
    }


def _search_scores(row: dict) -> dict[str, int | float]:
    from evals.search.scoring import EMPTY_KINDS, HIT_AT, TITLE_KINDS

    scores = {"search_offline_valid": 100 * row["ok"]}
    if not row["ok"]:
        return scores
    kind = row["kind"]
    if kind in TITLE_KINDS:
        rank = row["rank"]
        scores.update(
            {f"search_hit_{k}": 100 * (rank is not None and rank <= k) for k in HIT_AT}
        )
    elif kind == "author":
        scores["search_author_share"] = 100 * row["author_share"]
    elif kind == "filter":
        scores["search_filter_pass"] = 100 * (row["violations"] == 0)
    elif kind == "descriptive":
        scores["search_ndcg_10"] = 100 * row["ndcg"]
    elif kind in EMPTY_KINDS:
        scores["search_expected_empty"] = 100 * (row["returned"] == 0)
    return scores


def _feed_scores(row: dict) -> dict[str, int | float]:
    scores = {"feed_offline_valid": 100 * row["ok"]}
    if row["ok"] and row["show"]:
        scores["feed_interest_coverage"] = (
            100 * (len(row["show"]) - row["missing"]) / len(row["show"])
        )
    if row["ok"] and row["owned"] is not None:
        scores["feed_owned_exclusion"] = 100 * (row["owned"] == 0)
    return scores


def _ranked(raw: dict, task: str) -> tuple[list[Row], dict]:
    rows = [
        Row(
            row["id" if task == "search" else "case_id"],
            row["seconds"],
            {
                key: row[key]
                for key in (
                    "ok",
                    "returned",
                    "rank",
                    "unlabeled",
                    "least",
                    "missing",
                    "owned",
                    "editions",
                )
                if key in row
            },
            _search_scores(row) if task == "search" else _feed_scores(row),
        )
        for row in raw["scores"]
    ]
    database = raw.get("database") or {}
    return rows, {
        "catalog": {
            key: database[key] for key in ("books", "with_vectors") if key in database
        },
        **(
            {"labels_fingerprint": raw["labels_fingerprint"]}
            if "labels_fingerprint" in raw
            else {}
        ),
    }


def load_report(
    task: str, path: Path, cases_path: Path | None = None
) -> tuple[str, list[Case], list[Row], dict]:
    if task in REGRESSIONS:
        cases, rows = [], []
        for test in ElementTree.parse(path).iter("testcase"):
            kind = test.attrib["classname"].rsplit(".", 1)[-1]
            if kind not in REGRESSIONS[task]:
                continue
            case_id = f"{kind}-{fingerprint(test.attrib['name'])}"
            cases.append(Case(case_id, kind))
            if test.find("skipped") is None:
                passed = test.find("failure") is None and test.find("error") is None
                rows.append(
                    Row(
                        case_id,
                        float(test.attrib.get("time", 0)),
                        {"passed": passed},
                        {f"{task}_regression_score": 100 * passed},
                    )
                )
        return (
            f"regression-{fingerprint(sorted(case.id for case in cases))}",
            cases,
            rows,
            {
                "evaluation": "regression",
                "skipped": len(cases) - len(rows),
            },
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    bundle = _case_bundle(task, cases_path)
    if task == "chat":
        rows, metadata = _chat(raw, bundle)
        version = raw["meta"]["cases_version"]
    elif task == "agent":
        rows, metadata = _agent(raw)
        version = raw["summary"]["version"]
    elif task == "extraction":
        rows, metadata = _extraction(raw)
        version = raw["bundle_version"]
    else:
        rows, metadata = _ranked(raw, task)
        version = raw["bundle_version"]
    if version != bundle["version"]:
        raise ValueError(
            f"결과의 사례 버전 {version}과 현재 사례 버전 {bundle['version']}이 다릅니다"
        )

    cases = [
        Case(
            str(case["no"] if task == "chat" else case["id"]),
            case["kind"],
            case.get("split") or case.get("source"),
            case_inputs(task, case),
        )
        for case in bundle["cases"]
    ]
    benchmark = (
        [bundle, metadata["labels_fingerprint"]]
        if "labels_fingerprint" in metadata
        else bundle
    )
    return (
        f"v{version}-{fingerprint(benchmark)}",
        cases,
        rows,
        {**metadata, "dataset_version": version},
    )


def _seed(key: str, name: str, client: Client) -> str:
    from app.agent.langsmith import BASE_TEMPLATE
    from app.chat import asking, picking, recall, topic_switch
    from app.extraction.prompts import EXTRACTION_PROMPT
    from app.gate.judge import LANE_PROMPT
    from app.photo.read import READ_TEMPLATE
    from app.routers.chat import ANSWER_PROMPT, CARD_PROMPT, SPEC_PROMPT

    templates = {
        "agent": BASE_TEMPLATE,
        "chat.spec": SPEC_PROMPT,
        "chat.relation": topic_switch.RELATION_PROMPT,
        "chat.scope": asking.SCOPE_PROMPT,
        "chat.recall": recall.RECALL_PROMPT,
        "chat.cards": CARD_PROMPT,
        "chat.pick": picking.JUDGE_PROMPT,
        "chat.lane": LANE_PROMPT,
        "chat.answer": ANSWER_PROMPT,
        "extraction": EXTRACTION_PROMPT,
        "photo.read": READ_TEMPLATE,
    }
    if client.get_prompt(name) is not None:
        raise ValueError(f"이미 존재하는 프롬프트입니다 ({name})")
    return client.push_prompt(name, object=templates[key], is_public=False)


def _run(task: str, output: Path, extra: list[str]) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    if task in REGRESSIONS:
        args = [
            "-m",
            "pytest",
            *(f"tests/{name}.py" for name in REGRESSIONS[task]),
            "--junitxml",
            str(output),
            *extra,
        ]
    else:
        args = ["-m", f"evals.{task}.run", "--out", str(output), *extra]
        if task in ("extraction", "search", "feed"):
            args.append("--yes")
    return subprocess.run([sys.executable, *args], cwd=ROOT, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        at = argv.index("--")
        argv, extra = argv[:at], argv[at + 1 :]
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "publish"):
        sub = commands.add_parser(command)
        sub.add_argument("task", choices=TASKS)
        sub.add_argument("report", type=Path)
        sub.add_argument("--label", default="run")
        sub.add_argument("--local", action="store_true", help="점수를 로컬에만 출력")
        sub.add_argument("--case-file", type=Path, help="결과에 사용한 별도 사례 파일")
    seed = commands.add_parser("seed")
    seed.add_argument("prompt", choices=PROMPTS)
    seed.add_argument("--name")
    args = parser.parse_args(argv)

    if extra and args.command != "run":
        parser.error("-- 뒤의 인자는 run에서만 쓸 수 있습니다")
    if any(
        value.split("=", 1)[0] in {"--out", "--junitxml", "--junit-xml"}
        for value in extra
    ):
        parser.error("결과 경로는 run의 report 위치에 지정합니다")
    local = getattr(args, "local", False)
    settings = None if local else Settings(database_url="")
    if settings is not None and not settings.langsmith_api_key.get_secret_value():
        parser.error(
            "LANGSMITH_API_KEY가 필요합니다. 로컬에서 점수만 보려면 --local을 붙입니다"
        )

    try:
        if args.command == "seed":
            name = (
                args.name
                or settings.langsmith_prompts.get(args.prompt)
                or (settings.langsmith_agent_prompt if args.prompt == "agent" else "")
                or ("bookjeok-" + args.prompt.replace(".", "-"))
            )
            client = Client(**client_options(settings), auto_batch_tracing=False)
            try:
                print(_seed(args.prompt, name.partition(":")[0], client))
            finally:
                client.close()
            return 0

        status = 0
        report = args.report.resolve()
        if args.command == "run":
            previous_write = report.stat().st_mtime_ns if report.is_file() else None
            if args.task == "agent" and args.case_file:
                extra = [*extra, "--cases", str(args.case_file.resolve())]
            status = _run(args.task, report, extra)
            if status != 0 and not (status == 1 and args.task in REGRESSIONS):
                return status
            if not report.is_file() or report.stat().st_mtime_ns == previous_write:
                raise ValueError("이번 실행이 평가 원본을 저장하지 않았습니다")
        dataset, cases, rows, metadata = load_report(args.task, report, args.case_file)
        if not rows:
            raise ValueError("채점된 사례가 없습니다")
        scores = defaultdict(list)
        for row in rows:
            for metric, value in row.scores.items():
                scores[metric].append(value)
        for metric, values in sorted(scores.items()):
            print(f"{metric} {fmean(values):.2f}/100 (n={len(values)})")
        if settings is not None:
            client = Client(
                **client_options(settings),
                auto_batch_tracing=False,
                tracing_sampling_rate=1,
                timeout_ms=5000,
            )
            try:
                print(
                    publish(
                        client,
                        args.task,
                        dataset,
                        cases,
                        rows,
                        metadata,
                        label=args.label,
                    )
                )
            finally:
                client.close()
        return status
    except LangSmithError as exc:
        parser.error(
            f"LangSmith 요청 실패({type(exc).__name__}). 저장한 평가 원본은 publish로 다시 올릴 수 있습니다."
        )
    except (OSError, KeyError, ValueError, ElementTree.ParseError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
