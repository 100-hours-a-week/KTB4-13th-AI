"""새 라벨을 사람이 점수를 가린 채 다시 매기고 일치도를 낸다(#556 「품질 지표와 라벨」).

    # 1. 출처 설정별로 나눠 100권을 고른다. 채점이 다 끝난 뒤에 돌린다(sources 파일을 읽는다)
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.searchlab.regrade pick \\
        --sources results/label/sources-r1.json results/label/sources-r2.json --sheet sheet.md --key key.json
    # 2. 사람이 옵시디언에서 책마다 체크박스 하나를 고른다
    # 3. 일치도. 가중 κ 가 0.6 미만이면 서술형 판정을 보류한다
    uv run python -m evals.searchlab.regrade score --sheet sheet.md --key key.json

출처 설정은 책이 나온 칸의 비중(3:1·1:1·1:3 묶음)으로 나눈다. 한 묶음이 모자라지 않게 묶음마다 최소 권수를 먼저 주고,
나머지는 묶음 크기에 비례해 나눈다. 시트에는 점수도 출처도 적지 않는다. 가중 κ 는 선형 가중이다(점수 1 차이 0.5, 2 차이 1).
"""

import argparse
import asyncio
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from evals.search import label, run

SEED = "556"
SHEET_SIZE = 100
MIN_PER_GROUP = 10
KAPPA_MIN = 0.6
GRADES = (2, 1, 0)
NAMES = {2: "맞음", 1: "조금", 0: "아님"}
_WEIGHT = re.compile(r"-w(\d\d)-")


def weight_of(source: str) -> str:
    """출처 파일 이름의 비중. 기준선 파일은 3:1 이다."""
    found = _WEIGHT.search(source)
    return found.group(1) if found else "31"


def groups(
    sources: Sequence[Mapping[str, Mapping[str, Mapping[str, Any]]]],
) -> dict[tuple[str, str], str]:
    """(사례, ISBN) → 출처 묶음. 그 책이 나온 칸의 비중을 모은 이름이다."""
    seen: dict[tuple[str, str], set[str]] = {}
    for source in sources:
        for case, marks in source.items():
            for mark in marks.values():
                weights = {weight_of(name) for name in mark["ranks"]}
                seen.setdefault((case, mark["isbn"]), set()).update(weights)
    return {key: "+".join(sorted(v)) for key, v in seen.items()}


def pick(
    grouped: Mapping[tuple[str, str], str],
    size: int = SHEET_SIZE,
    least: int = MIN_PER_GROUP,
) -> list[tuple[str, str]]:
    """묶음마다 최소 권수를 주고 나머지를 크기에 비례해 나눠 고른다. 모자라면 있는 만큼이다."""
    rng = random.Random(SEED)
    members: dict[str, list[tuple[str, str]]] = {}
    for key in sorted(grouped):
        members.setdefault(grouped[key], []).append(key)
    picked: list[tuple[str, str]] = []
    for name in sorted(members):
        rng.shuffle(members[name])
        take = min(least, len(members[name]))
        picked += members[name][:take]
        members[name] = members[name][take:]
    left = sorted(key for rest in members.values() for key in rest)
    rng.shuffle(left)
    picked += left[: max(0, size - len(picked))]
    rng.shuffle(picked)
    return picked


