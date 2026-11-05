"""검색 시간 회차 기록을 #556 「미리 정하는 판정 기준」대로 정리한다(#597).

    uv run python -m evals.searchlab.analyze out/a1-ef --baseline ef400

`rounds.py` 가 남긴 `out/<plan>/` 를 읽어 마크다운 표를 `out/<plan>/report.md` 에 쓴다. DB·AWS 는 쓰지 않는다.

- 검색어 하나의 값: 처음 조회는 그 한 번, 반복 조회는 첫 번을 뺀 나머지의 중앙값(ms)
- 같은 설정을 두 번 쟀으면(A·B·B·A) 검색어마다 두 회차의 기하평균을 그 설정의 값으로 쓴다
- 설정끼리는 같은 검색어로 짝지어 log(새 설정 ÷ 기준선)의 중앙값, 짝 부트스트랩 95% 구간, Wilcoxon 으로 본다
"""

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# 짝 부트스트랩 반복 수와 시드. 같은 기록이면 같은 구간이 나온다
BOOTSTRAP = 10_000
SEED = 556
# 탈락: 느린 쪽(p95)이 이만큼 넘게 늘어남(#556 「판정 기준」 2)
P95_LIMIT = 1.20
ALPHA = 0.05
# 버릴 회차: 회차 뒤 CPU 계산이 앞보다 이만큼 넘게 느림
BENCH_LIMIT = 1.30
# 버릴 회차: 그 회차의 평균 읽기 지연이 계획 안 회차들의 중앙값보다 이만큼 넘게 높음
READ_LATENCY_LIMIT = 1.30
# 짝짓지 않음: 두 회차의 디스크 버스트 잔고 최저값이 이만큼(%p) 넘게 다름
BALANCE_GAP = 20.0

_SUFFIX = re.compile(r"-\d+$")
# 앱의 벡터 검색 한도(app/search/vector.py QUERY_TIMEOUT_SECONDS, 테스트로 맞춰 본다). 측정 회차는 한도를 풀고
# 재므로, 앱이라면 끊겼을 검색은 잰 벡터 시간이 이 값을 넘은 것으로 센다
APP_VECTOR_LIMIT_MS = 2000.0


def config_of(label: str) -> str:
    """회차 이름에서 설정 이름. A·B·B·A 로 두 번 잰 회차는 이름 뒤에 몇 번째인지 붙어 있다(ef400-2 → ef400)."""
    return _SUFFIX.sub("", label)


def query_values(lines: Sequence[Mapping[str, Any]], state: str) -> dict[str, float]:
    """검색어마다 시간(ms). 반복 조회는 첫 번을 빼고 중앙값. 실패한 검색은 넣지 않는다."""
    reps: dict[str, list[tuple[int, float]]] = {}
    for line in lines:
        if (
            line.get("type") != "search"
            or line.get("state") != state
            or line.get("error")
        ):
            continue
        reps.setdefault(line["id"], []).append((line["rep"], line["total_ms"]))
    out = {}
    for qid, got in reps.items():
        values = [ms for rep, ms in got if state == "first" or rep > 1]
        if values:
            out[qid] = float(np.median(values))
    return out


def cut_counts(lines: Sequence[Mapping[str, Any]], state: str) -> tuple[int, int]:
    """(키워드만으로 답한 검색 수, 앱이라면 벡터가 끊겼을 검색 수). 반복 조회는 첫 번을 뺀다."""
    degraded = over = 0
    for line in lines:
        if (
            line.get("type") != "search"
            or line.get("state") != state
            or line.get("error")
        ):
            continue
        if state == "repeat" and line.get("rep", 1) == 1:
            continue
        degraded += bool(line.get("degraded"))
        over += sum(line.get("stages", {}).get("vector", [])) > APP_VECTOR_LIMIT_MS
    return degraded, over


def percentile(values: Sequence[float], q: float) -> float:
    return (
        float(np.percentile(np.asarray(values, dtype=float), q))
        if len(values)
        else math.nan
    )


