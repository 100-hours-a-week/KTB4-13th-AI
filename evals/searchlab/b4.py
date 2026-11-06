"""#556 B4 격자를 돌린다. 걸러내는 선 × 후보 합계 × 비중이고, 선을 0.6 밑으로 내린 칸은 문턱 = 선 갈래도 잰다.

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval CURSOR_SIGNING_KEY=local-smoke-only \\
        uv run python -m evals.searchlab.b4 --out evals/search/results/b4 --jobs 5

칸마다 관련도순(<칸>-rel.json, 단계 기록 포함)과 첫 화면(<칸>-screen.json)을 남긴다. 이미 있는 파일은 건너뛰므로 멈춘
곳부터 이어 돌린다. 서로 다른 칸은 서로 영향을 주지 않아 나란히 돌린다(시간은 재지 않는다).
"""

import argparse
import itertools
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

COVERAGES = (0.5, 0.55, 0.6, 0.65, 0.7)
BUDGETS = (500, 1000, 1500)
WEIGHTS = ((3, 1), (1, 1), (1, 3))
# 글자 조각 색인의 기본 문턱. 선이 이 밑이면 문턱 = 선 갈래도 잰다
THRESHOLD = 0.6


@dataclass(frozen=True)
class Cell:
    coverage: float
    budget: int
    keyword: int
    vector: int
    lowered: bool = False  # 문턱을 선과 같게 내렸는가

    @property
    def name(self) -> str:
        mark = "t" if self.lowered else ""
        return (
            f"m{round(self.coverage * 100):03d}{mark}-b{self.budget}"
            f"-w{self.keyword}{self.vector}"
        )

    def args(self) -> list[str]:
        sets = {
            "min_coverage": self.coverage,
            "lookup_budget": self.budget,
            "keyword_weight": self.keyword,
            "vector_weight": self.vector,
        }
        out = [item for k, v in sets.items() for item in ("--set", f"{k}={v}")]
        return out + (["--trgm-threshold", str(self.coverage)] if self.lowered else [])


def cells() -> list[Cell]:
    base = [
        Cell(c, b, k, v)
        for c, b, (k, v) in itertools.product(COVERAGES, BUDGETS, WEIGHTS)
    ]
    lowered = [
        Cell(c.coverage, c.budget, c.keyword, c.vector, lowered=True)
        for c in base
        if c.coverage < THRESHOLD
    ]
    return base + lowered


def jobs(out: Path, picked: Sequence[Cell]) -> list[tuple[Path, list[str]]]:
    """(결과 파일, 실행기 인자). 이미 있는 파일은 뺀다."""
    todo = []
    for cell in picked:
        for suffix, view in (("rel", ["--trace"]), ("screen", ["--screen"])):
            path = out / f"{cell.name}-{suffix}.json"
            if not path.exists():
                todo.append((path, ["--yes", *cell.args(), *view, "--out", str(path)]))
    return todo


def one(job: tuple[Path, list[str]]) -> tuple[Path, int]:
    path, args = job
    with path.with_suffix(".txt").open("w", encoding="utf-8") as log:
        done = subprocess.run(
            [sys.executable, "-m", "evals.search.run", *args],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return path, done.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#556 B4 격자를 돌린다")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--only", help="칸 이름에 이 글자가 든 칸만(쉼표로 여럿)")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    wanted = [w for w in (args.only or "").split(",") if w]
    picked = [c for c in cells() if not wanted or any(w in c.name for w in wanted)]
    todo = jobs(args.out, picked)
    print(f"칸 {len(picked)}개, 돌릴 파일 {len(todo)}개", flush=True)
    failed = 0
    with ThreadPoolExecutor(args.jobs) as pool:
        for done, (path, code) in enumerate(pool.map(one, todo), start=1):
            failed += code != 0
            print(f"{done}/{len(todo)} {path.name} 끝남({code})", flush=True)
    print(f"끝. 실패 {failed}개", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
