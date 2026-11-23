"""#751 검색 B 시트에 넣을 책을 고른다. 한 번 쓰는 코드라 이 브랜치에만 둔다.

처음 838권(main의 10/19 라벨, dabb63c)에서 50권, 확인용 507권에서 50권. 묶음 안에서는 검색어 종류(facet)의
라벨 수 비율대로 권수를 나누고(큰 나머지 순), 종류 안에서는 무작위로 고른다. 세션 점수로 나누지 않는다.
두 묶음을 섞어 순서로 묶음을 알 수 없게 한다.

    uv run python -m evals.search.pick_751 --first first-838.json --out picks.json
"""

import argparse
import json
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

from evals.search import regrade, run

SEED = "751"
EACH = 50


def allot(sizes: Mapping[str, int], total: int) -> dict[str, int]:
    """종류별 라벨 수 비율대로 total 을 나눈다. 나머지는 소수점이 큰 종류부터 하나씩."""
    whole = sum(sizes.values())
    exact = {name: total * size / whole for name, size in sizes.items()}
    share = {name: int(value) for name, value in exact.items()}
    left = total - sum(share.values())
    for name in sorted(exact, key=lambda n: (-(exact[n] - share[n]), n))[:left]:
        share[name] += 1
    return share


def draw(
    labels: Mapping[str, Mapping[str, int]],
    facet: Mapping[str, str],
    pool: str,
    total: int,
    rng: random.Random,
) -> list[dict[str, str]]:
    by_facet: dict[str, list[tuple[str, str]]] = {}
    for case in sorted(labels):
        for isbn in sorted(labels[case]):
            by_facet.setdefault(facet[case], []).append((case, isbn))
    share = allot({name: len(items) for name, items in by_facet.items()}, total)
    picked = []
    for name in sorted(by_facet):
        picked += rng.sample(by_facet[name], share[name])
    return [{"case": c, "isbn": i, "pool": pool} for c, i in picked]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--first", required=True, type=Path, help="dabb63c 의 labels.json"
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    confirm = json.loads(regrade.CONFIRM_CASES_PATH.read_text(encoding="utf-8"))
    facet = {
        c["id"]: c["facet"]
        for c in [*run.load_bundle()["cases"], *confirm["cases"]]
        if c["kind"] == "descriptive"
    }
    first = json.loads(args.first.read_text(encoding="utf-8"))["labels"]
    if sum(len(v) for v in first.values()) != 838:
        raise SystemExit("처음 묶음은 838권이어야 한다")
    confirm_labels = json.loads(
        regrade.CONFIRM_LABELS_PATH.read_text(encoding="utf-8")
    )["labels"]

    rng = random.Random(SEED)
    picked = draw(first, facet, regrade.FIRST, EACH, rng)
    picked += draw(confirm_labels, facet, regrade.CONFIRM, EACH, rng)
    rng.shuffle(picked)
    args.out.write_text(
        json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"{len(picked)}권: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
