"""검색 평가 결과를 같은 사례끼리 짝지어 #556 「미리 정하는 판정 기준」대로 판정한다.

    # 기준선과 새 안 하나. --csv 는 기록 이슈에 붙일 사례별 요약이다
    uv run python -m evals.searchlab.quality compare --base base-screen.json --new w13-screen.json --csv w13.csv
    # 여러 안을 한 표로
    uv run python -m evals.searchlab.quality grid --base base-screen.json results/b4/*-screen.json
    # 확인용 검색어로 마지막 후보만 볼 때. Holm 보정은 여기서만 건다
    uv run python -m evals.searchlab.quality confirm --base base.json --new a.json --new b.json
    # 두 안을 같이 켠 효과가 따로 잰 효과의 합에서 벗어난 정도
    uv run python -m evals.searchlab.quality interaction --none base.json --a a.json --b b.json --both ab.json

파일은 `evals/search/run.py --out` 이 남긴 것이다. DB 와 LLM 을 쓰지 않는다. 서술형은 파일에 적힌 숫자를 쓰지 않고
지금의 labels.json 으로 다시 매긴다(모든 판정은 마지막 라벨 집합으로 한다).

판정은 인기순 첫 화면 12권으로 낸 파일끼리만 한다. 관련도순 파일은 숫자만 내고 "원리 확인용"이라고 적는다.

| 무엇 | 탈락 | 좋아짐 |
| --- | --- | --- |
| 제목 | 1등 맞힌 수가 2건 이상 줄어듦(잃은 수 − 얻은 수) | 보지 않음 |
| 표기 변형 1등 | 잃은 쪽이 많고 McNemar 정확 검정 p < 0.05 | 짝 부트스트랩 구간 하한 > 0 |
| 저자 | 짝 부트스트랩 구간 하한 < −0.02 | 보지 않음 |
| 서술형 | 자동 탈락 없음. 구간과 바뀐 사례를 보고 사람이 정한다 | nDCG 차이의 구간 하한 > 0 |
| 필터 | 조건을 어긴 책이 1권이라도 | 보지 않음 |

하나라도 탈락이면 탈락이다. 탈락이 없고 Judged@12 평균이 어느 쪽이든 0.9 미만이면 "라벨 보충 뒤 판정"이다. 그 뒤
서술형이나 표기 변형이 좋아졌으면 "좋아짐", 아니면 "판단 불가"다("같다"가 아니다). 재지 못한 사례(200 이 아니거나
줄여 답함)가 있으면 짝이 빠진 채 판정하지 않고 "다시 재기"로 적는다.
"""

import argparse
import csv
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from evals.search import run
from evals.search.label import load_labels
from evals.search.scoring import DESCRIPTIVE_TOP, Book, descriptive

# 짝 부트스트랩 반복 수와 시드. 시간 측정기(analyze.py)와 같다. 같은 파일이면 같은 구간이 나온다
BOOTSTRAP = 10_000
SEED = 556
ALPHA = 0.05
# 탈락 문턱(#556 「판정 기준」 2)
TITLE_LOSS = 2
AUTHOR_MARGIN = 0.02
# 라벨을 보충해야 하는 선(#556 「품질 지표와 라벨」)
JUDGED_MIN = 0.9
# 영향 없음으로 보는 상호작용의 크기(#556 「판정 기준」 6)
INTERACTION_LIMIT = 0.01

# 판정할 수 있는 파일. 사용자가 보는 첫 화면으로 잰 것이다
JUDGED_SORT = run.SCREEN_SORT
JUDGED_TOP = run.SCREEN_TOP

FAIL = "탈락"
PASS = "통과"
BETTER = "좋아짐"
UNCLEAR = "판단 불가"
NEEDS_LABELS = "라벨 보충 뒤 판정"
REMEASURE = "다시 재기"
NOT_JUDGED = "원리 확인용"


@dataclass(frozen=True)
class Run:
    """--out 파일 하나. 서술형은 지금의 라벨로 다시 매겼다."""

    name: str
    meta: Mapping[str, Any]
    rows: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class Interval:
    """짝지은 차이의 평균과 95% 구간. p 는 Holm 보정에만 쓴다."""

    mean: float
    low: float
    high: float
    p: float


@dataclass
class Line:
    """판정표의 한 줄."""

    name: str
    base: float
    new: float
    detail: str
    verdict: str


