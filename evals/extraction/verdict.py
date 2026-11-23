"""취향 뽑기 평가 결과 둘(기준과 후보)을 대화끼리 짝지어 #569 의 규칙대로 판정한다(#753).

    uv run python -m evals.extraction.verdict --base main.json --new pr.json
    uv run python -m evals.extraction.verdict --base main.json --new pr.json --group borrowed

두 파일은 `evals/extraction/run.py --out` 이 남긴 것이다. LLM 을 부르지 않는다.

판정(#569 「판정 기준」, 11/5 고정):
- 대화마다 여러 회 중 다수결로 통과를 정한다(3회면 2회 이상)
- 통과율 차이(후보 − 기준)의 짝 부트스트랩 95% 구간 아래 끝이 −M 보다 위면 통과, 아니면 탈락
- M 은 10%p 다. 구간이 넓어 아래 끝이 −M 에 못 미치는 것도 탈락이다(떨어졌을 가능성을 배제하지 못함)
- 실패 이유별로 크게 나빠진 것이 없는지는 표를 보고 사람이 정한다
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evals import stats

MARGIN = 0.10
# 짝 부트스트랩 시드. V3 판정(#569)과 같아서, 같은 파일이면 그때의 구간이 다시 나온다
SEED = 569

PASS = "통과"
FAIL = "탈락"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def mismatch(base: Mapping[str, Any], new: Mapping[str, Any]) -> str | None:
    """견줄 수 없는 이유. 묶음이 다르면 대화와 기대가 다르다."""
    if base.get("bundle_version") != new.get("bundle_version"):
        return (
            f"bundle_version 가 다르다"
            f"({base.get('bundle_version')} 대 {new.get('bundle_version')})"
        )
    return None


def by_case(
    runs: Sequence[Mapping[str, Any]], cases: set[str] | None = None
) -> dict[str, list[bool]]:
    out: dict[str, list[bool]] = {}
    for run in runs:
        if cases is None or run["case_id"] in cases:
            out.setdefault(run["case_id"], []).append(bool(run["passed"]))
    return out


def majority(passes: Sequence[bool]) -> bool:
    """여러 회 중 절반을 넘게 통과했으면 통과. 짝수 회에서 반반이면 통과로 보지 않는다."""
    return sum(passes) * 2 > len(passes)


def unstable(cases: Mapping[str, Sequence[bool]]) -> int:
    """회차 사이에 통과와 실패가 섞인 대화 수. 같은 코드를 다시 돌려도 이만큼은 달라진다."""
    return sum(1 for passes in cases.values() if len(set(passes)) > 1)


def paired(
    base: Mapping[str, Sequence[bool]],
    new: Mapping[str, Sequence[bool]],
    *,
    margin: float = MARGIN,
) -> dict[str, Any]:
    ids = sorted(set(base) & set(new))
    if not ids:
        raise ValueError("두 결과에 같은 대화가 없다")
    was = [majority(base[i]) for i in ids]
    now = [majority(new[i]) for i in ids]
    gap = stats.bootstrap(
        [float(b) - float(a) for a, b in zip(was, now, strict=True)], seed=SEED
    )
    return {
        "cases": len(ids),
        "base_rate": sum(was) / len(ids),
        "new_rate": sum(now) / len(ids),
        "diff": gap.mean,
        "low": gap.low,
        "high": gap.high,
        "base_only": sum(a and not b for a, b in zip(was, now, strict=True)),
        "new_only": sum(b and not a for a, b in zip(was, now, strict=True)),
        "margin": margin,
        "verdict": PASS if gap.low > -margin else FAIL,
        "only_in_base": sorted(set(base) - set(new)),
        "only_in_new": sorted(set(new) - set(base)),
    }


def failure_counts(runs: Sequence[Mapping[str, Any]], cases: set[str]) -> Counter:
    """실패 이유별 호출 수(한 호출에 이유가 여럿이면 각각 센다)."""
    return Counter(
        f for r in runs if r["case_id"] in cases for f in r.get("failures", [])
    )


def kind_rates(runs: Sequence[Mapping[str, Any]], cases: set[str]) -> dict[str, float]:
    kinds: dict[str, list[bool]] = {}
    per = by_case(runs, cases)
    kind_of = {r["case_id"]: r["kind"] for r in runs}
    for case_id, passes in per.items():
        kinds.setdefault(kind_of[case_id], []).append(majority(passes))
    return {k: sum(v) / len(v) for k, v in sorted(kinds.items())}


def report(
    base: Mapping[str, Any],
    new: Mapping[str, Any],
    cases: set[str] | None,
    margin: float = MARGIN,
) -> str:
    """판정표. PR 본문에 그대로 붙일 수 있게 마크다운으로 낸다."""
    b, n = by_case(base["runs"], cases), by_case(new["runs"], cases)
    r = paired(b, n, margin=margin)
    common = set(b) & set(n)
    runs_b = max((len(v) for v in b.values()), default=0)
    runs_n = max((len(v) for v in n.values()), default=0)
    lines = [
        "## 취향 뽑기 판정",
        "",
        f"- 기준: {base.get('model')}, 프롬프트 판 {base.get('prompt_version')}, 대화마다 {runs_b}회",
        f"- 후보: {new.get('model')}, 프롬프트 판 {new.get('prompt_version')}, 대화마다 {runs_n}회",
        f"- 묶음 판 {base.get('bundle_version')}, 대화 {r['cases']}개",
        "",
        "| 기준 통과율 | 후보 통과율 | 차이 | 95% 구간 | 한도 | 판정 |",
        "| --- | --- | --- | --- | --- | --- |",
        (
            f"| {r['base_rate']:.1%} | {r['new_rate']:.1%} | {r['diff'] * 100:+.1f}%p | "
            f"[{r['low'] * 100:+.1f}%p, {r['high'] * 100:+.1f}%p] | −{margin * 100:.0f}%p | **{r['verdict']}** |"
        ),
        "",
        f"- 한쪽만 통과한 대화: 기준만 {r['base_only']}개, 후보만 {r['new_only']}개",
        f"- 회차 사이에 결과가 섞인 대화: 기준 {unstable(b)}개, 후보 {unstable(n)}개(같은 코드를 다시 돌려도 달라지는 정도)",
    ]
    if r["only_in_base"] or r["only_in_new"]:
        lines.append(
            f"- 한쪽에만 있는 대화(견주지 않음): 기준만 {len(r['only_in_base'])}개, 후보만 {len(r['only_in_new'])}개"
        )
    kb, kn = kind_rates(base["runs"], common), kind_rates(new["runs"], common)
    lines += ["", "| 종류 | 기준 | 후보 |", "| --- | --- | --- |"]
    lines += [f"| {k} | {kb[k]:.0%} | {kn.get(k, float('nan')):.0%} |" for k in kb]
    fb, fn = failure_counts(base["runs"], common), failure_counts(new["runs"], common)
    lines += [
        "",
        "실패 이유별 호출 수(크게 나빠진 이유가 있는지는 사람이 봅니다):",
        "",
        "| 실패 이유 | 기준 | 후보 |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {f} | {fb.get(f, 0)} | {fn.get(f, 0)} |" for f in sorted(set(fb) | set(fn))
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="취향 뽑기 평가 결과 둘을 대화끼리 짝지어 판정한다"
    )
    parser.add_argument("--base", required=True, help="기준(main) 결과 파일")
    parser.add_argument("--new", required=True, help="후보(PR) 결과 파일")
    parser.add_argument("--group", help="이 묶음의 대화만 견준다(예: borrowed)")
    parser.add_argument(
        "--margin",
        type=float,
        default=MARGIN,
        help="한도 M(기본 0.10, #569 고정값)",
    )
    args = parser.parse_args(argv)

    base, new = load(Path(args.base)), load(Path(args.new))
    reason = mismatch(base, new)
    if reason:
        print(f"견주지 않는다: {reason}", file=sys.stderr)
        return 2
    cases = None
    if args.group:
        from evals.extraction.run import load_cases

        cases = {c["id"] for c in load_cases() if c["group"] == args.group}
        if not cases:
            print(f"묶음 '{args.group}' 의 대화가 없다", file=sys.stderr)
            return 2
    print(report(base, new, cases, args.margin))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
