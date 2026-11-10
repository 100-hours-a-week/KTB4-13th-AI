"""① 검색 평가의 /search 호출(evals/search/collect.py) 테스트(#495). DB 와 임베딩 모델 없이 돈다.

진짜 앱 대신 가짜 `app` 패키지를 만든 폴더를 넘긴다. 따로 띄운 파이썬이 그 폴더의 코드로 검색하는지 본다.
"""

from pathlib import Path

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


# #308 뒤 응답은 book_id 없이 isbn 만 싣는다. 책 번호는 그 코드의 책 표로 바꾼다(끝 자리를 번호로 쓰는 가짜)
_MAIN_ISBN = """
from fastapi import FastAPI

app = FastAPI()


@app.post("/search")
async def search():
    return {"data": {"results": [{"isbn": "9780000000003"}, {"isbn": "9780000000007"}]}}
"""
_DB = """
from contextlib import asynccontextmanager


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield "conn"


def get_pool():
    return _Pool()
"""
_ISBN = """
async def to_book_ids(conn, isbns):
    assert conn == "conn"
    return [int(isbn[-1]) for isbn in isbns]
"""


def test_ISBN만_실린_응답은_그_코드의_책_표로_책_번호를_찾는다(tmp_path: Path) -> None:
    root = _fake_code(tmp_path)
    (root / "app" / "main.py").write_text(_MAIN_ISBN, encoding="utf-8")
    (root / "app" / "core" / "db.py").write_text(_DB, encoding="utf-8")
    (root / "app" / "core" / "isbn.py").write_text(_ISBN, encoding="utf-8")

    results = collect_with(root, [{"query": "abc"}])

    assert [r["book_ids"] for r in results] == [[3, 7]]
