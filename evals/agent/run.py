"""⑦ 쇼핑 에이전트 평가 실행(#381) — cases.json 의 요청을 실제 모델로 돌려 채점한다.

사례마다 가짜 BE 를 서가와 시작 상태로 만들고, 서버의 `/agent/act` 와 같은 길(그래프 → 답변 검사, 턴 시간
제한)로 모델을 부른다. 서버를 띄우지 않고 같은 프로세스에서 돈다. 사례마다 시작 상태가 달라서다.
채점은 grade.py 가 한다(코드로만 판정, LLM 을 쓰지 않는다).

LLM 을 실제로 부르므로 비용이 든다(59개 × 3번이면 호출 약 350번). CI 에서는 돌리지 않는다.

사용:
    uv run python -m evals.agent.run --repeat 3 --label baseline
    uv run python -m evals.agent.run --kinds add,pick --repeat 1 --model openai/gpt-5.4-mini
    uv run python -m evals.agent.run --ids add-01,pick-02 --concurrency 1

모델은 --model, 없으면 AGENT_LLM_MODEL_ID, 그것도 없으면 LLM_MODEL_ID 다. 결과 원본은 --out(기본
evals/agent/results/<시각>-<label>.json)에 저장하고 요약 표를 화면에 낸다.
"""

import argparse
import asyncio
import json
import logging
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.agent.compare import LlmComparer
from app.agent.graph import run_agent
from app.agent.reply import GroundingFailed, finish
from app.agent.schemas import parse_request
from app.core.config import get_settings
from app.gateway.commerce_fake import FakeBook, FakeCommerce
from app.gateway.llm import LLMUnavailableError, get_chat_model
from evals.agent import grade

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_CONCURRENCY = 4


async def build_commerce(data: dict, case: dict) -> tuple[FakeCommerce, int]:
    """README 의 순서대로 시작 상태를 만든다. 판매 중지 책이 이미 담긴 장바구니를 만들려고, 처음엔 모두
    판매 중으로 두고 담은 뒤에 판매 중지시킨다. (가짜 BE, 요청한 사용자 번호)를 돌려준다."""
    uid = case["request"]["user_id"]
    others = case["state"].get("others", [])
    fake = FakeCommerce(
        [
            FakeBook(
                b["isbn"],
                b["title"],
                b["price"],
                b["stock"],
                description=b["description"],
            )
            for b in data["catalog"]
        ],
        users=[uid, *(o["user_id"] for o in others)],
    )
    for line in case["state"]["cart"]:
        await fake.add_item(uid, line["isbn"], line["qty"])
    for other in others:
        for line in other["cart"]:
            await fake.add_item(other["user_id"], line["isbn"], line["qty"])
    for book in data["catalog"]:
        if not book["active"]:
            fake.discontinue(book["isbn"])
    return fake, uid


def _error_status(exc: BaseException) -> int:
    """서버 라우터와 같은 매핑: LLM 장애 503, 시간 초과 504, 그 밖은 500."""
    if isinstance(exc, TimeoutError):
        return 504
    if isinstance(exc, LLMUnavailableError):
        return 504 if _is_timeout(exc) else 503
    return 500


def _is_timeout(exc: BaseException) -> bool:
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, TimeoutError) or "Timeout" in type(cause).__name__:
            return True
        cause = cause.__cause__ or cause.__context__
    return False


async def run_case(data: dict, case: dict, model: Any, *, timeout: float) -> dict:
    """사례 하나를 한 번 돌려 실행 기록을 만든다. 오류도 기록으로 남기고 예외를 던지지 않는다."""
    fake, uid = await build_commerce(data, case)
    others = [o["user_id"] for o in case["state"].get("others", [])]
    record: dict[str, Any] = {
        "status": 200,
        "reply": "",
        # LLM 이 쓴 마지막 글(답변 검사 전). 검사가 지운 문장이 무엇이었는지, 실제 모델이 어떤 말투로 답하는지
        # 모으는 데 쓴다. 서버 로그에는 원문을 남기지 않지만(대화 원문), 평가용 가짜 사용자의 글은 남겨도 된다
        "raw_reply": "",
        "data": None,
        "removed": [],
        "error": None,
        "start_cart": fake.cart_state(uid),
        "start_others": {u: fake.cart_state(u) for u in others},
        "llm_calls": 0,
        "iterations": 0,
        "hit_cap": False,
        "tokens_in": 0,
        "tokens_out": 0,
    }
    req = parse_request(case["request"])
    if req is None:
        record.update(status=400, error="요청 형식 오류(사례 파일 문제)")
    else:
        started = time.perf_counter()
        try:
            comparer = LlmComparer(fake, model)
            async with asyncio.timeout(timeout):
                run = await run_agent(req, model=model, gateway=fake, comparer=comparer)
            finished = finish(run)
            record.update(
                raw_reply=run.final_text,
                reply=finished.data.reply,
                data=finished.data.model_dump(mode="json"),
                removed=finished.removed,
                llm_calls=run.llm_calls,
                iterations=run.iterations,
                hit_cap=run.hit_cap,
                tokens_in=sum(t.input_tokens for t in run.timings),
                tokens_out=sum(t.output_tokens for t in run.timings),
            )
        except GroundingFailed as e:
            record.update(
                status=422,
                removed=e.removed,
                error="grounding_failed",
                raw_reply=run.final_text,
            )
        except Exception as e:  # noqa: BLE001 - 어떤 오류든 기록으로 남기고 다음 사례로 간다
            record.update(status=_error_status(e), error=type(e).__name__)
        record["ms"] = (time.perf_counter() - started) * 1000
    record.setdefault("ms", 0.0)
    record["cart"] = fake.cart_state(uid)
    record["others"] = {u: fake.cart_state(u) for u in others}
    return record


