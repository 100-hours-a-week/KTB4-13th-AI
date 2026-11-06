"""#556 B4·C2·C3 의 원리 확인. 격자 결과(--out 파일)만 읽는다. DB·LLM 을 쓰지 않는다.

    uv run python -m evals.searchlab.grid_checks report --dir evals/search/results/b4
    uv run python -m evals.searchlab.grid_checks c3 --dir evals/search/results/b4 --out evals/search/results/c3

- 같아야 하는 비교: 선 0.6 이하(문턱은 그대로) 칸의 모든 낱말 경로는 결과가 같아야 한다. 후보 합계도 그 경로에 듣지 않는다
- 비중 열마다 선·합계를 바꿀 때 첫 화면이 바뀐 검색어 수
- 1:1 열의 동점 쌍 수(같은 등수의 키워드 책과 벡터 책은 점수가 같아 book_id 가 순서를 정한다)
- C2: 1:3 첫 화면에 키워드에만 든 책, 서술형을 키워드 결과 0권·1권 이상으로 나눈 차이, 1등을 잃은 사례의 벡터 등수
- C3: 판별 혼동표(평가 묶음 종류 × "서술형" 판별)와, 사례마다 판별대로 비중을 고른 결과
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from app.search import keyword, rrf
from evals.search import run
from evals.search.label import load_labels
from evals.searchlab import quality

COVERAGES = ("m050", "m055", "m060", "m065", "m070")
BUDGETS = ("b500", "b1000", "b1500")
WEIGHTS = ("w31", "w11", "w13")
BASE = "m060-b1000-w31"
SEMANTIC = "m060-b1000-w13"
SCREEN_VECTORS = 20  # 정렬할 때 넣는 벡터 권수(sort_vector_limit)
TOP = run.SCREEN_TOP


def rows(path: Path) -> dict[str, dict]:
    return {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))["scores"]}


def trace_of(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return row["trace"]


def all_words(row: Mapping[str, Any]) -> bool:
    return not trace_of(row).get("each_word", False)


def same_all_words(folder: Path) -> list[str]:
    """같아야 하는 비교. 어긋난 곳을 적어 돌려준다. 비어 있으면 모두 같다."""
    broken = []
    reference = rows(folder / f"{BASE}-rel.json")
    paths = [i for i, r in reference.items() if all_words(r)]
    for coverage in ("m050", "m055", "m060"):
        for budget in BUDGETS:
            other = rows(folder / f"{coverage}-{budget}-w31-rel.json")
            for case in paths:
                got = trace_of(other[case])
                if got.get("each_word") or (
                    got["keyword_ids"] != trace_of(reference[case])["keyword_ids"]
                ):
                    broken.append(f"{coverage}-{budget} {case}")
    return broken


def changed(a: Mapping[str, dict], b: Mapping[str, dict]) -> Counter:
    """첫 화면이 바뀐 사례 수를 종류별로 센다."""
    return Counter(
        row["kind"]
        for case, row in a.items()
        if row["book_ids"][:TOP] != b[case]["book_ids"][:TOP]
    )


def column_effects(folder: Path) -> list[str]:
    """비중 열마다, 선이나 합계를 하나 바꿀 때 첫 화면이 바뀐 사례 수."""
    lines = [
        "| 비중 | 바꾼 것 | 제목 | 표기 변형 | 저자 | 서술형 | 필터 | 합계 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for weight in WEIGHTS:
        base = rows(folder / f"m060-b1000-{weight}-screen.json")
        cells = [(c, "b1000") for c in COVERAGES if c != "m060"]
        cells += [("m060", b) for b in BUDGETS if b != "b1000"]
        for coverage, budget in cells:
            other = rows(folder / f"{coverage}-{budget}-{weight}-screen.json")
            count = changed(base, other)
            lines.append(
                f"| {weight} | {coverage}-{budget} | {count['title']} | "
                f"{count['title-variant']} | {count['author']} | "
                f"{count['descriptive']} | {count['filter']} | {sum(count.values())} |"
            )
    return lines


def fused(row: Mapping[str, Any], weights: Sequence[float]) -> tuple[list[int], dict]:
    """첫 화면과 같은 재료로 합친 순서와 점수(인기순, 벡터 20권, 판매 없음)."""
    t = trace_of(row)
    exact = set(t.get("exact_ids", []))
    rest = [i for i in t["keyword_ids"] if i not in exact]
    pool = t.get("vector_ids", [])[:SCREEN_VECTORS]
    scores: dict[int, float] = {}
    for ranking, weight in zip((rest, pool), weights, strict=True):
        for rank, book in enumerate(ranking, start=1):
            scores[book] = scores.get(book, 0.0) + weight / (rrf.K + rank)
    return rrf.fuse([rest, pool], list(weights)), scores


def tie_pairs(folder: Path) -> Counter:
    """1:1 열에서 첫 화면 안의 동점 쌍 수와 그런 사례 수를 종류별로 센다."""
    out: Counter = Counter()
    for row in rows(folder / "m060-b1000-w11-rel.json").values():
        order, scores = fused(row, (1.0, 1.0))
        pairs = sum(1 for a, b in pairwise(order[:TOP]) if scores[a] == scores[b])
        if pairs:
            out[row["kind"]] += pairs
            out[f"{row['kind']} 사례"] += 1
    return out


def keyword_only(folder: Path) -> tuple[Counter, Counter, list[str]]:
    """1:3 첫 화면에 키워드에만 든 책. (권수, 벡터가 모자란 사례의 권수, 벡터가 넉넉한데 나온 사례)."""
    traced = rows(folder / f"{SEMANTIC}-rel.json")
    screen = rows(folder / f"{SEMANTIC}-screen.json")
    books: Counter = Counter()
    short: Counter = Counter()
    surprises = []
    for case, row in screen.items():
        t = trace_of(traced[case])
        exact = set(t.get("exact_ids", []))
        vector = t.get("vector_ids", [])[:SCREEN_VECTORS]
        only = [
            i for i in row["book_ids"][:TOP] if i not in exact and i not in set(vector)
        ]
        if not only:
            continue
        if len(vector) + len(exact) >= TOP:
            surprises.append(case)
            books[row["kind"]] += len(only)
        else:
            short[row["kind"]] += len(only)
    return books, short, surprises


def split_descriptive(
    base: quality.Run, new: quality.Run, traced: Mapping[str, dict]
) -> list[str]:
    """서술형을 키워드 결과 0권과 1권 이상으로 나눈 nDCG 차이."""
    lines = [
        "| 키워드 결과 | 사례 | 기준선 | 새 안 | 차이 | 구간 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    ids = [i for i, r in base.rows.items() if r["kind"] == "descriptive"]
    for name, wanted in (("0권", True), ("1권 이상", False)):
        group = [i for i in ids if (not trace_of(traced[i])["keyword_ids"]) == wanted]
        if not group:
            lines.append(f"| {name} | 0 | - | - | - | - |")
            continue
        was = [base.rows[i]["ndcg"] for i in group]
        now = [new.rows[i]["ndcg"] for i in group]
        gap = quality.bootstrap([b - a for a, b in zip(was, now, strict=True)])
        lines.append(
            f"| {name} | {len(group)} | {sum(was) / len(was):.3f} | {sum(now) / len(now):.3f} "
            f"| {gap.mean:+.3f} | [{gap.low:+.3f}, {gap.high:+.3f}] |"
        )
    return lines


def lost_first(
    base: quality.Run, new: quality.Run, traced: Mapping[str, dict], kind: str
) -> list[str]:
    """1등을 잃은 사례와 기준선 1등 책의 벡터 등수(앞 20권에 들어야 화면에 남는다)."""
    ids = [i for i, r in base.rows.items() if r["kind"] == kind]
    lost, _ = quality.flips(base, new, ids)
    lines = []
    for case in lost:
        first = base.rows[case]["book_ids"][0]
        vector = trace_of(traced[case]).get("vector_ids", [])
        rank = vector.index(first) + 1 if first in vector else None
        change = base.rows[case].get("change") or ""
        lines.append(f"- {case} {change}: 정답의 벡터 등수 {rank or '50등 밖'}")
    return lines


def is_descriptive(row: Mapping[str, Any], query: str, rule: str) -> bool:
    """서술형 판별. r1 은 #556 C3 의 신호 그대로, r2 는 낱말이 둘 이상인 것만 넓힌다."""
    t = trace_of(row)
    widened = t.get("each_word", False) and not t.get("exact_ids")
    if rule == "r1":
        return widened
    return widened and len(keyword.tokenize(query)) >= 2