@dataclass
class Comparison:
    name: str
    overrides: Mapping[str, Any]
    lines: list[Line]
    verdict: str
    changed: dict[str, int]
    pairs: dict[str, int]
    dropped: list[str]
    notes: list[str] = field(default_factory=list)


def rescore(row: Mapping[str, Any], labels: Mapping, look: int) -> dict[str, Any]:
    """서술형 사례 하나를 지금의 라벨로 다시 매긴다."""
    if "isbns" not in row:
        raise ValueError(f"{row['id']}: 파일에 ISBN 이 없어 다시 매길 수 없다")
    books = [
        Book(i, isbn, "", None, None, None, None)
        for i, isbn in zip(row["book_ids"], row["isbns"], strict=True)
    ]
    return {**row, **descriptive(books, labels.get(row["id"], {}), look)}


def load(path: Path, labels: Mapping) -> Run:
    meta = json.loads(path.read_text(encoding="utf-8"))
    look = meta.get("look") or DESCRIPTIVE_TOP
    rows = {
        row["id"]: rescore(row, labels, look) if row["kind"] == "descriptive" else row
        for row in meta["scores"]
    }
    return Run(path.stem, meta, rows)


def mismatch(base: Run, new: Run) -> str | None:
    """견줄 수 없는 이유. 묶음·정렬·권수·DB 가 다르면 설정 말고 다른 것이 바뀐 것이다."""
    for key in ("bundle_version", "sort", "top", "look", "catalog", "database"):
        if base.meta.get(key) != new.meta.get(key):
            return f"{key} 가 다르다({base.meta.get(key)} 대 {new.meta.get(key)})"
    return None


def judged_file(run_: Run) -> bool:
    return run_.meta["sort"] == JUDGED_SORT and run_.meta["top"] == JUDGED_TOP


def bootstrap(diffs: Sequence[float], *, seed: int = SEED) -> Interval:
    """같은 사례끼리 낸 차이의 평균과 짝 부트스트랩 95% 구간(백분위). 차이가 모두 0이면 구간도 0이다."""
    d = np.asarray(diffs, dtype=float)
    if d.size == 0:
        raise ValueError("짝지은 사례가 없다")
    if not d.any():
        return Interval(0.0, 0.0, 0.0, 1.0)
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, d.size, size=(BOOTSTRAP, d.size))].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    p = min(1.0, 2 * min(float((means <= 0).mean()), float((means >= 0).mean())))
    return Interval(float(d.mean()), float(low), float(high), p)


def mcnemar(lost: int, gained: int) -> float:
    """엇갈린 사례만 세는 McNemar 정확 검정의 양쪽 p 값. 엇갈린 사례가 없으면 1 이다."""
    n = lost + gained
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(lost, gained) + 1)) / 2**n
    return min(1.0, 2 * tail)


def holm(pvalues: Mapping[str, float], *, alpha: float = ALPHA) -> dict[str, bool]:
    """Holm 보정. 작은 p 부터 alpha/(남은 수) 와 견주고, 처음 못 넘으면 그 뒤는 모두 못 넘는다."""
    passed = dict.fromkeys(pvalues, False)
    ordered = sorted(pvalues, key=lambda name: pvalues[name])
    for i, name in enumerate(ordered):
        if pvalues[name] >= alpha / (len(ordered) - i):
            break
        passed[name] = True
    return passed


def pair_ids(base: Run, new: Run, kind: str) -> tuple[list[str], list[str]]:
    """(둘 다 온전히 잰 사례, 어느 쪽이든 재지 못해 빠진 사례)."""
    ids = sorted(i for i, row in base.rows.items() if row["kind"] == kind)
    missing = set(ids) ^ {i for i, row in new.rows.items() if row["kind"] == kind}
    if missing:
        raise ValueError(f"두 파일의 사례가 다르다: {sorted(missing)[:5]}")
    good = [i for i in ids if base.rows[i]["ok"] and new.rows[i]["ok"]]
    return good, sorted(set(ids) - set(good))


def first_hit(row: Mapping[str, Any]) -> bool:
    return row["rank"] == 1


def title_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    was = sum(first_hit(base.rows[i]) for i in ids)
    now = sum(first_hit(new.rows[i]) for i in ids)
    lost, gained = flips(base, new, ids)
    fell = len(lost) - len(gained)
    verdict = FAIL if fell >= TITLE_LOSS else PASS
    return Line(
        f"제목 {len(ids)}건 1등",
        was,
        now,
        f"잃음 {len(lost)} · 얻음 {len(gained)}",
        verdict,
    )