async def run_all(
    data: dict,
    cases: list[dict],
    model: Any,
    *,
    repeat: int,
    concurrency: int,
    timeout: float,
    on_done=None,
) -> list[dict]:
    """사례마다 repeat 번 돌리고 채점한다. 순서는 (반복, 사례)대로 유지한다."""
    gate = asyncio.Semaphore(concurrency)

    async def one(case: dict, n: int) -> dict:
        async with gate:
            record = await run_case(data, case, model, timeout=timeout)
        result = {
            "id": case["id"],
            "kind": case["kind"],
            "group": data["kinds"][case["kind"]]["group"],
            "run": n,
            "record": record,
            "grade": grade.grade_case(case, record),
        }
        if on_done is not None:
            on_done(result)
        return result

    return list(
        await asyncio.gather(*(one(c, n) for n in range(1, repeat + 1) for c in cases))
    )


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _tally(results: list[dict], key: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in results:
        row = out.setdefault(r[key], {"n": 0, "passed": 0})
        row["n"] += 1
        row["passed"] += r["grade"]["passed"]
    return out


def summarize(data: dict, results: list[dict], *, model: str, repeat: int) -> dict:
    records = [r["record"] for r in results]
    ms = [rec["ms"] for rec in records]
    okay = [rec for rec in records if rec["status"] == 200]
    cases = {c["id"]: c for c in data["cases"]}

    unconfirmed = sum(
        1
        for r in results
        if grade.allowed_outcomes(cases[r["id"]]) == ["confirm"]
        and r["record"]["status"] == 200
        and r["record"]["cart"] != r["record"]["start_cart"]
    )
    injection = [r for r in results if r["kind"] == "injection"]

    failed: dict[str, dict[str, Any]] = {}
    for r in results:
        if r["grade"]["passed"]:
            continue
        row = failed.setdefault(
            r["id"], {"id": r["id"], "runs_failed": 0, "failures": []}
        )
        row["runs_failed"] += 1
        for text in r["grade"]["failures"]:
            if text not in row["failures"]:
                row["failures"].append(text)

    return {
        "version": data["version"],
        "model": model,
        "repeat": repeat,
        "total": len(results),
        "passed": sum(r["grade"]["passed"] for r in results),
        "by_group": _tally(results, "group"),
        "by_kind": _tally(results, "kind"),
        "metrics": {
            "llm_calls_avg": statistics.fmean(r["llm_calls"] for r in okay)
            if okay
            else 0,
            "ms_median": statistics.median(ms) if ms else 0,
            "ms_p95": _percentile(ms, 0.95),
            "ms_max": max(ms, default=0),
            "tokens_in_avg": statistics.fmean(r["tokens_in"] for r in okay)
            if okay
            else 0,
            "tokens_out_avg": statistics.fmean(r["tokens_out"] for r in okay)
            if okay
            else 0,
            "hit_cap": sum(1 for r in okay if r["hit_cap"]),
            "status_counts": dict(Counter(str(r["status"]) for r in records)),
            "removed_reasons": dict(Counter(x for r in records for x in r["removed"])),
            "executed_without_confirm": unconfirmed,
            "injection_failed": [
                sum(not r["grade"]["passed"] for r in injection),
                len(injection),
            ],
        },
        "failed_cases": sorted(failed.values(), key=lambda row: row["id"]),
    }


def _pct(passed: int, n: int) -> str:
    return f"{passed}/{n} ({passed / n:.0%})" if n else "-"


def format_report(summary: dict) -> str:
    m = summary["metrics"]
    lines = [
        (
            f"## ⑦ 평가 결과 — 모델 `{summary['model']}`, 사례 버전 {summary['version']}, "
            f"{summary['repeat']}번 반복"
        ),
        "",
        f"**통과 {_pct(summary['passed'], summary['total'])}**",
        "",
        "| 묶음 | 통과 |",
        "| --- | --- |",
    ]
    for group, row in sorted(summary["by_group"].items()):
        lines.append(f"| {group} | {_pct(row['passed'], row['n'])} |")
    lines += ["", "| 종류 | 통과 |", "| --- | --- |"]
    for kind, row in summary["by_kind"].items():
        lines.append(f"| {kind} | {_pct(row['passed'], row['n'])} |")
    bad, total = m["injection_failed"]
    lines += [
        "",
        "| 지표 | 값 |",
        "| --- | --- |",
        f"| 턴당 LLM 호출 | {m['llm_calls_avg']:.2f} |",
        f"| 지연(ms) 중앙 / p95 / 최대 | {m['ms_median']:,.0f} / {m['ms_p95']:,.0f} / {m['ms_max']:,.0f} |",
        f"| 토큰(평균) 입력 / 출력 | {m['tokens_in_avg']:,.0f} / {m['tokens_out_avg']:,.0f} |",
        f"| 반복 상한에 걸림 | {m['hit_cap']}회 |",
        f"| 확인 없이 실행된 조합 | {m['executed_without_confirm']}회 |",
        f"| 속이는 입력에 넘어감 | {bad}/{total}회 |",
        f"| 응답 상태 | {m['status_counts']} |",
        f"| 답변 검사가 지운 문장(이유별) | {m['removed_reasons'] or '없음'} |",
    ]
    if summary["failed_cases"]:
        lines += ["", "### 실패한 사례", ""]
        for row in summary["failed_cases"]:
            lines.append(
                f"- `{row['id']}` {row['runs_failed']}회 실패: "
                + "; ".join(row["failures"])
            )
    return "\n".join(lines)


def _select(data: dict, args: argparse.Namespace) -> list[dict]:
    cases = data["cases"]
    if args.ids:
        wanted = set(args.ids.split(","))
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            raise SystemExit(f"없는 사례 번호: {sorted(unknown)}")
        cases = [c for c in cases if c["id"] in wanted]
    if args.kinds:
        kinds = set(args.kinds.split(","))
        unknown = kinds - set(data["kinds"])
        if unknown:
            raise SystemExit(f"없는 종류: {sorted(unknown)}")
        cases = [c for c in cases if c["kind"] in kinds]
    if args.group:
        cases = [c for c in cases if data["kinds"][c["kind"]]["group"] == args.group]
    return cases


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--model",
        default="",
        help="모델 ID. 없으면 AGENT_LLM_MODEL_ID, LLM_MODEL_ID 순",
    )
    parser.add_argument("--repeat", type=int, default=1, help="사례마다 돌릴 횟수")
    parser.add_argument(
        "--ids", default="", help="쉼표로 구분한 사례 번호(예: add-01,pick-02)"
    )
    parser.add_argument("--kinds", default="", help="쉼표로 구분한 종류(예: add,pick)")
    parser.add_argument("--group", choices=["typical", "guard"], default="")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--label", default="run", help="결과 파일 이름에 붙이는 말")
    parser.add_argument("--out", default="", help="결과 원본 JSON 위치")
    args = parser.parse_args(argv)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if settings.llm_mock:
        raise SystemExit("LLM_MOCK 이 켜져 있어 실제 모델을 부를 수 없다")
    model_id = args.model or settings.agent_llm_model_id or settings.llm_model_id
    data = grade.load_cases()
    cases = _select(data, args)
    if not cases:
        raise SystemExit("고른 사례가 없다")

    model = get_chat_model(json_mode=False, model_id=model_id)
    total = len(cases) * args.repeat
    print(
        f"모델 {model_id}: 사례 {len(cases)}개 × {args.repeat}번 = {total}번",
        flush=True,
    )

    done = 0

    def progress(result: dict) -> None:
        nonlocal done
        done += 1
        mark = "." if result["grade"]["passed"] else "F"
        print(mark, end="" if done % 50 else f" {done}/{total}\n", flush=True)

    results = asyncio.run(
        run_all(
            data,
            cases,
            model,
            repeat=args.repeat,
            concurrency=args.concurrency,
            timeout=settings.agent_turn_timeout_seconds,
            on_done=progress,
        )
    )
    print()
    summary = summarize(data, results, model=model_id, repeat=args.repeat)
    stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else RESULTS_DIR / f"{stamp}-{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"summary": summary, "results": results}, ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    print(format_report(summary))
    print(f"\n결과 원본: {out}")


if __name__ == "__main__":
    main()
