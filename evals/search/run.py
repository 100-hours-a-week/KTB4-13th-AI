"""① 검색 평가 묶음을 돌려 채점하는 실행기(#495).

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.run            # 몇 건을 돌릴지만 본다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.run --yes      # 실제로 돌린다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.run --yes --code ../v1

검색은 따로 띄운 파이썬에서 부르고(collect.py), 채점은 이 저장소의 코드로 한다. `--code` 로 다른 커밋의
worktree 를 주면 검색만 그 코드로 바뀐다. V1 운영 코드처럼 채점에 쓰는 모듈(editions 등)이 없는 커밋도 같은
묶음, 같은 규칙으로 잰다.

검색은 DB 에 쓰지 않는다. 그래도 묶음의 책을 고른 평가 DB 가 아닌 곳에서 잰 숫자와 섞이지 않게 이름이
`_eval` 로 끝나는 DB 에만 돌린다.
"""

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core import products
from evals.search.collect import collect_with
from evals.search.scoring import HIT_AT, Book, Response, Score, score, summarize

CASES_PATH = Path(__file__).with_name("cases.json")
# 검색할 코드의 기본값. 이 저장소다
REPO = Path(__file__).resolve().parents[2]
EVAL_DB_SUFFIX = "_eval"

Search = Callable[[list[dict]], list[dict]]
Facts = Callable[[set[int]], dict[int, Book]]

# 받은 책의 정보. 가격은 검색의 가격 필터와 같은 식으로 상품 표에서 읽는다.
_FACTS_SQL = f"""
SELECT b.book_id, b.isbn13 AS isbn, b.title, b.author, b.category, b.pub_year,
       {products.price_sql("b")} AS price
FROM v_books b
WHERE b.book_id = ANY($1::int[])
"""


def load_bundle() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def select(
    cases: Sequence[dict],
    *,
    kinds: Sequence[str] | None = None,
    ids: Sequence[str] | None = None,
) -> list[dict]:
    """돌릴 사례를 고른다. 없는 이름을 주면 ValueError 다 — 오타로 0건을 돌리지 않게 한다."""
    for label, wanted, known in (
        ("종류", kinds, {c["kind"] for c in cases}),
        ("번호", ids, {c["id"] for c in cases}),
    ):
        unknown = sorted(set(wanted or ()) - known)
        if unknown:
            raise ValueError(f"묶음에 없는 {label}: {', '.join(unknown)}")
    return [
        case
        for case in cases
        if (not kinds or case["kind"] in kinds) and (not ids or case["id"] in ids)
    ]


def request(case: dict, top: int) -> dict:
    """사례의 /search 요청. 관련도순 위 top 권을 한 번에 받는다."""
    return {"query": case["query"], "filters": case.get("filters", {}), "size": top}


def database_name(url: str) -> str:
    """DB 주소에서 이름만. 주소에는 계정과 비밀번호가 들어 있을 수 있어 그대로 찍지 않는다."""
    return urlsplit(url).path.lstrip("/")


def refusal(name: str, *, allow_any: bool) -> str | None:
    """이 DB 에 돌리면 안 되는 이유. 돌려도 되면 None."""
    if allow_any or name.endswith(EVAL_DB_SUFFIX):
        return None
    return (
        f"DB '{name}' 는 평가용이 아니다. 묶음의 책은 평가 DB 에서 골랐으므로 이름이 "
        f"'{EVAL_DB_SUFFIX}' 로 끝나는 DB 에만 돌린다. 그래도 돌리려면 --any-db 를 붙인다."
    )


