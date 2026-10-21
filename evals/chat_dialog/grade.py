"""③ 대화로 좁혀 가기 평가 채점(#530). run.py 원본만 읽고 LLM 은 부르지 않는다.

사용:
    uv run python -m evals.chat_dialog.grade 원본.json
    uv run python -m evals.chat_dialog.grade --compare 전.json 후.json
"""

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from evals.chat_dialog.run import load_scenarios

# 말풍선이 "추천합니다"류로 끝나는가(10/19 평가에서 70%)
_RECOMMEND_ENDING = re.compile(
    r"추천(합니다|드립니다|드려요|해요|합니다!|드립니다!|해요!)|추천됩니다"
)
_PAREN = re.compile(r"\([^)]*\)\s*|\[[^\]]*\]\s*")


def succeeded(result: dict, must_count: int) -> bool:
    """가짜 사용자가 책을 고르고, 채점 LLM 이 must 조건을 모두 맞았다고 본 대화."""
    judge = result.get("judge") or {}
    met = judge.get("met")
    return (
        bool(result.get("done"))
        and isinstance(met, list)
        and len(met) == must_count
        and all(m is True for m in met)
    )


def _said(turn: dict) -> str:
    return (turn.get("reply") or "") + " " + (turn.get("followup") or "")


def asks(turn: dict) -> bool:
    """챗봇이 이 턴에 무언가 물었는가."""
    return "?" in _said(turn)


def question_of(turn: dict) -> str | None:
    """이 턴에 물은 문장(물음표로 끝나는 마지막 문장). 같은 질문 반복을 세는 데 쓴다."""
    found = re.findall(r"[^.!?\n]*\?", _said(turn))
    return found[-1].strip() if found else None


def lists_titles(turn: dict) -> bool:
    """말풍선이 카드에 보이는 제목을 다시 늘어놓는가(제목 앞 여섯 글자가 말풍선에 있음)."""
    reply = turn.get("reply") or ""
    for card in turn.get("cards") or []:
        key = _PAREN.sub("", card.get("title") or "").strip()[:6]
        if len(key) >= 2 and key in reply:
            return True
    return False


def ends_with_recommend(turn: dict) -> bool:
    return bool(_RECOMMEND_ENDING.search(turn.get("reply") or ""))


def reached_turn(
    result: dict, verdicts: dict[tuple[int, int], dict], must_count: int
) -> int | None:
    """챗봇이 must 를 모두 맞는 책을 처음 보여 준 응답 차례(1부터). 못 보여 줬으면 None. reach.py 판정이 있어야 한다."""
    for number, turn in enumerate(result["turns"], start=1):
        for card in turn["cards"]:
            met = verdicts.get((result["scenario"], card["book_id"]), {}).get("met")
            if (
                isinstance(met, list)
                and len(met) == must_count
                and all(m is True for m in met)
            ):
                return number
    return None


def summarize(raw: dict) -> dict:
    must_counts = {s["no"]: len(s["must"]) for s in load_scenarios()}
    results = raw["results"]
    runs = sorted({r["run"] for r in results})
    ok = [succeeded(r, must_counts[r["scenario"]]) for r in results]
    by_run = {
        run: sum(o for r, o in zip(results, ok) if r["run"] == run) for run in runs
    }
    by_scenario: Counter = Counter()
    for r, o in zip(results, ok):
        by_scenario[r["scenario"]] += o
    to_success = [len(r["turns"]) for r, o in zip(results, ok) if o]

    turns = [t for r in results for t in r["turns"] if t["status"] == 200]
    card_turns = [t for t in turns if t["cards"]]
    asked_convs = sum(1 for r in results if any(asks(t) for t in r["turns"]))
    repeated = 0
    for r in results:
        questions = [q for t in r["turns"] if (q := question_of(t))]
        repeated += len(questions) - len(set(questions))
    card_questions = [
        t for r in results for t in r["turns"] if t.get("asked_about_card")
    ]
    answered = sum(
        1
        for t in card_questions
        if t["status"] == 200 and not t["cards"] and t.get("route") != "shop"
    )
    secs = [t["sec"] for t in turns]
    reach = {}
    if raw.get("reach"):
        verdicts = {(v["scenario"], v["book_id"]): v for v in raw["reach"]["verdicts"]}
        reached = [
            reached_turn(r, verdicts, must_counts[r["scenario"]]) for r in results
        ]
        hit = [n for n in reached if n is not None]
        reach_by_run = {
            run: sum(1 for r, n in zip(results, reached) if r["run"] == run and n)
            for run in runs
        }
        reach_by_scenario: Counter = Counter()
        for r, n in zip(results, reached):
            reach_by_scenario[r["scenario"]] += n is not None
        reach = {
            "reached": len(hit),
            "reach_rate": round(100 * len(hit) / len(results), 1) if results else 0.0,
            "reach_by_run": reach_by_run,
            "reach_by_scenario": dict(sorted(reach_by_scenario.items())),
            "turns_to_reach_mean": round(statistics.mean(hit), 2) if hit else None,
            "reached_on_first_turn": sum(1 for n in hit if n == 1),
        }
    return {
        **reach,
        "meta": raw.get("meta"),
        "conversations": len(results),
        "success": sum(ok),
        "success_rate": round(100 * sum(ok) / len(results), 1) if results else 0.0,
        "success_by_run": by_run,
        "success_by_scenario": dict(sorted(by_scenario.items())),
        "turns_to_success_mean": round(statistics.mean(to_success), 2)
        if to_success
        else None,
        "picked_but_wrong": sum(
            1 for r, o in zip(results, ok) if r.get("done") and not o
        ),
        "not_picked": sum(1 for r in results if not r.get("done")),
        "errors": sum(1 for r in results if r.get("error")),
        "bot_turns": len(turns),
        "convs_where_bot_asked": asked_convs,
        "repeated_questions": repeated,
        "card_turns": len(card_turns),
        "reply_lists_titles_pct": _pct(
            sum(lists_titles(t) for t in card_turns), len(card_turns)
        ),
        "reply_ends_recommend_pct": _pct(
            sum(ends_with_recommend(t) for t in card_turns), len(card_turns)
        ),
        "card_questions": len(card_questions),
        "card_questions_answered_without_new_cards": answered,
        "shop_route_turns": sum(1 for t in turns if t.get("route") == "shop"),
        "degraded_turns": sum(1 for t in turns if t.get("degraded")),
        "median_sec": statistics.median(secs) if secs else None,
    }


def _pct(n: int, total: int) -> float:
    return round(100 * n / total, 1) if total else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("raw", nargs="?")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args()
    if args.compare:
        before, after = (
            summarize(json.loads(Path(p).read_text(encoding="utf-8")))
            for p in args.compare
        )
        for key in before:
            if key != "meta":
                print(f"{key}: {before[key]} → {after[key]}")
        return
    print(
        json.dumps(
            summarize(json.loads(Path(args.raw).read_text(encoding="utf-8"))),
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