def confusion(traced: Mapping[str, dict], rule: str) -> list[str]:
    queries = {c["id"]: c["query"] for c in run.load_bundle()["cases"]}
    table: dict[str, Counter] = {}
    for case, row in traced.items():
        kind = row["kind"]
        name = f"{kind}/{row['change']}" if row.get("change") else kind
        for key in {kind, name}:
            table.setdefault(key, Counter())[
                is_descriptive(row, queries[case], rule)
            ] += 1
    lines = ["| 종류 | 사례 | 서술형으로 갈림 | 그 밖 |", "| --- | --- | --- | --- |"]
    for key in sorted(table):
        count = table[key]
        lines.append(
            f"| {key} | {count[True] + count[False]} | {count[True]} | {count[False]} |"
        )
    return lines


def compose(folder: Path, out: Path, rule: str) -> Path:
    """사례마다 판별대로 비중을 고른 첫 화면 파일을 만든다. 사례끼리는 서로 영향을 주지 않는다."""
    queries = {c["id"]: c["query"] for c in run.load_bundle()["cases"]}
    traced = rows(folder / f"{BASE}-rel.json")
    base = json.loads((folder / f"{BASE}-screen.json").read_text(encoding="utf-8"))
    semantic = rows(folder / f"{SEMANTIC}-screen.json")
    scores = [
        semantic[r["id"]]
        if is_descriptive(traced[r["id"]], queries[r["id"]], rule)
        else r
        for r in base["scores"]
    ]
    saved = {
        **base,
        "scores": scores,
        "mode_overrides": {"서술형 판별": rule, "서술형 비중": "1:3"},
    }
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"c3-{rule}-screen.json"
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    return path


