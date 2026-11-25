"""검색 평가 결과 둘(기준과 후보)을 같은 사례끼리 짝지어 #556 「미리 정하는 판정 기준」대로 판정한다(#752).

    # 기준과 후보 하나. 판정표를 PR 본문의 "고정 Dataset 검증 결과" 칸에 붙인다
    uv run python -m evals.search.verdict compare --base main-screen.json --new pr-screen.json
    # 확인용 검색어로 마지막 후보 여럿을 볼 때. Holm 보정은 여기서만 건다
    uv run python -m evals.search.verdict --labels evals/search/confirm-labels.json \\
        confirm --base base.json --new a.json --new b.json

파일은 `evals/search/run.py --screen --out` 이 남긴 것이다. DB 와 LLM 을 쓰지 않는다. 서술형은 파일에 적힌 숫자를
쓰지 않고 지금의 라벨로 다시 매긴다(모든 판정은 마지막 라벨 집합으로 한다).

판정은 인기순 첫 화면 12권으로 낸 파일끼리만 한다. 관련도순 파일은 숫자만 내고 "원리 확인용"이라고 적는다.

| 무엇 | 탈락 | 좋아짐 |
| --- | --- | --- |
| 제목 | 1등 맞힌 수가 2건 이상 줄어듦(잃은 수 − 얻은 수) | 보지 않음 |
| 표기 변형 1등 | 잃은 쪽이 많고 McNemar 정확 검정 p < 0.05 | 짝 부트스트랩 구간 하한 > 0 |
| 저자 | 짝 부트스트랩 구간 하한 < −0.02 | 보지 않음 |
| 서술형 | 자동 탈락 없음. 구간과 바뀐 사례를 보고 사람이 정한다 | nDCG 차이의 구간 하한 > 0 |
| 필터 | 조건을 어긴 책이 1권이라도 | 보지 않음 |

하나라도 탈락이면 탈락이다. 탈락이 없고 Judged 평균이 어느 쪽이든 0.9 미만이면 "라벨 보충 뒤 판정"이다. 그 뒤
서술형이나 표기 변형이 좋아졌으면 "좋아짐", 아니면 "판단 불가"다("같다"가 아니다). 재지 못한 사례(200 이 아니거나
줄여 답함)가 있으면 짝이 빠진 채 판정하지 않고 "다시 재기"로 적는다.

`--engine`(Elasticsearch 판정, #754. V4 최종안 5절에서 측정 전에 정함)은 검색 엔진을 바꾸는 판정이라 기준 둘을 더한다.

| 무엇 | 탈락 | 좋아짐 |
| --- | --- | --- |
| 서술형 | nDCG 차이의 구간 위 끝 < 0(Judged 가 모자라면 라벨 보충 뒤) | 그대로 |
| 찾던 책(키워드 0건 중 찾던 책을 붙인 사례) | 표기 변형과 같이 McNemar | 표기 변형과 같이 구간 하한 > 0 |
"""

import argparse
import json
import statistics
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import stats
from evals.search import run
from evals.search.label import load_labels
from evals.search.scoring import DESCRIPTIVE_TOP, Book, descriptive

# 짝 부트스트랩 시드. V3 판정(#556)과 같아서, 같은 파일이면 그때의 구간이 다시 나온다
SEED = 556
# 탈락 문턱(#556 「판정 기준」 2)
TITLE_LOSS = 2
AUTHOR_MARGIN = 0.02

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
    lines: list[Line]
    verdict: str
    changed: dict[str, int]
    pairs: dict[str, int]
    dropped: list[str]


def _bootstrap(diffs: Sequence[float]) -> stats.Interval:
    return stats.bootstrap(diffs, seed=SEED)


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
    """견줄 수 없는 이유. 묶음·정렬·권수·DB 가 다르면 검색 코드 말고 다른 것이 바뀐 것이다."""
    for key in ("bundle_version", "sort", "top", "look", "catalog", "database"):
        if base.meta.get(key) != new.meta.get(key):
            return f"{key} 가 다르다({base.meta.get(key)} 대 {new.meta.get(key)})"
    return None


def judged_file(run_: Run) -> bool:
    return run_.meta["sort"] == run.SCREEN_SORT and run_.meta["top"] == run.SCREEN_TOP


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


