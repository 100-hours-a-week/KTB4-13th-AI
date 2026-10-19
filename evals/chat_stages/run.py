"""③ 챗봇이 맞는 책을 어느 단계에서 잃는지 잰다.

    # 1. 서술형 검색어 40개를 ③에 첫 턴으로 넣고, 실험마다 단계별로 남은 책을 남긴다(LLM 을 부른다)
    LLM_PROVIDER=eval-no-zdr DATABASE_URL=postgresql://localhost/bookjeok_ai_eval \\
      uv run python -m evals.chat_stages.run collect --out evals/chat_stages/results/run.json
    # 2. 점수 없는 책을 매긴다(label.py). 3. 표를 낸다(DB·LLM 을 부르지 않는다)
    uv run python -m evals.chat_stages.run report evals/chat_stages/results/run.json

단계는 ③ 의 get_candidates 순서다: ① 검색 30권 → 사후 거르기(가진 책·분야·출판사·소개글 없는 책·겹치는
판본) → 10권 후보 → LLM 이 고른 카드 3장. 책의 점수는 ① 검색 평가의 서술형 라벨(맞음 2·조금 1·아님 0,
evals/search/labels.json)과 여기서 더 매긴 라벨(labels.json)을 쓴다.

조건 문장(spec)은 검색어마다 한 번만 뽑아 세 실험에 같이 쓴다. 실험의 차이만 보려는 것이다. 카드는 같은 후보로
--runs 번 다시 만든다. 앱의 /recommendations/chat 을 부르지 않고 그 안의 단계를 같은 순서로 직접 부른다
(chat._recommend). 그래야 단계마다의 목록을 꺼낼 수 있다. 사용자는 이력이 없는 평가 사용자 하나다.
"""

import argparse
import asyncio
import json
import math
import statistics
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

from evals.chat_stages import variants

HERE = Path(__file__).parent
LABELS_PATH = HERE / "labels.json"
# 서술형 검색어 묶음과 라벨(#502). 다른 묶음은 --search-dir 로 준다
SEARCH_DIR = Path("evals/search")
# 원문 검색어로 ① 을 부를 때 받는 권수(① 검색 평가와 같은 조건)
RAW_TOP = 20
TOP = 10
CARD_LIMIT = 3
# 후보 고르기 시간을 실험마다 이만큼 재서 가운데값을 낸다
REPEATS = 3


def load_cases(search_dir: Path) -> list[dict]:
    bundle = json.loads((search_dir / "cases.json").read_text(encoding="utf-8"))
    return [c for c in bundle["cases"] if c["kind"] == "descriptive"]


def load_labels(search_dir: Path) -> dict[str, dict[str, int]]:
    """사례 번호 → ISBN → 점수. ① 의 라벨에 여기서 더 매긴 라벨을 얹는다(겹치면 ① 의 것을 쓴다)."""
    merged: dict[str, dict[str, int]] = {}
    for path in (LABELS_PATH, search_dir / "labels.json"):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for case_id, marks in data["labels"].items():
                merged.setdefault(case_id, {}).update(marks)
    return merged


class _Tap:
    """chat.py 가 부르는 검색과 판본 걷어 내기 자리에 끼워, 단계마다의 책 번호를 남긴다."""

    def __init__(self) -> None:
        from app.routers import chat

        self.chat = chat
        self.trace: dict[str, Any] = {}
        self.inner = chat.service
        self.dedupe = chat._dedupe_editions
        chat.service = self
        chat._dedupe_editions = self._dedupe

    async def search(self, req):
        outcome = await self.inner.search(req)
        self.trace["search"] = [r["book_id"] for r in outcome.results]
        self.trace["degraded"] = outcome.degraded
        return outcome

    def _dedupe(self, pool):
        kept = self.dedupe(pool)
        # get_candidates 는 이 결과를 CANDIDATE_LIMIT 로 자른다. 자르기 직전의 목록이다
        self.trace["filtered"] = [c["book_id"] for c in kept]
        return kept

    def remove(self) -> None:
        self.chat.service = self.inner
        self.chat._dedupe_editions = self.dedupe


