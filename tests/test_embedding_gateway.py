"""임베딩 어댑터 단위 테스트.

모델을 실제로 내려받는 테스트는 기본으로 건너뛴다(약 470MB, CI 에서 매번 받으면 느리다).
돌리려면: EMBEDDING_MODEL_TEST=1 uv run pytest
"""

import asyncio
import os

import pytest

from app.gateway import embedding

run_model_test = pytest.mark.skipif(
    os.getenv("EMBEDDING_MODEL_TEST") != "1",
    reason="모델을 내려받는 테스트. EMBEDDING_MODEL_TEST=1 일 때만 실행한다",
)


def test_purpose가_query면_query_접두어를_붙인다() -> None:
    assert embedding.build_inputs(["김영하"], "query") == ["query: 김영하"]


def test_purpose가_document면_passage_접두어를_붙인다() -> None:
    assert embedding.build_inputs(["잔잔한 에세이"], "document") == [
        "passage: 잔잔한 에세이"
    ]


def test_purpose가_enum_밖이면_document로_본다() -> None:
    # 명세의 기본값이 document 다. 라우터가 걸러도 어댑터 단독 호출을 대비한다.
    assert embedding.build_inputs(["텍스트"], "unknown") == ["passage: 텍스트"]


def test_여러_건이면_입력_순서를_그대로_유지한다() -> None:
    out = embedding.build_inputs(["첫째", "둘째", "셋째"], "query")
    assert out == ["query: 첫째", "query: 둘째", "query: 셋째"]


def test_빈_목록이면_빈_목록을_돌려준다() -> None:
    assert embedding.build_inputs([], "query") == []


def test_모델을_읽기_전이면_is_loaded가_거짓이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 다른 테스트가 먼저 모델을 읽었을 수 있으므로 이 테스트 동안만 되돌린다.
    monkeypatch.setattr(embedding, "_model", None)
    assert embedding.is_loaded() is False


@run_model_test
def test_실제_모델은_384차원_단위벡터를_돌려준다() -> None:
    # pytest-asyncio 를 넣지 않으려고 여기서 직접 루프를 돌린다.
    vectors, dim, model = asyncio.run(
        embedding.embed(["쓸쓸하고 담담한 위로"], "query")
    )

    assert dim == 384
    assert len(vectors) == 1
    assert len(vectors[0]) == 384
    assert "e5-small" in model
    # 정규화했으므로 벡터 길이가 1이다
    norm = sum(v * v for v in vectors[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-3


@run_model_test
def test_모델을_읽으면_is_loaded가_참이_된다() -> None:
    embedding.load_model()
    assert embedding.is_loaded() is True
