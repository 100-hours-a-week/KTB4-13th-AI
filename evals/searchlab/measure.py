"""검색 시간 한 회차를 잰다(#593).

    DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.measure --label base-ef400 --state first --warmup
    DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.measure --label base-ef400 --state repeat --ef-search 1000

앱의 검색 함수(`app.search.service.search`)를 그대로 부른다. 검색어 벡터만 미리 만든 것(`out/vectors.json`)으로
바꿔서, 앱 서버 없이도 SQL·`SET LOCAL`·준비된 문장이 앱과 같다. 단계 함수를 감싸 단계별 시간을 재고, 켜 두면
문장마다 읽은 블록 수를 `pg_stat_statements` 로 읽는다. 캐시 비우기(재부팅)와 회차 순서는 이 도구 밖에서 정한다.

결과는 `out/<label>.jsonl` 에 쓴다. 첫 줄은 회차 머리(환경·설정), 그 뒤로 검색 한 번에 한 줄이다.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evals.searchlab import vectors as vector_file
from evals.searchlab.queries import QUERIES_PATH

OUT_DIR = Path(__file__).with_name("out")
# 반복 조회는 같은 검색어를 이만큼 연달아 부르고 첫 번은 판정에 쓰지 않는다(#556 「캐시 상태 셋」)
REPEATS = 7
TOP = 20
EVAL_DB_SUFFIX = "_eval"
# 앱 풀의 문장 한도(app/core/db.py)
APP_COMMAND_TIMEOUT = 5.0

# 회차 머리에 적는 설정. 시간에 닿는 것만 고른다
_SETTINGS = (
    "server_version",
    "shared_buffers",
    "effective_cache_size",
    "work_mem",
    "random_page_cost",
    "effective_io_concurrency",
    "jit",
    "max_parallel_workers_per_gather",
    "plan_cache_mode",
    "track_io_timing",
    "io_method",
)
_SIZES_SQL = """
SELECT c.relname, pg_relation_size(c.oid) AS bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'i', 'm')
ORDER BY bytes DESC
LIMIT 12
"""
_EXTENSIONS_SQL = "SELECT extname, extversion FROM pg_extension ORDER BY extname"
_PGSS_COLUMNS_SQL = """
SELECT attname FROM pg_attribute
WHERE attrelid = 'pg_stat_statements'::regclass AND attnum > 0 AND NOT attisdropped
"""
# 판 마다 있는 칸이 다르다(일반·맞춤 계획 횟수는 18 부터). 있는 것만 읽는다
_PGSS_WANTED = (
    "queryid",
    "query",
    "calls",
    "rows",
    "shared_blks_hit",
    "shared_blks_read",
    "total_exec_time",
    "total_plan_time",
    "shared_blk_read_time",
    "generic_plan_calls",
    "custom_plan_calls",
)


@dataclass(frozen=True)
class Knobs:
    """바꿔 보는 설정. None 이면 지금 값 그대로다."""

    ef_search: int | None = None
    random_page_cost: float | None = None
    effective_io_concurrency: int | None = None
    # 벡터 검색 한도(초). 앱은 2초에서 끊고 키워드만으로 답한다. 처음 조회는 이 한도에 자주 걸려, 그대로 재면 시간이
    # 2초에서 잘려 설정끼리 차이가 안 보인다. 측정 회차는 한도를 풀고 재며, 앱이라면 끊겼을 검색은 잰 시간으로 센다.
    vector_timeout: float | None = None

    def command_timeout(self) -> float:
        """풀의 문장 한도. 앱은 5초다. 벡터 한도를 그보다 길게 풀면 같이 늘린다."""
        return max(APP_COMMAND_TIMEOUT, self.vector_timeout or 0)

    def session_settings(self) -> dict[str, str]:
        """연결마다 거는 설정. 앱은 이 둘을 바꾸지 않으므로 연결을 만들 때 세션 설정으로 건다."""
        out = {}
        if self.random_page_cost is not None:
            out["random_page_cost"] = str(self.random_page_cost)
        if self.effective_io_concurrency is not None:
            out["effective_io_concurrency"] = str(self.effective_io_concurrency)
        return out


@dataclass
class Stages:
    """검색 한 번 동안 단계 함수가 걸린 시간(ms). 같은 단계가 두 번 불리면(키워드 넓히기) 둘 다 남긴다."""

    ms: dict[str, list[float]] = field(default_factory=dict)

    def add(self, name: str, seconds: float) -> None:
        self.ms.setdefault(name, []).append(round(seconds * 1000, 3))


# 감쌀 단계 함수. (모듈, 이름, 기록 이름). keyword._run 은 키워드 후보 한 번(모든 낱말 / 낱말마다 넓히기)이다.
STAGES = (
    ("keyword", "search_ids", "keyword"),
    ("keyword", "_run", "keyword_query"),
    ("vector", "search_ids", "vector"),
    ("vector", "has_any", "vector_has_any"),
    ("keyword", "exact_title_ids", "exact_title"),
    ("sorting", "sort_ids", "sort"),
    ("books", "fetch", "books"),
)


@contextlib.contextmanager
def patched(knobs: Knobs, query_vectors: Mapping[str, list[float]]) -> Iterator[Stages]:
    """검색 함수의 벡터 만들기를 미리 만든 벡터로, 단계 함수를 시간을 재는 함수로 바꾼다. 끝나면 되돌린다."""
    from app.search import books, keyword, service, sorting, vector

    modules = {"keyword": keyword, "vector": vector, "sorting": sorting, "books": books}
    stages = Stages()
    saved: list[tuple[Any, str, Any]] = []

    def swap(obj: Any, name: str, value: Any) -> None:
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def timed(fn: Callable, label: str) -> Callable:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                stages.add(label, time.perf_counter() - started)

        return wrapper

    async def embed_query(query: str) -> list[float]:
        try:
            return query_vectors[query]
        except KeyError:
            raise KeyError(f"벡터 파일에 없는 검색어다: {query!r}") from None

    try:
        swap(service, "_embed_query", embed_query)
        # 다음 페이지 커서는 서명 키가 있어야 만든다. 실험 서버에 운영 키를 두지 않으므로 고정 값으로 바꾼다
        # (서명은 수 마이크로초라 시간에 닿지 않는다).
        swap(service.cursor, "encode", lambda data, now=None: "measure")
        for module_name, name, label in STAGES:
            module = modules[module_name]
            swap(module, name, timed(getattr(module, name), label))
        if knobs.ef_search is not None:
            swap(vector, "SEARCH_EF_SEARCH", knobs.ef_search)
        if knobs.vector_timeout is not None:
            swap(vector, "QUERY_TIMEOUT_SECONDS", knobs.vector_timeout)
        yield stages
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def ordered(
    items: Sequence[Mapping[str, Any]], seed: str | None
) -> list[Mapping[str, Any]]:
    """잴 순서. 시드가 있으면 섞는다. 같은 낱말을 쓰는 사례(필터·정렬)가 늘 같은 자리에서 데워지지 않게 한다."""
    if seed is None:
        return list(items)
    return sorted(
        items, key=lambda x: hashlib.md5(f"{seed}:{x['id']}".encode()).hexdigest()
    )


def request_of(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "query": item["query"],
        "filters": dict(item.get("filters", {})),
        "sort": item.get("sort", "relevance"),
        "size": TOP,
    }


def plan(
    items: Sequence[Mapping[str, Any]], state: str
) -> list[tuple[Mapping[str, Any], int]]:
    """(검색어, 몇 번째) 목록. 처음 조회는 한 번씩, 반복 조회는 검색어마다 REPEATS 번 연달아."""
    if state == "first":
        return [(item, 1) for item in items]
    if state == "repeat":
        return [(item, rep) for item in items for rep in range(1, REPEATS + 1)]
    raise ValueError(f"모르는 캐시 상태: {state}")


def pgss_select(columns: set[str]) -> str:
    have = [c for c in _PGSS_WANTED if c in columns]
    return (
        f"SELECT {', '.join(have)} FROM pg_stat_statements "
        "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "AND query NOT ILIKE '%pg_stat_statements%'"
    )


def pgss_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """문장마다 읽은 블록과 시간. BEGIN·SET·COMMIT 처럼 값이 0 인 문장은 뺀다."""
    out = []
    for row in rows:
        r = dict(row)
        if not (r.get("shared_blks_hit") or r.get("shared_blks_read") or r.get("rows")):
            continue
        r["query"] = " ".join(str(r.get("query", "")).split())[:120]
        for key in ("total_exec_time", "total_plan_time", "shared_blk_read_time"):
            if key in r and r[key] is not None:
                r[key] = round(float(r[key]), 3)
        out.append(r)
    return sorted(
        out, key=lambda r: -(r.get("shared_blks_hit", 0) + r.get("shared_blks_read", 0))
    )


def database_name(url: str) -> str:
    """DB 주소에서 이름만. 주소에는 계정과 비밀번호가 들어 있을 수 있어 그대로 찍지 않는다."""
    return urlsplit(url).path.lstrip("/")


def code_commit() -> str:
    """잰 코드의 커밋. app 을 고친 채로 쟀으면 그렇다고 붙인다."""
    root = Path(__file__).parents[2]

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        changed = git("status", "--porcelain", "--untracked-files=no", "--", "app")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit} (app 을 고친 채)" if changed else commit


async def environment(conn: Any) -> dict[str, Any]:
    """회차 머리에 적는 DB 환경. 재부팅했는지는 started 로 본다."""
    settings = {}
    for name in _SETTINGS:
        with contextlib.suppress(Exception):
            settings[name] = await conn.fetchval(f"SHOW {name}")
    return {
        "database": await conn.fetchval("SELECT current_database()"),
        "started": str(await conn.fetchval("SELECT pg_postmaster_start_time()")),
        "settings": settings,
        "extensions": {
            r["extname"]: r["extversion"] for r in await conn.fetch(_EXTENSIONS_SQL)
        },
        "sizes": {r["relname"]: r["bytes"] for r in await conn.fetch(_SIZES_SQL)},
    }


async def run(
    url: str,
    items: Sequence[Mapping[str, Any]],
    *,
    state: str,
    knobs: Knobs,
    query_vectors: Mapping[str, list[float]],
    warmup: Sequence[Mapping[str, Any]] = (),
    pgss: bool = False,
    write: Callable[[dict], None],
) -> None:
    import asyncpg

    from app.core import db
    from app.core.config import get_settings
    from app.search import service
    from app.search.schemas import SearchRequest

    settings = get_settings()
    side = await asyncpg.connect(url)
    # 재는 쪽은 앱과 같은 풀 설정으로 만들되 연결은 하나만 둔다. 준비된 문장이 연결마다 따로라, 연결이 여럿이면
    # 같은 문장이 어느 연결에서 몇 번째인지(일반 계획으로 바뀌는 때)가 회차마다 달라진다.
    pool = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=1,
        command_timeout=knobs.command_timeout(),
        server_settings={
            "timezone": settings.tz,
            "default_transaction_read_only": "on",
            **knobs.session_settings(),
        },
    )
    saved_pool = db._pool
    db._pool = pool
    try:
        columns = set()
        if pgss:
            columns = {r["attname"] for r in await side.fetch(_PGSS_COLUMNS_SQL)}
        async with pool.acquire() as conn:
            env = await environment(conn)
        write(
            {
                "type": "round",
                "state": state,
                "knobs": asdict(knobs),
                "commit": code_commit(),
                "queries": vector_file.queries_digest(
                    QUERIES_PATH.read_text(encoding="utf-8")
                ),
                "items": len(items),
                "warmup": len(warmup),
                "pgss": pgss,
                "env": env,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        with patched(knobs, query_vectors) as stages:
            if warmup:
                started = time.perf_counter()
                for item in warmup:
                    await service.search(SearchRequest(**request_of(item)))
                write(
                    {
                        "type": "warmup",
                        "n": len(warmup),
                        "seconds": round(time.perf_counter() - started, 1),
                    }
                )
            for item, rep in plan(items, state):
                if pgss:
                    await side.execute("SELECT pg_stat_statements_reset()")
                stages.ms.clear()
                error = None
                ids: list[int] = []
                degraded = None
                started = time.perf_counter()
                try:
                    outcome = await service.search(SearchRequest(**request_of(item)))
                    ids = [r["book_id"] for r in outcome.results]
                    degraded = outcome.degraded
                except Exception as exc:  # noqa: BLE001 — 한 건이 실패해도 회차는 이어 가고 실패는 기록으로 남긴다
                    error = f"{type(exc).__name__}: {exc}"[:200]
                total_ms = round((time.perf_counter() - started) * 1000, 3)
                line = {
                    "type": "search",
                    "id": item["id"],
                    "kind": item["kind"],
                    "sort": item.get("sort", "relevance"),
                    "state": state,
                    "rep": rep,
                    "total_ms": total_ms,
                    "stages": dict(stages.ms),
                    "degraded": degraded,
                    "ids": ids,
                    "error": error,
                }
                if pgss:
                    line["statements"] = pgss_rows(
                        await side.fetch(pgss_select(columns))
                    )
                write(line)
    finally:
        db._pool = saved_pool
        await pool.close()
        await side.close()


def refusal(name: str, *, allow_any: bool) -> str | None:
    if allow_any or name.endswith(EVAL_DB_SUFFIX):
        return None
    return (
        f"DB '{name}' 는 실험용이 아니다. 실험 DB 는 이름을 '{EVAL_DB_SUFFIX}' 로 끝나게 바꿔 둔다. "
        "그래도 돌리려면 --any-db 를 붙인다(읽기만 한다)."
    )


def select(
    items: Sequence[Mapping[str, Any]], kinds: Sequence[str] | None, limit: int | None
) -> list:
    picked = [x for x in items if not kinds or x["kind"] in kinds]
    return picked[:limit] if limit else picked


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검색 시간 한 회차를 잰다")
    parser.add_argument(
        "--label", required=True, help="결과 파일 이름. out/<label>.jsonl 에 이어 쓴다"
    )
    parser.add_argument(
        "--state",
        choices=("first", "repeat"),
        required=True,
        help="처음 조회 / 반복 조회",
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="재기 전에 데우기 검색어 500건을 먼저 부른다",
    )
    parser.add_argument("--order-seed", help="잴 순서를 섞는 값. 회차마다 바꾼다")
    parser.add_argument(
        "--ef-search", type=int, help="벡터 탐색 폭(지금 앱 값은 app/search/vector.py)"
    )
    parser.add_argument("--random-page-cost", type=float)
    parser.add_argument("--effective-io-concurrency", type=int)
    parser.add_argument(
        "--vector-timeout",
        type=float,
        help="벡터 검색 한도(초). 앱은 2초. 측정 회차는 풀고 잰다(예: 30)",
    )
    parser.add_argument(
        "--kinds", nargs="*", help="이 종류만 잰다(title, author, sort ...)"
    )
    parser.add_argument("--limit", type=int, help="앞에서 몇 건만 잰다(시험용)")
    parser.add_argument(
        "--pgss",
        action="store_true",
        help="문장마다 읽은 블록을 pg_stat_statements 로 읽는다",
    )
    parser.add_argument(
        "--any-db",
        action="store_true",
        help="이름이 _eval 로 끝나지 않는 DB 에도 돌린다",
    )
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    url = get_settings().database_url
    reason = refusal(database_name(url), allow_any=args.any_db)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    bundle = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    items = ordered(select(bundle["measure"], args.kinds, args.limit), args.order_seed)
    knobs = Knobs(
        args.ef_search,
        args.random_page_cost,
        args.effective_io_concurrency,
        args.vector_timeout,
    )
    out = OUT_DIR / f"{args.label}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:

        def write(line: dict) -> None:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            fh.flush()

        asyncio.run(
            run(
                url,
                items,
                state=args.state,
                knobs=knobs,
                query_vectors=vector_file.load(),
                warmup=bundle["warmup"] if args.warmup else (),
                pgss=args.pgss,
                write=write,
            )
        )
    print(f"{len(items)}건({args.state})을 재 {out} 에 썼다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
