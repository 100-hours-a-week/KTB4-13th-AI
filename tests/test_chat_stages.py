"""③ 단계 측정(evals/chat_stages)의 채점과 실험 끼우기 테스트. DB·LLM 없이 돈다."""

import asyncio
from types import SimpleNamespace

import pytest

from app.routers import chat
from evals.chat_stages import run, variants


def test_ndcg_는_위_10권이_모두_2점일_때_1이다() -> None:
    assert run.ndcg([2] * 10) == pytest.approx(1.0)
    # 이상값을 고정해서, 1점만 10권이면 1이 아니라 1/3 이다(① 검색 평가와 같은 식)
    assert run.ndcg([1] * 10) == pytest.approx(1 / 3)
    # 점수 없는 책은 0점으로 센다
    assert run.ndcg([None] * 10) == 0.0


def _raw() -> dict:
    variant = {
        "search": [1, 2, 3, 4],
        "degraded": None,
        "filtered": [1, 2, 3],
        "candidates": [1, 2],
        "candidate_seconds": [0.1, 0.2, 0.3],
        "match_scores": [10, 20],
        "cards": [{"book_ids": [2], "seconds": 3.0, "degraded": False}],
    }
    return {
        "cases": [
            {
                "id": "descriptive-01",
                "facet": "topic",
                "query": "q",
                "raw_search": [1, 4],
                "spec": {
                    "intent": "semantic",
                    "semantic": "q",
                    "exact": {"title": None, "author": None},
                },
                "spec_seconds": 1.0,
                "variants": {name: variant for name in variants.VARIANTS},
            }
        ],
        "books": {
            "1": {"isbn": "a", "intro": True},
            "2": {"isbn": "b", "intro": True},
            "3": {"isbn": "c", "intro": True},
            "4": {"isbn": "d", "intro": False},
        },
    }


def test_단계마다_2점_책을_세고_소개글_없어_빠진_책을_가른다() -> None:
    labels = {"descriptive-01": {"a": 2, "b": 1, "c": 2, "d": 2}}
    base = run.score(_raw(), labels)["variants"]["base"]
    assert base["search_rel"] == 3
    assert base["filtered_rel"] == 2
    assert base["candidates_rel"] == 1
    # 4번 책(2점)은 소개글이 없어 거르기에서 빠졌다
    assert base["lost_no_intro_rel"] == 1
    assert base["card_grade"] == 1
    assert base["unlabeled"] == 0
    assert base["turn_seconds"][0] == pytest.approx(1.0 + 0.2 + 3.0)


def _spec(intent: str = "semantic") -> SimpleNamespace:
    return SimpleNamespace(
        intent=intent, semantic="조건" if intent == "semantic" else None
    )


def test_점수_먼저는_점수로_줄_세운_뒤_10권으로_자른다(monkeypatch) -> None:
    async def fake_attach(conn, candidates, user_id, spec):
        for c in candidates:
            c["match_score"] = c["book_id"]

    monkeypatch.setattr(chat, "_attach_match_scores", fake_attach)
    undo = variants.install("rerank")
    try:
        assert chat.CANDIDATE_LIMIT == chat.SEARCH_SIZE
        pool = [{"book_id": i} for i in range(1, 21)]
        asyncio.run(chat._attach_match_scores(None, pool, 1, _spec()))
        assert [c["book_id"] for c in pool] == list(range(20, 10, -1))

        # 제목·저자 찾기(exact)는 검색 순서를 그대로 두고 자르기만 한다
        pool = [{"book_id": i} for i in range(1, 21)]
        asyncio.run(chat._attach_match_scores(None, pool, 1, _spec("exact")))
        assert [c["book_id"] for c in pool] == list(range(1, 11))
    finally:
        undo()
    assert chat.CANDIDATE_LIMIT == 10
    assert chat._attach_match_scores is fake_attach


def test_벡터_검색은_semantic_요청에만_걸린다(monkeypatch) -> None:
    called = []

    async def fake_search(req):
        called.append(req.query)
        return SimpleNamespace(results=[], degraded=None)

    async def fake_get_candidates(spec, *args, **kwargs):
        await chat.service.search(SimpleNamespace(query=spec.intent))
        return []

    monkeypatch.setattr(variants.service, "search", fake_search)
    monkeypatch.setattr(chat, "get_candidates", fake_get_candidates)
    undo = variants.install("vector")
    try:
        # exact 는 ① 검색으로 간다. semantic 은 임베딩을 부르므로 여기서는 exact 만 본다
        asyncio.run(chat.get_candidates(_spec("exact")))
        assert called == ["exact"]
        assert isinstance(chat.service, variants.VectorFirst)
    finally:
        undo()
    assert chat.service is variants.service
