"""#556 C5 계산(evals/searchlab/c5.py) 테스트. DB 없이 돈다."""

import struct
from pathlib import Path

import numpy as np
import pytest

from evals.searchlab import c5


def _copy_file(path: Path, rows: list[tuple[int, list[float]]], dim: int) -> None:
    body = b"PGCOPY\n\xff\r\n\x00" + struct.pack(">II", 0, 0)
    for book_id, vector in rows:
        body += struct.pack(">hii", 2, 4, book_id)
        body += struct.pack(">iHH", 4 + 4 * dim, dim, 0)
        body += struct.pack(f">{dim}f", *vector)
    path.write_bytes(body + b"\xff\xff")


def test_COPY_바이너리를_book_id_순서의_단위_벡터로_읽는다(tmp_path: Path) -> None:
    path = tmp_path / "emb.bin"
    _copy_file(path, [(7, [3.0, 4.0]), (2, [1.0, 0.0])], 2)
    ids, vectors = c5.read_copy(path, dim=2)
    assert ids.tolist() == [2, 7]
    assert vectors == pytest.approx(np.array([[1.0, 0.0], [0.6, 0.8]]), abs=1e-6)


def test_AUC_는_있는_말이_없는_말보다_클_확률이고_동점은_절반이다() -> None:
    labels = np.array([1, 1, 0, 0])
    assert c5.auc(np.array([3.0, 4.0, 1.0, 2.0]), labels) == 1.0
    assert c5.auc(np.array([1.0, 1.0, 1.0, 1.0]), labels) == 0.5


def test_코사인_위_권은_큰_순서다() -> None:
    queries = np.array([[1.0, 0.0]], dtype=np.float32)
    vectors = np.array([[0.0, 1.0], [1.0, 0.0], [0.6, 0.8]], dtype=np.float32)
    top = c5.exact_top(queries, vectors, top=2)
    assert top == pytest.approx(np.array([[1.0, 0.6]]), abs=1e-6)


def test_무작위_책_목록은_시드로_고정하고_있으면_그대로_쓴다(tmp_path: Path) -> None:
    path = tmp_path / "mu.json"
    ids = np.arange(10_000)
    first = c5.mu_books(ids, path)
    assert len(first) == c5.MU_BOOKS == len(set(first))
    assert c5.mu_books(np.arange(3), path) == first


def test_예측_판정() -> None:
    fell = {"gap": -0.07, "low": -0.15, "high": -0.01}
    unclear = {"gap": -0.07, "low": -0.17, "high": 0.03}
    rose = {"gap": 0.1, "low": 0.02, "high": 0.2}
    near = {"gap": 0.043, "low": -0.03, "high": 0.11}
    far = {"gap": 0.06, "low": 0.01, "high": 0.11}
    first, second = c5.judge(fell, near).values()
    assert (first, second) == ("맞음", "맞음")
    assert list(c5.judge(unclear, far).values()) == ["판단 불가", "틀림"]
    assert c5.judge(rose, near)["1등 − 50등이 절댓값보다 낮다"] == "틀림"