async def _case(chat, case: dict, runs: int, order: Sequence[str]) -> dict:
    from app.search import service
    from app.search.schemas import MAX_QUERY_CHARS, SearchRequest
    from evals.chat.run import EVAL_USER_ID, INITIAL_SPEC

    message = case["query"]
    req = chat.parse_request(
        {
            "user_id": EVAL_USER_ID,
            "consented": False,
            "spec": INITIAL_SPEC,
            "message": message,
        }
    )
    row: dict[str, Any] = {"id": case["id"], "facet": case["facet"], "query": message}
    raw = await service.search(
        SearchRequest(query=message[:MAX_QUERY_CHARS], size=RAW_TOP)
    )
    row["raw_search"] = [r["book_id"] for r in raw.results]
    if chat._unanswerable_request(req.message, req.spec, req.recent_turns):
        row["unanswerable"] = True
        return row

    started = perf_counter()
    spec, spec_degraded = await chat.update_spec(
        req.message, req.spec, req.recent_turns
    )
    spec = await chat._settle_category(spec)
    spec = chat._recover_topic(spec, req.message)
    row.update(
        spec=spec.model_dump(),
        spec_degraded=spec_degraded,
        spec_seconds=perf_counter() - started,
        variants={},
    )
    for name in order:
        undo = variants.install(name)
        tap = _Tap()
        try:
            seconds, stages = [], None
            for _ in range(REPEATS):
                tap.trace = {}
                started = perf_counter()
                candidates = await chat.get_candidates(
                    spec, [], EVAL_USER_ID, owned_book_ids=None
                )
                seconds.append(perf_counter() - started)
                picked = [c["book_id"] for c in candidates]
                if stages is None:
                    stages, kept = {**tap.trace, "candidates": picked}, candidates
                elif picked != stages["candidates"]:
                    stages["unstable"] = True
            cards = []
            for _ in range(runs):
                started = perf_counter()
                got, _, degraded = await chat.generate_cards(kept, spec)
                cards.append(
                    {
                        "book_ids": [c["book_id"] for c in got],
                        "seconds": perf_counter() - started,
                        "degraded": degraded,
                    }
                )
            row["variants"][name] = {
                **stages,
                "candidate_seconds": seconds,
                "match_scores": [c["match_score"] for c in kept],
                "cards": cards,
            }
        finally:
            tap.remove()
            undo()
    return row


async def _isbns(book_ids: set[int]) -> dict[int, str | None]:
    from app.core import db

    async with db.get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id, isbn13, coalesce(description, '') <> '' AS has_intro "
            "FROM v_books WHERE book_id = ANY($1::int[])",
            sorted(book_ids),
        )
    return {r["book_id"]: {"isbn": r["isbn13"], "intro": r["has_intro"]} for r in rows}


async def collect(cases: list[dict], runs: int) -> dict:
    from app.core.config import get_settings
    from app.main import app
    from app.routers import chat
    from app.search import service
    from app.search.schemas import SearchRequest

    rows = []
    async with app.router.lifespan_context(app):
        # 첫 검색은 DB 연결과 임베딩 모델을 여느라 느리다. 재는 시간에 섞이지 않게 한 번 불러 둔다
        await service.search(SearchRequest(query="책", size=1))
        for index, case in enumerate(cases):
            # 실험의 순서를 돌려 가며 잰다. 늘 먼저 도는 쪽만 캐시가 식은 채로 재지 않게 한다
            shift = index % len(variants.VARIANTS)
            order = variants.VARIANTS[shift:] + variants.VARIANTS[:shift]
            rows.append(await _case(chat, case, runs, order))
            print(
                f"{index + 1}/{len(cases)} {case['query']}", file=sys.stderr, flush=True
            )
        seen = {i for row in rows for i in _all_ids(row)}
        books = await _isbns(seen)
        settings = get_settings()
    return {
        "made": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        "model": settings.llm_model_id,
        "provider": settings.llm_provider,
        "runs": runs,
        "repeats": REPEATS,
        "cases": rows,
        "books": {str(k): v for k, v in books.items()},
    }


def _all_ids(row: Mapping[str, Any]) -> set[int]:
    ids = set(row.get("raw_search", []))
    for v in row.get("variants", {}).values():
        ids |= (
            set(v.get("search", [])) | set(v.get("filtered", [])) | set(v["candidates"])
        )
        for card in v["cards"]:
            ids |= set(card["book_ids"])
    return ids


# --- 채점 ---------------------------------------------------------------------------------------------


def gain(grade: int) -> int:
    return 2**grade - 1


def ndcg(grades: Sequence[int | None]) -> float:
    """위 10권 nDCG. 이상값은 10권이 모두 2점인 순서로 고정한다(evals/search/scoring.py 와 같은 식)."""
    dcg = sum(gain(g or 0) / math.log2(r + 1) for r, g in enumerate(grades[:TOP], 1))
    return dcg / sum(gain(2) / math.log2(r + 1) for r in range(1, TOP + 1))


def _p90(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]


