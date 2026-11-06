"""① 검색 평가의 /search 호출(evals/search/collect.py) 테스트(#495). DB 와 임베딩 모델 없이 돈다.

진짜 앱 대신 가짜 `app` 패키지를 만든 폴더를 넘긴다. 따로 띄운 파이썬이 그 폴더의 코드로 검색하는지 본다.
"""

import asyncio
from pathlib import Path

import pytest

from evals.search import collect
from evals.search.collect import collect_with

# 검색어의 글자 수를 첫 책 번호로 돌려준다. "fail" 은 500, "slow" 는 줄인 응답이다.
_MAIN = """
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


@app.post("/search")
async def search(request: Request):
    if request.headers.get("authorization") != "Bearer token":
        return JSONResponse({"code": "unauthorized"}, status_code=401)
    query = (await request.json())["query"]
    if query == "fail":
        return JSONResponse({"code": "internal_server_error"}, status_code=500)
    headers = {"X-Degraded": "keyword-only"} if query == "slow" else {}
    results = [{"book_id": len(query)}, {"book_id": 7}]
    return JSONResponse({"data": {"results": results}}, headers=headers)
"""
_CONFIG = """
from types import SimpleNamespace


def get_settings():
    return SimpleNamespace(ai_service_token="token")
"""


def _fake_code(root: Path) -> Path:
    (root / "app" / "core").mkdir(parents=True)
    (root / "app" / "main.py").write_text(_MAIN, encoding="utf-8")
    (root / "app" / "core" / "config.py").write_text(_CONFIG, encoding="utf-8")
    return root


def test_준_폴더의_코드로_요청_순서대로_검색한다(tmp_path: Path) -> None:
    requests = [{"query": "abc"}, {"query": "slow"}, {"query": "fail"}]
    results = collect_with(_fake_code(tmp_path), requests)

    assert [(r["status"], r["degraded"], r["book_ids"]) for r in results] == [
        (200, None, [3, 7]),
        (200, "keyword-only", [4, 7]),
        (500, None, []),
    ]
    assert all(r["seconds"] >= 0 for r in results)


def test_검색창_모드의_값을_바꿔_끼우고_타입은_지금_값을_따른다(monkeypatch) -> None:
    from app.search import mode as modes

    monkeypatch.setattr(modes, "SEARCH", modes.Mode())
    got = collect.apply_mode({"keyword_weight": "1", "lookup_limit": "300"})

    assert modes.SEARCH.keyword_weight == 1.0 and isinstance(
        modes.SEARCH.keyword_weight, float
    )
    assert modes.SEARCH.lookup_limit == 300 and isinstance(
        modes.SEARCH.lookup_limit, int
    )
    assert got["min_coverage"] == modes.Mode().min_coverage
    # 뜻 모드는 그대로다
    assert modes.MEANING == modes.Mode()


def test_검색_모드에_없는_이름이면_멈춘다(monkeypatch) -> None:
    from app.search import mode as modes

    monkeypatch.setattr(modes, "SEARCH", modes.Mode())
    with pytest.raises(SystemExit, match="없는 이름"):
        collect.apply_mode({"weight": "1"})


def test_문턱을_바꾸면_키워드_쿼리_앞에_문턱을_건다(monkeypatch) -> None:
    from app.search import keyword

    sent: list[str] = []

    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Conn:
        def transaction(self):
            return _Tx()

        async def execute(self, sql):
            sent.append(sql)

        async def fetch(self, sql, *args):
            sent.append(sql)
            return []

    monkeypatch.setattr(keyword, "_run", keyword._run)
    collect.apply_threshold(0.5)
    asyncio.run(keyword._run(_Conn(), "SELECT 1", []))

    assert sent == [
        "SET LOCAL pg_trgm.word_similarity_threshold = 0.5",
        "SET LOCAL enable_seqscan = off",
        "SELECT 1",
    ]


def test_벡터_한도를_바꿔_끼운다(monkeypatch) -> None:
    from app.search import vector

    monkeypatch.setattr(vector, "QUERY_TIMEOUT_SECONDS", 2.0)
    collect.apply_vector_timeout(30.0)
    assert vector.QUERY_TIMEOUT_SECONDS == 30.0


def test_단계_기록은_요청마다_키워드_벡터_경로를_담는다(monkeypatch) -> None:
    from app.search import keyword, vector

    async def keyword_ids(*args, **kwargs):
        keyword._cand_each_word(1, "")
        return [1, 2]

    async def exact_ids(*args, **kwargs):
        return {2}

    async def vector_ids(*args, **kwargs):
        return [3]

    monkeypatch.setattr(keyword, "search_ids", keyword_ids)
    monkeypatch.setattr(keyword, "exact_title_ids", exact_ids)
    monkeypatch.setattr(vector, "search_ids", vector_ids)
    monkeypatch.setattr(keyword, "_cand_each_word", lambda *a: "cand")
    seen = collect.watch_stages()

    async def one() -> None:
        await keyword.search_ids()
        await keyword.exact_title_ids()
        await vector.search_ids()

    asyncio.run(one())
    assert seen == {
        "each_word": True,
        "keyword_ids": [1, 2],
        "exact_ids": [2],
        "vector_ids": [3],
    }
