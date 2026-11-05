"""잴 검색어·데우기 검색어의 벡터를 미리 만든다(#591).

    uv run python -m evals.searchlab.vectors

측정은 앱 서버 없이 SQL 만 보내므로, 검색어 벡터를 앱과 같은 함수(`app.gateway.embedding.embed`)로 미리 만들어 둔다.
앱이 검색할 때처럼 한 번에 한 검색어씩 만든다(여러 개를 한 번에 넣으면 길이 맞춤 때문에 값이 아주 조금 달라질 수 있다).
DB 는 쓰지 않는다. 결과는 `out/vectors.json` 에 쓰고 저장소에는 올리지 않는다(약 5MB).
"""

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from evals.searchlab.queries import QUERIES_PATH

VECTORS_PATH = Path(__file__).with_name("out") / "vectors.json"

Embed = Callable[[list[str], str], Awaitable[tuple[list[list[float]], int, str]]]


def queries_digest(text: str) -> str:
    """검색어 파일의 지문. 벡터 파일이 어느 검색어 파일로 만든 것인지 측정 때 맞춰 본다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def unique_queries(bundle: dict[str, Any]) -> list[str]:
    """벡터를 만들 검색어. 정렬 사례처럼 같은 검색어가 여러 번 나와도 한 번만 만든다."""
    seen: dict[str, None] = {}
    for item in [*bundle["measure"], *bundle["warmup"]]:
        seen.setdefault(item["query"], None)
    return list(seen)


async def make(
    queries: Sequence[str], embed: Embed
) -> tuple[dict[str, list[float]], str]:
    vectors: dict[str, list[float]] = {}
    model = ""
    for done, query in enumerate(queries, start=1):
        got, _, model = await embed([query], "query")
        vectors[query] = got[0]
        if done % 100 == 0:
            print(f"벡터 {done}/{len(queries)}", file=sys.stderr, flush=True)
    return vectors, model


def load(
    path: Path = VECTORS_PATH, queries_path: Path = QUERIES_PATH
) -> dict[str, list[float]]:
    """측정 도구가 벡터를 읽는다. 검색어 파일이 바뀌었으면 ValueError — 옛 벡터로 재지 않게 한다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    digest = queries_digest(queries_path.read_text(encoding="utf-8"))
    if data["queries"] != digest:
        raise ValueError(
            f"{path} 는 다른 검색어 파일로 만든 벡터다({data['queries']}, 지금 {digest}). 다시 만든다"
        )
    return data["vectors"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검색어 벡터를 미리 만든다")
    parser.add_argument("--out", default=str(VECTORS_PATH), help="쓸 파일")
    args = parser.parse_args(argv)

    from app.gateway.embedding import embed

    text = QUERIES_PATH.read_text(encoding="utf-8")
    queries = unique_queries(json.loads(text))
    vectors, model = asyncio.run(make(queries, embed))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {"model": model, "queries": queries_digest(text), "vectors": vectors}
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"검색어 {len(vectors)}개의 벡터({model})를 {out} 에 썼다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
