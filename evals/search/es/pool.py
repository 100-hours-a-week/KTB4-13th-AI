"""Elasticsearch 판정(#754): 결과 파일의 서술형 첫 화면에서 라벨이 없는 책을 모아 채점 파일을 낸다.

    # 1. 결과 파일 여럿(이름=파일)의 서술형 위 12권에서 점수 없는 책을 모은다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.es.pool pool \\
        --labels evals/search/labels.json --out <작업 폴더> --run a=a-screen.json --run b=b-screen.json
    # 2. <작업 폴더>/grades.json 빈칸을 채운 뒤 라벨에 넣는다
    uv run python -m evals.search.es.pool apply --labels evals/search/labels.json --out <작업 폴더> \\
        --judge "Claude 세션 채점(모델), 날짜, ..."

label.py 의 pool 은 코드를 다시 돌려 관련도순 위 20권을 모은다. 판정은 인기순 첫 화면 12권으로 하므로, 여기서는
이미 잰 결과 파일의 그 12권을 그대로 모은다. 섞기·가리기·넣기는 label.py 의 것을 그대로 쓴다. sources.json 은
채점이 끝날 때까지 열지 않는다.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from evals.search import label, run

LOOK = run.SCREEN_TOP
SEARCH = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="결과 파일에서 라벨 없는 책을 모은다")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pool", "apply"):
        one = sub.add_parser(name)
        one.add_argument("--labels", required=True, type=Path)
        one.add_argument("--out", required=True, type=Path)
    sub.choices["pool"].add_argument("--run", action="append", required=True)
    sub.choices["apply"].add_argument("--judge", required=True)
    args = parser.parse_args(argv)
    data = label.load_labels(args.labels)

    if args.command == "pool":
        runs = dict(item.split("=", 1) for item in args.run)
        files = {
            name: json.loads(Path(path).read_text(encoding="utf-8"))
            for name, path in runs.items()
        }
        bundles = {saved["bundle"] for saved in files.values()}
        if len(bundles) != 1:
            print(f"묶음이 다른 파일이 섞였다: {sorted(bundles)}", file=sys.stderr)
            return 2
        bundle = run.load_bundle(SEARCH / bundles.pop())
        cases = [c for c in bundle["cases"] if c["kind"] == "descriptive"]
        found = {}
        for name, saved in files.items():
            rows = {row["id"]: row for row in saved["scores"]}
            found[name] = [
                {"book_ids": rows[c["id"]]["book_ids"][:LOOK]} for c in cases
            ]
        ids = sorted({i for r in found.values() for x in r for i in x["book_ids"]})
        books = asyncio.run(label._books(os.environ["DATABASE_URL"], "book_id", ids))
        text, grades, sources = label.gather(cases, found, books, data["labels"])
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "candidates.md").write_text(text, encoding="utf-8")
        (args.out / "grades.json").write_text(
            label.dump({"grades": grades}, "grades"), encoding="utf-8"
        )
        (args.out / "sources.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8"
        )
        count = sum(len(v) for v in grades.values())
        print(f"점수를 매길 책 {count}권: {args.out / 'candidates.md'}")
    else:
        grades = json.loads((args.out / "grades.json").read_text(encoding="utf-8"))
        sources = json.loads((args.out / "sources.json").read_text(encoding="utf-8"))
        added = label.apply(grades["grades"], sources, data, args.judge)
        args.labels.write_text(label.dump(data, "labels"), encoding="utf-8")
        print(f"라벨 {added}개를 더했다: {args.labels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
