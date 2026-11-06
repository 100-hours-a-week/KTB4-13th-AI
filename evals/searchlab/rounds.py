"""검색 시간 회차를 재부팅부터 이어 돌린다(#595). 부하·측정 EC2 에서 돌린다.

    # 무엇을 할지만 본다(재부팅·측정 안 함)
    DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.rounds plans/a1-ef.json --db-instance <실험 RDS 이름>
    # 실제로 돌린다
    DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.rounds plans/a1-ef.json --db-instance <실험 RDS 이름> --yes

회차마다: 앞 CPU 측정 → 실험 RDS 재부팅(또는 정지·시작) → 켜질 때까지 기다림 → DB 가 다시 켜졌는지 확인 →
`measure.py` 로 데우기·처음 조회 → 반복 조회 → 뒤 CPU 측정 → 그 시간대의 CloudWatch 지표. 회차 요약은
`out/<plan>/rounds.jsonl`, 검색 기록은 `out/<plan>/<round>.jsonl` 에 쓴다. AWS 는 aws CLI 로 부른다.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evals.searchlab.measure import OUT_DIR

# 이 글자가 이름에 없는 DB 는 재부팅하지 않는다. 실험 자원 이름에는 모두 exp 를 넣기로 했다(CLOUD #62).
EXPERIMENT_MARK = "exp"
# 회차마다 남기는 RDS 지표(#556 「기록하는 칸」)
RDS_METRICS = (
    "ReadIOPS",
    "ReadLatency",
    "ReadThroughput",
    "CPUUtilization",
    "FreeableMemory",
    "CPUCreditBalance",
    "EBSIOBalance%",
    "EBSByteBalance%",
)
EC2_METRICS = ("CPUUtilization", "CPUCreditBalance")
# 재부팅 앞 이만큼의 지표도 같이 읽는다. 앞뒤 읽기 지연을 견주려는 것이다
BEFORE = timedelta(minutes=10)
# 재부팅 뒤 DB 가 연결을 받을 때까지 기다리는 한도
CONNECT_WAIT_SECONDS = 600
# 짧은 CPU 측정. 회차 앞뒤 값이 크게 다르면(크레딧이 바닥나는 등) 그 회차는 버린다
BENCH_LOOPS = 5_000_000

Runner = Callable[[Sequence[str]], str]


def aws_runner(profile: str | None, region: str) -> Runner:
    def run(args: Sequence[str]) -> str:
        command = ["aws", *args, "--region", region, "--output", "json"]
        if profile:
            command += ["--profile", profile]
        done = subprocess.run(command, capture_output=True, text=True, check=True)
        return done.stdout

    return run


def refusal(db_instance: str) -> str | None:
    if EXPERIMENT_MARK in db_instance:
        return None
    return f"'{db_instance}' 는 이름에 '{EXPERIMENT_MARK}' 가 없어 실험 DB 로 보지 않는다. 재부팅하지 않는다."


def load_plan(path: Path) -> dict[str, Any]:
    """회차 계획. rounds 의 순서대로 돈다. 회차마다 설정만 다르고 나머지는 계획 전체에 같다."""
    plan = json.loads(path.read_text(encoding="utf-8"))
    labels = [r["label"] for r in plan["rounds"]]
    if len(set(labels)) != len(labels):
        # 같은 설정을 두 번 재면(A·B·B·A) 이름 뒤에 몇 번째인지 붙인다
        seen: dict[str, int] = {}
        for r in plan["rounds"]:
            seen[r["label"]] = seen.get(r["label"], 0) + 1
            r["label"] = f"{r['label']}-{seen[r['label']]}"
    plan.setdefault("states", ["first", "repeat"])
    plan.setdefault("warmup", True)
    plan.setdefault("clear", "reboot")
    return plan


def measure_args(
    plan: Mapping[str, Any], rnd: Mapping[str, Any], index: int, state: str
) -> list[str]:
    """한 회차·한 캐시 상태를 재는 measure.py 인자."""
    args = ["--label", f"{plan['name']}/{rnd['label']}", "--state", state]
    if state == "first":
        # 순서는 회차마다 다르게 섞는다. 같은 낱말을 쓰는 사례가 늘 같은 자리에서 데워지지 않게 한다
        args += ["--order-seed", f"{plan['name']}-{index}"]
        if plan["warmup"]:
            args.append("--warmup")
    for key, flag in (
        ("ef_search", "--ef-search"),
        ("random_page_cost", "--random-page-cost"),
        ("effective_io_concurrency", "--effective-io-concurrency"),
        ("vector_timeout", "--vector-timeout"),
    ):
        # 회차 값이 없으면 계획 전체의 값(벡터 한도처럼 모든 회차에 같은 것)
        value = rnd.get(key, plan.get(key))
        if value is not None:
            args += [flag, str(value)]
    if plan.get("kinds"):
        args += ["--kinds", *plan["kinds"]]
    if plan.get("limit"):
        args += ["--limit", str(plan["limit"])]
    if plan.get("server_stats") or plan.get("any_db"):
        # 단계별 DB 쪽 값을 통계를 비우지 않고 앞뒤 차이로 읽는다. 운영 값과 같은 방법으로 견줄 때(시험대)와
        # 실험 DB 가 아닐 때. 검색마다 통계를 비우는 --pgss 는 실험 DB 에서만 돈다
        args.append("--server-stats")
    elif plan.get("pgss", True):
        args.append("--pgss")
    if plan.get("any_db"):
        # 실험 서버를 못 받았을 때 개발 서버에서 하는 예비 측정(#556 「진행 순서」)
        args.append("--any-db")
    return args


def cpu_bench(tries: int = 3) -> float:
    """같은 파이썬 계산을 몇 번 돌려 가장 빠른 값(초). 첫 번은 캐시·클럭 때문에 느릴 수 있어 가장 빠른 값을 쓴다."""
    best = float("inf")
    for _ in range(tries):
        started = time.perf_counter()
        total = 0
        for i in range(BENCH_LOOPS):
            total += i * i
        best = min(best, time.perf_counter() - started)
    return round(best, 3)


def clear_cache(aws: Runner, db_instance: str, how: str) -> None:
    """빈 캐시를 만든다. reboot 는 DB 를 다시 띄우고, stop-start 는 서버를 내렸다 올려 OS 캐시까지 비운다."""
    ident = ["--db-instance-identifier", db_instance]
    if how == "reboot":
        aws(["rds", "reboot-db-instance", *ident])
    elif how == "stop-start":
        aws(["rds", "stop-db-instance", *ident])
        aws(["rds", "wait", "db-instance-stopped", *ident])
        aws(["rds", "start-db-instance", *ident])
    else:
        raise ValueError(f"모르는 캐시 비우기: {how}")
    aws(["rds", "wait", "db-instance-available", *ident])


def metrics(
    aws: Runner,
    namespace: str,
    dimension: tuple[str, str],
    names: Sequence[str],
    start: datetime,
    end: datetime,
) -> dict[str, list[dict]]:
    """1분 간격 평균·최대. 지표마다 실패해도(권한 등) 나머지는 읽는다."""
    out: dict[str, list[dict]] = {}
    for name in names:
        try:
            got = json.loads(
                aws(
                    [
                        "cloudwatch",
                        "get-metric-statistics",
                        "--namespace",
                        namespace,
                        "--metric-name",
                        name,
                        "--dimensions",
                        f"Name={dimension[0]},Value={dimension[1]}",
                        "--start-time",
                        start.isoformat(),
                        "--end-time",
                        end.isoformat(),
                        "--period",
                        "60",
                        "--statistics",
                        "Average",
                        "Maximum",
                    ]
                )
            )
            points = sorted(got.get("Datapoints", []), key=lambda p: p["Timestamp"])
            out[name] = [
                {"t": p["Timestamp"], "avg": p.get("Average"), "max": p.get("Maximum")}
                for p in points
            ]
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            out[name] = [{"error": str(exc)[:200]}]
    return out


def this_instance() -> str | None:
    """이 EC2 의 인스턴스 이름(IMDSv2). EC2 가 아니면 None."""
    base = "http://169.254.169.254/latest"
    try:
        token_req = urllib.request.Request(
            f"{base}/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        )
        token = urllib.request.urlopen(token_req, timeout=1).read().decode()
        req = urllib.request.Request(
            f"{base}/meta-data/instance-id", headers={"X-aws-ec2-metadata-token": token}
        )
        return urllib.request.urlopen(req, timeout=1).read().decode()
    except OSError:
        return None


async def _started(url: str) -> str:
    import asyncpg

    conn = await asyncpg.connect(url, timeout=10)
    try:
        return str(await conn.fetchval("SELECT pg_postmaster_start_time()"))
    finally:
        await conn.close()


def db_started(url: str, wait_seconds: int = CONNECT_WAIT_SECONDS) -> str:
    """DB 가 켜진 시각. 재부팅 직후 연결을 못 받으면 받을 때까지 기다린다."""
    import asyncio

    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return asyncio.run(_started(url))
        except Exception as exc:  # 켜지는 중에는 어떤 오류든 다시 시도한다
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"DB 가 {wait_seconds}초 안에 연결을 받지 않았다: {exc}"
                ) from exc
            time.sleep(5)


def run_plan(
    plan: Mapping[str, Any],
    *,
    url: str,
    db_instance: str | None,
    aws: Runner,
    measure: Callable[[list[str]], None],
    started: Callable[[str], str] = db_started,
    bench: Callable[[], float] = cpu_bench,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    write: Callable[[dict], None],
    ec2_instance: str | None = None,
) -> None:
    """계획의 회차를 차례로 돈다. db_instance 가 None 이면 재부팅 없이 잰다(로컬 시험·개발 서버 예비 측정)."""
    for index, rnd in enumerate(plan["rounds"], start=1):
        summary: dict[str, Any] = {
            "type": "round",
            "plan": plan["name"],
            "index": index,
            "round": dict(rnd),
        }
        summary["bench_before"] = bench()
        t0 = now()
        before = started(url)
        if db_instance is not None:
            clear_cache(aws, db_instance, plan["clear"])
            after = started(url)
            if after == before:
                # 켜진 시각이 그대로면 캐시가 비지 않았다. 이어 재면 캐시가 찬 채로 잰 회차가 섞인다
                raise RuntimeError(
                    f"{rnd['label']}: DB 가 다시 켜지지 않았다(켜진 시각 {after})"
                )
            summary["db_started"] = after
        summary["cleared"] = None if db_instance is None else plan["clear"]
        summary["cleared_seconds"] = round((now() - t0).total_seconds(), 1)
        for state in plan["states"]:
            t = now()
            measure(measure_args(plan, rnd, index, state))
            summary[f"{state}_seconds"] = round((now() - t).total_seconds(), 1)
        summary["bench_after"] = bench()
        end = now()
        summary["window"] = [t0.isoformat(), end.isoformat()]
        if db_instance is not None:
            summary["rds"] = metrics(
                aws,
                "AWS/RDS",
                ("DBInstanceIdentifier", db_instance),
                RDS_METRICS,
                t0 - BEFORE,
                end,
            )
        if ec2_instance is not None:
            summary["ec2"] = metrics(
                aws,
                "AWS/EC2",
                ("InstanceId", ec2_instance),
                EC2_METRICS,
                t0 - BEFORE,
                end,
            )
        write(summary)
        print(
            f"회차 {index}/{len(plan['rounds'])} {rnd['label']} 끝",
            file=sys.stderr,
            flush=True,
        )


def describe(plan: Mapping[str, Any], db_instance: str | None) -> list[str]:
    """--yes 없이 돌릴 때 보여 줄 일. 재부팅 횟수와 measure 인자."""
    lines = [
        f"계획 {plan['name']}: 회차 {len(plan['rounds'])}개, 캐시 비우기 {plan['clear'] if db_instance else '안 함'}"
    ]
    for index, rnd in enumerate(plan["rounds"], start=1):
        for state in plan["states"]:
            lines.append(
                f"  {index}. {' '.join(measure_args(plan, rnd, index, state))}"
            )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="검색 시간 회차를 재부팅부터 이어 돌린다"
    )
    parser.add_argument("plan", help="회차 계획 파일(plans/*.json)")
    parser.add_argument(
        "--db-instance", help="재부팅할 실험 RDS 이름. 없으면 재부팅 없이 잰다"
    )
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION", "ap-northeast-2")
    )
    parser.add_argument(
        "--aws-profile",
        help="맥에서 돌릴 때 쓸 aws 프로필. EC2 에서는 서버 역할을 쓴다",
    )
    parser.add_argument(
        "--yes", action="store_true", help="실제로 돌린다. 없으면 할 일만 보여 준다"
    )
    args = parser.parse_args(argv)

    plan_path = Path(args.plan)
    if not plan_path.is_absolute() and not plan_path.exists():
        plan_path = Path(__file__).parent / plan_path
    plan = load_plan(plan_path)
    if args.db_instance is not None:
        reason = refusal(args.db_instance)
        if reason:
            print(reason, file=sys.stderr)
            return 2
    print("\n".join(describe(plan, args.db_instance)))
    if not args.yes:
        print("실제로 돌리려면 --yes 를 붙인다")
        return 0

    from app.core.config import get_settings

    url = get_settings().database_url
    out = OUT_DIR / plan["name"] / "rounds.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    def measure(margs: list[str]) -> None:
        subprocess.run(
            [sys.executable, "-m", "evals.searchlab.measure", *margs], check=True
        )

    with out.open("a", encoding="utf-8") as fh:

        def write(line: dict) -> None:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            fh.flush()

        run_plan(
            plan,
            url=url,
            db_instance=args.db_instance,
            aws=aws_runner(args.aws_profile, args.region),
            measure=measure,
            write=write,
            ec2_instance=this_instance(),
        )
    print(f"회차 요약을 {out} 에 썼다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
