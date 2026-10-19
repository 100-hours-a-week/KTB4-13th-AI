"""③ 단계 측정에서 나온 책 중 점수가 없는 책을 매긴다. ① 검색 평가의 매기는 법(evals/search/label.py, #502)을 따른다.

    # 1. 측정 원본에서 점수 없는 책을 모아, 검색어마다 섞어 가린 채점 파일로 낸다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.chat_stages.label pool \\
        evals/chat_stages/results/run.json
    # 2. candidates.md 를 읽고 grades.json 의 빈칸에 점수를 적은 뒤 라벨에 넣는다
    uv run python -m evals.chat_stages.label apply --judge "누가 매겼는지, 날짜"
    # 3. 여기서 매긴 라벨의 10%를 사람이 확인할 표로 뽑는다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.chat_stages.label review

점수 기준은 evals/search/README.md 의 "점수 기준"과 같다. ① 의 라벨에 이미 있는 책은 다시 매기지 않는다. 책이 어느
실험의 어느 단계에서 나왔는지는 sources.json 에만 적고, 채점이 끝날 때까지 열지 않는다.
"""

import argparse
import asyncio
import json
import math
import random
import sys
from collections.abc import Sequence
from pathlib import Path

from evals.chat_stages import run

WORK = run.HERE / "results" / "label"
SEED = "chat-stages"
REVIEW_SHARE = 0.1
INTRO_CHARS = 300
SCALE = {"2": "맞음", "1": "조금", "0": "아님"}

_BOOKS_SQL = f"""
SELECT book_id, isbn13, title, author, category,
       left(coalesce(description, ''), {INTRO_CHARS}) AS intro
FROM v_books WHERE book_id = ANY($1::int[])
"""


def _line(text: str | None) -> str:
    return " ".join((text or "").split())


def _load() -> dict:
    if run.LABELS_PATH.exists():
        return json.loads(run.LABELS_PATH.read_text(encoding="utf-8"))
    return {"version": 1, "scale": SCALE, "judges": [], "review": None, "labels": {}}