def flips(base: Run, new: Run, ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """(기준만 1등을 맞힌 사례, 후보만 맞힌 사례)."""
    lost = [i for i in ids if first_hit(base.rows[i]) and not first_hit(new.rows[i])]
    gained = [i for i in ids if first_hit(new.rows[i]) and not first_hit(base.rows[i])]
    return lost, gained


def title_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    lost, gained = flips(base, new, ids)
    fell = len(lost) - len(gained)
    return Line(
        f"제목 {len(ids)}건 1등",
        sum(first_hit(base.rows[i]) for i in ids),
        sum(first_hit(new.rows[i]) for i in ids),
        f"잃음 {len(lost)} · 얻음 {len(gained)}",
        FAIL if fell >= TITLE_LOSS else PASS,
    )


def _paired_line(
    name: str,
    base: Run,
    new: Run,
    ids: Sequence[str],
    hit: Callable[[Mapping[str, Any]], bool],
) -> Line:
    """사례마다 맞힘·못 맞힘인 줄. 잃은 쪽이 많고 McNemar p < 0.05 면 탈락, 구간 하한 > 0 이면 좋아짐."""
    lost = [i for i in ids if hit(base.rows[i]) and not hit(new.rows[i])]
    gained = [i for i in ids if hit(new.rows[i]) and not hit(base.rows[i])]
    p = stats.mcnemar(len(lost), len(gained))
    gap = _bootstrap([float(hit(new.rows[i])) - float(hit(base.rows[i])) for i in ids])
    if len(lost) > len(gained) and p < stats.ALPHA:
        verdict = FAIL
    elif gap.low > 0:
        verdict = BETTER
    else:
        verdict = UNCLEAR
    return Line(
        name,
        sum(hit(base.rows[i]) for i in ids),
        sum(hit(new.rows[i]) for i in ids),
        f"잃음 {len(lost)} · 얻음 {len(gained)}, McNemar p {p:.3g}, 구간 {_span(gap)}",
        verdict,
    )


def variant_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    return _paired_line(f"표기 변형 {len(ids)}건 1등", base, new, ids, first_hit)


def sought_found(row: Mapping[str, Any]) -> bool:
    """찾던 책이 첫 화면에 들었는가(#764)."""
    return row["rank"] is not None


def sought_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    """찾던 책을 붙인 키워드 0건 사례(오타·로마자·초성). --engine 에서만 본다(#754)."""
    return _paired_line(f"찾던 책 {len(ids)}건 첫 화면", base, new, ids, sought_found)


def author_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    was = [base.rows[i]["author_share"] for i in ids]
    now = [new.rows[i]["author_share"] for i in ids]
    gap = _bootstrap([b - a for a, b in zip(was, now, strict=True)])
    return Line(
        f"저자 {len(ids)}건 그 저자 비율",
        statistics.fmean(was),
        statistics.fmean(now),
        f"차이 {gap.mean:+.3f}, 구간 {_span(gap)}",
        FAIL if gap.low < -AUTHOR_MARGIN else PASS,
    )


def descriptive_lines(
    base: Run, new: Run, ids: Sequence[str], engine: bool = False
) -> list[Line]:
    """서술형 줄들. 판정은 nDCG(라벨 없는 책 = 0, 하한)로 하고 나머지는 숫자만 낸다.

    engine 이면 nDCG 구간 위 끝이 0 아래일 때 탈락이다. 라벨 없는 책을 0점으로 친 하한이라, Judged 가 모자라면
    탈락 대신 라벨 보충 뒤 판정으로 둔다.
    """
    enough = stats.enough_labels(
        statistics.fmean(base.rows[i]["judged"] for i in ids),
        statistics.fmean(new.rows[i]["judged"] for i in ids),
    )
    lines = []
    for key, name in (("ndcg", "서술형 nDCG"), ("ndcg_labeled", "서술형 nDCG′")):
        was = [base.rows[i][key] for i in ids]
        now = [new.rows[i][key] for i in ids]
        gap = _bootstrap([b - a for a, b in zip(was, now, strict=True)])
        verdict = "-"
        note = ", 확실히 내려감(사람이 정한다)" if gap.high < 0 else ""
        if key == "ndcg":
            verdict = BETTER if gap.low > 0 else UNCLEAR
            if engine and gap.high < 0:
                verdict = FAIL if enough else NEEDS_LABELS
                note = ", 확실히 내려감"
        lines.append(
            Line(
                name,
                statistics.fmean(was),
                statistics.fmean(now),
                f"차이 {gap.mean:+.3f}, 구간 {_span(gap)}{note}",
                verdict,
            )
        )
    lines.append(
        Line(
            "서술형 0점 책(평균)",
            statistics.fmean(base.rows[i]["irrelevant"] for i in ids),
            statistics.fmean(new.rows[i]["irrelevant"] for i in ids),
            "",
            "-",
        )
    )
    return lines


def judged_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    was = statistics.fmean(base.rows[i]["judged"] for i in ids)
    now = statistics.fmean(new.rows[i]["judged"] for i in ids)
    return Line(
        "Judged",
        was,
        now,
        f"문턱 {stats.JUDGED_MIN}",
        PASS if stats.enough_labels(was, now) else NEEDS_LABELS,
    )


def filter_line(base: Run, new: Run, ids: Sequence[str]) -> Line:
    return Line(
        f"필터 {len(ids)}건 어긴 책",
        sum(base.rows[i]["violations"] for i in ids),
        sum(new.rows[i]["violations"] for i in ids),
        "",
        FAIL if sum(new.rows[i]["violations"] for i in ids) > 0 else PASS,
    )


def changed_cases(base: Run, new: Run, kind: str, top: int) -> int:
    """위 top 권의 책이나 순서가 바뀐 사례 수."""
    return sum(
        1
        for i, row in base.rows.items()
        if row["kind"] == kind
        and list(row["book_ids"][:top]) != list(new.rows[i]["book_ids"][:top])
    )


def compare(base: Run, new: Run, engine: bool = False) -> Comparison:
    """두 파일을 판정한다. 견줄 수 없는 파일이면 ValueError 다. engine 은 모듈 설명의 --engine."""
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
        lines += descriptive_lines(base, new, ids["descriptive"], engine)
        lines.append(judged_line(base, new, ids["descriptive"]))
    if ids.get("filter"):
        lines.append(filter_line(base, new, ids["filter"]))
    sought = [i for i in ids.get("no-keyword", []) if base.rows[i].get("sought")]
    if engine and sought:
        lines.append(sought_line(base, new, sought))
    top = base.meta["top"]
    return Comparison(
        name=new.name,
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


def confirm(
    base: Run, news: Sequence[Run]
) -> list[tuple[str, stats.Interval, bool, list[str]]]:
    """확인용 검색어의 서술형 nDCG 를 후보마다 견주고 Holm 으로 묶는다. (이름, 구간, 넘음, 빠진 사례)."""
    gaps, dropped = {}, {}
    for new in news:
        reason = mismatch(base, new)
        if reason:
            raise ValueError(f"{new.name}: {reason}")
        ids, dropped[new.name] = pair_ids(base, new, "descriptive")
        gaps[new.name] = _bootstrap(
            [new.rows[i]["ndcg"] - base.rows[i]["ndcg"] for i in ids]
        )
    passed = stats.holm({name: gap.p for name, gap in gaps.items()})
    return [
        (name, gap, passed[name] and gap.mean > 0, dropped[name])
        for name, gap in gaps.items()
    ]


def _span(gap: stats.Interval) -> str:
    return f"[{gap.low:+.3f}, {gap.high:+.3f}]"


def _value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}"


def render(base: Run, result: Comparison) -> str:
    """판정표. PR 본문에 그대로 붙일 수 있게 마크다운으로 낸다."""
    lines = [
        f"### {result.name} (기준 {base.name})",
        "",
        f"판정: **{result.verdict}**",
        "",
        "| 무엇 | 기준 | 후보 | 자세히 | 판정 |",
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검색 평가 결과를 짝지어 판정한다")
    parser.add_argument(
        "--labels",
        type=Path,
        help="서술형 라벨 파일. 없으면 labels.json. 확인용 검색어는 그 라벨로 준다(판정 셈법은 같다)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("compare", help="기준과 후보 하나")
    one.add_argument("--base", required=True, type=Path)
    one.add_argument("--new", required=True, type=Path)
    one.add_argument(
        "--engine",
        action="store_true",
        help="검색 엔진을 바꾸는 판정(#754): 서술형이 확실히 내려가면 탈락, 찾던 책 McNemar",
    )
    final = sub.add_parser("confirm", help="마지막 후보 여럿을 Holm 보정으로")
    final.add_argument("--base", required=True, type=Path)
    final.add_argument("--new", required=True, action="append", type=Path)
    final.add_argument(
        "--engine",
        action="store_true",
        help="검색 엔진을 바꾸는 판정(#754): 서술형 구간 위 끝이 0 아래면 탈락",
    )
    args = parser.parse_args(argv)

    labels = (load_labels(args.labels) if args.labels else load_labels())["labels"]
    try:
        base = load(args.base, labels)
        if args.command == "compare":
            print(render(base, compare(base, load(args.new, labels), args.engine)))
        else:
            news = [load(path, labels) for path in args.new]
            for name, gap, passed, dropped in confirm(base, news):
                verdict = REMEASURE if dropped else BETTER if passed else UNCLEAR
                if args.engine and not dropped and gap.high < 0:
                    new = next(n for n in news if n.name == name)
                    judged = [
                        statistics.fmean(r["judged"] for r in x.rows.values())
                        for x in (base, new)
                    ]
                    verdict = FAIL if stats.enough_labels(*judged) else NEEDS_LABELS
                print(
                    f"{name}: 차이 {gap.mean:+.3f}, 구간 {_span(gap)}, p {gap.p:.3g}, {verdict}"
                )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