def summary(values: Mapping[str, float]) -> dict[str, float]:
    v = list(values.values())
    return {
        "n": len(v),
        "median": percentile(v, 50),
        "p90": percentile(v, 90),
        "p95": percentile(v, 95),
    }


def combine(rounds: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """같은 설정의 회차 여러 개를 검색어마다 기하평균으로 합친다. 모든 회차에 있는 검색어만."""
    if not rounds:
        return {}
    common = set(rounds[0]).intersection(*rounds[1:])
    return {
        q: float(math.exp(np.mean([math.log(r[q]) for r in rounds])))
        for q in sorted(common)
    }


@dataclass
class Compare:
    n: int
    log_median: float
    low: float
    high: float
    wilcoxon_p: float
    p95_ratio: float
    p95_low: float
    p95_high: float
    verdict: str
    notes: list[str] = field(default_factory=list)


def compare(
    new: Mapping[str, float], base: Mapping[str, float], *, seed: int = SEED
) -> Compare:
    """같은 검색어로 짝지어 본다. 판정은 #556 「판정 기준」의 시간 줄과 「통계 방법」 그대로다."""
    from scipy.stats import wilcoxon

    qs = sorted(set(new) & set(base))
    n = len(qs)
    if n < 2:
        return Compare(
            n,
            math.nan,
            math.nan,
            math.nan,
            math.nan,
            math.nan,
            math.nan,
            math.nan,
            "판단 불가",
            ["짝이 모자람"],
        )
    a = np.array([new[q] for q in qs])
    b = np.array([base[q] for q in qs])
    d = np.log(a / b)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(BOOTSTRAP, n))
    medians = np.median(d[idx], axis=1)
    low, high = (float(x) for x in np.percentile(medians, [2.5, 97.5]))
    p95s = np.percentile(a[idx], 95, axis=1) / np.percentile(b[idx], 95, axis=1)
    p95_low, p95_high = (float(x) for x in np.percentile(p95s, [2.5, 97.5]))
    p95_ratio = percentile(a, 95) / percentile(b, 95)
    if np.all(d == 0):
        p = 1.0
    else:
        p = float(wilcoxon(d).pvalue)

    notes = []
    if high < 0:
        by_ci = "줄어듦"
    elif low > 0:
        by_ci = "늘어남"
    else:
        by_ci = None
    by_test = None if p >= ALPHA else ("줄어듦" if np.median(d) < 0 else "늘어남")
    if by_ci != by_test:
        # 구간과 검정이 어긋나거나 둘 다 가르지 못하면 "같다"가 아니라 판단 불가다(「판정 기준」 3)
        verdict = "판단 불가"
        if by_ci or by_test:
            notes.append(
                f"구간({by_ci or '가르지 못함'})과 Wilcoxon({by_test or '가르지 못함'})이 어긋남"
            )
    else:
        verdict = by_ci or "판단 불가"
    if p95_ratio > P95_LIMIT:
        verdict = "탈락"
        notes.append(f"p95 {p95_ratio:.2f}배(> {P95_LIMIT:.2f})")
    return Compare(
        n,
        float(np.median(d)),
        low,
        high,
        p,
        p95_ratio,
        p95_low,
        p95_high,
        verdict,
        notes,
    )


def fit_blocks(points: Sequence[tuple[float, float]]) -> dict[str, float] | None:
    """시간(ms) = a + b × DB 캐시 밖에서 읽은 블록. 기계·캐시 차이를 블록당 시간으로 보려는 것이다."""
    if len(points) < 3:
        return None
    x = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float)
    if np.all(x == x[0]):
        return None
    b, a = np.polyfit(x, y, 1)
    pred = a + b * x
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot else math.nan
    return {"a_ms": float(a), "b_ms_per_block": float(b), "r2": r2, "n": len(points)}


def block_points(lines: Sequence[Mapping[str, Any]]) -> list[tuple[float, float]]:
    """처음 조회의 (읽은 블록 합, 시간). pg_stat_statements 를 켜고 잰 회차만 있다."""
    out = []
    for line in lines:
        if (
            line.get("type") == "search"
            and line.get("state") == "first"
            and "statements" in line
            and not line.get("error")
        ):
            read = sum(s.get("shared_blks_read", 0) for s in line["statements"])
            out.append((float(read), float(line["total_ms"])))
    return out


