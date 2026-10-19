"""① /search 를 앱 안에서 부르는 부분(#495). 실행기(run.py)가 쓴다.

    PYTHONPATH=<코드 폴더> python -P evals/search/collect.py <요청 파일> <결과 파일>

서버를 띄우지 않고 앱에 요청을 그대로 넣는다. 요청 검사부터 응답까지 실제 요청과 같은 길로 지난다. LLM 은
부르지 않는다. 검색어의 벡터만 로컬 임베딩 모델로 만든다.

검색할 코드를 다른 커밋의 것(V1 운영 코드 등)으로 바꿔 끼울 수 있게, 검색은 따로 띄운 파이썬에서 부른다. 그
파이썬은 준 폴더의 `app` 만 읽는다. 이 저장소의 `app` 과 섞이면 어느 코드로 잰 숫자인지 알 수 없다. 그래서 이
파일은 `evals` 를 읽지 않고 표준 라이브러리와 httpx 만으로 돈다.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from time import perf_counter

import httpx

SEARCH_URL = "/search"
CALL_TIMEOUT_SECONDS = 60
# 이만큼 부를 때마다 어디까지 왔는지 찍는다. 묶음 전부에 몇 분 걸린다
PROGRESS_EVERY = 100


async def collect(requests: list[dict]) -> list[dict]:
    """요청마다 /search 를 한 번씩 부른다. 한 번에 하나씩 부른다(시간을 재기 때문이다)."""
    from app.core.config import get_settings
    from app.main import app

    token = get_settings().ai_service_token
    results = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://eval",
            timeout=CALL_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        ) as client,
    ):
        # 첫 요청은 DB 연결을 여느라 느리다. 재는 시간에 섞이지 않게 한 번 불러 둔다.
        await client.post(SEARCH_URL, json={"query": "책", "size": 1})
        for done, body in enumerate(requests, start=1):
            started = perf_counter()
            response = await client.post(SEARCH_URL, json=body)
            seconds = perf_counter() - started
            ok = response.status_code == 200
            items = (response.json().get("data") or {}).get("results", []) if ok else []
            results.append(
                {
                    "status": response.status_code,
                    "degraded": response.headers.get("X-Degraded"),
                    # V1 응답에는 isbn 이 없다. 책 번호로 받아 같은 DB 에서 책 정보를 읽는다
                    "book_ids": [item["book_id"] for item in items],
                    "seconds": seconds,
                }
            )
            if done % PROGRESS_EVERY == 0:
                print(f"검색 {done}/{len(requests)}", file=sys.stderr, flush=True)
    return results


def main(argv: list[str]) -> int:
    sent, got = (Path(arg) for arg in argv)
    # 서버는 요청마다 로그를 남긴다. 수백 번 부르면 진행이 묻히므로 경고부터만 보인다.
    logging.disable(logging.INFO)
    requests = json.loads(sent.read_text(encoding="utf-8"))
    got.write_text(json.dumps(asyncio.run(collect(requests))), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
