"""시험대 검증: 실험 서버와 운영의 같은 검색어 기록을 #556 「시험대 검증」 넷으로 견준다(#597).

    uv run python -m evals.searchlab.testbench \\
        --exp out/testbench/base-1.jsonl --prod out/prod/first.jsonl \\
        --exp-ef1000 out/testbench/ef1000.jsonl --exp-ef400 out/testbench/ef400.jsonl \\
        --prod-ef1000 out/prod/ef1000.jsonl --prod-ef400 out/prod/ef400.jsonl

두 쪽 모두 `measure.py --server-stats` 로 잰 기록이어야 한다. 터널로 잰 운영 기록은 단계 시간에 왕복이 섞여 있어서
DB 쪽 값(`server`)으로만 견준다. DB·AWS 는 쓰지 않는다.

- 1: 단계별(키워드·벡터) 처음 조회의 DB 쪽 실행 시간, 중앙값과 p90 이 운영의 ±30% 안
- 2: 검색어마다 캐시 밖에서 읽은 블록 수(단계 합)의 중앙값과 p90 이 운영의 ±30% 안
- 3: 처음 조회 동안 벡터 색인(hnsw)의 캐시 적중률이 운영의 ±10%p 안
- 4: 반복 조회(첫 번 뺀 중앙값)에서 탐색 폭 1000 ÷ 400 벡터 시간 비의 검색어 중앙값이 운영의 ±0.1 안

비교는 두 쪽 모두 실패 없이 잰 검색어끼리 한다. 넷을 다 지켜야 통과다.
"""

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from evals.searchlab.analyze import overlap, percentile

STAGES = (("keyword", "키워드"), ("vector", "벡터"))
TIME_BAND = 0.30
BLOCK_BAND = 0.30
HIT_BAND = 0.10
RATIO_BAND = 0.10


def server_values(
    lines: Sequence[Mapping[str, Any]],
    state: str,
    stage: str | None,
    field_name: str = "exec_ms",
) -> dict[str, float]:
    """검색어마다 DB 쪽 값. stage 가 None 이면 모든 단계의 합. 반복 조회는 첫 번을 뺀 중앙값."""
    reps: dict[str, list[float]] = {}
    for line in lines:
        if (
            line.get("type") != "search"
            or line.get("state") != state
            or line.get("error")
        ):
            continue
        if state == "repeat" and line.get("rep", 1) == 1:
            continue
        server = line.get("server")
        if server is None:
            raise ValueError(
                f"{line['id']}: DB 쪽 값이 없다. measure.py --server-stats 로 잰 기록이어야 한다"
            )
        names = [stage] if stage else list(server)
        total = sum(
            float(entry.get(field_name) or 0)
            for name in names
            for entry in server.get(name, [])
            if entry
        )
        reps.setdefault(line["id"], []).append(total)
    return {qid: float(np.median(v)) for qid, v in reps.items()}


def section_hit_ratio(
    lines: Sequence[Mapping[str, Any]], state: str, am: str = "hnsw"
) -> float | None:
    """그 캐시 상태 구간 끝의 statio 에서 색인 캐시 적중률."""
    current = None
    for line in lines:
        if line.get("type") == "round":
            current = line.get("state")
        elif line.get("type") == "statio" and current == state:
            hit = sum(r["hit"] for r in line["relations"] if r["am"] == am)
            read = sum(r["read"] for r in line["relations"] if r["am"] == am)
            return round(hit / (hit + read), 4) if hit + read else None
    return None


def started(lines: Sequence[Mapping[str, Any]]) -> str | None:
    """DB 가 마지막으로 켜진 시각(첫 회차 머리)."""
    for line in lines:
        if line.get("type") == "round":
            return line.get("env", {}).get("started")
    return None


def within_ratio(exp: float, prod: float, band: float) -> tuple[bool, float]:
    """(운영의 ±band 안인가, 실험 ÷ 운영). 둘 다 0 이면 같다고 본다."""
    if prod == 0:
        return (exp == 0, 1.0 if exp == 0 else math.inf)
    ratio = round(exp / prod, 4)
    return (1 - band <= ratio <= 1 + band, ratio)


@dataclass
class Check:
    name: str
    exp: float | None
    prod: float | None
    value: float | None  # 비(1, 2) 또는 차(3, 4)
    band: str
    ok: bool


@dataclass
class Result:
    n: int
    checks: list[Check] = field(default_factory=list)
    overlap: float = math.nan
    identical: float = math.nan
    exp_started: str | None = None
    prod_started: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def _common(
    a: Mapping[str, float], b: Mapping[str, float]
) -> tuple[list[float], list[float]]:
    keys = sorted(set(a) & set(b))
    return [a[k] for k in keys], [b[k] for k in keys]


def _ratio_checks(
    name: str, exp: list[float], prod: list[float], band: float
) -> list[Check]:
    out = []
    for label, q in (("중앙값", 50), ("p90", 90)):
        e, p = percentile(exp, q), percentile(prod, q)
        ok, ratio = within_ratio(e, p, band)
        out.append(
            Check(
                f"{name} {label}", e, p, ratio, f"비 {1 - band:.1f}–{1 + band:.1f}", ok
            )
        )
    return out