def generic_plans(lines: Sequence[Mapping[str, Any]]) -> tuple[int, int]:
    """(일반 계획을 쓴 문장 수, 계획 횟수를 아는 문장 수). PostgreSQL 18 부터 안다."""
    used = known = 0
    for line in lines:
        for s in line.get("statements", []):
            if "generic_plan_calls" in s:
                known += 1
                used += bool(s.get("generic_plan_calls"))
    return used, known


def overlap(
    new: Sequence[Mapping[str, Any]], base: Sequence[Mapping[str, Any]]
) -> float:
    """처음 조회의 위 20권이 기준선과 겹치는 비율(검색어마다 겹친 권수 ÷ 기준선 권수의 평균)."""

    def ids(lines):
        return {
            x["id"]: x["ids"]
            for x in lines
            if x.get("type") == "search"
            and x.get("state") == "first"
            and not x.get("error")
        }

    a, b = ids(new), ids(base)
    rates = [len(set(a[q]) & set(b[q])) / len(b[q]) for q in set(a) & set(b) if b[q]]
    return float(np.mean(rates)) if rates else math.nan


def metric_avg(points: Sequence[Mapping[str, Any]]) -> float | None:
    vals = [p["avg"] for p in points if p.get("avg") is not None]
    return float(np.mean(vals)) if vals else None


def metric_min(points: Sequence[Mapping[str, Any]]) -> float | None:
    vals = [p["avg"] for p in points if p.get("avg") is not None]
    return float(min(vals)) if vals else None


