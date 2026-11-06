"""검색어 벡터 미리 만들기(evals/searchlab/vectors.py) 테스트(#591). 모델을 읽지 않는다."""

import asyncio
import json

import pytest

from evals.searchlab import vectors


def test_같은_검색어는_한_번만_순서대로() -> None:
    bundle = {
        "measure": [{"query": "가"}, {"query": "나"}, {"query": "가"}],
        "warmup": [{"query": "다"}, {"query": "나"}],
    }
    assert vectors.unique_queries(bundle) == ["가", "나", "다"]


def test_앱처럼_한_번에_한_검색어씩_검색어_용도로_만든다() -> None:
    calls = []

    async def fake_embed(texts: list[str], purpose: str):
        calls.append((texts, purpose))
        return [[float(len(texts[0]))]], 1, "model-x"

    got, model = asyncio.run(vectors.make(["가", "나다"], fake_embed))
    assert calls == [(["가"], "query"), (["나다"], "query")]
    assert got == {"가": [1.0], "나다": [2.0]}
    assert model == "model-x"


def test_다른_검색어_파일로_만든_벡터는_읽지_않는다(tmp_path) -> None:
    queries_path = tmp_path / "queries.json"
    queries_path.write_text('{"a": 1}', encoding="utf-8")
    out = tmp_path / "vectors.json"
    digest = vectors.queries_digest(queries_path.read_text(encoding="utf-8"))
    out.write_text(
        json.dumps({"queries": digest, "vectors": {"가": [0.5]}}), encoding="utf-8"
    )
    assert vectors.load(out, queries_path) == {"가": [0.5]}

    queries_path.write_text('{"a": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="다른 검색어 파일"):
        vectors.load(out, queries_path)
