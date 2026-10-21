"""③ 대화로 좁혀 가기 평가(#530) — 원하는 것을 숨긴 가짜 사용자가 챗봇과 대화한다.

가짜 사용자(LLM)는 scenarios.json 의 첫마디로 시작하고, 원하는 것(want)은 자기만 안다. 챗봇이 물으면 그에 맞게
답하고, 카드가 안 맞으면 안 맞는 점을 하나씩 말한다. 카드 중 원하는 것에 맞는 책이 있으면 골라 끝낸다. 챗봇 응답은
5번까지 받는다. 끝나면 채점 LLM 이 고른 책이 must 조건에 맞는지 본다(채점은 grade.py 가 원본만으로 다시 할 수
있게 판정을 원본에 같이 남긴다).

BE 대화방 경로가 AI 서버를 부르는 방식을 따른다(BE main 07b2bdd 기준): 직전 응답의 spec 을 되돌려 보내고, 최근
대화에 챗봇 답(reply + followup, 뒤쪽 200자)을 싣고, 쇼핑 칸(멱등 키, 마지막으로 카드가 보인 묶음)을 싣는다.
그래서 추천·쇼핑 문지기도 켜진다.

LLM 을 실제로 부르므로 비용이 든다. CI 에서는 돌리지 않는다.

사용:
    DATABASE_URL=postgresql://…/bookjeok_ai_eval EXTERNAL_AI_API_KEY=… \\
    uv run python -m evals.chat_dialog.run --base-url http://127.0.0.1:8001 --runs 3 --out 원본.json
"""

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import httpx

from app.core.products import price_sql

HERE = Path(__file__).parent
INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}
EVAL_USER_ID = 999_999
MAX_BOT_TURNS = 5
MAX_RECENT_TURNS = 20
MAX_TURN_CHARS = 200
REQUEST_TIMEOUT_SECONDS = 90
SIM_DESCRIPTION_CHARS = 200
JUDGE_DESCRIPTION_CHARS = 1000

SIMULATOR_PROMPT = """너는 온라인 서점의 책 추천 챗봇을 쓰는 사람이다. 아래 '원하는 것'은 너만 알고 챗봇은 모른다.

원하는 것: {want}
{ask_rule}
지금까지 대화:
{dialog}

지금 화면에 보이는 책 카드:
{cards}

규칙:
- 사람처럼 짧게 말한다(한 문장, 40자 안). 편한 말투로.
- 원하는 것을 한꺼번에 다 말하지 않는다. 챗봇이 물으면 그 물음에 대한 것만 답하고, 카드가 안 맞으면 안 맞는 점을 하나만 말한다.
- 챗봇이 카드 번호를 들어 물으면 번호로 답해도 된다.
- 카드 중 원하는 것에 모두 맞는 책이 있으면 그 책을 고르고 끝낸다. 카드 소개를 보고 판단한다.
- 장바구니에 담거나 사 달라고 하지 않는다.
- 앞에서 한 말을 그대로 되풀이하지 않는다.

JSON 으로만 답하라: {{"done": 끝낼지 true/false, "pick": 고른 카드 번호 또는 null, "message": 챗봇에게 보낼 다음 말(끝내면 null), "asked_about_card": 이번 말이 카드에 대해 묻는 말이면 true}}"""

ASK_RULE = (
    "\n원하는 것에 맞아 보이는 카드가 처음 보이면, 고르기 전에 그 카드를 번호로 가리켜 "
    "'{ask}' 한 번 물어본다. 챗봇 답을 보고 고른다. 이미 물었으면 다시 묻지 않는다.\n"
)
FINAL_RULE = "\n챗봇과 더 대화할 수 없다. 지금 보이는 카드 중에서 고를지만 정한다(message 는 null).\n"

JUDGE_PROMPT = """사용자가 서점 챗봇에서 책 한 권을 골랐다. 이 책이 사용자가 원한 조건에 맞는지 판단하라.

사용자가 원한 것: {want}
꼭 맞아야 하는 조건:
{must}

고른 책: 『{title}』 {author}
책 소개: {description}

조건마다 제목과 책 소개로 판단하라. 소개로 알 수 없으면 맞지 않은 것으로 본다.
JSON 으로만 답하라: {{"met": [조건 차례대로 true/false], "reason": "한 문장"}}"""


def load_scenarios(path: Path = HERE / "scenarios.json") -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["scenarios"]