def discard_reasons(rounds: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """회차 이름 → 버릴 이유. 이유가 없으면 빈 목록."""
    latencies = {
        r["round"]["label"]: metric_avg(r.get("rds", {}).get("ReadLatency", []))
        for r in rounds
    }
    known = [v for v in latencies.values() if v]
    typical = float(np.median(known)) if known else None
    out = {}
    for r in rounds:
        label = r["round"]["label"]
        reasons = []
        before, after = r.get("bench_before"), r.get("bench_after")
        if before and after and after / before > BENCH_LIMIT:
            reasons.append(f"CPU 계산이 회차 뒤 {after / before:.2f}배 느림")
        lat = latencies.get(label)
        if typical and lat and lat / typical > READ_LATENCY_LIMIT:
            reasons.append(f"읽기 지연이 다른 회차의 {lat / typical:.2f}배")
        out[label] = reasons
    return out


def unpaired(rounds: Sequence[Mapping[str, Any]]) -> list[tuple[str, str, float]]:
    """디스크 버스트 잔고 최저값이 BALANCE_GAP 넘게 다른 회차 쌍. 이 쌍끼리는 짝짓지 않는다."""
    mins = {
        r["round"]["label"]: metric_min(r.get("rds", {}).get("EBSIOBalance%", []))
        for r in rounds
    }
    labels = [k for k, v in mins.items() if v is not None]
    out = []
    for i, x in enumerate(labels):
        for y in labels[i + 1 :]:
            gap = abs(mins[x] - mins[y])
            if gap > BALANCE_GAP:
                out.append((x, y, gap))
    return out


def read_plan_dir(path: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    rounds = []
    summary_path = path / "rounds.jsonl"
    if summary_path.exists():
        rounds = [
            json.loads(line)
            for line in summary_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    records = {}
    for file in sorted(path.glob("*.jsonl")):
        if file.name != "rounds.jsonl":
            records[file.stem] = [
                json.loads(line)
                for line in file.read_text(encoding="utf-8").splitlines()
                if line
            ]
    return rounds, records


def _ms(x: float) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.0f}"


def report(path: Path, baseline: str | None) -> str:
    rounds, records = read_plan_dir(path)
    reasons = discard_reasons(rounds) if rounds else {}
    lines = [f"# {path.name} 회차 정리", ""]

    lines += [
        "## 회차",
        "",
        "| 회차 | 상태 | 건수 | 중앙값 ms | p90 | p95 | 실패 | 키워드만 | 앱이면 벡터 끊김 | 버릴 이유 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, recs in records.items():
        for state in ("first", "repeat"):
            vals = query_values(recs, state)
            if not vals:
                continue
            s = summary(vals)
            errors = sum(
                1
                for x in recs
                if x.get("type") == "search"
                and x.get("state") == state
                and x.get("error")
            )
            why = "; ".join(reasons.get(label, [])) or ""
            degraded, over = cut_counts(recs, state)
            lines.append(
                f"| {label} | {state} | {s['n']} | {_ms(s['median'])} | {_ms(s['p90'])} | {_ms(s['p95'])} | {errors} "
                f"| {degraded} | {over} | {why} |"
            )
    lines.append("")

    pairs = unpaired(rounds) if rounds else []
    if pairs:
        lines += ["짝짓지 않을 회차(디스크 버스트 잔고 최저값 차이):", ""]
        lines += [f"- {x} · {y}: {gap:.0f}%p" for x, y, gap in pairs]
        lines.append("")

    kept = {label: recs for label, recs in records.items() if not reasons.get(label)}
    by_config: dict[str, list[str]] = {}
    for label in kept:
        by_config.setdefault(config_of(label), []).append(label)

    if baseline:
        if baseline not in by_config:
            lines += [f"기준선 '{baseline}' 의 쓸 수 있는 회차가 없다.", ""]
        else:
            lines += [
                f"## 기준선 {baseline}과 견주기",
                "",
                "| 설정 | 상태 | 짝 | log 비 중앙값 | 95% 구간 | 시간 비(중앙값) | Wilcoxon p | p95 비 | 판정 | 위 20권 일치 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for config, labels in sorted(by_config.items()):
                if config == baseline:
                    continue
                for state in ("first", "repeat"):
                    new = combine([query_values(kept[x], state) for x in labels])
                    base = combine(
                        [query_values(kept[x], state) for x in by_config[baseline]]
                    )
                    c = compare(new, base)
                    agree = (
                        overlap(kept[labels[0]], kept[by_config[baseline][0]])
                        if state == "first"
                        else math.nan
                    )
                    note = f" ({'; '.join(c.notes)})" if c.notes else ""
                    lines.append(
                        f"| {config} | {state} | {c.n} | {c.log_median:+.3f} | [{c.low:+.3f}, {c.high:+.3f}] | "
                        f"{math.exp(c.log_median):.2f}배 | {c.wilcoxon_p:.3g} | {c.p95_ratio:.2f}배 [{c.p95_low:.2f}, {c.p95_high:.2f}] | {c.verdict}{note} | "
                        f"{'' if math.isnan(agree) else f'{agree:.0%}'} |"
                    )
            lines.append("")

    fits = []
    for label, recs in kept.items():
        fit = fit_blocks(block_points(recs))
        used, known = generic_plans(recs)
        if fit or known:
            fits.append((label, fit, used, known))
    if fits:
        lines += [
            "## 읽은 블록과 시간",
            "",
            "| 회차 | 블록당 ms | 고정 ms | R² | 일반 계획 문장 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for label, fit, used, known in fits:
            if fit:
                lines.append(
                    f"| {label} | {fit['b_ms_per_block']:.3f} | {fit['a_ms']:.0f} | {fit['r2']:.2f} | {used}/{known} |"
                )
            else:
                lines.append(f"| {label} | — | — | — | {used}/{known} |")
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="검색 시간 회차 기록을 판정 기준대로 정리한다"
    )
    parser.add_argument("path", help="rounds.py 가 쓴 계획 폴더(out/<plan>)")
    parser.add_argument("--baseline", help="견줄 기준 설정 이름(예: ef400)")
    args = parser.parse_args(argv)
    path = Path(args.path)
    if not path.is_absolute() and not path.exists():
        path = Path(__file__).parent / path
    if not path.is_dir():
        print(f"{path} 가 없다", file=sys.stderr)
        return 2
    text = report(path, args.baseline)
    (path / "report.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
