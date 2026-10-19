"""③ 챗봇을 BE 를 거쳐 짧게 점검한다(#504) — dev BE 의 챗봇 입구에 찾기 문장 몇 개를 보내 500 이 0건인지 본다.

run.py 는 AI 서버에 바로 보내서 BE 에서만 나는 실패를 못 본다. 10/19 에 찾은 예: AI 명세는 책소개 없는 책의
긴 이유(reason_long)를 null 로 두는데, BE 의 recommendation_cards.reason_long 은 NOT NULL 이라 카드를 저장하다
500 이 난다. 채점이 아니라 "BE 를 거쳐도 깨지지 않는가"만 본다.

로그인이 카카오뿐이라 토큰은 사람이 받아 넣는다(README "BE 를 거치는 점검"). 토큰은 1시간 산다.
보낸 문장마다 dev DB 에 추천 카드가 저장된다(평소 쓰는 것과 같다).

사용:
    BE_ACCESS_TOKEN=eyJ… uv run python -m evals.chat.smoke_be
    BE_ACCESS_TOKEN=eyJ… uv run python -m evals.chat.smoke_be --base-url https://dev.bookjeok.site --out 결과.json
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from evals.chat.run import INITIAL_SPEC

CHAT_PATH = "/api/v1/recommend/chat"
# 찾기 문장. 책소개가 없는 판본이 많은 책(토지)과 10/19 dev 에서 실패한 문장(클린 코드, 김영하)을 넣는다.
MESSAGES = (
    "클린 코드 있어?",
    "김영하 소설 추천해줘",
    "토지 찾아줘",
    "데미안",
    "한강 책 보여줘",
)
# BE 가 AI 를 30초까지 기다린다. 그보다 넉넉히.
REQUEST_TIMEOUT_SECONDS = 60


def build_body(message: str) -> dict:
    """새 대화의 첫 턴. BE 요청은 camelCase 이고 spec 의 6개 키를 다 보내야 400 이 아니다."""
    return {
        "consented": False,
        "spec": INITIAL_SPEC,
        "message": message,
        "recentTurns": [],
        "excludeBookIds": [],
    }


def _post(url: str, token: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200].decode("utf-8", "replace")}
    except (urllib.error.URLError, TimeoutError) as e:
        return 0, {"raw": repr(e)[:200]}


def read_result(message: str, status: int, payload: dict, seconds: float) -> dict:
    """응답 하나를 한 줄 기록으로. 200 이면 카드 수와 긴 이유가 빈 카드 수를 남긴다."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    cards = data.get("cards") or []
    return {
        "message": message,
        "status": status,
        "sec": round(seconds, 1),
        "cards": len(cards),
        "titles": [c.get("title") for c in cards],
        "null_reason_long": sum(not c.get("reasonLong") for c in cards),
        "error": None if status == 200 else payload,
    }


def summarize(results: list[dict]) -> dict:
    return {
        "sent": len(results),
        "ok": sum(r["status"] == 200 for r in results),
        "server_errors": sum(r["status"] >= 500 for r in results),
        "unauthorized": sum(r["status"] == 401 for r in results),
        "unreachable": sum(r["status"] == 0 for r in results),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="③ 챗봇 BE 경유 점검")
    parser.add_argument("--base-url", default="https://dev.bookjeok.site")
    parser.add_argument("--out", help="결과를 저장할 JSON 경로(저장소 밖)")
    args = parser.parse_args()
    token = os.environ.get("BE_ACCESS_TOKEN", "").removeprefix("Bearer ").strip()
    if not token:
        parser.error("BE_ACCESS_TOKEN 이 필요하다(README 'BE 를 거치는 점검')")

    results = []
    for message in MESSAGES:
        started = time.monotonic()
        status, payload = _post(
            f"{args.base_url}{CHAT_PATH}", token, build_body(message)
        )
        result = read_result(message, status, payload, time.monotonic() - started)
        results.append(result)
        print(
            f"{status} {result['sec']:>5}초 카드 {result['cards']}장"
            f" (긴 이유 빈 카드 {result['null_reason_long']})  {message}",
            flush=True,
        )
        if status == 401:
            parser.error("401 — 토큰이 틀렸거나 1시간이 지났다. 다시 받아 넣는다")

    summary = summarize(results)
    print(
        f"\n보냄 {summary['sent']} · 200 {summary['ok']} · 5xx {summary['server_errors']}"
        f" · 닿지 못함 {summary['unreachable']}"
    )
    if args.out:
        meta = {
            "base_url": args.base_url,
            "at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        }
        Path(args.out).write_text(
            json.dumps(
                {"meta": meta, "summary": summary, "results": results},
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
    if summary["ok"] != summary["sent"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