def score(raw: Mapping[str, Any], labels: Mapping[str, Mapping[str, int]]) -> dict:
    """실험마다 단계별 숫자. 검색어마다 낸 값을 평균한다. 점수 없는 책은 0점으로 세고 따로 센다."""
    books = raw["books"]

    def grades(case_id: str, ids: Sequence[int]) -> list[int | None]:
        marks = labels.get(case_id, {})
        return [marks.get((books.get(str(i)) or {}).get("isbn")) for i in ids]

    answered = [row for row in raw["cases"] if "variants" in row]
    names = [n for n in variants.VARIANTS if all(n in r["variants"] for r in answered)]
    out: dict[str, Any] = {
        "cases": len(raw["cases"]),
        "answered": len(answered),
        "exact": sum(r["spec"]["intent"] == "exact" for r in answered),
        "raw_ndcg": statistics.mean(
            ndcg(grades(r["id"], r["raw_search"])) for r in raw["cases"]
        ),
        "variants": {},
    }
    for name in names:
        per: dict[str, list[float]] = {}
        unlabeled = 0
        cand_secs, card_secs, turn_secs = [], [], []
        facets: dict[str, list[float]] = {}
        for row in answered:
            v = row["variants"][name]
            cid = row["id"]
            stage = {
                "search": grades(cid, v["search"]),
                "filtered": grades(cid, v["filtered"]),
                "candidates": grades(cid, v["candidates"]),
            }
            # 거르기·후보·카드는 모두 ① 30권 안의 책이라 30권에서만 센다
            unlabeled += sum(g is None for g in stage["search"])
            for key, gs in stage.items():
                per.setdefault(f"{key}_n", []).append(len(gs))
                per.setdefault(f"{key}_rel", []).append(sum(g == 2 for g in gs))
            lost_intro = [
                g == 2
                for i, g in zip(v["search"], stage["search"], strict=True)
                if i not in v["filtered"] and not books[str(i)]["intro"]
            ]
            per.setdefault("lost_no_intro_rel", []).append(sum(lost_intro))
            per.setdefault("search_ndcg", []).append(ndcg(stage["search"]))
            per.setdefault("candidates_ndcg", []).append(ndcg(stage["candidates"]))
            per.setdefault("reachable_rel", []).append(
                min(TOP, per["filtered_rel"][-1])
            )
            facets.setdefault(row["facet"], []).append(per["candidates_ndcg"][-1])
            card_grades, card_rel, zero = [], [], 0
            for card in v["cards"]:
                gs = grades(cid, card["book_ids"])
                card_grades += [g or 0 for g in gs]
                card_rel.append(sum(g == 2 for g in gs))
                zero += not gs
                card_secs.append(card["seconds"])
            per.setdefault("card_grade", []).append(
                statistics.mean(card_grades) if card_grades else 0.0
            )
            per.setdefault("card_rel", []).append(statistics.mean(card_rel))
            # 10권에 2점 책이 있었는데 카드에 넣지 않은 수. 카드는 3장까지라 3권까지만 센다
            can = min(CARD_LIMIT, per["candidates_rel"][-1])
            per.setdefault("card_missed", []).append(
                statistics.mean(max(0, can - n) for n in card_rel)
            )
            per.setdefault("zero_cards", []).append(zero)
            per.setdefault("degraded_cards", []).append(
                sum(c["degraded"] for c in v["cards"])
            )
            per.setdefault("search_degraded", []).append(v.get("degraded") is not None)
            cand_secs += v["candidate_seconds"]
            mid = statistics.median(v["candidate_seconds"])
            turn_secs += [row["spec_seconds"] + mid + c["seconds"] for c in v["cards"]]
        out["variants"][name] = {
            **{k: statistics.mean(vals) for k, vals in per.items()},
            "zero_cards": sum(per["zero_cards"]),
            "degraded_cards": sum(per["degraded_cards"]),
            "search_degraded": sum(per["search_degraded"]),
            "unlabeled": unlabeled,
            "candidate_seconds": (statistics.median(cand_secs), _p90(cand_secs)),
            "card_seconds": (statistics.median(card_secs), _p90(card_secs)),
            "turn_seconds": (statistics.median(turn_secs), _p90(turn_secs)),
            "facets": {f: statistics.mean(v) for f, v in sorted(facets.items())},
            # 카드는 10권 안에서 고르므로, 2점 책이 3권 미만이면 카드 3장을 다 맞는 책으로 채울 수 없다
            "short_of_three": sum(n < 3 for n in per["candidates_rel"]),
            "per_case": {k: per[k] for k in PAIRED},
        }
    return out


# 실험끼리 검색어마다 짝지어 견주는 숫자
PAIRED = ("candidates_ndcg", "candidates_rel", "card_grade")
BOOTSTRAP = 2000


