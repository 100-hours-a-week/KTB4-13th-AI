"""⑤ 평가 묶음을 돌려 코드 채점하는 실행기(#341).

    uv run python -m evals.extraction.run --kinds request --runs 3          # 몇 번 부르는지만 본다
    uv run python -m evals.extraction.run --kinds request --runs 3 --yes    # 실제로 돌린다

대화마다 API(`POST /preferences/extractions`)를 같은 프로세스에서 그대로 부른다. 서버를 따로 띄우지
않고, 요청 검사·LLM·규칙·임베딩을 실제 요청과 같은 길로 지난다. LLM 은 `.env` 의 설정대로 진짜를
부르므로 돈이 든다. 그래서 `--yes` 없이는 호출 수만 알려 주고 끝낸다.

채점은 코드로 볼 수 있는 것만 한다(scoring.py). LLM 채점은 뒤 단계다.
"""

import argparse
import asyncio
import json
import logging
import statistics
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

from evals.extraction.scoring import Outcome, Score, score

CASES_PATH = Path(__file__).with_name("cases.json")
URL = "/preferences/extractions"
# LLM 호출의 제한 시간(30초)보다 길게 둔다. 짧으면 서버가 504 로 답하기 전에 여기서 끊긴다.
CALL_TIMEOUT_SECONDS = 60

Target = Callable[[dict[str, Any]], Awaitable[Outcome]]


def load_bundle() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def load_cases() -> list[dict[str, Any]]:
    """대화 목록. 대화마다 자기 종류의 묶음(typical·guard)을 `group` 으로 붙여 준다."""
    bundle = load_bundle()
    return [
        {**case, "group": bundle["kinds"][case["kind"]]["group"]}
        for case in bundle["cases"]
    ]


def select(
    cases: Sequence[dict[str, Any]],
    *,
    kinds: Sequence[str] | None = None,
    ids: Sequence[str] | None = None,
    group: str | None = None,
) -> list[dict[str, Any]]:
    """돌릴 대화를 고른다. 없는 이름을 주면 ValueError 다 — 오타로 0개를 돌리지 않게 한다."""
    for label, wanted, known in (
        ("종류", kinds, {c["kind"] for c in cases}),
        ("번호", ids, {c["id"] for c in cases}),
        ("묶음", [group] if group else None, {c["group"] for c in cases}),
    ):
        unknown = sorted(set(wanted or ()) - known)
        if unknown:
            raise ValueError(f"묶음에 없는 {label}: {', '.join(unknown)}")

    picked = [
        case
        for case in cases
        if (not kinds or case["kind"] in kinds)
        and (not ids or case["id"] in ids)
        and (not group or case["group"] == group)
    ]
    if ids:
        picked.sort(key=lambda case: list(ids).index(case["id"]))
    return picked


async def run(
    cases: Sequence[dict[str, Any]], target: Target, runs: int
) -> list[Score]:
    """대화마다 runs 번씩 차례로 부르고 채점한다. 한 번에 하나씩만 부른다(호출 한도와 순서 때문)."""
    scores: list[Score] = []
    for case in cases:
        for _ in range(runs):
            scores.append(score(case, await target(case["request"])))
    return scores


def _rate(scores: Sequence[Score]) -> dict[str, Any]:
    passed = sum(s.passed for s in scores)
    return {
        "cases": len({s.case_id for s in scores}),
        "runs": len(scores),
        "passed": passed,
        "pass_rate": round(passed / len(scores), 3) if scores else 0.0,
    }


def summarize(scores: Sequence[Score], runs: int) -> dict[str, Any]:
    """종류별·묶음별 통과율과, 돌릴 때마다 결과가 달라진 대화를 낸다."""
    groups = {case["kind"]: case["group"] for case in load_cases()}

    by_kind: dict[str, list[Score]] = {}
    by_case: dict[str, list[Score]] = {}
    for s in scores:
        by_kind.setdefault(s.kind, []).append(s)
        by_case.setdefault(s.case_id, []).append(s)

    by_group: dict[str, list[Score]] = {}
    for kind, items in by_kind.items():
        by_group.setdefault(groups[kind], []).extend(items)

    seconds = [s.seconds for s in scores]
    return {
        "runs_per_case": runs,
        "calls": len(scores),
        "total": _rate(scores),
        "by_group": {group: _rate(items) for group, items in by_group.items()},
        "by_kind": {
            kind: {"group": groups[kind], **_rate(items)}
            for kind, items in by_kind.items()
        },
        # 실패 이유별 횟수(호출 기준). 한 호출에 이유가 여럿이면 각각 센다
        "failures": dict(Counter(f for s in scores for f in s.failures)),
        # 같은 대화인데 통과와 실패가 섞였거나, 뽑은 개수가 달랐던 대화
        "unstable": [
            case_id
            for case_id, items in by_case.items()
            if len({(s.passed, s.extracted) for s in items}) > 1
        ],
        # 코드로는 다 볼 수 없어 LLM 채점이 필요한 호출 수
        "needs_llm": sum(s.needs_llm for s in scores),
        "seconds": {
            "median": round(statistics.median(seconds), 2),
            "max": round(max(seconds), 2),
        }
        if seconds
        else {},
    }


