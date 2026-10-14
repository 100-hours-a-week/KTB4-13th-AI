"""③ 챗봇 평가 실행(#322) — cases.json 의 질문을 떠 있는 서버에 보내고 응답 원본을 저장한다.

화면(FE)이 요청을 만드는 방식을 그대로 따른다: 직전 응답의 spec 을 되돌려 보내고, 최근 대화와
이미 보여 준 책을 쌓아 보낸다. 개인화는 넣지 않는다(이력이 없는 사용자). 채점은 grade.py 가 한다.

LLM 을 실제로 부르므로 비용이 든다(60문항 × 5회에 요청 350번). CI 에서는 돌리지 않는다.

사용:
    DATABASE_URL=postgresql://…/bookjeok_ai_eval \
    uv run python -m evals.chat.run --base-url http://127.0.0.1:8001 --runs 5 --out 원본.json
"""

import argparse
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

from app.core.products import price_sql
from evals.chat.grade import load_cases

# 화면이 보내는 초기 spec(명세 ③). 턴이 이어지면 직전 응답의 spec 으로 바뀐다.
INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}
# 이력이 없는 사용자. 평가 DB 에는 사용자 표가 비어 있다.
EVAL_USER_ID = 999_999
# 화면은 최근 20턴을, 턴마다 200자까지만 보낸다(명세 ③).
MAX_RECENT_TURNS = 20
MAX_TURN_CHARS = 200
REQUEST_TIMEOUT_SECONDS = 90
# 서버 로그에 이 말이 있으면 벡터 검색이 끊겨 키워드 검색만으로 답한 요청이 섞인 것이다.
VECTOR_FALLBACK_LOG = "벡터 검색 실패"


def _request(url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw[:200].decode("utf-8", "replace")}
    except (urllib.error.URLError, TimeoutError) as e:
        # 서버에 닿지 못했거나 시간이 넘었다. 상태 0 으로 남겨 채점에서 "요청 오류"가 되게 한다.
        return 0, {"message": repr(e)[:200]}


def run_case(base_url: str, token: str, case: dict) -> tuple[str, list[dict]]:
    """질문 하나(여러 턴일 수 있다)를 끝까지 보낸다."""
    spec, recent, shown, turns = INITIAL_SPEC, [], [], []
    for message in case["turns"]:
        body = {
            "user_id": EVAL_USER_ID,
            "consented": False,
            "spec": spec,
            "message": message,
            "recent_turns": recent[-MAX_RECENT_TURNS:],
            "exclude_book_ids": shown,
        }
        started = time.monotonic()
        status, payload = _request(f"{base_url}/recommendations/chat", token, body)
        data = payload.get("data") or {}
        cards = data.get("cards") or []
        turns.append(
            {
                "msg": message,
                "status": status,
                "sec": round(time.monotonic() - started, 2),
                "spec": data.get("spec"),
                "reply": data.get("reply"),
                "followup": data.get("followup"),
                "degraded": data.get("degraded"),
                "cards": [
                    {
                        key: card.get(key)
                        for key in (
                            "book_id",
                            "isbn",
                            "rank",
                            "match_score",
                            "title",
                            "author",
                            "price",
                            "reason_short",
                            "reason_long",
                        )
                    }
                    for card in cards
                ],
                "error": None if status == 200 else payload.get("message"),
            }
        )
        if status != 200:
            break
        spec = data["spec"]
        said = (data.get("reply") or "") + (
            "\n\n" + data["followup"] if data.get("followup") else ""
        )
        recent += [
            {"role": "user", "text": message[:MAX_TURN_CHARS]},
            {"role": "assistant", "text": said[:MAX_TURN_CHARS]},
        ]
        shown = list(dict.fromkeys(shown + [c["book_id"] for c in cards]))
    return str(case["no"]), turns