def flips(base: Run, new: Run, ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """(기준선만 1등을 맞힌 사례, 새 안만 맞힌 사례)."""
    lost = [i for i in ids if first_hit(base.rows[i]) and not first_hit(new.rows[i])]
    gained = [i for i in ids if first_hit(new.rows[i]) and not first_hit(base.rows[i])]
    return lost, gained


def variant_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    lost, gained = flips(base, new, ids)
    p = mcnemar(len(lost), len(gained))
    gap = bootstrap(
        [float(first_hit(new.rows[i])) - float(first_hit(base.rows[i])) for i in ids]
    )
    if len(lost) > len(gained) and p < ALPHA:
        verdict = FAIL
    elif gap.low > 0:
        verdict = BETTER
    else:
        verdict = UNCLEAR
    return Line(
        f"표기 변형 {len(ids)}건 1등",
        sum(first_hit(base.rows[i]) for i in ids),
        sum(first_hit(new.rows[i]) for i in ids),
        f"잃음 {len(lost)} · 얻음 {len(gained)}, McNemar p {p:.3g}, 구간 {_span(gap)}",
        verdict,
    )


def author_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    was = [base.rows[i]["author_share"] for i in ids]
    now = [new.rows[i]["author_share"] for i in ids]
    gap = bootstrap([b - a for a, b in zip(was, now, strict=True)])
    return Line(
        f"저자 {len(ids)}건 그 저자 비율",
        float(np.mean(was)),
        float(np.mean(now)),
        f"차이 {gap.mean:+.3f}, 구간 {_span(gap)}",
        FAIL if gap.low < -AUTHOR_MARGIN else PASS,
    )


def descriptive_lines(base: Run, new: Run, ids: Sequence[str]) -> list[Line]:
    """서술형 줄들. 판정은 nDCG(라벨 없는 책 = 0, 하한)로 하고 나머지는 숫자만 낸다."""
    lines = []
    for key, name in (("ndcg", "서술형 nDCG"), ("ndcg_labeled", "서술형 nDCG′")):
        was = [base.rows[i][key] for i in ids]
        now = [new.rows[i][key] for i in ids]
        gap = bootstrap([b - a for a, b in zip(was, now, strict=True)])
        verdict = "-"
        if key == "ndcg":
            verdict = BETTER if gap.low > 0 else UNCLEAR
        note = ", 확실히 내려감(사람이 정한다)" if gap.high < 0 else ""
        lines.append(
            Line(
                name,
                float(np.mean(was)),
                float(np.mean(now)),
                f"차이 {gap.mean:+.3f}, 구간 {_span(gap)}{note}",
                verdict,
            )
        )
    zero = [(base.rows[i]["irrelevant"], new.rows[i]["irrelevant"]) for i in ids]
    lines.append(
        Line(
            "서술형 0점 책(평균)",
            float(np.mean([a for a, _ in zero])),
            float(np.mean([b for _, b in zero])),
            "",
            "-",
        )
    )
    return lines


def judged_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    was = float(np.mean([base.rows[i]["judged"] for i in ids]))
    now = float(np.mean([new.rows[i]["judged"] for i in ids]))
    return Line(
        "Judged",
        was,
        now,
        f"문턱 {JUDGED_MIN}",
        NEEDS_LABELS if min(was, now) < JUDGED_MIN else PASS,
    )


def filter_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    was = sum(base.rows[i]["violations"] for i in ids)
    now = sum(new.rows[i]["violations"] for i in ids)
    return Line(
        f"필터 {len(ids)}건 어긴 책",
        was,
        now,
        "",
        FAIL if now > 0 else PASS,
    )


def changed_cases(base: Run, new: Run, kind: str, top: int) -> int:
    """위 top 권의 책이나 순서가 바뀐 사례 수."""
    return sum(
        1
        for i, row in base.rows.items()
        if row["kind"] == kind
        and list(row["book_ids"][:top]) != list(new.rows[i]["book_ids"][:top])
    )


def compare(base: Run, new: Run) -> Comparison:
    """두 파일을 판정한다. 견줄 수 없는 파일이면 ValueError 다."""
    reason = mismatch(base, new)
    if reason:
        raise ValueError(f"{new.name}: {reason}")
    kinds = sorted({row["kind"] for row in base.rows.values()})
    ids, dropped = {}, []
    for kind in kinds:
        ids[kind], lost = pair_ids(base, new, kind)
        dropped += lost
    lines: list[Line] = []
    if ids.get("title"):
        lines.append(title_line(base, new, ids["title"]))
    if ids.get("title-variant"):
        lines.append(variant_line(base, new, ids["title-variant"]))
    if ids.get("author"):
        lines.append(author_line(base, new, ids["author"]))
    if ids.get("descriptive"):
        lines += descriptive_lines(base, new, ids["descriptive"])
        lines.append(judged_line(base, new, ids["descriptive"]))
    if ids.get("filter"):
        lines.append(filter_line(base, new, ids["filter"]))
    top = base.meta["top"]
    return Comparison(
        name=new.name,
        overrides=new.meta.get("mode_overrides") or {},
        lines=lines,
        verdict=overall(lines, dropped, judged_file(base)),
        changed={kind: changed_cases(base, new, kind, top) for kind in kinds},
        pairs={kind: len(ids[kind]) for kind in kinds},
        dropped=dropped,
    )


def overall(lines: Sequence[Line], dropped: Sequence[str], judged: bool) -> str:
    """줄마다의 판정을 하나로 모은다(모듈 설명의 순서)."""
    if not judged:
        return NOT_JUDGED
    if dropped:
        return REMEASURE
    verdicts = {line.verdict for line in lines}
    if FAIL in verdicts:
        return FAIL
    if NEEDS_LABELS in verdicts:
        return NEEDS_LABELS
    return BETTER if BETTER in verdicts else UNCLEAR


def interaction(
    none: Sequence[float],
    a: Sequence[float],
    b: Sequence[float],
    both: Sequence[float],
    *,
    limit: float = INTERACTION_LIMIT,
) -> tuple[Interval, str]:
    """2×2 의 상호작용 (y_AB − y_A − y_B + y_0) / 2 와 판정(#556 「판정 기준」 6)."""
    values = (
        np.asarray(both, float)
        - np.asarray(a, float)
        - np.asarray(b, float)
        + np.asarray(none, float)
    ) / 2
    gap = bootstrap(values.tolist())
    if gap.low >= -limit and gap.high <= limit:
        return gap, "영향 없음"
    if gap.low > 0 or gap.high < 0:
        return gap, "영향 있음"
    return gap, UNCLEAR


def _span(gap: Interval) -> str:
    return f"[{gap.low:+.3f}, {gap.high:+.3f}]"


def _value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"


def _settings(overrides: Mapping[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in overrides.items()) or "지금 값"


def render(base: Run, result: Comparison) -> str:
    lines = [
        f"### {result.name} (기준 {base.name})",
        "",
        f"바꾼 값: {_settings(result.overrides)}. 판정: **{result.verdict}**",
        "",
        "| 무엇 | 기준선 | 새 안 | 자세히 | 판정 |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {x.name} | {_value(x.base)} | {_value(x.new)} | {x.detail} | {x.verdict} |"
        for x in result.lines
    ]
    moved = ", ".join(
        f"{kind} {count}/{result.pairs[kind]}" for kind, count in result.changed.items()
    )
    lines += ["", f"위 {base.meta['top']}권이 바뀐 사례: {moved}"]
    if result.dropped:
        lines.append(f"재지 못해 빠진 사례: {', '.join(result.dropped)}")
    return "\n".join(lines)


def render_grid(base: Run, results: Sequence[Comparison]) -> str:
    names = [line.name for line in results[0].lines]
    head = ["안", "바꾼 값", "판정", *names, "바뀐 서술형"]
    lines = [
        "| " + " | ".join(head) + " |",
        "| " + " | ".join("---" for _ in head) + " |",
    ]
    for result in results:
        cells = [f"{_value(x.new)} ({x.verdict})" for x in result.lines]
        lines.append(
            f"| {result.name} | {_settings(result.overrides)} | {result.verdict} | "
            + " | ".join(cells)
            + f" | {result.changed.get('descriptive', 0)} |"
        )
    return "\n".join(lines)


def case_value(row: Mapping[str, Any]) -> float | None:
    """사례 하나의 대표 값. 종류마다 판정이 보는 값이다."""
    kind = row["kind"]
    if kind in ("title", "title-variant"):
        return float(first_hit(row))
    return {
        "author": row.get("author_share"),
        "filter": row.get("violations"),
        "descriptive": row.get("ndcg"),
    }.get(kind)


def write_csv(path: Path, base: Run, new: Run) -> None:
    """사례별 요약. 기록 이슈 코멘트에 붙인다(원자료는 저장소에 없다)."""
    queries = {case["id"]: case["query"] for case in run.load_bundle()["cases"]}
    top = base.meta["top"]
    with path.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(
            [
                "사례",
                "종류",
                "소분류",
                "검색어",
                "기준선",
                "새 안",
                "차이",
                "위 권 바뀜",
            ]
        )
        for case_id, row in base.rows.items():
            other = new.rows[case_id]
            was, now = case_value(row), case_value(other)
            gap = None if was is None or now is None else round(now - was, 4)
            writer.writerow(
                [
                    case_id,
                    row["kind"],
                    row.get("change") or row.get("facet") or "",
                    queries.get(case_id, ""),
                    was,
                    now,
                    gap,
                    int(list(row["book_ids"][:top]) != list(other["book_ids"][:top])),
                ]
            )


def confirm(
    base: Run, news: Sequence[Run]
) -> list[tuple[str, Interval, bool, list[str]]]:
    """확인용 검색어의 서술형 nDCG 를 후보마다 견주고 Holm 으로 묶는다. (이름, 구간, 넘음, 빠진 사례)."""
    gaps, dropped = {}, {}
    for new in news:
        reason = mismatch(base, new)
        if reason:
            raise ValueError(f"{new.name}: {reason}")
        ids, dropped[new.name] = pair_ids(base, new, "descriptive")
        gaps[new.name] = bootstrap(
            [new.rows[i]["ndcg"] - base.rows[i]["ndcg"] for i in ids]
        )
    passed = holm({name: gap.p for name, gap in gaps.items()})
    return [
        (name, gap, passed[name] and gap.mean > 0, dropped[name])
        for name, gap in gaps.items()
    ]


def case_values(run_: Run, kind: str) -> dict[str, float]:
    return {
        i: float(value)
        for i, row in run_.rows.items()
        if row["kind"] == kind and row["ok"] and (value := case_value(row)) is not None
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검색 평가 결과를 짝지어 판정한다")
    parser.add_argument(
        "--labels",
        type=Path,
        help="서술형 라벨 파일. 없으면 labels.json. 확인용 검색어는 그 라벨로 준다(판정 셈법은 같다)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("compare", help="기준선과 새 안 하나")
    one.add_argument("--base", required=True, type=Path)
    one.add_argument("--new", required=True, type=Path)
    one.add_argument("--csv", type=Path, help="사례별 요약을 남길 CSV")
    many = sub.add_parser("grid", help="여러 안을 한 표로")
    many.add_argument("--base", required=True, type=Path)
    many.add_argument("news", nargs="+", type=Path)
    final = sub.add_parser("confirm", help="마지막 후보를 Holm 보정으로")
    final.add_argument("--base", required=True, type=Path)
    final.add_argument("--new", required=True, action="append", type=Path)
    pair = sub.add_parser("interaction", help="두 안의 상호작용")
    for name in ("none", "a", "b", "both"):
        pair.add_argument(f"--{name}", required=True, type=Path)
    pair.add_argument("--kind", default="descriptive")
    args = parser.parse_args(argv)

    labels = (load_labels(args.labels) if args.labels else load_labels())["labels"]
    try:
        if args.command == "compare":
            base, new = load(args.base, labels), load(args.new, labels)
            print(render(base, compare(base, new)))
            if args.csv:
                write_csv(args.csv, base, new)
                print(f"\n사례별 요약: {args.csv}")
        elif args.command == "grid":
            base = load(args.base, labels)
            results = [compare(base, load(path, labels)) for path in args.news]
            print(render_grid(base, results))
        elif args.command == "confirm":
            base = load(args.base, labels)
            news = [load(path, labels) for path in args.new]
            for name, gap, passed, dropped in confirm(base, news):
                verdict = REMEASURE if dropped else BETTER if passed else UNCLEAR
                print(
                    f"{name}: 차이 {gap.mean:+.3f}, 구간 {_span(gap)}, p {gap.p:.3g}, {verdict}"
                )
        else:
            runs = [load(getattr(args, n), labels) for n in ("none", "a", "b", "both")]
            values = [case_values(r, args.kind) for r in runs]
            ids = sorted(set(values[0]).intersection(*values[1:]))
            gap, verdict = interaction(*[[v[i] for i in ids] for v in values])
            print(f"상호작용 {gap.mean:+.4f}, 구간 {_span(gap)}, {verdict}")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