def paired(summary: Mapping[str, Any], base: str = "base") -> dict[str, dict]:
    """기준 대비 검색어마다의 차이: 나아진·같은·나빠진 검색어 수와 평균 차이의 95% 구간(검색어를 다시 뽑는 부트스트랩)."""
    import random

    v = summary["variants"]
    out: dict[str, dict] = {}
    for name in v:
        if name == base:
            continue
        out[name] = {}
        for key in PAIRED:
            diffs = [
                b - a
                for a, b in zip(
                    v[base]["per_case"][key], v[name]["per_case"][key], strict=True
                )
            ]
            rng = random.Random(f"{name}:{key}")
            means = sorted(
                statistics.mean(rng.choices(diffs, k=len(diffs)))
                for _ in range(BOOTSTRAP)
            )
            out[name][key] = {
                "better": sum(d > 1e-9 for d in diffs),
                "same": sum(abs(d) <= 1e-9 for d in diffs),
                "worse": sum(d < -1e-9 for d in diffs),
                "mean": statistics.mean(diffs),
                "ci": (
                    means[int(0.025 * BOOTSTRAP)],
                    means[int(0.975 * BOOTSTRAP) - 1],
                ),
            }
    return out


NAMES = {"base": "지금(main)", "rerank": "(a) 점수 먼저", "vector": "(b) 벡터 검색"}