def _dump(data: dict) -> str:
    """사례 하나를 한 줄에 쓴다(evals/search/labels.json 과 같은 모양)."""

    def one(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    head = [f" {one(k)}: {one(v)}," for k, v in data.items() if k != "labels"]
    rows = ",\n".join(f"  {one(k)}: {one(v)}" for k, v in data["labels"].items())
    return "\n".join(["{", *head, ' "labels": {', rows, " }", "}"]) + "\n"


async def _books(book_ids: list[int]) -> dict[int, dict]:
    from app.core import db

    await db.connect()
    try:
        async with db.get_pool().acquire() as conn:
            rows = await conn.fetch(_BOOKS_SQL, book_ids)
    finally:
        await db.disconnect()
    return {r["book_id"]: dict(r) for r in rows}


def _appearances(row: dict) -> list[tuple[str, list[int]]]:
    """(어디서 나왔는지, 책 번호들). 원문 검색어의 ① 결과와 실험마다의 검색·후보·카드다."""
    found = [("raw", row.get("raw_search", []))]
    for name, v in row.get("variants", {}).items():
        found += [
            (f"{name}:search", v["search"]),
            (f"{name}:candidates", v["candidates"]),
        ]
        found += [(f"{name}:card", card["book_ids"]) for card in v["cards"]]
    return found


def pool(raw: dict, labels: dict[str, dict[str, int]]) -> tuple[dict, list[int]]:
    """(사례 번호 → 점수 없는 책 번호 → 나온 곳, 책 번호 전부). 같은 ISBN 은 render 에서 한 번만 매긴다."""
    found: dict[str, dict[str, list[str]]] = {}
    for row in raw["cases"]:
        marks = labels.get(row["id"], {})
        where = found.setdefault(row["id"], {})
        for tag, ids in _appearances(row):
            for book_id in ids:
                isbn = (raw["books"].get(str(book_id)) or {}).get("isbn")
                if isbn and isbn not in marks:
                    where.setdefault(str(book_id), []).append(tag)
    ids = sorted({int(i) for where in found.values() for i in where})
    return found, ids


def render(raw: dict, found: dict, books: dict[int, dict]) -> tuple[str, dict, dict]:
    lines = [
        "# ③ 단계 측정 채점 파일",
        "",
        "점수: 2 맞음 · 1 조금 · 0 아님. 기준은 evals/search/README.md 의 점수 기준.",
    ]
    grades: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    for row in raw["cases"]:
        by_isbn: dict[str, list] = {}
        for book_id, tags in found[row["id"]].items():
            book = books[int(book_id)]
            by_isbn.setdefault(book["isbn13"], []).append((int(book_id), tags))
        order = sorted(by_isbn)
        random.Random(f"{SEED}:{row['id']}").shuffle(order)
        lines += ["", f"## {row['id']} · {row['facet']} · {row['query']}", ""]
        grades[row["id"]], sources[row["id"]] = {}, {}
        for n, isbn in enumerate(order, 1):
            blind = f"c{n:02d}"
            book = books[by_isbn[isbn][0][0]]
            lines.append(
                f"- {blind} | {_line(book['title'])} | {_line(book['author'])} "
                f"| {_line(book['category'])} | {_line(book['intro'])}"
            )
            grades[row["id"]][blind] = None
            sources[row["id"]][blind] = {"isbn": isbn, "from": by_isbn[isbn]}
    return "\n".join(lines) + "\n", grades, sources


def apply(grades: dict, sources: dict, data: dict, judge: str) -> int:
    added = 0
    for case_id, marks in grades.items():
        for blind, grade in marks.items():
            if grade is None:
                continue
            if grade not in (0, 1, 2):
                raise ValueError(
                    f"{case_id} {blind}: 점수는 0, 1, 2 중 하나다({grade})"
                )
            known = data["labels"].setdefault(case_id, {})
            isbn = sources[case_id][blind]["isbn"]
            if isbn not in known:
                known[isbn] = grade
                added += 1
    data["labels"] = {
        k: dict(sorted(v.items())) for k, v in sorted(data["labels"].items())
    }
    entry = next((j for j in data["judges"] if j["by"] == judge), None)
    if entry is None and added:
        data["judges"].append(entry := {"by": judge, "labels": 0})
    if entry:
        entry["labels"] += added
    return added


def pick_review(data: dict) -> list[dict]:
    pairs = sorted((c, i) for c, marks in data["labels"].items() for i in marks)
    picked = sorted(
        random.Random(SEED).sample(pairs, math.ceil(len(pairs) * REVIEW_SHARE))
    )
    return [{"case": c, "isbn": i, "grade": data["labels"][c][i]} for c, i in picked]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="③ 단계 측정의 점수 없는 책을 매긴다")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pool")
    p.add_argument("raw", type=Path, nargs="+", help="측정 원본(여럿이면 모두 합친다)")
    p.add_argument("--search-dir", type=Path, default=run.SEARCH_DIR)
    sub.add_parser("apply").add_argument("--judge", required=True)
    r = sub.add_parser("review")
    r.add_argument("--search-dir", type=Path, default=run.SEARCH_DIR)
    r.add_argument("--out", type=Path, default=run.HERE / "review.md")
    args = parser.parse_args(argv)
    WORK.mkdir(parents=True, exist_ok=True)

    if args.command == "pool":
        labels = run.load_labels(args.search_dir)
        raws = [json.loads(path.read_text(encoding="utf-8")) for path in args.raw]
        # 원본이 여럿이면 사례를 이어 붙인다. 같은 사례가 두 번 나와도 ISBN 으로 한 번만 매긴다
        raw = {"cases": [], "books": {}}
        for one in raws:
            raw["books"].update(one["books"])
        merged: dict[str, dict] = {}
        for one in raws:
            for row in one["cases"]:
                if row["id"] not in merged:
                    merged[row["id"]] = {**row, "variants": {}}
                for name, v in row.get("variants", {}).items():
                    merged[row["id"]]["variants"][
                        f"{name}#{len(merged[row['id']]['variants'])}"
                    ] = v
        raw["cases"] = list(merged.values())
        found, ids = pool(raw, labels)
        books = asyncio.run(_books(ids))
        text, grades, sources = render(raw, found, books)
        (WORK / "candidates.md").write_text(text, encoding="utf-8")
        (WORK / "grades.json").write_text(
            json.dumps({"grades": grades}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        (WORK / "sources.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8"
        )
        count = sum(len(v) for v in grades.values())
        print(f"점수를 매길 책 {count}권: {WORK / 'candidates.md'}")
    elif args.command == "apply":
        data = _load()
        grades = json.loads((WORK / "grades.json").read_text(encoding="utf-8"))[
            "grades"
        ]
        sources = json.loads((WORK / "sources.json").read_text(encoding="utf-8"))
        added = apply(grades, sources, data, args.judge)
        run.LABELS_PATH.write_text(_dump(data), encoding="utf-8")
        print(f"라벨 {added}개를 더했다: {run.LABELS_PATH}")
    else:
        data = _load()
        review = pick_review(data)
        cases = {c["id"]: c["query"] for c in run.load_cases(args.search_dir)}
        books = asyncio.run(_isbn_books([item["isbn"] for item in review]))
        rows = [
            f"# ③ 단계 측정 라벨 중 사람이 확인할 {len(review)}개 ({REVIEW_SHARE:.0%}, 시드 {SEED})",
            "",
            "점수가 기준(evals/search/README.md 의 점수 기준)과 다르면 '고친 점수' 칸에 적습니다.",
            "",
            "| 번호 | 검색어 | 책 | 소개글 앞부분 | 점수 | 고친 점수 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for n, item in enumerate(review, 1):
            book = books[item["isbn"]]
            intro = _line(book["intro"]).replace("|", "/")[:150] or "(소개글 없음)"
            rows.append(
                f"| r{n:02d} | {cases[item['case']]} | {_line(book['title'])} "
                f"({_line(book['author'])}, {_line(book['category'])}) | {intro} | {item['grade']} | |"
            )
        args.out.write_text("\n".join(rows) + "\n", encoding="utf-8")
        print(f"사람이 확인할 라벨 {len(review)}개: {args.out}")
    return 0


async def _isbn_books(isbns: list[str]) -> dict[str, dict]:
    from app.core import db

    await db.connect()
    try:
        async with db.get_pool().acquire() as conn:
            rows = await conn.fetch(
                _BOOKS_SQL.replace(
                    "book_id = ANY($1::int[])", "isbn13 = ANY($1::text[])"
                ),
                isbns,
            )
    finally:
        await db.disconnect()
    return {r["isbn13"]: dict(r) for r in rows}


if __name__ == "__main__":
    sys.exit(main())