def ef_ratio(
    pair: tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]],
) -> dict[str, float]:
    """검색어마다 반복 조회의 탐색 폭 1000 ÷ 400 벡터 DB 쪽 시간."""
    hi = server_values(pair[0], "repeat", "vector")
    lo = server_values(pair[1], "repeat", "vector")
    return {q: hi[q] / lo[q] for q in set(hi) & set(lo) if lo[q] > 0}


def judge(
    exp: Sequence[Mapping[str, Any]],
    prod: Sequence[Mapping[str, Any]],
    exp_repeat: tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]] | None,
    prod_repeat: tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]] | None,
) -> Result:
    checks: list[Check] = []
    n = 0
    for stage, name in STAGES:
        e, p = _common(
            server_values(exp, "first", stage), server_values(prod, "first", stage)
        )
        n = max(n, len(e))
        checks += _ratio_checks(f"{name} 처음 조회", e, p, TIME_BAND)
    e, p = _common(
        server_values(exp, "first", None, "read"),
        server_values(prod, "first", None, "read"),
    )
    checks += _ratio_checks("캐시 밖 블록", e, p, BLOCK_BAND)

    he, hp = section_hit_ratio(exp, "first"), section_hit_ratio(prod, "first")
    diff = None if he is None or hp is None else round(he - hp, 4)
    checks.append(
        Check(
            "벡터 색인 캐시 적중률",
            he,
            hp,
            diff,
            f"차 ±{HIT_BAND:.2f}",
            diff is not None and abs(diff) <= HIT_BAND,
        )
    )

    if exp_repeat and prod_repeat:
        re_, rp = _common(ef_ratio(exp_repeat), ef_ratio(prod_repeat))
        me = float(np.median(re_)) if re_ else math.nan
        mp = float(np.median(rp)) if rp else math.nan
        diff = round(me - mp, 4) if re_ else None
        checks.append(
            Check(
                "탐색 폭 1000/400 시간 비",
                me,
                mp,
                diff,
                f"차 ±{RATIO_BAND:.1f}",
                diff is not None and abs(diff) <= RATIO_BAND + 1e-9,
            )
        )

    def ids(lines):
        return {
            x["id"]: x["ids"]
            for x in lines
            if x.get("type") == "search"
            and x.get("state") == "first"
            and not x.get("error")
        }

    a, b = ids(exp), ids(prod)
    same = [a[q] == b[q] for q in set(a) & set(b)]
    return Result(
        n=n,
        checks=checks,
        overlap=overlap(exp, prod),
        identical=float(np.mean(same)) if same else math.nan,
        exp_started=started(exp),
        prod_started=started(prod),
    )


def _fmt(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:,.3f}" if abs(x) < 10 else f"{x:,.1f}"


def report(result: Result) -> str:
    rows = [
        f"# 시험대 검증 — {'통과' if result.ok else '통과 못 함'}",
        "",
        f"- 견준 검색어 {result.n}건. 실험 DB 켜진 시각 {result.exp_started}, 운영 DB 켜진 시각 {result.prod_started}",
        f"- 처음 조회 위 20권: 겹친 비율 평균 {_fmt(result.overlap)}, 똑같은 검색어 비율 {_fmt(result.identical)}",
        "",
        "| 항목 | 실험 서버 | 운영 | 비·차 | 범위 | 통과 |",
        "| --- | ---: | ---: | ---: | --- | --- |",
    ]
    for c in result.checks:
        rows.append(
            f"| {c.name} | {_fmt(c.exp)} | {_fmt(c.prod)} | {_fmt(c.value)} | {c.band} | {'예' if c.ok else '아니오'} |"
        )
    if len(result.checks) < 8:
        rows += [
            "",
            "- 탐색 폭 1000/400 반복 조회 기록이 없어 넷째 기준은 보지 않았다(통과 아님).",
        ]
    return "\n".join(rows) + "\n"


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="실험 서버와 운영의 시험대 기록을 견준다"
    )
    parser.add_argument(
        "--exp",
        type=Path,
        required=True,
        help="실험 서버 시험대 회차 기록(처음 조회가 든 것)",
    )
    parser.add_argument("--prod", type=Path, required=True, help="운영 처음 조회 기록")
    for side in ("exp", "prod"):
        for ef in ("1000", "400"):
            parser.add_argument(
                f"--{side}-ef{ef}", type=Path, help=f"{side} 반복 조회, 탐색 폭 {ef}"
            )
    parser.add_argument("--out", type=Path, help="표를 쓸 파일")
    args = parser.parse_args(argv)

    def pair(side: str):
        hi, lo = getattr(args, f"{side}_ef1000"), getattr(args, f"{side}_ef400")
        return (_read(hi), _read(lo)) if hi and lo else None

    result = judge(_read(args.exp), _read(args.prod), pair("exp"), pair("prod"))
    text = report(result)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