def sheet(
    picked: Sequence[tuple[str, str]],
    queries: Mapping[str, str],
    books: Mapping[str, Mapping[str, Any]],
) -> tuple[str, dict[str, dict[str, str]]]:
    """(체크박스 시트, 번호 → 사례·ISBN). 시트에는 점수도 출처도 없다."""
    lines = [
        "# 서술형 라벨 사람 재채점",
        "",
        "제목·저자·분류·소개글 앞 300자만 보고, 이 검색어를 친 사람이 찾던 책이라고 할지를 고릅니다.",
        "",
        "| 점수 | 뜻 |",
        "| --- | --- |",
        "| 2 맞음 | 검색어가 바라는 것(주제·분위기·대상·상황)을 다룬다. 여러 주제 중 하나여도 소개글에 분명하면 2점. 조건이 여럿이면 모두 맞음 |",
        "| 1 조금 | 일부만 맞음(가까운 주제, 대상·형식이 다름) |",
        "| 0 아님 | 상관없거나 낱말만 겹침 |",
        "",
        "- 검색어에 대상·형식이 있으면 다른 책은 1점까지(주제·분위기가 맞을 때만)",
        "- 수험서·문제집·연구 보고서는 그것을 찾는 게 아니면 1점까지",
        "- 조건이 여럿이면 핵심 조건(무엇을 다루는 책인지)이 맞아야 1점 이상",
        "- 마음을 편하게 하려고 만든 활동책(컬러링북)은 분위기 검색에서 2점",
        "- 소개글이 없으면 제목·분류로. 제목이 분명하면 2점, 모호하면 0점",
        "",
    ]
    key = {}
    for n, (case, isbn) in enumerate(picked, 1):
        number = f"r{n:03d}"
        book = books[isbn]
        key[number] = {"case": case, "isbn": isbn}
        lines += [
            f"## {number} · {queries[case]}",
            "",
            f"**{_clean(book['title'])}** · {_clean(book['author'])} · {_clean(book['category'])}",
            "",
            _clean(book["intro"]) or "(소개글 없음)",
            "",
            *[f"- [ ] {grade} {NAMES[grade]}" for grade in GRADES],
            "",
        ]
    return "\n".join(lines), key


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def read_marks(text: str) -> dict[str, int]:
    """시트에서 체크한 점수. 하나도 안 고른 번호는 빠지고, 둘 이상 고르면 ValueError 다."""
    marks: dict[str, int] = {}
    for part in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        number = part.split(" ", 1)[0]
        checked = re.findall(r"^- \[[xX]\] ([012]) ", part, flags=re.MULTILINE)
        if len(checked) > 1:
            raise ValueError(f"{number}: 점수를 둘 이상 골랐다")
        if checked:
            marks[number] = int(checked[0])
    return marks


def weighted_kappa(a: Sequence[int], b: Sequence[int], levels: int = 3) -> float:
    """선형 가중 κ. 1 이면 모두 같고, 0 이면 우연히 맞는 정도다."""
    seen = np.zeros((levels, levels))
    for x, y in zip(a, b, strict=True):
        seen[x, y] += 1
    seen /= seen.sum()
    expected = np.outer(seen.sum(axis=1), seen.sum(axis=0))
    i, j = np.indices((levels, levels))
    weight = np.abs(i - j) / (levels - 1)
    denominator = (weight * expected).sum()
    return 1.0 if denominator == 0 else 1 - (weight * seen).sum() / denominator


def score(
    marks: Mapping[str, int],
    key: Mapping[str, Mapping[str, str]],
    labels: Mapping[str, Mapping[str, int]],
) -> dict[str, Any]:
    pairs = [
        (labels[key[n]["case"]][key[n]["isbn"]], grade) for n, grade in marks.items()
    ]
    session = [a for a, _ in pairs]
    person = [b for _, b in pairs]
    kappa = weighted_kappa(session, person)
    return {
        "매긴 수": len(pairs),
        "같은 비율": sum(a == b for a, b in pairs) / len(pairs),
        "가중 κ": kappa,
        "표(세션, 사람)": Counter(pairs),
        "서술형 판정": "진행" if kappa >= KAPPA_MIN else "보류",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="새 라벨 사람 재채점")
    sub = parser.add_subparsers(dest="command", required=True)
    first = sub.add_parser("pick")
    first.add_argument("--sources", nargs="+", required=True, type=Path)
    first.add_argument("--sheet", required=True, type=Path)
    first.add_argument("--key", required=True, type=Path)
    second = sub.add_parser("score")
    second.add_argument("--sheet", required=True, type=Path)
    second.add_argument("--key", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "pick":
        from app.core.config import get_settings

        url = get_settings().database_url
        sources = [json.loads(p.read_text(encoding="utf-8")) for p in args.sources]
        picked = pick(groups(sources))
        queries = {c["id"]: c["query"] for c in run.load_bundle()["cases"]}
        isbns = sorted({isbn for _, isbn in picked})
        books = asyncio.run(label._books(url, "isbn13", isbns))
        text, key = sheet(picked, queries, books)
        args.sheet.write_text(text, encoding="utf-8")
        args.key.write_text(json.dumps(key, ensure_ascii=False), encoding="utf-8")
        print(f"{len(picked)}권: {args.sheet}")
        return 0
    marks = read_marks(args.sheet.read_text(encoding="utf-8"))
    key = json.loads(args.key.read_text(encoding="utf-8"))
    if not marks:
        print("체크한 책이 없다", file=sys.stderr)
        return 2
    result = score(marks, key, label.load_labels()["labels"])
    print(
        f"매긴 수 {result['매긴 수']}/{len(key)}, 같은 비율 {result['같은 비율']:.1%}"
    )
    print(f"가중 κ {result['가중 κ']:.3f}, 서술형 판정 {result['서술형 판정']}")
    print("(세션, 사람) 점수별 권수:", dict(result["표(세션, 사람)"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