def code_label(code: Path) -> str:
    """잰 코드의 커밋. app 을 고친 채로 쟀으면(상수만 바꿔 본 실험) 그렇다고 붙인다."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(code), *args], capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        changed = git("status", "--porcelain", "--untracked-files=no", "--", "app")
    except (OSError, subprocess.CalledProcessError):
        return code.name
    return f"{commit} (app 을 고친 채)" if changed else commit


async def read_db(url: str, book_ids: set[int]) -> tuple[dict[int, Book], dict]:
    """받은 책의 정보와, 돌린 DB 의 책 표 크기."""
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch(_FACTS_SQL, sorted(book_ids))
        info = {
            "name": database_name(url),
            "books": await conn.fetchval("SELECT count(*) FROM v_books"),
            "with_vectors": await conn.fetchval("SELECT count(*) FROM book_embeddings"),
        }
    finally:
        await conn.close()
    shelf = {r["book_id"]: Book(**dict(r)) for r in rows}
    return shelf, info


def score_all(
    cases: Sequence[dict], results: Sequence[dict], shelf: dict[int, Book], top: int
) -> list[Score]:
    return [
        score(
            case,
            Response(**{**result, "book_ids": tuple(result["book_ids"])}),
            shelf,
            top,
        )
        for case, result in zip(cases, results, strict=True)
    ]


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


_NOTE = (
    "제목은 띄어쓰기·문장부호·영문 대소문자를 빼고 같으면 맞은 것으로 본다. "
    "시간은 이 PC 에서 잰 것이라 종류끼리 견주는 데만 쓴다."
)


def render(summary: dict[str, Any], top: int) -> str:
    """사람이 읽는 결과 표. PR 본문에 그대로 붙일 수 있게 마크다운으로 낸다."""
    kinds = summary["kinds"]
    titles = [(kind, row) for kind, row in kinds.items() if "mrr" in row]
    titles += [(f"↳ {change}", row) for change, row in summary["changes"].items()]
    others = [(kind, row) for kind, row in kinds.items() if "mrr" not in row]
    lines = [f"위 {top}권 기준. {_NOTE}"]
    if titles:
        lines += [
            "",
            "| 종류 | 사례 | 1등 안 | 5등 안 | 20등 안 | MRR | 못 찾음 | 시간(초, 중앙값) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    for name, row in titles:
        hits = " | ".join(_pct(row[f"hit_{k}"]) for k in HIT_AT)
        lines.append(
            f"| {name} | {row['measured']} | {hits} | {_num(row['mrr'], 3)} "
            f"| {row['missed']} | {_num(row['seconds_median'], 3)} |"
        )
    if others:
        lines += [
            "",
            "| 종류 | 사례 | 잰 것 | 돌아온 권수(평균) | 시간(초, 중앙값) |",
            "| --- | --- | --- | --- | --- |",
        ]
    for kind, row in others:
        measure = "-"
        if "author_share_mean" in row:
            measure = f"위 10권 중 그 저자의 책 {_num(row['author_share_mean'], 2)}"
        elif "violations" in row:
            measure = (
                f"조건을 어긴 책 {row['violations']}권({row['violating_cases']}건)"
            )
        lines.append(
            f"| {kind} | {row['measured']} | {measure} | {_num(row['returned_mean'])} "
            f"| {_num(row['seconds_median'], 3)} |"
        )
    failed = sum(row["cases"] - row["measured"] for row in kinds.values())
    if failed:
        lines += [
            "",
            f"- 재지 못한 사례 {failed}건(200 이 아니거나 줄여 답함). 평균에서 뺐다",
        ]
    return "\n".join(lines)


def _split(text: str | None) -> list[str] | None:
    return [part.strip() for part in text.split(",") if part.strip()] if text else None


def main(
    argv: Sequence[str] | None = None,
    *,
    search: Search | None = None,
    facts: Facts | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="① 검색 평가 묶음을 돌려 채점한다")
    parser.add_argument("--kinds", help="돌릴 종류(쉼표로 여럿). 예: title,author")
    parser.add_argument("--ids", help="돌릴 사례 번호(쉼표로 여럿). 예: title-001")
    parser.add_argument("--code", help="검색할 코드의 폴더. 없으면 이 저장소")
    parser.add_argument("--yes", action="store_true", help="실제로 돌린다")
    parser.add_argument("--out", help="사례마다의 결과를 남길 JSON 파일")
    parser.add_argument("--any-db", action="store_true", help="_eval 이 아닌 DB 에도")
    args = parser.parse_args(argv)

    bundle = load_bundle()
    try:
        picked = select(bundle["cases"], kinds=_split(args.kinds), ids=_split(args.ids))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    code = Path(args.code) if args.code else REPO
    if not (code / "app" / "main.py").is_file():
        print(
            f"{code} 에 app/main.py 가 없다. 저장소나 worktree 의 폴더를 준다",
            file=sys.stderr,
        )
        return 2
    label = code_label(code)
    print(f"사례 {len(picked)}건, /search 를 {len(picked)}번 부른다. 코드: {label}")

    injected = search is not None and facts is not None
    if not injected:
        from app.core.config import get_settings

        url = get_settings().database_url
        print(f"DB: {database_name(url)}")
        reason = refusal(database_name(url), allow_any=args.any_db)
        if reason:
            print(reason, file=sys.stderr)
            return 2
    if not args.yes:
        print("실제로 돌리려면 --yes 를 붙인다. LLM 은 부르지 않고 DB 에 쓰지 않는다.")
        return 0

    requests = [request(case, bundle["top"]) for case in picked]
    database = None
    if injected:
        results = search(requests)
        shelf = facts({i for r in results for i in r["book_ids"]})
    else:
        results = collect_with(code, requests)
        book_ids = {i for r in results for i in r["book_ids"]}
        shelf, database = asyncio.run(read_db(url, book_ids))
    scores = score_all(picked, results, shelf, bundle["top"])
    summary = summarize(scores)
    print()
    catalog = bundle["catalog"]
    if database and (database["books"], database["with_vectors"]) != (
        catalog["books"],
        catalog["with_vectors"],
    ):
        # 책 표가 다르면 같은 묶음이어도 숫자를 견줄 수 없다.
        print(
            f"주의: 이 DB 의 책 표(책 {database['books']:,}권, 벡터 {database['with_vectors']:,}권)가 "
            f"묶음을 만든 책 표(책 {catalog['books']:,}권, 벡터 {catalog['with_vectors']:,}권)와 다르다.\n"
        )
    print(render(summary, bundle["top"]))

    if args.out:
        saved = {
            "bundle_version": bundle["version"],
            "catalog": catalog,
            "database": database,
            "code": label,
            "summary": summary,
            # 받은 책 번호도 남긴다. 코드끼리 위 N권이 얼마나 바뀌었는지 견줄 때 쓴다
            "scores": [
                {**asdict(s), "book_ids": r["book_ids"]}
                for s, r in zip(scores, results, strict=True)
            ],
        }
        out = Path(args.out)
        # 다 돌린 뒤에 폴더가 없어 저장하지 못하면 결과가 사라진다
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\n사례마다의 결과: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