def render(summary: Mapping[str, Any]) -> str:
    v = summary["variants"]
    names = list(v)
    head = "| | " + " | ".join(NAMES[n] for n in names) + " |"
    rule = "| --- |" + " --- |" * len(names)

    def line(label: str, fmt) -> str:
        return f"| {label} | " + " | ".join(fmt(v[n]) for n in names) + " |"

    lines = [
        (
            f"검색어 {summary['cases']}개 중 추천까지 간 것 {summary['answered']}개"
            f"(그중 조건 뽑기가 제목·저자 찾기(exact)로 본 것 {summary['exact']}개)."
            " 2점 책 수는 검색어마다의 평균이다."
        ),
        "",
        head,
        rule,
        line(
            "① 검색 30권 중 2점",
            lambda s: f"{s['search_rel']:.1f} / {s['search_n']:.1f}권",
        ),
        line("↳ 소개글 없어 빠진 2점", lambda s: f"{s['lost_no_intro_rel']:.1f}"),
        line(
            "사후 거르기 뒤 2점",
            lambda s: f"{s['filtered_rel']:.1f} / {s['filtered_n']:.1f}권",
        ),
        line("↳ 10권에 다 넣을 수 있었던 2점", lambda s: f"{s['reachable_rel']:.1f}"),
        line(
            "10권 후보 중 2점",
            lambda s: f"{s['candidates_rel']:.1f} / {s['candidates_n']:.1f}권",
        ),
        line("10권 후보 nDCG@10", lambda s: f"{s['candidates_ndcg']:.3f}"),
        line("카드 3장 평균 점수(0–2)", lambda s: f"{s['card_grade']:.2f}"),
        line("카드 중 2점", lambda s: f"{s['card_rel']:.2f}장"),
        line(
            "↳ 10권에 있던 2점을 카드에 안 넣은 수",
            lambda s: f"{s['card_missed']:.2f}장",
        ),
        line(
            "10권에 2점이 3권 미만인 검색어",
            lambda s: f"{s['short_of_three']}개",
        ),
        line(
            "카드 0장 / degraded",
            lambda s: f"{s['zero_cards']} / {s['degraded_cards']}",
        ),
        line("① 위 10권 nDCG(참고)", lambda s: f"{s['search_ndcg']:.3f}"),
        line(
            "후보 고르기 시간(중앙/p90)",
            lambda s: "{:.2f}/{:.2f}초".format(*s["candidate_seconds"]),
        ),
        line(
            "카드 만들기 시간(중앙/p90)",
            lambda s: "{:.2f}/{:.2f}초".format(*s["card_seconds"]),
        ),
        line(
            "턴 어림(조건+후보+카드)",
            lambda s: "{:.2f}/{:.2f}초".format(*s["turn_seconds"]),
        ),
        line(
            "벡터가 끊긴 검색 / 점수 없는 책",
            lambda s: f"{s['search_degraded']} / {s['unlabeled']}",
        ),
        "",
        f"참고: 원문 검색어 그대로 ① 위 10권 nDCG {summary['raw_ndcg']:.3f}",
        "",
        "소분류별 10권 후보 nDCG@10",
        "",
        "| 소분류 | " + " | ".join(NAMES[n] for n in names) + " |",
        rule,
    ]
    for facet in v[names[0]]["facets"]:
        lines.append(
            f"| {facet} | "
            + " | ".join(f"{v[n]['facets'][facet]:.3f}" for n in names)
            + " |"
        )
    labels = {
        "candidates_ndcg": "10권 후보 nDCG@10",
        "candidates_rel": "10권 후보 중 2점",
        "card_grade": "카드 평균 점수",
    }
    lines += [
        "",
        "지금(main) 대비 검색어마다의 차이(나아짐/같음/나빠짐, 평균 차이와 95% 구간)",
        "",
        "| | " + " | ".join(NAMES[n] for n in names if n != "base") + " |",
        "| --- |" + " --- |" * (len(names) - 1),
    ]
    diffs = paired(summary)
    for key, label in labels.items():
        cells = [
            "{better}/{same}/{worse}, {mean:+.3f} [{lo:+.3f}, {hi:+.3f}]".format(
                **d[key], lo=d[key]["ci"][0], hi=d[key]["ci"][1]
            )
            for d in diffs.values()
        ]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render_cases(
    raw: Mapping[str, Any], labels: Mapping[str, Mapping[str, int]]
) -> str:
    """검색어마다 단계별 2점 책 수와 카드 평균 점수. 실험끼리 갈린 곳을 찾는 데 쓴다."""
    books = raw["books"]

    def rel(case_id: str, ids: Sequence[int]) -> int:
        marks = labels.get(case_id, {})
        return sum(marks.get((books.get(str(i)) or {}).get("isbn")) == 2 for i in ids)

    def card(case_id: str, v: Mapping[str, Any]) -> str:
        marks = labels.get(case_id, {})
        gs = [
            marks.get((books.get(str(i)) or {}).get("isbn")) or 0
            for c in v["cards"]
            for i in c["book_ids"]
        ]
        return f"{statistics.mean(gs):.2f}" if gs else "-"

    names = list(variants.VARIANTS)
    lines = [
        "| 번호 | 검색어 | 조건 문장(intent) | "
        + " | ".join(f"{NAMES[n]} 30→거름→10→카드" for n in names)
        + " |",
        "| --- | --- | --- |" + " --- |" * len(names),
    ]
    for row in raw["cases"]:
        if "variants" not in row:
            lines.append(
                f"| {row['id'][-2:]} | {row['query']} | (추천 안 함) |"
                + " |" * len(names)
            )
            continue
        spec = row["spec"]
        said = (
            spec["semantic"] or spec["exact"]["title"] or spec["exact"]["author"] or ""
        )
        cells = []
        for n in names:
            v = row["variants"][n]
            cells.append(
                f"{rel(row['id'], v['search'])}→{rel(row['id'], v['filtered'])}"
                f"→{rel(row['id'], v['candidates'])}→{card(row['id'], v)}"
            )
        lines.append(
            f"| {row['id'][-2:]} | {row['query']} | {said} ({spec['intent']}) | "
            + " | ".join(cells)
            + " |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="③ 단계별로 맞는 책을 잃는 곳을 잰다")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser(
        "collect", help="③ 에 검색어를 넣고 단계별 책을 남긴다(LLM 을 부른다)"
    )
    c.add_argument("--out", type=Path, required=True)
    c.add_argument(
        "--runs", type=int, default=3, help="같은 후보로 카드를 몇 번 만들지"
    )
    c.add_argument("--ids", help="쉼표로 가른 사례 번호(일부만)")
    r = sub.add_parser("report", help="표를 낸다")
    r.add_argument("raw", type=Path)
    r.add_argument("--json", type=Path, help="숫자를 파일로도 남긴다")
    r.add_argument("--cases", action="store_true", help="검색어마다의 표도 낸다")
    for p in (c, r):
        p.add_argument("--search-dir", type=Path, default=SEARCH_DIR)
    args = parser.parse_args(argv)

    if args.command == "collect":
        from app.core.config import get_settings

        if not get_settings().database_url.rstrip("/").endswith("_eval"):
            print("평가 DB(이름이 _eval 로 끝나는 DB)에만 돌린다", file=sys.stderr)
            return 2
        cases = load_cases(args.search_dir)
        if args.ids:
            wanted = set(args.ids.split(","))
            cases = [c for c in cases if c["id"] in wanted]
        raw = asyncio.run(collect(cases, args.runs))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        print(f"저장: {args.out}")
        return 0

    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    labels = load_labels(args.search_dir)
    summary = score(raw, labels)
    print(render(summary))
    if args.cases:
        print(render_cases(raw, labels))
    if args.json:
        args.json.write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
