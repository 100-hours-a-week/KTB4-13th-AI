"""검색 시간 측정(#556)에 쓸 검색어를 고정한다(#591).

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.searchlab.queries

잴 검색어 170건은 평가 묶음(`evals/search/cases.json`)에서 종류별로 고른다. 데우기 검색어 500건은 평가 DB 에서
제목·저자를 다른 시드로 새로 뽑는다. 같은 묶음·같은 책 표·같은 시드면 같은 결과가 나온다. 책 표를 읽기만 한다.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core import editions
from app.feed.interest_picks import has_author
from evals.search import build

QUERIES_PATH = Path(__file__).with_name("queries.json")
CASES_PATH = Path(__file__).parents[1] / "search" / "cases.json"
VERSION = 1
# 사례를 고르는 순서를 정하는 값. 바꾸면 다른 묶음이 나온다
SEED = "556"
WARMUP_SEED = "556-warmup"

# 잴 검색어. 종류별 수는 #556 「측정 방법 › 캐시 상태 셋」 그대로다.
MEASURE = {"title": 60, "author": 15, "filter": 15, "descriptive": 40}
# 표기 변형 20건은 평가 묶음의 비율(30·20·13·12·25 / 100)대로 나눈다
VARIANTS = {
    "nospace": 6,
    "no-subtitle": 4,
    "volume-roman": 3,
    "volume-circled": 2,
    "case": 5,
}
# 정렬 사례 20건. 정렬은 후보를 다 모은 뒤 따로 한 번 더 DB 를 부르므로 따로 잰다. 검색어는 위에서 고르고 남은
# 제목·저자와 서술형에서 가져온다(서술형은 40건을 모두 쓰므로 같은 검색어를 정렬만 바꿔 다시 쓴다).
SORT_SOURCES = {"title": 8, "author": 6, "descriptive": 6}
# 정렬을 이 순서로 돌려 붙인다. 20건이면 인기순·최신순 7건씩, 가격 낮은순·높은순 3건씩이다.
SORT_CYCLE = ("popular", "newest", "price_asc", "popular", "newest", "price_desc")

# 데우기 검색어. 측정과 같은 종류(제목·저자)로 캐시를 운영처럼 채운다. 서술형은 사람이 쓴 것뿐이라 쓰지 않는다.
WARMUP_TITLES = 450
WARMUP_AUTHORS = 50


def _order(seed: str, key: str) -> str:
    return hashlib.md5(f"{seed}:{key}".encode()).hexdigest()


def _shuffled(cases: Sequence[Mapping[str, Any]], seed: str) -> list[Mapping[str, Any]]:
    """시드로 정한 순서. 사례 번호로 정해 묶음 안의 위치가 바뀌어도 같은 순서가 나온다."""
    return sorted(cases, key=lambda c: (_order(seed, c["id"]), c["id"]))


def _item(case: Mapping[str, Any], *, sort: str = "relevance", **fields: Any) -> dict:
    item = {"id": case["id"], "kind": case["kind"], "query": case["query"]}
    if case.get("filters"):
        item["filters"] = dict(case["filters"])
    return item | {"sort": sort} | fields


def pick_measure(cases: Sequence[Mapping[str, Any]], seed: str = SEED) -> list[dict]:
    """평가 묶음에서 잴 검색어를 고른다. 모자라면 ValueError — 조용히 적게 재지 않게 한다."""
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for case in _shuffled(cases, seed):
        by_kind.setdefault(case["kind"], []).append(case)

    def take(label: str, n: int, pool: Sequence[Mapping[str, Any]]) -> list:
        got = list(pool)[:n]
        if len(got) < n:
            raise ValueError(f"{label} 사례가 {len(got)}건뿐이다(필요 {n})")
        return got

    picked = {kind: take(kind, n, by_kind.get(kind, [])) for kind, n in MEASURE.items()}
    variants = [
        case
        for change, n in VARIANTS.items()
        for case in take(
            f"title-variant/{change}",
            n,
            [c for c in by_kind.get("title-variant", []) if c.get("change") == change],
        )
    ]
    sources = []
    for kind, n in SORT_SOURCES.items():
        pool = by_kind.get(kind, [])
        # 제목·저자는 위에서 고르고 남은 것에서, 서술형은 같은 검색어를 다시 쓴다
        rest = pool if kind == "descriptive" else pool[MEASURE[kind] :]
        sources += take(f"sort/{kind}", n, rest)
    sorted_cases = [
        _item(case, sort=SORT_CYCLE[i % len(SORT_CYCLE)], of=case["id"])
        | {"id": f"sort-{i + 1:02d}", "kind": "sort"}
        for i, case in enumerate(sources)
    ]
    return [
        *(_item(c) for c in picked["title"]),
        *(_item(c, change=c["change"]) for c in variants),
        *(_item(c) for c in picked["author"]),
        *(_item(c) for c in picked["filter"]),
        *(_item(c) for c in picked["descriptive"]),
        *sorted_cases,
    ]


def pick_warmup_titles(
    pool: Sequence[Mapping[str, Any]], taken: set[str], n: int = WARMUP_TITLES
) -> list[dict]:
    """책 목록(시드 순서)에서 데우기 제목을 고른다. 평가 묶음에 든 책·검색어와 제목이 같은 책은 쓰지 않는다."""
    seen = set(taken)
    out = []
    for row in pool:
        key = editions.title_key(row["title"])
        if not key or key in seen or row["isbn13"] in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": f"warm-title-{len(out) + 1:03d}",
                "kind": "title",
                "query": row["title"],
            }
        )
        if len(out) == n:
            return out
    raise ValueError(
        f"데우기 제목이 {len(out)}건뿐이다(필요 {n}). build.POOL 을 늘린다"
    )


def taken_keys(cases: Sequence[Mapping[str, Any]]) -> set[str]:
    """평가 묶음이 쓰는 책(ISBN)과 검색어(제목 키). 데우기 검색어가 측정 검색어를 미리 데우지 않게 뺀다."""
    keys = {c["isbn"] for c in cases if c.get("isbn")}
    for case in cases:
        key = editions.title_key(case["query"])
        if key:
            keys.add(key)
        for title in case.get("expect", {}).get("titles", []):
            keys.add(editions.title_key(title))
    return keys


async def pick_warmup_authors(
    conn: Any,
    pool: Sequence[Mapping[str, Any]],
    taken: set[str],
    n: int = WARMUP_AUTHORS,
) -> list[dict]:
    """평가 묶음과 같은 규칙(사람 이름, 그 저자 책이 평가 DB 에 10권 이상)으로 저자를 고른다. 평가 묶음의 저자는 뺀다."""
    names: list[str] = []
    seen = set(taken)
    for row in pool:
        name = build.first_author(row["author"])
        if name in seen or not build.person_like(name):
            continue
        seen.add(name)
        rows = await conn.fetch(build._AUTHOR_SQL, name)
        if sum(has_author(r["author"], name) for r in rows) >= build.AUTHOR_MIN_BOOKS:
            names.append(name)
            if len(names) == n:
                return [
                    {"id": f"warm-author-{i:02d}", "kind": "author", "query": a}
                    for i, a in enumerate(names, start=1)
                ]
    raise ValueError(
        f"데우기 저자가 {len(names)}명뿐이다(필요 {n}). build.POOL 을 늘린다"
    )


async def pick_warmup(
    url: str, cases: Sequence[Mapping[str, Any]]
) -> tuple[list[dict], dict]:
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        pool = await conn.fetch(
            build._POOL_SQL, WARMUP_SEED, *build.TITLE_CHARS, build.POOL
        )
        titles = pick_warmup_titles(pool, taken_keys(cases))
        authors = await pick_warmup_authors(
            conn, pool, {c["query"] for c in cases if c["kind"] == "author"}
        )
        catalog = {
            "books": await conn.fetchval("SELECT count(*) FROM v_books"),
            "with_vectors": await conn.fetchval("SELECT count(*) FROM book_embeddings"),
        }
    finally:
        await conn.close()
    return [*titles, *authors], catalog


def dump(bundle: Mapping[str, Any]) -> str:
    """한 줄에 검색어 하나. 고칠 때 diff 가 그 줄만 보이게 한다."""

    def one(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    head = {k: v for k, v in bundle.items() if k not in ("measure", "warmup")}
    lines = ["{"] + [f" {one(k)}: {one(v)}," for k, v in head.items()]
    for name in ("measure", "warmup"):
        items = bundle[name]
        lines.append(f' "{name}": [')
        lines += [
            f"  {one(item)}{',' if i < len(items) - 1 else ''}"
            for i, item in enumerate(items)
        ]
        lines.append(" ]," if name == "measure" else " ]")
    return "\n".join(lines + ["}"]) + "\n"


async def make(url: str) -> dict[str, Any]:
    source = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = source["cases"]
    warmup, catalog = await pick_warmup(url, cases)
    return {
        "version": VERSION,
        "seed": SEED,
        "warmup_seed": WARMUP_SEED,
        "cases_version": source["version"],
        "catalog": catalog,
        "measure": pick_measure(cases),
        "warmup": warmup,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="검색 시간 측정에 쓸 검색어를 고정한다"
    )
    parser.add_argument("--out", default=str(QUERIES_PATH), help="쓸 파일")
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    url = get_settings().database_url
    name = urlsplit(url).path.lstrip("/")
    if not name.endswith("_eval"):
        # 다른 책 표에서 뽑으면 평가 묶음과 같은 규칙으로 고른 책이 아니게 된다
        print(f"DB '{name}' 는 평가용(이름이 _eval 로 끝남)이 아니다", file=sys.stderr)
        return 2
    bundle = asyncio.run(make(url))
    Path(args.out).write_text(dump(bundle), encoding="utf-8")
    print(
        f"잴 검색어 {len(bundle['measure'])}건, 데우기 {len(bundle['warmup'])}건을 {args.out} 에 썼다"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