def report(folder: Path) -> str:
    labels = load_labels()["labels"]
    base = quality.load(folder / f"{BASE}-screen.json", labels)
    traced = rows(folder / f"{BASE}-rel.json")
    broken = same_all_words(folder)
    books, short, surprises = keyword_only(folder)
    lines = [
        "## 같아야 하는 비교",
        "",
        f"모든 낱말 경로 {sum(all_words(r) for r in traced.values())}건, 어긋난 곳 {len(broken)}건",
        *[f"- {item}" for item in broken[:20]],
        "",
        "## 비중 열마다 첫 화면이 바뀐 사례 수",
        "",
        *column_effects(folder),
        "",
        "## 1:1 열의 동점",
        "",
        str(dict(tie_pairs(folder))),
        "",
        "## C2 구현 확인: 1:3 첫 화면의 키워드에만 든 책",
        "",
        f"벡터가 넉넉한데 나옴 {dict(books)} {surprises[:10]}, 벡터가 모자란 사례 {dict(short)}",
        "",
    ]
    for weight in ("w11", "w13"):
        new = quality.load(folder / f"m060-b1000-{weight}-screen.json", labels)
        lines += [f"## C2 {weight}: 서술형을 키워드 결과로 나눔", ""]
        lines += split_descriptive(base, new, traced)
        lines += ["", f"### {weight}: 1등을 잃은 표기 변형", ""]
        lines += lost_first(base, new, traced, "title-variant")
        lines += ["", f"### {weight}: 1등을 잃은 제목", ""]
        lines += lost_first(base, new, traced, "title")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#556 B4·C2·C3 원리 확인")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("report")
    one.add_argument("--dir", required=True, type=Path)
    two = sub.add_parser("c3")
    two.add_argument("--dir", required=True, type=Path)
    two.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "report":
        print(report(args.dir))
        return 0
    traced = rows(args.dir / f"{BASE}-rel.json")
    for rule in ("r1", "r2"):
        print(f"### 판별 {rule}\n")
        print("\n".join(confusion(traced, rule)))
        print(f"\n{compose(args.dir, args.out, rule)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
