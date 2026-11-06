"""비동기 홈 목록 부하를 잰다(#603). 부하·측정 EC2 에서 실험 AI 서버로 보낸다.

    # 31명을 한 사람씩 연달아(밤 배치처럼)
    DATABASE_URL=postgresql://<실험 DB> AI_SERVICE_TOKEN=<실험 토큰> \
        uv run python -m evals.feed.async_load --base-url http://<실험 AI>:8000 --pattern sequential --users 31 --yes
    # 10명을 한꺼번에(온보딩·결제가 몰릴 때처럼), 끝에 운영처럼 120초 기다린 뒤 서비스를 내려 버린 수를 센다
    ... --pattern burst --users 10 --stop-after 120 --cluster <실험 클러스터> --service <실험 서비스> --yes

[#582](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/582) 의 켜는 기준을 재는 도구다. 서버는 홈 목록을 응답
뒤에 만들도록 켜 둔다(PROFILE_FEED_ASYNC=true). 재는 것:

- 사용자마다 프로필 계산(⑥) 응답 시간과, 목록이 다 만들어지기까지 걸린 시간(보낸 때부터 목록 칸의 요청
  번호표 `pending` 이 지워질 때까지. DB 를 읽어 본다)
- 보내기 전과 보내는 동안의 검색(① /search) 시간. 일정한 간격으로 부른다
- `--stop-after` 를 주면, 다 보낸 뒤 그만큼 새 요청 없이 기다렸다가(운영 ECS 는 대상 그룹에서 빠진 뒤 120초를
  기다려 종료 신호를 보낸다) 실험 서비스를 0개로 내리고, 끝내 안 만들어진 사용자(버린 목록)를 센다

결과는 `results/async-<label>.jsonl` 에 쓴다. 끝나면 가상 사용자의 프로필을 지운다.
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CASES_PATH = Path(__file__).with_name("cases.json")
SEARCH_CASES_PATH = Path(__file__).parents[1] / "search" / "cases.json"
RESULTS_DIR = Path(__file__).with_name("results")
PROFILE_URL = "/preferences/profile"
SEARCH_URL = "/search"
EMBED_URL = "/embeddings"
EMBED_BATCH = 256  # 명세 ② 의 한 번 호출 상한
# 취향 기억 벡터의 용도. ⑤ 가 붙이는 것과 같다(app.extraction.service.MEMORY_VECTOR_PURPOSE, 테스트로 맞춰 본다).
# 앱 모듈을 읽지 않으려고 값만 둔다. 부하·측정 EC2 에서 앱 설정 없이 돌게 한다
MEMORY_VECTOR_PURPOSE = "query"
CALL_TIMEOUT_SECONDS = 60
# 목록이 다 만들어지기를 기다리는 한도. 켜는 기준이 "마지막 목록이 5분 안"이라 그보다 넉넉히 둔다
DRAIN_LIMIT_SECONDS = 600
POLL_SECONDS = 0.5
# 이름에 이 글자가 없는 서비스·클러스터는 내리지 않는다(실험 자원 이름에는 exp 를 넣기로 함, CLOUD #62)
EXPERIMENT_MARK = "exp"
EVAL_DB_SUFFIX = "_eval"
_PENDING_SQL = "SELECT user_id FROM taste_profile WHERE user_id = ANY($1::int[]) AND feed ? 'pending'"
_CLEANUP_SQL = "DELETE FROM taste_profile WHERE user_id = ANY($1::int[])"

Post = Callable[[str, dict], Awaitable[tuple[int, dict]]]


def pick_users(cases: Sequence[Mapping[str, Any]], n: int) -> list[Mapping[str, Any]]:
    """앞에서부터 n 명. 재료가 있는 사용자(취향 벡터를 만드는 사람)만 쓴다. 모자라면 ValueError."""
    usable = [c for c in cases if not c["expect"].get("cold_start")]
    if len(usable) < n:
        raise ValueError(f"재료가 있는 가상 사용자가 {len(usable)}명뿐이다(필요 {n})")
    return usable[:n]


def probe_queries(cases: Sequence[Mapping[str, Any]], n: int = 50) -> list[str]:
    """검색 시간을 볼 검색어. 검색 평가 묶음의 제목 검색어 앞에서부터."""
    return [c["query"] for c in cases if c["kind"] == "title"][:n]


def profile_body(case: Mapping[str, Any], memories: list[dict], label: str) -> dict:
    user = case["user"]
    return {
        "user_id": case["user_id"],
        "idempotency_key": f"load_{label}_{case['id']}",
        "onboarding": user["onboarding"],
        "memories": memories,
        "history": user["history"],
    }


def memory_body(
    memories: Sequence[Mapping[str, Any]], vectors: Sequence[Sequence[float]]
) -> list[dict]:
    """취향 기억의 벡터는 ⑤ 가 붙이는 것처럼 핵심어로 만든다(evals/feed/run.py 와 같다)."""
    return [
        {**m, "vector": list(v), "dim": len(v)}
        for m, v in zip(memories, vectors, strict=True)
    ]


def percentile(values: Sequence[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def stats(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "median": round(statistics.median(values), 4) if values else None,
        "p95": round(percentile(values, 0.95), 4) if values else None,
        "max": round(max(values), 4) if values else None,
    }


def refusal_service(cluster: str, service: str) -> str | None:
    if EXPERIMENT_MARK in cluster and EXPERIMENT_MARK in service:
        return None
    return f"'{cluster}/{service}' 는 이름에 '{EXPERIMENT_MARK}' 가 없어 실험 서비스로 보지 않는다. 내리지 않는다."


def refusal_db(url: str) -> str | None:
    name = urlsplit(url).path.lstrip("/")
    if name.endswith(EVAL_DB_SUFFIX):
        return None
    return f"DB '{name}' 는 실험용이 아니다(이름이 '{EVAL_DB_SUFFIX}' 로 끝나야 함). 가상 사용자를 쓰고 지우므로 돌리지 않는다."


class Prober:
    """일정한 간격으로 검색을 한 번씩 부르고 걸린 시간을 모은다. 앞 검색이 끝난 뒤 다음을 부른다."""

    def __init__(self, post: Post, queries: Sequence[str], interval: float) -> None:
        self._post = post
        self._queries = list(queries)
        self._interval = interval
        self.samples: list[tuple[str, float, int]] = []  # (구간, 초, 상태)
        self.phase = "before"
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        i = 0
        while True:
            query = self._queries[i % len(self._queries)]
            i += 1
            started = time.perf_counter()
            status, _ = await self._post(SEARCH_URL, {"query": query, "size": 20})
            self.samples.append((self.phase, time.perf_counter() - started, status))
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for phase in ("before", "during", "after"):
            got = [s for p, s, status in self.samples if p == phase and status == 200]
            out[phase] = stats(got) | {
                "errors": sum(
                    1 for p, _, st in self.samples if p == phase and st != 200
                )
            }
        before, during = out["before"]["p95"], out["during"]["p95"]
        out["p95_ratio"] = round(during / before, 3) if before and during else None
        return out


async def wait_drained(
    pending: Callable[[], Awaitable[set[int]]],
    submitted: Mapping[int, float],
    *,
    limit: float = DRAIN_LIMIT_SECONDS,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[int, float | None]:
    """사용자마다 보낸 때부터 목록이 다 만들어질 때까지의 초. 한도를 넘긴 사용자는 None."""
    done: dict[int, float | None] = {}
    started = clock()
    while True:
        left = await pending()
        now = clock()
        for user_id, sent in submitted.items():
            if user_id not in done and user_id not in left:
                done[user_id] = round(now - sent, 3)
        if not left or now - started > limit:
            break
        await asyncio.sleep(poll)
    for user_id in submitted:
        done.setdefault(user_id, None)
    return done


def aws(args: Sequence[str], region: str, profile: str | None) -> str:
    command = ["aws", *args, "--region", region, "--output", "json"]
    if profile:
        command += ["--profile", profile]
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout


def stop_service(cluster: str, service: str, region: str, profile: str | None) -> None:
    """실험 서비스를 0개로 내리고 다 내려갈 때까지 기다린다. 종료 신호를 받은 서버는 줄을 버린다."""
    aws(
        [
            "ecs",
            "update-service",
            "--cluster",
            cluster,
            "--service",
            service,
            "--desired-count",
            "0",
        ],
        region,
        profile,
    )
    aws(
        ["ecs", "wait", "services-stable", "--cluster", cluster, "--services", service],
        region,
        profile,
    )


async def run(
    args: argparse.Namespace, url: str, token: str, write: Callable[[dict], None]
) -> None:
    import asyncpg
    import httpx

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    users = pick_users(cases, args.users)
    queries = probe_queries(
        json.loads(SEARCH_CASES_PATH.read_text(encoding="utf-8"))["cases"]
    )
    user_ids = [c["user_id"] for c in users]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn = await asyncpg.connect(url)
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=CALL_TIMEOUT_SECONDS, headers=headers
    ) as client:

        async def post(path: str, body: dict) -> tuple[int, dict]:
            try:
                response = await client.post(path, json=body)
            except httpx.HTTPError as exc:
                return 0, {"error": f"{type(exc).__name__}: {exc}"[:200]}
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, {}

        try:
            # 취향 기억 벡터는 보내기 전에 다 만들어 둔다(BE 는 이미 벡터를 들고 부른다)
            bodies = []
            for case in users:
                memories = case["user"]["memories"]
                vectors: list[list[float]] = []
                texts = [m["keyword"] for m in memories]
                for start in range(0, len(texts), EMBED_BATCH):
                    status, got = await post(
                        EMBED_URL,
                        {
                            "texts": texts[start : start + EMBED_BATCH],
                            "purpose": MEMORY_VECTOR_PURPOSE,
                        },
                    )
                    if status != 200:
                        raise RuntimeError(f"/embeddings 가 {status} 로 답했다: {got}")
                    vectors += got["data"]["vectors"]
                bodies.append(
                    (
                        case,
                        profile_body(case, memory_body(memories, vectors), args.label),
                    )
                )

            prober = Prober(post, queries, args.probe_interval)
            prober.start()
            await asyncio.sleep(args.probe_before)
            prober.phase = "during"
            sent: dict[int, float] = {}
            replies: list[dict] = []

            async def send(case: Mapping[str, Any], body: dict) -> None:
                sent[case["user_id"]] = time.perf_counter()
                started = time.perf_counter()
                status, _ = await post(PROFILE_URL, body)
                replies.append(
                    {
                        "id": case["id"],
                        "user_id": case["user_id"],
                        "status": status,
                        "seconds": round(time.perf_counter() - started, 3),
                    }
                )

            t0 = time.perf_counter()
            if args.pattern == "sequential":
                for case, body in bodies:
                    await send(case, body)
            else:
                await asyncio.gather(*(send(case, body) for case, body in bodies))
            sent_seconds = time.perf_counter() - t0

            async def pending() -> set[int]:
                return {r["user_id"] for r in await conn.fetch(_PENDING_SQL, user_ids)}

            dropped = None
            if args.stop_after is not None:
                # 운영처럼 새 요청 없이 기다리는 동안 일꾼은 줄을 비운다. 다 비면 기다림도 끝난다
                drained = await wait_drained(pending, sent, limit=args.stop_after)
                await prober.stop()
                before_stop = await pending()
                stop_service(args.cluster, args.service, args.region, args.aws_profile)
                left = await pending()
                # 내리기 직전에 남은 수와 내린 뒤에도 안 만들어진 수(만들던 하나가 그사이 끝날 수 있다)
                dropped = {
                    "pending_at_stop": len(before_stop),
                    "never_built": len(left),
                }
            else:
                drained = await wait_drained(pending, sent)
                prober.phase = "after"
                await asyncio.sleep(min(args.probe_before, 30))
                await prober.stop()

            built = [v for v in drained.values() if v is not None]
            write(
                {
                    "type": "load",
                    "label": args.label,
                    "pattern": args.pattern,
                    "users": len(users),
                    "sent_seconds": round(sent_seconds, 3),
                    "profile": stats(
                        [r["seconds"] for r in replies if r["status"] == 200]
                    )
                    | {"errors": sum(1 for r in replies if r["status"] != 200)},
                    "built": stats(built)
                    | {"not_built": sum(v is None for v in drained.values())},
                    "search": prober.summary(),
                    "dropped": dropped,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            )
            for r in replies:
                write({"type": "user", **r, "built_seconds": drained.get(r["user_id"])})
        finally:
            if not args.keep:
                await conn.execute(_CLEANUP_SQL, user_ids)
            await conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="비동기 홈 목록 부하를 잰다")
    parser.add_argument(
        "--base-url",
        required=True,
        help="실험 AI 서버 주소(예: http://<태스크 주소>:8000)",
    )
    parser.add_argument(
        "--pattern",
        choices=("sequential", "burst"),
        required=True,
        help="연달아 / 한꺼번에",
    )
    parser.add_argument("--users", type=int, required=True, help="가상 사용자 수")
    parser.add_argument(
        "--label", default="run", help="결과 파일 이름 results/async-<label>.jsonl"
    )
    parser.add_argument(
        "--probe-interval", type=float, default=1.0, help="검색을 부르는 간격(초)"
    )
    parser.add_argument(
        "--probe-before",
        type=float,
        default=60.0,
        help="보내기 전에 검색만 부르는 시간(초)",
    )
    parser.add_argument(
        "--stop-after",
        type=float,
        help="다 보낸 뒤 이만큼 기다렸다 서비스를 내린다(운영은 120)",
    )
    parser.add_argument("--cluster", help="--stop-after 때 내릴 실험 클러스터")
    parser.add_argument("--service", help="--stop-after 때 내릴 실험 서비스")
    parser.add_argument(
        "--region", default=os.environ.get("AWS_REGION", "ap-northeast-2")
    )
    parser.add_argument("--aws-profile")
    parser.add_argument(
        "--keep", action="store_true", help="가상 사용자의 프로필을 지우지 않는다"
    )
    parser.add_argument(
        "--yes", action="store_true", help="실제로 보낸다. 없으면 할 일만 보여 준다"
    )
    args = parser.parse_args(argv)

    if args.stop_after is not None:
        if not (args.cluster and args.service):
            print(
                "--stop-after 에는 --cluster 와 --service 가 필요하다", file=sys.stderr
            )
            return 2
        reason = refusal_service(args.cluster, args.service)
        if reason:
            print(reason, file=sys.stderr)
            return 2
    url = os.environ.get("DATABASE_URL", "")
    reason = refusal_db(url)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    plan = f"{args.pattern} {args.users}명 → {args.base_url}, 검색 {args.probe_interval:g}초 간격"
    if args.stop_after is not None:
        plan += f", 다 보낸 뒤 {args.stop_after:g}초 기다렸다 {args.cluster}/{args.service} 를 0개로 내림"
    print(plan)
    if not args.yes:
        print("실제로 보내려면 --yes 를 붙인다")
        return 0

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"async-{args.label}.jsonl"
    with out.open("a", encoding="utf-8") as fh:

        def write(line: dict) -> None:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()

        asyncio.run(run(args, url, os.environ.get("AI_SERVICE_TOKEN", ""), write))
    print(f"결과를 {out} 에 썼다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
