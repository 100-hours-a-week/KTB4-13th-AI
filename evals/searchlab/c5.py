"""#556 C5: 벡터 유사도로 없는 말과 있는 말을 얼마나 가를 수 있는지 잰다. 앱 코드는 바꾸지 않는다.

    # 개발 AI DB 에서 받은 벡터 사본(COPY 바이너리)을 로컬 임시 DB 에 넣고 색인을 만든 뒤에 돌린다
    uv run python -m evals.searchlab.c5 --probe nonsense_probe.json --vectors emb.bin \\
        --db postgresql://localhost/bookjeok_c5_eval --out c5.json

- 검색어: #548 의 130개(없는 말 50, 있는 말 80). 벡터는 앱과 같은 임베딩으로 만든다
- 1·10·20·50등 코사인을 정확 계산과 탐색 폭 400 두 가지로 낸다
- μ: 고정한 무작위 책 5,000권과의 코사인 평균. 목록은 c5_mu_books.json 이다(시드 556, 벡터를 book_id 로 줄 세워 뽑음)
- AUC: 있는 말의 값이 없는 말의 값보다 클 확률(동점 0.5). 구간은 있는 말과 없는 말을 따로 다시 뽑은 부트스트랩이다

#556 예측: 1등 − 50등의 AUC 는 1등 절댓값보다 낮다. 1등 − μ 의 AUC 는 절댓값과 ±0.05 안이다.
"""

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

BOOTSTRAP = 10_000
SEED = 556
EF_SEARCH = 400
TOP = 50
MU_BOOKS = 5_000
MU_PATH = Path(__file__).with_name("c5_mu_books.json")
# 예측: 1등 − μ 와 절댓값의 AUC 차가 이 안이다
MU_MARGIN = 0.05
RANKS = (1, 10, 20, 50)


def read_copy(path: Path, dim: int = 384) -> tuple[np.ndarray, np.ndarray]:
    """COPY (book_id, embedding) 바이너리 파일을 (book_id 순서의 번호, 단위 벡터)로 읽는다."""
    raw = np.fromfile(path, dtype=np.uint8)
    header = 19 + int.from_bytes(raw[15:19].tobytes(), "big")
    row = np.dtype(
        [
            ("fields", ">i2"),
            ("id_len", ">i4"),
            ("id", ">i4"),
            ("vec_len", ">i4"),
            ("dim", ">u2"),
            ("unused", ">u2"),
            ("v", ">f4", (dim,)),
        ]
    )
    count = (len(raw) - header - 2) // row.itemsize
    rows = np.frombuffer(raw[header : header + count * row.itemsize].tobytes(), row)
    if not ((rows["fields"] == 2).all() and (rows["dim"] == dim).all()):
        raise ValueError("(book_id, embedding) 두 칸의 COPY 바이너리가 아니다")
    if raw[header + count * row.itemsize :].tobytes() != b"\xff\xff":
        raise ValueError("파일 끝이 맞지 않는다")
    order = np.argsort(rows["id"], kind="stable")
    vectors = rows["v"][order].astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return rows["id"][order].astype(np.int64), vectors