async def fetch_books(database_url: str, book_ids: list[int]) -> dict[str, dict]:
    """카드에 나온 책의 사실(출판사, 출간연도, 가격, 책소개 유무). 채점이 DB 없이 돌게 원본에 같이 넣는다."""
    if not book_ids:
        return {}
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            "SELECT b.book_id, b.isbn13, b.title, b.author, b.publisher, b.pub_year,"
            f" {price_sql('b')} AS price,"
            " (b.description IS NOT NULL AND b.description <> '') AS has_description"
            " FROM v_books b WHERE b.book_id = ANY($1::int[])",
            book_ids,
        )
    finally:
        await conn.close()
    return {
        str(r["book_id"]): {
            "isbn": r["isbn13"],
            "title": r["title"],
            "author": r["author"],
            "publisher": r["publisher"],
            "pub_year": r["pub_year"],
            "price": r["price"],
            "has_description": r["has_description"],
        }
        for r in rows
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="③ 챗봇 평가 실행")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--runs", type=int, default=5, help="질문마다 돌릴 횟수")
    parser.add_argument("--out", required=True, help="원본을 저장할 경로(저장소 밖)")
    parser.add_argument("--workers", type=int, default=4, help="동시에 보낼 질문 수")
    parser.add_argument("--only", default="", help="이 번호의 질문만. 예: 8,43")
    parser.add_argument(
        "--model",
        default=os.environ.get("LLM_MODEL_ID", ""),
        help="서버가 쓰는 모델(기록용)",
    )
    parser.add_argument("--note", default="", help="이 실행에 붙일 메모")
    parser.add_argument(
        "--server-log", help="서버 로그 파일. 주면 끝난 뒤 벡터 검색이 끊긴 횟수를 센다"
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL 이 필요하다(서버가 쓰는 평가 DB 와 같은 값)")
    token = os.environ.get("AI_SERVICE_TOKEN", "")

    cases_file = load_cases()
    only = {int(n) for n in args.only.split(",") if n.strip()}
    cases = [c for c in cases_file["cases"] if not only or c["no"] in only]

    status, health = _request(f"{args.base_url}/health", token)
    if status != 200:
        parser.error(f"서버가 준비되지 않았다: /health {status} {health}")

    meta = {
        "cases_version": cases_file["version"],
        "server_version": health.get("version"),
        "model": args.model,
        "started_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(
            timespec="seconds"
        ),
        "base_url": args.base_url,
        "runs": args.runs,
        "workers": args.workers,
        "note": args.note,
    }
    runs: list[dict[str, list[dict]]] = []
    out = Path(args.out)
    for number in range(1, args.runs + 1):
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            run = dict(pool.map(lambda c: run_case(args.base_url, token, c), cases))
        runs.append(run)
        turns = [t for ts in run.values() for t in ts]
        errors = sum(t["status"] != 200 for t in turns)
        degraded = sum(bool(t["degraded"]) for t in turns)
        print(
            f"{number}회차: 요청 {len(turns)}번, 오류 {errors}번, degraded {degraded}턴,"
            f" {time.monotonic() - started:.0f}초",
            flush=True,
        )
        # 회차마다 저장한다. 중간에 끊겨도 끝난 회차는 남는다.
        out.write_text(
            json.dumps({"meta": meta, "books": {}, "runs": runs}, ensure_ascii=False),
            encoding="utf-8",
        )

    book_ids = sorted(
        {
            c["book_id"]
            for r in runs
            for ts in r.values()
            for t in ts
            for c in t["cards"]
        }
    )
    books = asyncio.run(fetch_books(database_url, book_ids))
    if args.server_log:
        log = Path(args.server_log).read_text(encoding="utf-8", errors="replace")
        meta["vector_fallbacks"] = log.count(VECTOR_FALLBACK_LOG)
        print(f"벡터 검색이 끊긴 횟수: {meta['vector_fallbacks']}")
        if meta["vector_fallbacks"]:
            print(
                "  ! 0 이 아니면 이 실행은 버린다. 책을 못 찾은 이유가 코드인지 측정 환경인지 가를 수 없다."
            )
    out.write_text(
        json.dumps({"meta": meta, "books": books, "runs": runs}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"원본 저장: {out} (책 {len(books)}권)")


if __name__ == "__main__":
    main()
