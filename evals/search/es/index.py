"""평가 DB 의 책을 로컬 Elasticsearch 에 색인한다(#754). 한 번 쓰는 측정 코드라 브랜치에만 둔다.

    evals/search/es/run.sh   # Elasticsearch 를 띄운다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.es.index

검색이 읽는 값만 옮긴다: 제목·저자·소개글(nori 기본 분석기), 필터에 쓰는 분류·판매가·출간연도, 책 벡터.
판매가는 검색 필터와 같은 식(app.core.products.price_sql)으로 읽는다. 매핑에는 분석기와 필드 형태만 적고 나머지
(샤드·벡터 색인 방식·유사도)는 Elasticsearch 기본값에 맡긴다. 9.x 는 384차원 이상 벡터를 1비트 양자화(BBQ)로
색인한다. 만든 매핑은 그대로 찍어 결과에 남긴다.
"""

import argparse
import asyncio
import json
import os
import sys
from time import perf_counter

import asyncpg
import httpx

from app.core import products

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
INDEX = "books"
BATCH = 2000

MAPPING = {
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "book_id": {"type": "integer"},
            "title": {"type": "text", "analyzer": "nori"},
            "author": {"type": "text", "analyzer": "nori"},
            "description": {"type": "text", "analyzer": "nori"},
            "category": {"type": "keyword"},
            "price": {"type": "integer"},
            "pub_year": {"type": "integer"},
            "embedding": {"type": "dense_vector", "dims": 384},
        },
    }
}

SQL = f"""
SELECT b.book_id, b.title, b.author, b.description, b.category, b.pub_year,
       {products.price_sql("b")} AS price, e.embedding::text AS embedding
FROM v_books b
LEFT JOIN book_embeddings e USING (book_id)
ORDER BY b.book_id
"""


def document(row: asyncpg.Record) -> dict:
    doc = {
        "book_id": row["book_id"],
        "title": row["title"],
        "author": row["author"],
        "description": row["description"],
        "category": row["category"],
        "price": row["price"],
        "pub_year": row["pub_year"],
    }
    if row["embedding"] is not None:
        doc["embedding"] = json.loads(row["embedding"])
    return {k: v for k, v in doc.items() if v is not None}


def bulk_body(rows: list[asyncpg.Record]) -> str:
    lines = []
    for row in rows:
        lines.append(json.dumps({"index": {"_index": INDEX, "_id": row["book_id"]}}))
        lines.append(json.dumps(document(row), ensure_ascii=False))
    return "\n".join(lines) + "\n"


async def send(client: httpx.AsyncClient, rows: list[asyncpg.Record]) -> None:
    response = await client.post(
        "/_bulk",
        content=bulk_body(rows).encode(),
        headers={"Content-Type": "application/x-ndjson"},
    )
    response.raise_for_status()
    body = response.json()
    if body["errors"]:
        failed = [i for i in body["items"] if i["index"].get("error")]
        raise RuntimeError(f"색인 실패 {len(failed)}건: {failed[0]}")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="평가 DB 의 책을 Elasticsearch 에 색인한다"
    )
    parser.add_argument(
        "--recreate", action="store_true", help="있는 색인을 지우고 다시 만든다"
    )
    args = parser.parse_args(argv)

    url = os.environ["DATABASE_URL"]
    if not url.rsplit("/", 1)[-1].endswith("_eval"):
        print(f"평가 DB(_eval)가 아니다: {url}", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(base_url=ES_URL, timeout=300) as client:
        exists = (await client.head(f"/{INDEX}")).status_code == 200
        if exists and not args.recreate:
            print(
                f"색인 {INDEX} 가 이미 있다. 다시 만들려면 --recreate", file=sys.stderr
            )
            return 2
        if exists:
            (await client.delete(f"/{INDEX}")).raise_for_status()
        (await client.put(f"/{INDEX}", json=MAPPING)).raise_for_status()
        field = await client.get(
            f"/{INDEX}/_mapping/field/embedding?include_defaults=true"
        )
        vector = field.json()[INDEX]["mappings"]["embedding"]["mapping"]["embedding"]
        print("벡터 매핑(기본값 포함):", json.dumps(vector, ensure_ascii=False))

        started = perf_counter()
        done = 0
        conn = await asyncpg.connect(url)
        try:
            async with conn.transaction():
                batch: list[asyncpg.Record] = []
                async for row in conn.cursor(SQL, prefetch=BATCH):
                    batch.append(row)
                    if len(batch) == BATCH:
                        await send(client, batch)
                        done += len(batch)
                        batch = []
                        if done % 100_000 == 0:
                            print(
                                f"{done:,}권 {perf_counter() - started:.0f}초",
                                flush=True,
                            )
                if batch:
                    await send(client, batch)
                    done += len(batch)
        finally:
            await conn.close()
        (await client.post(f"/{INDEX}/_refresh")).raise_for_status()
        count = (await client.get(f"/{INDEX}/_count")).json()["count"]
        print(
            f"색인 끝: 보낸 {done:,}권, 색인에 {count:,}권, {perf_counter() - started:.0f}초"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