def render(summary: dict[str, Any]) -> str:
    """사람이 읽는 결과 표. PR 본문에 그대로 붙일 수 있게 마크다운 표로 낸다."""

    def row(name: str, group: str, item: dict[str, Any]) -> str:
        return (
            f"| {name} | {group} | {item['cases']} | {item['passed']}/{item['runs']} "
            f"| {item['pass_rate']:.0%} |"
        )

    lines = [
        "| 종류 | 묶음 | 대화 | 통과/호출 | 통과율 |",
        "| --- | --- | --- | --- | --- |",
        *(row(kind, item["group"], item) for kind, item in summary["by_kind"].items()),
        *(
            row(f"**{group} 합계**", group, item)
            for group, item in summary["by_group"].items()
        ),
        row("**전체**", "", summary["total"]),
        "",
        f"- 대화마다 {summary['runs_per_case']}번, 호출 {summary['calls']}번",
        f"- 실패 이유(호출 기준): {summary['failures'] or '없음'}",
        f"- 돌릴 때마다 결과가 달라진 대화: {', '.join(summary['unstable']) or '없음'}",
        f"- 코드로 다 볼 수 없어 LLM 채점이 필요한 호출: {summary['needs_llm']}번",
    ]
    if summary["seconds"]:
        lines.append(
            f"- 대화 하나에 걸린 시간: 중앙값 {summary['seconds']['median']}초, "
            f"가장 긴 것 {summary['seconds']['max']}초"
        )
    return "\n".join(lines)


def api_target() -> Target:
    """서버 앱을 같은 프로세스에서 부르는 대상. LLM 과 임베딩은 설정대로 진짜를 쓴다."""
    from app.core.config import get_settings
    from app.main import app

    token = get_settings().ai_service_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def call(request: dict[str, Any]) -> Outcome:
        started = perf_counter()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://eval",
            timeout=CALL_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(URL, json=request, headers=headers)
        seconds = perf_counter() - started
        data = response.json().get("data") if response.status_code == 200 else None
        return Outcome(response.status_code, data, seconds)

    return call


def _model_line() -> str:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.llm_mock:
        return "LLM_MOCK=true 라서 진짜 LLM 을 부르지 않는다(이 API 는 503 으로 답한다)"
    return f"모델 {settings.llm_model_id or '(설정 없음)'}, 공급자 {settings.llm_provider or '(설정 없음)'}"


def _split(text: str | None) -> list[str] | None:
    return [part.strip() for part in text.split(",") if part.strip()] if text else None


def main(argv: Sequence[str] | None = None, *, target: Target | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="⑤ 취향 뽑기 평가 묶음을 돌려 채점한다"
    )
    parser.add_argument("--kinds", help="돌릴 종류(쉼표로 여럿). 예: request,none")
    parser.add_argument("--ids", help="돌릴 대화 번호(쉼표로 여럿). 예: request-01")
    parser.add_argument("--group", help="돌릴 묶음: typical 또는 guard")
    parser.add_argument(
        "--runs", type=int, default=1, help="대화마다 몇 번 돌릴지(기본 1)"
    )
    parser.add_argument("--yes", action="store_true", help="실제로 LLM 을 부른다")
    parser.add_argument("--out", help="호출마다의 결과를 남길 JSON 파일")
    args = parser.parse_args(argv)

    if args.runs < 1:
        print("--runs 는 1 이상이어야 한다", file=sys.stderr)
        return 2
    try:
        cases = select(
            load_cases(),
            kinds=_split(args.kinds),
            ids=_split(args.ids),
            group=args.group,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    # 묶음의 요청은 모두 동의한 사용자의 비어 있지 않은 대화라, 호출마다 LLM 을 한 번 부른다
    print(
        f"대화 {len(cases)}개 × {args.runs}회 = LLM 호출 {len(cases) * args.runs}번. "
        f"{_model_line()}"
    )
    if not args.yes:
        print("실제로 돌리려면 --yes 를 붙인다. 돈이 드는 호출이다.")
        return 0

    # 서버는 요청마다 로그를 남긴다. 수십 번 부르면 결과 표가 묻히므로 도는 동안은 경고부터만 보인다
    logging.disable(logging.INFO)
    try:
        scores = asyncio.run(run(cases, target or api_target(), args.runs))
    finally:
        logging.disable(logging.NOTSET)
    summary = summarize(scores, args.runs)
    print()
    print(render(summary))

    if args.out:
        from app.extraction.prompts import PROMPT_VERSION

        saved = {
            "bundle_version": load_bundle()["version"],
            "prompt_version": PROMPT_VERSION,
            "model": _model_line(),
            "summary": summary,
            "runs": [
                {**asdict(s), "failures": list(s.failures), "values": list(s.values)}
                for s in scores
            ],
        }
        Path(args.out).write_text(
            json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n호출마다의 결과: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
