"""서술형 라벨을 사람이 점수를 가린 채 다시 매기고 세션 점수와의 일치도를 낸다(#751).

    # 1. 고른 책으로 시트를 만든다. 고르는 규칙은 표본마다 달라 이 모듈 밖에서 정하고, 고른 목록만 넘긴다
    #    picks.json: [{"case": "descriptive-01", "isbn": "978…", "pool": "처음"}, …]
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.regrade sheet \\
        --picks picks.json --prefix b --sheet B.md --key B-key.json
    # 2. 사람이 옵시디언에서 책마다 체크박스 하나를 고른다. 열쇠 파일은 채점이 끝날 때까지 열지 않는다
    # 3. 일치도. 시트와 열쇠를 짝으로 준다. 열쇠에 묶음이 없는 옛 시트는 셋째 값으로 묶음을 준다
    uv run python -m evals.search.regrade score \\
        --sheet A.md A-key.json 나중 --sheet B.md B-key.json --sheet C.md C-key.json

묶음은 라벨이 생긴 때다. 처음(labels.json 첫 채점 838권), 나중(그 뒤 더한 579권), 확인용(confirm-labels.json
507권). 같은 책을 같은 사람이 며칠 뒤 다시 매긴 시트는 묶음을 "다시"로 두고, 세션이 아니라 처음 매긴 사람 점수와
견준다. 문턱은 묶음별 가중 κ 를 묶음 크기로 가중해 합친 값에 건다. 0.6 은 #556(11/6)에 정한 값이다.
가중 κ 는 선형 가중이다(점수 1 차이 0.5, 2 차이 1).
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.search import label, run

KAPPA_MIN = 0.6
GRADES = (2, 1, 0)
NAMES = {2: "맞음", 1: "조금", 0: "아님"}
FIRST, LATER, CONFIRM, REPEAT = "처음", "나중", "확인용", "다시"
CONFIRM_CASES_PATH = run.CASES_PATH.with_name("confirm.json")
CONFIRM_LABELS_PATH = label.LABELS_PATH.with_name("confirm-labels.json")

Pair = tuple[str, str]  # (사례, ISBN)


def load_labels() -> dict[str, dict[str, int]]:
    """평가 묶음과 확인용 묶음의 라벨을 합친 것. 사례 번호가 겹치지 않는다."""
    merged = dict(label.load_labels()["labels"])
    merged.update(label.load_labels(CONFIRM_LABELS_PATH)["labels"])
    return merged


def pool_sizes(labels: Mapping[str, Any], confirm: Mapping[str, Any]) -> dict[str, int]:
    """묶음별 라벨 수. 처음은 labels.json 첫 채점, 나중은 그 뒤 채점을 모두 더한 것이다."""
    judges = [judge["labels"] for judge in labels["judges"]]
    return {
        FIRST: judges[0] if judges else 0,
        LATER: sum(judges[1:]),
        CONFIRM: sum(judge["labels"] for judge in confirm["judges"]),
    }


def queries() -> dict[str, str]:
    """사례 번호 → 검색어. 평가 묶음과 확인용 묶음을 합친다."""
    confirm = json.loads(CONFIRM_CASES_PATH.read_text(encoding="utf-8"))
    cases = [*run.load_bundle()["cases"], *confirm["cases"]]
    return {case["id"]: case["query"] for case in cases}


def sheet(
    picked: Sequence[Mapping[str, str]],
    queries: Mapping[str, str],
    books: Mapping[str, Mapping[str, Any]],
    prefix: str = "r",
) -> tuple[str, dict[str, dict[str, str]]]:
    """(체크박스 시트, 번호 → 사례·ISBN·묶음). 시트에는 점수도 묶음도 없다."""
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
    for n, item in enumerate(picked, 1):
        number = f"{prefix}{n:03d}"
        book = books[item["isbn"]]
        key[number] = {"case": item["case"], "isbn": item["isbn"], "pool": item["pool"]}
        lines += [
            f"## {number} · {queries[item['case']]}",
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
    if len(a) != len(b) or not a:
        raise ValueError("두 점수의 수가 같고 하나 이상이어야 한다")
    n = len(a)
    seen = Counter(zip(a, b, strict=True))
    rows = Counter(a)
    cols = Counter(b)
    observed = expected = 0.0
    for i in range(levels):
        for j in range(levels):
            weight = abs(i - j) / (levels - 1)
            observed += weight * seen[(i, j)] / n
            expected += weight * rows[i] * cols[j] / n / n
    return 1.0 if expected == 0 else 1 - observed / expected


def agreement(pairs: Sequence[tuple[int, int]]) -> dict[str, Any]:
    """(앞 점수, 뒤 점수) 짝의 일치도."""
    first = [x for x, _ in pairs]
    second = [y for _, y in pairs]
    return {
        "매긴 수": len(pairs),
        "같은 비율": sum(x == y for x, y in pairs) / len(pairs),
        "가중 κ": weighted_kappa(first, second),
        "2점과 0점으로 갈린 수": sum(abs(x - y) == 2 for x, y in pairs),
        "표": Counter(pairs),
    }


def score(
    sheets: Iterable[tuple[Mapping[str, int], Mapping[str, Mapping[str, str]]]],
    labels: Mapping[str, Mapping[str, int]],
    sizes: Mapping[str, int],
) -> dict[str, Any]:
    """시트(체크한 점수, 열쇠)를 모아 묶음별로 세션 점수와 견주고, 묶음 크기로 가중해 합친다.

    "다시" 묶음은 세션이 아니라 같은 책에 처음 매긴 사람 점수와 견주고, 합친 값에 넣지 않는다.
    """
    by_pool: dict[str, list[tuple[int, int]]] = {}
    person: dict[Pair, int] = {}
    again: dict[Pair, int] = {}
    for marks, key in sheets:
        for number, grade in marks.items():
            item = key[number]
            pair = (item["case"], item["isbn"])
            if item["pool"] == REPEAT:
                again[pair] = grade
                continue
            person[pair] = grade
            by_pool.setdefault(item["pool"], []).append(
                (labels[pair[0]][pair[1]], grade)
            )
    pools = {name: agreement(pairs) for name, pairs in sorted(by_pool.items())}
    weights = {name: sizes[name] for name in pools}
    total = sum(weights.values())
    combined = (
        sum(weights[name] * pools[name]["가중 κ"] for name in pools) / total
        if total
        else None
    )
    repeated = [(person[p], g) for p, g in sorted(again.items()) if p in person]
    return {
        "묶음": pools,
        "합친 가중 κ": combined,
        "서술형 판정": None
        if combined is None
        else ("진행" if combined >= KAPPA_MIN else "보류"),
        "다시": agreement(repeated) if repeated else None,
    }


def _key(path: Path, pool: str | None) -> dict[str, dict[str, str]]:
    key = json.loads(path.read_text(encoding="utf-8"))
    for item in key.values():
        if pool is not None:
            item["pool"] = pool
        if not item.get("pool"):
            raise ValueError(
                f"{path}: 열쇠에 묶음이 없다. --sheet 셋째 값으로 묶음을 준다"
            )
        if item["pool"] not in (FIRST, LATER, CONFIRM, REPEAT):
            raise ValueError(f"{path}: 모르는 묶음 {item['pool']}")
    return key


def _line(name: str, result: Mapping[str, Any]) -> str:
    return (
        f"{name}: {result['매긴 수']}권, 같은 비율 {result['같은 비율']:.1%}, "
        f"가중 κ {result['가중 κ']:.3f}, 2점과 0점으로 갈림 {result['2점과 0점으로 갈린 수']}권"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="서술형 라벨 사람 재채점")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("sheet", help="고른 책으로 시트와 열쇠를 만든다")
    make.add_argument("--picks", required=True, type=Path)
    make.add_argument("--prefix", default="r")
    make.add_argument("--sheet", required=True, type=Path)
    make.add_argument("--key", required=True, type=Path)
    make.add_argument("--any-db", action="store_true")
    check = sub.add_parser("score", help="체크한 점수와 세션 점수의 일치도")
    check.add_argument(
        "--sheet",
        action="append",
        nargs="+",
        required=True,
        metavar="시트 열쇠 [묶음]",
    )
    args = parser.parse_args(argv)

    if args.command == "sheet":
        if (url := label._database(args)) is None:
            return 2
        picked = json.loads(args.picks.read_text(encoding="utf-8"))
        isbns = sorted({item["isbn"] for item in picked})
        books = asyncio.run(label._books(url, "isbn13", isbns))
        missing = sorted(set(isbns) - set(books))
        if missing:
            print(f"책 표에 없는 ISBN {len(missing)}개: {missing[:5]}", file=sys.stderr)
            return 2
        text, key = sheet(picked, queries(), books, args.prefix)
        args.sheet.write_text(text, encoding="utf-8")
        args.key.write_text(json.dumps(key, ensure_ascii=False), encoding="utf-8")
        print(f"{len(picked)}권: {args.sheet}")
        return 0

    sheets = []
    for values in args.sheet:
        if len(values) not in (2, 3):
            parser.error("--sheet 는 시트, 열쇠, [묶음] 이다")
        pool = values[2] if len(values) == 3 else None
        marks = read_marks(Path(values[0]).read_text(encoding="utf-8"))
        sheets.append((marks, _key(Path(values[1]), pool)))
    if not any(marks for marks, _ in sheets):
        print("체크한 책이 없다", file=sys.stderr)
        return 2
    sizes = pool_sizes(label.load_labels(), label.load_labels(CONFIRM_LABELS_PATH))
    result = score(sheets, load_labels(), sizes)
    for name, pool in result["묶음"].items():
        print(_line(f"{name}(라벨 {sizes[name]}권)", pool))
    if result["합친 가중 κ"] is not None:
        print(
            f"합친 가중 κ {result['합친 가중 κ']:.3f}(문턱 {KAPPA_MIN}), "
            f"서술형 판정 {result['서술형 판정']}"
        )
    if result["다시"]:
        print(_line("같은 사람 다시", result["다시"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
