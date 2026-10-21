"""대화 중 보여 준 책마다 must 조건에 맞는지 채점 LLM 으로 판정해 원본에 더한다(#530). 서버는 다시 부르지 않는다.

가짜 사용자가 "1번이 제일 마음에 들어"라고 하면서도 고르지 않고 맴도는 일이 있어서, 가짜 사용자가 골랐는지(done)
만으로는 챗봇이 맞는 책에 닿았는지 알 수 없다. 그래서 챗봇이 보여 준 책 자체를 판정한다. 같은 시나리오에서 같은
책은 한 번만 판정한다.

사용:
    DATABASE_URL=… EXTERNAL_AI_API_KEY=… uv run python -m evals.chat_dialog.reach 원본.json [원본2.json …]
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg
import httpx

from evals.chat_dialog.run import (
    JUDGE_DESCRIPTION_CHARS,
    JUDGE_PROMPT,
    Llm,
    fetch_books,
    load_scenarios,
)


async def main_async(paths: list[str], judge_model: str, workers: int) -> None:
    scenarios = {s["no"]: s for s in load_scenarios()}
    raws = {p: json.loads(Path(p).read_text(encoding="utf-8")) for p in paths}
    pairs = sorted(
        {
            (r["scenario"], c["book_id"])
            for raw in raws.values()
            for r in raw["results"]
            for t in r["turns"]
            for c in t["cards"]
        }
    )
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    books = await fetch_books(pool, sorted({b for _, b in pairs}))
    await pool.close()
    semaphore = asyncio.Semaphore(workers)
    verdicts: dict[tuple[int, int], dict] = {}
    async with httpx.AsyncClient() as client:
        llm = Llm(client)

        async def _judge(scenario_no: int, book_id: int) -> None:
            scenario, book = scenarios[scenario_no], books.get(book_id, {})
            must = "\n".join(
                f"{i}. {m}" for i, m in enumerate(scenario["must"], start=1)
            )
            async with semaphore:
                try:
                    verdicts[(scenario_no, book_id)] = await llm.json(
                        judge_model,
                        JUDGE_PROMPT.format(
                            want=scenario["want"],
                            must=must,
                            title=book.get("title") or "",
                            author=book.get("author") or "",
                            description=(book.get("description") or "(소개 없음)")[
                                :JUDGE_DESCRIPTION_CHARS
                            ],
                        ),
                        temperature=0,
                    )
                except Exception as e:  # noqa: BLE001 — 판정 실패는 맞지 않은 것으로 센다
                    verdicts[(scenario_no, book_id)] = {"error": type(e).__name__}

        await asyncio.gather(*(_judge(s, b) for s, b in pairs))
    for path, raw in raws.items():
        raw["reach"] = {
            "judge_model": judge_model,
            "verdicts": [
                {"scenario": s, "book_id": b, **verdicts[(s, b)]}
                for s, b in pairs
                if any(r["scenario"] == s for r in raw["results"])
            ],
        }
        Path(path).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        print(f"판정을 더함: {path} ({len(raw['reach']['verdicts'])}권)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("raws", nargs="+")
    parser.add_argument("--judge-model", default="openai/gpt-4o")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    asyncio.run(main_async(args.raws, args.judge_model, args.workers))


if __name__ == "__main__":
    main()
