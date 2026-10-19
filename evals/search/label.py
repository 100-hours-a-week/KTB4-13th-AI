"""① 검색 평가의 서술형 검색어에 점수(맞음 2·조금 1·아님 0)를 매기는 도구(#502).

    # 1. 코드마다 위 20권을 합쳐, 아직 점수가 없는 책을 검색어마다 섞어 가린 채점 파일로 낸다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.label pool \\
        --code main=../main --code ef400=../ef400 --code ef200=../ef200
    # 2. candidates.md 를 읽고 grades.json 의 빈칸에 점수를 적은 뒤 라벨에 넣는다
    uv run python -m evals.search.label apply --judge "Claude 세션 채점(모델), 날짜"
    # 3. 라벨의 10%를 사람이 확인할 표로 뽑고, review.json 의 fixed 칸에 고친 점수를 넣는다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.label review
    uv run python -m evals.search.label apply-review --by "사용자 확인, 날짜"

점수는 사람이나 작업하는 세션이 직접 매긴다. 이 도구는 LLM 을 부르지 않는다. 중간 파일은 evals/search/results/label/
에 두고 저장소에 올리지 않는다. sources.json 에는 책이 어느 코드의 몇 등에서 나왔는지가 있어서, 채점이 끝날 때까지
열지 않는다. 출처를 알면 판정이 기운다.
"""

import json
import math
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

LABELS_PATH = Path(__file__).with_name("labels.json")
WORK = Path(__file__).parent / "results" / "label"
# 코드마다 위 몇 권을 모으는지. nDCG 는 위 10권으로 내지만, 순위가 조금 바뀌어도 다시 매기지 않게 넉넉히 모은다
POOL_TOP = 20
REVIEW_SHARE = 0.1
# 후보를 섞고 사람 확인용 라벨을 고르는 시드
SEED = "502"
INTRO_CHARS = 300
SCALE = {"2": "맞음", "1": "조금", "0": "아님"}

_BOOKS_SQL = """
SELECT book_id, isbn13, title, author, category,
       left(coalesce(description, ''), {intro}) AS intro
FROM v_books WHERE {key} = ANY($1::{kind}[])
"""


def load_labels(path: Path = LABELS_PATH) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": 1, "scale": SCALE, "judges": [], "review": None, "labels": {}}


def dump(data: Mapping[str, Any], rows: str) -> str:
    """`rows` 칸은 한 줄에 하나씩 쓴다. 라벨을 더한 diff 를 검색어 단위로 볼 수 있다."""

    def one(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = [f" {one(k)}: {one(v)}," for k, v in data.items() if k != rows]
    items = ",\n".join(f"  {one(k)}: {one(v)}" for k, v in data[rows].items())
    return "\n".join(["{", *lines, f" {one(rows)}: {{\n{items}\n }}", "}"]) + "\n"


def _line(text: str | None) -> str:
    return " ".join((text or "").split())


def gather(
    cases: Sequence[dict],
    found: Mapping[str, Sequence[dict]],
    books: Mapping[int, Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, int]],
) -> tuple[str, dict, dict]:
    """(채점 파일, 빈 점수 칸, 출처). `found` 는 코드 이름 → 사례마다의 검색 결과다.

    검색어마다 여러 코드의 위 POOL_TOP 권을 ISBN 으로 합치고, 이미 점수가 있는 책은 뺀다. 남은 책은 시드로 섞어
    c01 부터 번호를 붙인다. 어느 코드의 몇 등이었는지는 출처에만 적는다.
    """
    lines = [
        "# 서술형 검색어 채점 파일",
        "",
        "점수: 2 맞음 · 1 조금 · 0 아님. 기준은 README 의 점수 기준.",
    ]
    grades: dict[str, dict] = {}
    sources: dict[str, dict] = {}
    by_isbn = {book["isbn13"]: book for book in books.values()}
    for index, case in enumerate(cases):
        ranks: dict[str, dict[str, int]] = {}
        for code, results in found.items():
            for rank, book_id in enumerate(results[index]["book_ids"][:POOL_TOP], 1):
                isbn = books[book_id]["isbn13"] if book_id in books else None
                if isbn and isbn not in labels.get(case["id"], {}):
                    ranks.setdefault(isbn, {}).setdefault(code, rank)
        order = sorted(ranks)
        random.Random(f"{SEED}:{case['id']}").shuffle(order)
        lines += ["", f"## {case['id']} · {case.get('facet')} · {case['query']}", ""]
        grades[case["id"]], sources[case["id"]] = {}, {}
        for n, isbn in enumerate(order, 1):
            blind, book = f"c{n:02d}", by_isbn[isbn]
            lines.append(
                f"- {blind} | {_line(book['title'])} | {_line(book['author'])} "
                f"| {_line(book['category'])} | {_line(book['intro'])}"
            )
            grades[case["id"]][blind] = None
            sources[case["id"]][blind] = {"isbn": isbn, "ranks": ranks[isbn]}
    return "\n".join(lines) + "\n", grades, sources


def apply(
    grades: Mapping[str, Mapping[str, int | None]],
    sources: Mapping[str, Mapping[str, Mapping[str, Any]]],
    data: dict[str, Any],
    judge: str,
) -> int:
    """매긴 점수를 라벨에 넣는다. 빈칸은 건너뛰고, 이미 있는 라벨은 덮지 않는다(고치는 것은 apply-review)."""
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
    judges = data["judges"]
    entry = next((j for j in judges if j["by"] == judge), None)
    if entry is None and added:
        judges.append(entry := {"by": judge, "labels": 0})
    if entry:
        entry["labels"] += added
    return added


def pick_review(data: Mapping[str, Any], share: float = REVIEW_SHARE) -> list[dict]:
    """사람이 확인할 라벨. 모든 라벨에서 시드로 무작위로 고른다(share 만큼, 올림)."""
    pairs = sorted((c, i) for c, marks in data["labels"].items() for i in marks)
    picked = sorted(random.Random(SEED).sample(pairs, math.ceil(len(pairs) * share)))
    return [
        {"case": c, "isbn": i, "grade": data["labels"][c][i], "fixed": None}
        for c, i in picked
    ]


def apply_review(review: Mapping[str, dict], data: dict[str, Any], by: str) -> int:
    """사람이 고친 점수를 라벨에 넣고, 몇 권을 보고 몇 권을 고쳤는지 남긴다."""
    changed = 0
    for item in review.values():
        fixed = item["fixed"]
        if fixed is not None and fixed != item["grade"]:
            if fixed not in (0, 1, 2):
                raise ValueError(
                    f"{item['case']} {item['isbn']}: 점수는 0, 1, 2 중 하나다"
                )
            data["labels"][item["case"]][item["isbn"]] = fixed
            changed += 1
    data["review"] = {
        "by": by,
        "seed": SEED,
        "share": REVIEW_SHARE,
        "sampled": len(review),
        "changed": changed,
    }
    return changed