def mu_books(ids: np.ndarray, path: Path = MU_PATH) -> list[int]:
    """고정한 무작위 책 목록. 파일이 있으면 그것을 쓰고, 없으면 시드로 뽑아 남긴다."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["books"]
    picked = np.random.default_rng(SEED).choice(len(ids), MU_BOOKS, replace=False)
    books = [int(ids[i]) for i in picked]
    saved = {"seed": SEED, "vectors": len(ids), "books": books}
    path.write_text(json.dumps(saved) + "\n", encoding="utf-8")
    return books


def exact_top(queries: np.ndarray, vectors: np.ndarray, top: int = TOP) -> np.ndarray:
    """검색어마다 코사인이 큰 순서로 위 top 개."""
    scores = queries @ vectors.T
    part = np.partition(scores, scores.shape[1] - top, axis=1)[:, -top:]
    return -np.sort(-part, axis=1)


async def index_top(url: str, queries: Sequence[str]) -> np.ndarray:
    """탐색 폭 400으로 찾은 위 50개의 코사인. 앱의 벡터 검색과 같은 설정이다."""
    import asyncpg

    conn = await asyncpg.connect(url)
    out = []
    try:
        for literal in queries:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}")
                await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
                rows = await conn.fetch(
                    "SELECT 1 - (embedding <=> $1::vector) AS cosine "
                    "FROM book_embeddings ORDER BY embedding <=> $1::vector LIMIT $2",
                    literal,
                    TOP,
                )
            out.append(sorted((r["cosine"] for r in rows), reverse=True))
    finally:
        await conn.close()
    return np.asarray(out, dtype=float)


def auc(values: np.ndarray, labels: np.ndarray) -> float:
    """있는 말(1)의 값이 없는 말(0)의 값보다 클 확률. 동점은 0.5."""
    pos, neg = values[labels == 1], values[labels == 0]
    wins = (pos[:, None] > neg[None, :]).sum() + 0.5 * (
        pos[:, None] == neg[None, :]
    ).sum()
    return float(wins / (len(pos) * len(neg)))


def auc_gap(
    a: np.ndarray, b: np.ndarray, labels: np.ndarray, *, seed: int = SEED
) -> dict[str, float]:
    """AUC(a) − AUC(b) 와 95% 구간. 있는 말과 없는 말을 따로 다시 뽑는다."""
    pos, neg = np.where(labels == 1)[0], np.where(labels == 0)[0]
    rng = np.random.default_rng(seed)
    gaps = np.empty(BOOTSTRAP)
    for i in range(BOOTSTRAP):
        idx = np.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])
        gaps[i] = auc(a[idx], labels[idx]) - auc(b[idx], labels[idx])
    low, high = np.percentile(gaps, [2.5, 97.5])
    return {"gap": auc(a, labels) - auc(b, labels), "low": low, "high": high}


def measures(top: np.ndarray, mu: np.ndarray) -> dict[str, np.ndarray]:
    """가를 값들. top 은 검색어마다 코사인이 큰 순서의 위 50개다."""
    out = {f"{rank}등": top[:, rank - 1] for rank in RANKS}
    out["1등 − 50등"] = top[:, 0] - top[:, 49]
    out["1등 − 위 10권 평균"] = top[:, 0] - top[:, :10].mean(axis=1)
    out["1등 − μ"] = top[:, 0] - mu
    return out


def judge(rank50: Mapping[str, float], mu: Mapping[str, float]) -> dict[str, str]:
    """#556 C5 예측 둘을 판정한다(「판정 기준」 5). rank50 은 1등 − 50등, mu 는 1등 − μ 의 절댓값 대비 AUC 차다."""
    if rank50["high"] < 0:
        lower = "맞음"
    elif rank50["low"] > 0:
        lower = "틀림"
    else:
        lower = "판단 불가"
    return {
        "1등 − 50등이 절댓값보다 낮다": lower,
        "1등 − μ 가 절댓값과 ±0.05 안이다": "맞음"
        if abs(mu["gap"]) <= MU_MARGIN
        else "틀림",
    }


def table(values: Mapping[str, np.ndarray], labels: np.ndarray) -> list[dict[str, Any]]:
    return [
        {
            "값": name,
            "AUC": round(auc(v, labels), 3),
            "없는 말 중앙": round(float(np.median(v[labels == 0])), 4),
            "있는 말 중앙": round(float(np.median(v[labels == 1])), 4),
        }
        for name, v in values.items()
    ]


def main(argv: Sequence[str] | None = None) -> int:
    from app.core.pgvector import to_vector_literal
    from app.gateway import embedding

    parser = argparse.ArgumentParser(description="#556 C5")
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--vectors", required=True, type=Path)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    probe = json.loads(args.probe.read_text(encoding="utf-8"))
    groups = (("nonsense", 0), ("descriptive", 1), ("legit_extra", 1))
    words = [row["q"] for group, _ in groups for row in probe[group]]
    labels = np.array([y for group, y in groups for _ in probe[group]])
    encoded, _, _ = asyncio.run(embedding.embed(words, "query"))
    queries = np.asarray(encoded, dtype=np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)

    ids, vectors = read_copy(args.vectors)
    keep = np.isin(ids, mu_books(ids))
    mu = (queries @ vectors[keep].T).mean(axis=1)
    tops = {
        "정확 계산": exact_top(queries, vectors),
        f"탐색 폭 {EF_SEARCH}": asyncio.run(
            index_top(args.db, [to_vector_literal(q.tolist()) for q in queries])
        ),
    }

    saved: dict[str, Any] = {
        "검색어": {
            "없는 말": int((labels == 0).sum()),
            "있는 말": int((labels == 1).sum()),
        },
        "벡터": len(ids),
        "μ 책": int(keep.sum()),
    }
    for name, top in tops.items():
        values = measures(top, mu)
        rank50 = auc_gap(values["1등 − 50등"], values["1등"], labels)
        gap_mu = auc_gap(values["1등 − μ"], values["1등"], labels)
        saved[name] = {
            "표": table(values, labels),
            "1등 − 50등 대 절댓값": rank50,
            "1등 − μ 대 절댓값": gap_mu,
            "판정": judge(rank50, gap_mu),
        }
    args.out.write_text(
        json.dumps(saved, ensure_ascii=False, indent=1, default=float), encoding="utf-8"
    )
    print(json.dumps(saved, ensure_ascii=False, indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