class Llm:
    """OpenRouter(OpenAI 호환) 채팅 호출. 키는 환경변수에서만 읽고 어디에도 남기지 않는다."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._base = os.environ.get(
            "LLM_BASE_URL", "https://openrouter.ai/api/v1"
        ).rstrip("/")
        self._key = os.environ["EXTERNAL_AI_API_KEY"]

    async def json(self, model: str, prompt: str, temperature: float) -> dict:
        for attempt in range(3):
            try:
                res = await self._client.post(
                    f"{self._base}/chat/completions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={
                        "model": model,
                        "temperature": temperature,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=60,
                )
                res.raise_for_status()
                return json.loads(res.json()["choices"][0]["message"]["content"])
            except (httpx.HTTPError, ValueError, KeyError):
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError("unreachable")


async def fetch_books(pool: asyncpg.Pool, book_ids: list[int]) -> dict[int, dict]:
    if not book_ids:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT b.book_id, b.title, b.author, b.description, {price_sql("b")} AS price
            FROM v_books b
            WHERE b.book_id = ANY($1::int[])
            """,
            book_ids,
        )
    return {r["book_id"]: dict(r) for r in rows}


def _card_lines(cards: list[dict], books: dict[int, dict]) -> str:
    if not cards:
        return "(없음)"
    lines = []
    for number, card in enumerate(cards, start=1):
        book = books.get(card["book_id"], {})
        about = " ".join((book.get("description") or "").split())[
            :SIM_DESCRIPTION_CHARS
        ]
        lines.append(
            f"{number}번: 『{card['title']}』 {card.get('author') or ''} · "
            f"{card.get('price') or '?'}원 · 한 줄 이유: {card.get('reason_short') or ''} · "
            f"소개: {about or '(소개 없음)'}"
        )
    return "\n".join(lines)


def _dialog_lines(turns: list[dict]) -> str:
    lines = []
    for turn in turns:
        lines.append(f"나: {turn['msg']}")
        said = turn.get("reply") or ""
        if turn.get("followup"):
            said += " " + turn["followup"]
        titles = ", ".join(
            f"{i}. {c['title']}" for i, c in enumerate(turn["cards"], start=1)
        )
        lines.append(f"챗봇: {said}" + (f" [카드: {titles}]" if titles else ""))
    return "\n".join(lines)


def _body(
    message: str,
    spec: dict,
    recent: list[dict],
    shown: list[int],
    visible: list[dict],
    conv: str,
) -> dict:
    """BE 대화방 경로가 보내는 요청(쇼핑 칸 포함)."""
    context = [
        {"isbn": c["isbn"], "rank": i, "match_score": c.get("match_score") or 0}
        for i, c in enumerate(visible[:10], start=1)
        if c.get("isbn")
    ]
    offers = [
        {
            "isbn": c["isbn"],
            "title": c["title"][:500],
            "price": c["price"],
            "in_stock": True,
        }
        for c in visible[:10]
        if c.get("isbn") and isinstance(c.get("price"), int)
    ]
    return {
        "user_id": EVAL_USER_ID,
        "consented": False,
        "spec": spec,
        "message": message,
        "recent_turns": recent[-MAX_RECENT_TURNS:],
        "exclude_book_ids": shown,
        "idempotency_key": str(uuid.uuid4()),
        "conversation_id": conv,
        "user_context": {"logged_in": True},
        "context_cards": context,
        "cart": [],
        "offers": offers,
    }


