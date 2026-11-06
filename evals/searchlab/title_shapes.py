"""평가 묶음에 없는 제목 검색 꼴을 설정끼리 견준다(#556 판정 밖의 참고 점검).

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval CURSOR_SIGNING_KEY=local-smoke-only \\
        uv run python -m evals.searchlab.title_shapes --out shapes.json

평가 묶음의 제목은 책 제목을 그대로 친 것이라, 선이나 비중을 바꿨을 때 "제목에 낱말이 더 붙은 검색어"가 어떻게 되는지
재지 못한다. 그래서 제목 사례를 기계적으로 바꾼 검색어를 만들어 첫 화면(인기순 위 12권)의 1등과 12권 안을 센다. 사람이
친 검색어가 아니고 판정에 쓰지 않는다. 판정 기준을 고정한 뒤에 만든 점검이라 숫자만 적고 사람이 본다.

| 꼴 | 만드는 법 |
| --- | --- |
| 띄어 침 | 붙여 쓴 4자 이상 제목을 가운데서 띄운다 |
| 끝 자모 | 끝에 자모 하나를 더 친다 |
| 글자 바뀜 | 가장 긴 낱말의 한 글자를 다른 글자로 바꾼다 |
| 제목 + 책 추천, 제목 + 책 | 뒤에 낱말을 더 붙인다 |
| 제목 + 저자 | 뒤에 첫 저자 이름을 붙인다 |
"""

import argparse
import asyncio
import json
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.search import run
from evals.search.collect import collect_with
from evals.search.scoring import title_rank
from evals.searchlab import grid_checks

SEED = 556
EACH = 60
VECTOR_TIMEOUT = 30
BASE = "지금(3:1, 선 0.6)"
SEMANTIC = "1:3"
CONFIGS: dict[str, dict[str, str]] = {
    BASE: {},
    "합계 500": {"lookup_budget": "500"},
    "선 0.65": {"min_coverage": "0.65"},
    "선 0.65, 합계 500": {"min_coverage": "0.65", "lookup_budget": "500"},
    "선 0.7": {"min_coverage": "0.7"},
    "선 0.7, 합계 500": {"min_coverage": "0.7", "lookup_budget": "500"},
    SEMANTIC: {"keyword_weight": "1", "vector_weight": "3"},
}


def build(titles: Sequence[dict], authors: Mapping[str, str | None]) -> list[dict]:
    """제목 사례를 꼴마다 바꾼 검색어. 시드로 고정한다."""
    rng = random.Random(SEED)
    cases: list[dict] = []

    def add(group: str, query: str, case: dict) -> None:
        cases.append({"group": group, "query": query, "expect": case["expect"]})

    for case in titles:
        query = case["query"].strip()
        if " " not in query and len(query) >= 4:
            middle = len(query) // 2
            add("띄어 침", f"{query[:middle]} {query[middle:]}", case)
    multi = [c for c in titles if len(c["query"].split()) >= 2 and len(c["query"]) >= 6]
    rng.shuffle(multi)
    for case in multi[:EACH]:
        add("끝 자모", case["query"].strip() + "ㅏ", case)
    for case in multi[EACH : 2 * EACH]:
        words = case["query"].split()
        longest = max(range(len(words)), key=lambda i: len(words[i]))
        word = words[longest]
        at = rng.randrange(len(word))
        words[longest] = f"{word[:at]}가{word[at + 1 :]}"
        add("글자 바뀜", " ".join(words), case)
    for case in multi[2 * EACH : 3 * EACH]:
        add("제목 + 책 추천", case["query"].strip() + " 책 추천", case)
    pool = list(titles)
    rng.shuffle(pool)
    for case in pool[:EACH]:
        add("제목 + 책", case["query"].strip() + " 책", case)
    named = 0
    for case in pool[EACH:]:
        name = (authors.get(case["isbn"]) or "").split(",")[0].strip()
        if name and len(name) <= 10 and named < EACH:
            add("제목 + 저자", f"{case['query'].strip()} {name}", case)
            named += 1
    return cases


def count(
    cases: Sequence[dict], results: Sequence[dict], shelf: Mapping[int, Any]
) -> dict[str, list[int]]:
    """꼴마다 [사례, 1등, 12권 안]."""
    out: dict[str, list[int]] = {}
    for case, result in zip(cases, results, strict=True):
        rank = title_rank(case, [shelf.get(i) for i in result["book_ids"]])
        row = out.setdefault(case["group"], [0, 0, 0])
        row[0] += 1
        row[1] += rank == 1
        row[2] += rank is not None
    return out


async def read_authors(url: str, isbns: list[str]) -> dict[str, str | None]:
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(
            "SELECT isbn13, author FROM v_books WHERE isbn13 = ANY($1::text[])", isbns
        )
    finally:
        await conn.close()
    return {r["isbn13"]: r["author"] for r in rows}


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.config import get_settings

    parser = argparse.ArgumentParser(description="제목 검색 꼴 점검")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    url = get_settings().database_url
    titles = [c for c in run.load_bundle()["cases"] if c["kind"] == "title"]
    authors = asyncio.run(read_authors(url, [c["isbn"] for c in titles]))
    cases = build(titles, authors)
    requests = [run.request(c, run.SCREEN_TOP, run.SCREEN_SORT) for c in cases]
    results = {
        name: collect_with(
            run.REPO,
            requests,
            overrides,
            vector_timeout=VECTOR_TIMEOUT,
            trace=name == BASE,
        )
        for name, overrides in CONFIGS.items()
    }
    # 유형별 비중: 판별대로 사례마다 1:3 과 지금을 고른다
    for rule in ("r1", "r2"):
        results[f"유형별 비중 {rule}"] = [
            semantic if grid_checks.is_descriptive(base, case["query"], rule) else base
            for case, base, semantic in zip(
                cases, results[BASE], results[SEMANTIC], strict=True
            )
        ]
    ids = {i for rows in results.values() for r in rows for i in r["book_ids"]}
    shelf, _ = asyncio.run(run.read_db(url, ids))
    counted = {name: count(cases, rows, shelf) for name, rows in results.items()}
    groups = list(counted[BASE])
    head = [f"{g}({counted[BASE][g][0]})" for g in groups]
    print("| 안 | " + " | ".join(head) + " |")
    print("| --- | " + " | ".join("---" for _ in groups) + " |")
    for name, table in counted.items():
        cells = [f"{table[g][1]} / {table[g][2]}" for g in groups]
        print(f"| {name} | " + " | ".join(cells) + " |")
    saved = {"cases": cases, "counted": counted}
    args.out.write_text(
        json.dumps(saved, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