async def run_dialog(
    scenario: dict,
    run_no: int,
    *,
    server: httpx.AsyncClient,
    base_url: str,
    token: str,
    llm: Llm,
    pool: asyncpg.Pool,
    sim_model: str,
    judge_model: str,
) -> dict:
    spec, recent, shown, visible, turns = INITIAL_SPEC, [], [], [], []
    books: dict[int, dict] = {}
    message, asked_about_card = scenario["opening"], False
    done, pick, error = False, None, None
    ask_rule = (
        ASK_RULE.format(ask=scenario["ask_about_card"])
        if scenario.get("ask_about_card")
        else "\n"
    )
    conv = f"eval-{scenario['no']}-{run_no}-{uuid.uuid4().hex[:8]}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for bot_turn in range(1, MAX_BOT_TURNS + 1):
        started = time.monotonic()
        try:
            res = await server.post(
                f"{base_url}/recommendations/chat",
                json=_body(message, spec, recent, shown, visible, conv),
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            status, payload = res.status_code, res.json()
        except (httpx.HTTPError, ValueError) as e:
            status, payload = 0, {"message": repr(e)[:200]}
        data = payload.get("data") or {}
        cards = [
            {
                k: c.get(k)
                for k in (
                    "book_id",
                    "isbn",
                    "match_score",
                    "title",
                    "author",
                    "price",
                    "reason_short",
                )
            }
            for c in data.get("cards") or []
        ]
        turns.append(
            {
                "msg": message,
                "asked_about_card": asked_about_card,
                "status": status,
                "sec": round(time.monotonic() - started, 2),
                "route": data.get("route"),
                "reply": data.get("reply"),
                "followup": data.get("followup"),
                "degraded": data.get("degraded"),
                "spec": data.get("spec"),
                "cards": cards,
                "error": None if status == 200 else payload.get("message"),
            }
        )
        if status != 200:
            error = f"status {status}"
            break
        if data.get("spec"):
            spec = data["spec"]
        said = (data.get("reply") or "") + (
            "\n" + data["followup"] if data.get("followup") else ""
        )
        recent += [
            {"role": "user", "text": message[-MAX_TURN_CHARS:]},
            {"role": "assistant", "text": said[-MAX_TURN_CHARS:]},
        ]
        if cards:
            visible = cards
            shown = list(dict.fromkeys(shown + [c["book_id"] for c in cards]))
            books.update(await fetch_books(pool, [c["book_id"] for c in cards]))
        final = bot_turn == MAX_BOT_TURNS
        prompt = SIMULATOR_PROMPT.format(
            want=scenario["want"],
            ask_rule=ask_rule + (FINAL_RULE if final else ""),
            dialog=_dialog_lines(turns),
            cards=_card_lines(visible, books)
            + (
                ""
                if cards or not visible
                else "\n(이번 답에는 새 카드가 없어 직전 카드가 그대로 보인다)"
            ),
        )
        try:
            sim = await llm.json(sim_model, prompt, temperature=0.7)
        except Exception as e:  # noqa: BLE001 — 가짜 사용자 실패는 그 대화만 실패로 남긴다
            error = f"simulator: {type(e).__name__}"
            break
        pick_no = sim.get("pick")
        if (
            sim.get("done")
            and isinstance(pick_no, int)
            and 1 <= pick_no <= len(visible)
        ):
            done, pick = True, visible[pick_no - 1]
            break
        next_message = sim.get("message")
        if final or not isinstance(next_message, str) or not next_message.strip():
            break
        message = next_message.strip()[:200]
        asked_about_card = bool(sim.get("asked_about_card"))

    judge = None
    if done and pick:
        book = books.get(pick["book_id"], {})
        must = "\n".join(f"{i}. {m}" for i, m in enumerate(scenario["must"], start=1))
        try:
            judge = await llm.json(
                judge_model,
                JUDGE_PROMPT.format(
                    want=scenario["want"],
                    must=must,
                    title=pick["title"],
                    author=pick.get("author") or "",
                    description=(book.get("description") or "(소개 없음)")[
                        :JUDGE_DESCRIPTION_CHARS
                    ],
                ),
                temperature=0,
            )
        except Exception as e:  # noqa: BLE001
            judge = {"error": type(e).__name__}
    return {
        "scenario": scenario["no"],
        "run": run_no,
        "turns": turns,
        "done": done,
        "pick": pick,
        "judge": judge,
        "error": error,
    }


async def main_async(args: argparse.Namespace) -> None:
    scenarios = load_scenarios()
    if args.only:
        wanted = {int(n) for n in args.only.split(",")}
        scenarios = [s for s in scenarios if s["no"] in wanted]
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    semaphore = asyncio.Semaphore(args.workers)
    async with httpx.AsyncClient() as client:
        llm = Llm(client)

        async def _one(scenario: dict, run_no: int) -> dict:
            async with semaphore:
                result = await run_dialog(
                    scenario,
                    run_no,
                    server=client,
                    base_url=args.base_url.rstrip("/"),
                    token=os.environ.get("AI_SERVICE_TOKEN", ""),
                    llm=llm,
                    pool=pool,
                    sim_model=args.sim_model,
                    judge_model=args.judge_model,
                )
                print(
                    f"시나리오 {scenario['no']} 회차 {run_no}: "
                    f"{'고름' if result['done'] else '못 고름'} · 턴 {len(result['turns'])}",
                    flush=True,
                )
                return result

        started = datetime.now(ZoneInfo("Asia/Seoul"))
        results = await asyncio.gather(
            *(_one(s, r) for r in range(1, args.runs + 1) for s in scenarios)
        )
    await pool.close()
    secs = [t["sec"] for r in results for t in r["turns"] if t["status"] == 200]
    meta = {
        "server": args.base_url,
        "note": args.note,
        "sim_model": args.sim_model,
        "judge_model": args.judge_model,
        "runs": args.runs,
        "max_bot_turns": MAX_BOT_TURNS,
        "started_at": started.isoformat(timespec="seconds"),
        "median_sec": statistics.median(secs) if secs else None,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"meta": meta, "results": results}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"저장: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--only", help="시나리오 번호, 쉼표로")
    parser.add_argument("--sim-model", default="openai/gpt-4o-mini")
    parser.add_argument("--judge-model", default="openai/gpt-4o")
    parser.add_argument("--note", default="")
    parser.add_argument("--out", required=True)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
