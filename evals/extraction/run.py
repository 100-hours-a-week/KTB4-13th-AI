"""⑤ 평가 묶음을 돌려 코드 채점하는 실행기(#341).

    uv run python -m evals.extraction.run --kinds request --runs 3          # 몇 번 부르는지만 본다
    uv run python -m evals.extraction.run --kinds request --runs 3 --yes    # 실제로 돌린다
    uv run python -m evals.extraction.run --rescore result.json              # 남긴 응답을 지금 채점 코드로(LLM 없음)

대화마다 API(`POST /preferences/extractions`)를 같은 프로세스에서 그대로 부른다. 서버를 따로 띄우지
않고, 요청 검사·LLM·규칙·임베딩을 실제 요청과 같은 길로 지난다. LLM 은 `.env` 의 설정대로 진짜를
부르므로 돈이 든다. 그래서 `--yes` 없이는 호출 수만 알려 주고 끝낸다.

채점은 코드로 볼 수 있는 것만 한다(scoring.py). LLM 채점은 뒤 단계다.

`--out` 파일에는 호출마다 API 응답과, 서버 규칙(rules.screen)을 거치기 전의 LLM 답도 남긴다(#753). 채점 코드나
기대값만 고친 경우에는 `--rescore` 로 남긴 응답을 다시 채점한다. LLM 을 부르지 않으니 돈이 들지 않는다.
"""

import argparse
import asyncio
import json
import logging
import statistics
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import httpx

from evals.extraction.scoring import Outcome, Score, score

CASES_PATH = Path(__file__).with_name("cases.json")
URL = "/preferences/extractions"
# LLM 호출의 제한 시간(30초)보다 길게 둔다. 짧으면 서버가 504 로 답하기 전에 여기서 끊긴다.
CALL_TIMEOUT_SECONDS = 60

Target = Callable[[dict[str, Any]], Awaitable[Outcome]]
# 결과 파일에 벡터는 길이만 남긴다. 호출마다 1024개 숫자를 남기면 파일이 수십 MB 가 된다
VECTOR_LEN = "vector_len"


def load_bundle() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def load_cases() -> list[dict[str, Any]]:
    """대화 목록. 대화마다 자기 종류의 묶음(typical·guard·borrowed)을 `group` 으로 붙여 준다."""
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
    cases: Sequence[dict[str, Any]],
    target: Target,
    runs: int,
    outcomes: list[Outcome] | None = None,
) -> list[Score]:
    """대화마다 runs 번씩 차례로 부르고 채점한다. 한 번에 하나씩만 부른다(호출 한도와 순서 때문).

    outcomes 를 주면 호출마다 받은 응답을 채점 결과와 같은 순서로 넣는다.
    """
    scores: list[Score] = []
    for case in cases:
        for _ in range(runs):
            outcome = await target(case["request"])
            if outcomes is not None:
                outcomes.append(outcome)
            scores.append(score(case, outcome))
    return scores


def compact(data: Any) -> Any:
    """결과 파일에 남길 응답. 벡터는 길이만 남긴다. 채점은 벡터가 목록이고 dim 과 길이가 같은지만 본다."""
    if not isinstance(data, dict) or not isinstance(data.get("extractions"), list):
        return data
    items = [
        {**item, "vector": None, VECTOR_LEN: len(item["vector"])}
        if isinstance(item, dict) and isinstance(item.get("vector"), list)
        else item
        for item in data["extractions"]
    ]
    return {**data, "extractions": items}


def expand(data: Any) -> Any:
    """compact 를 되돌린다. 벡터는 같은 길이의 0 으로 채운다."""
    if not isinstance(data, dict) or not isinstance(data.get("extractions"), list):
        return data
    items = []
    for item in data["extractions"]:
        if isinstance(item, dict) and VECTOR_LEN in item:
            size = item[VECTOR_LEN]
            item = {k: v for k, v in item.items() if k != VECTOR_LEN}
            item["vector"] = [0.0] * size
        items.append(item)
    return {**data, "extractions": items}


def _failures(scores: Sequence[Score]) -> dict[str, int]:
    """실패 이유별 횟수(호출 기준). 한 호출에 이유가 여럿이면 각각 센다."""
    return dict(Counter(f for s in scores for f in s.failures))


def _rate(scores: Sequence[Score]) -> dict[str, Any]:
    passed = sum(s.passed for s in scores)
    return {
        "cases": len({s.case_id for s in scores}),
        "runs": len(scores),
        "passed": passed,
        "pass_rate": round(passed / len(scores), 3) if scores else 0.0,
    }


def _labels(scores: Sequence[Score]) -> dict[str, Any]:
    """세기·어디에·핵심어가 맞은 비율(#424). 찾은 기대 취향 가운데 그 칸을 적어 둔 것이 분모다."""
    labeled = sum(s.labeled for s in scores)
    where = sum(s.where_labeled for s in scores)

    def rate(count: int, total: int) -> float | None:
        return round(count / total, 3) if total else None

    return {
        "labeled": labeled,
        "sign": rate(sum(s.sign_matched for s in scores), labeled),
        "strength": rate(sum(s.strength_matched for s in scores), labeled),
        "keyword": rate(sum(s.keyword_matched for s in scores), labeled),
        "where_labeled": where,
        "where": rate(sum(s.where_matched for s in scores), where),
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
            kind: {"group": groups[kind], **_rate(items), "failures": _failures(items)}
            for kind, items in by_kind.items()
        },
        "failures": _failures(scores),
        "labels": _labels(scores),
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
        why = ", ".join(f"{k} {v}" for k, v in item.get("failures", {}).items())
        return (
            f"| {name} | {group} | {item['cases']} | {item['passed']}/{item['runs']} "
            f"| {item['pass_rate']:.0%} | {why} |"
        )

    lines = [
        "| 종류 | 묶음 | 대화 | 통과/호출 | 통과율 | 실패 이유(호출 수) |",
        "| --- | --- | --- | --- | --- | --- |",
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
    labels = summary["labels"]
    if labels["labeled"]:

        def pct(rate: float | None) -> str:
            return "-" if rate is None else f"{rate:.0%}"

        lines.append(
            f"- 찾은 취향 {labels['labeled']}개의 세기·핵심어·어디에: "
            f"세기 부호 {pct(labels['sign'])}, 크기까지 {pct(labels['strength'])}, "
            f"핵심어 {pct(labels['keyword'])}, "
            f"어디에 {pct(labels['where'])}(주제 {labels['where_labeled']}개)"
        )
    if summary["seconds"]:
        lines.append(
            f"- 대화 하나에 걸린 시간: 중앙값 {summary['seconds']['median']}초, "
            f"가장 긴 것 {summary['seconds']['max']}초"
        )
    return "\n".join(lines)


@contextmanager
def keep_llm_output(kept: list[Any]) -> Iterator[None]:
    """도는 동안 서버가 규칙에 넘기는 LLM 답(service.to_extractions 의 인자)을 kept 에 넣는다.

    서버 코드는 고치지 않고, 이 프로세스에서 부르는 동안만 그 함수를 감싼다. 끝나면 되돌린다.
    """
    from app.extraction import service

    original = service.to_extractions

    async def keeping(req: Any, llm_output: Any) -> list[dict[str, Any]]:
        kept.append(llm_output)
        return await original(req, llm_output)

    service.to_extractions = keeping
    try:
        yield
    finally:
        service.to_extractions = original


def api_target() -> Target:
    """서버 앱을 같은 프로세스에서 부르는 대상. LLM 과 임베딩은 설정대로 진짜를 쓴다."""
    from app.core.config import get_settings
    from app.main import app

    token = get_settings().ai_service_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def call(request: dict[str, Any]) -> Outcome:
        kept: list[Any] = []
        started = perf_counter()
        with keep_llm_output(kept):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://eval",
                timeout=CALL_TIMEOUT_SECONDS,
            ) as client:
                response = await client.post(URL, json=request, headers=headers)
        seconds = perf_counter() - started
        data = response.json().get("data") if response.status_code == 200 else None
        # LLM 이 답하지 못했으면(장애·시간 초과) 남길 답이 없다
        return Outcome(response.status_code, data, seconds, kept[-1] if kept else None)

    return call


def rescore(saved: dict[str, Any]) -> list[Score]:
    """남긴 응답을 지금의 채점 코드와 기대값으로 다시 채점한다. LLM 을 부르지 않는다."""
    if saved.get("bundle_version") != load_bundle()["version"]:
        raise ValueError(
            f"묶음 판이 다르다(파일 {saved.get('bundle_version')}, 지금 {load_bundle()['version']})"
        )
    if any("response" not in record for record in saved["runs"]):
        raise ValueError(
            "응답을 남기지 않은 파일이라 다시 채점할 수 없다(#753 전에 잰 파일)"
        )
    cases = {case["id"]: case for case in load_cases()}
    return [
        score(
            cases[record["case_id"]],
            Outcome(
                record["status"],
                expand(record["response"]),
                record["seconds"],
                record.get("llm_output"),
            ),
        )
        for record in saved["runs"]
    ]


def _model_line() -> str:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.llm_mock:
        return "LLM_MOCK=true 라서 진짜 LLM 을 부르지 않는다(이 API 는 503 으로 답한다)"
    # ⑤는 자기 모델 설정이 있으면 그것을, 없으면 챗봇과 같은 모델을 쓴다(app/extraction/service.py)
    model = settings.extraction_llm_model_id or settings.llm_model_id
    line = (
        f"모델 {model or '(설정 없음)'}, 공급자 {settings.llm_provider or '(설정 없음)'}, "
        f"주소 {_where(settings.llm_base_url)}"
    )
    if (
        settings.extraction_self_hosted_mode == "on"
        and settings.extraction_self_hosted_base_url
        and settings.extraction_self_hosted_model_id
    ):
        # 켜 두면 자체 서빙이 실패한 대화를 위 API 가 대신 답한다. GPU 품질을 재는 실행이면 섞인다(#569)
        line += (
            f". 취향 뽑기 자체 서빙 켬: 모델 {settings.extraction_self_hosted_model_id}, "
            f"주소 {_where(settings.extraction_self_hosted_base_url)}. "
            "자체 서빙이 실패한 대화는 위 API 답으로 채점된다"
        )
    return line


# 기록에 이름으로 남길 공개 API. 그 밖의 서버(GPU 서버 등)는 주소를 남기지 않는다 — 기록이 GitHub 에 올라간다
_PUBLIC_API_HOSTS = {
    "openrouter.ai": "OpenRouter API",
    "api.openai.com": "OpenAI API",
    "generativelanguage.googleapis.com": "Gemini API",
}


def _where(base_url: str) -> str:
    """LLM 주소가 어느 쪽인지. 같은 평가로 API(#568)와 GPU(#569)를 재서, 기록만 보고 갈라야 한다."""
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if host in _PUBLIC_API_HOSTS:
        return _PUBLIC_API_HOSTS[host]
    if host in ("localhost", "127.0.0.1", "::1"):
        # 로컬 Ollama 나 GPU 서버로 가는 포트포워딩. 포트로 가른다
        return f"이 컴퓨터({host}:{parts.port})"
    return "그 밖의 서버(주소는 남기지 않음)"


def _split(text: str | None) -> list[str] | None:
    return [part.strip() for part in text.split(",") if part.strip()] if text else None


def _record(s: Score, outcome: Outcome) -> dict[str, Any]:
    return {
        **asdict(s),
        "failures": list(s.failures),
        "values": list(s.values),
        "got": list(s.got),
        # 채점 코드만 고쳤을 때 --rescore 로 다시 채점하는 데 쓴다
        "response": compact(outcome.data),
        "llm_output": outcome.llm_output,
    }


def _write(
    path: str,
    head: dict[str, Any],
    summary: dict[str, Any],
    scores: Sequence[Score],
    outcomes: Sequence[Outcome],
) -> None:
    saved = {
        **head,
        "summary": summary,
        "runs": [_record(s, o) for s, o in zip(scores, outcomes, strict=True)],
    }
    Path(path).write_text(
        json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n호출마다의 결과: {path}")


def _rescore_main(path: str, out: str | None) -> int:
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        scores = rescore(saved)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    runs = saved["summary"]["runs_per_case"]
    print(
        f"{path} 의 호출 {len(scores)}번을 지금 채점 코드로 다시 채점한다. LLM 은 부르지 않는다. "
        f"답은 그때의 것이다(프롬프트 판 {saved.get('prompt_version')}, {saved.get('model')})"
    )
    summary = summarize(scores, runs)
    print()
    print(render(summary))
    if out:
        outcomes = [
            Outcome(r["status"], r["response"], r["seconds"], r.get("llm_output"))
            for r in saved["runs"]
        ]
        head = {
            "bundle_version": saved["bundle_version"],
            "prompt_version": saved.get("prompt_version"),
            "model": saved.get("model"),
            "rescored_from": Path(path).name,
        }
        # compact 한 응답을 그대로 다시 남긴다(expand 하지 않는다)
        _write(out, head, summary, scores, outcomes)
    return 0


def main(argv: Sequence[str] | None = None, *, target: Target | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="⑤ 취향 뽑기 평가 묶음을 돌려 채점한다"
    )
    parser.add_argument("--kinds", help="돌릴 종류(쉼표로 여럿). 예: request,none")
    parser.add_argument("--ids", help="돌릴 대화 번호(쉼표로 여럿). 예: request-01")
    parser.add_argument("--group", help="돌릴 묶음: typical, guard, borrowed")
    parser.add_argument(
        "--runs", type=int, default=1, help="대화마다 몇 번 돌릴지(기본 1)"
    )
    parser.add_argument("--yes", action="store_true", help="실제로 LLM 을 부른다")
    parser.add_argument("--out", help="호출마다의 결과를 남길 JSON 파일")
    parser.add_argument(
        "--rescore",
        metavar="결과파일",
        help="--out 으로 남긴 응답을 지금 채점 코드로 다시 채점한다. LLM 을 부르지 않는다",
    )
    args = parser.parse_args(argv)

    if args.rescore:
        if args.kinds or args.ids or args.group or args.yes:
            print(
                "--rescore 는 파일의 호출을 모두 다시 채점한다. 고르기와 --yes 는 쓰지 않는다",
                file=sys.stderr,
            )
            return 2
        return _rescore_main(args.rescore, args.out)
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
    outcomes: list[Outcome] = []
    logging.disable(logging.INFO)
    try:
        scores = asyncio.run(run(cases, target or api_target(), args.runs, outcomes))
    finally:
        logging.disable(logging.NOTSET)
    summary = summarize(scores, args.runs)
    print()
    print(render(summary))

    if args.out:
        from app.extraction.prompts import PROMPT_VERSION

        head = {
            "bundle_version": load_bundle()["version"],
            "prompt_version": PROMPT_VERSION,
            "model": _model_line(),
        }
        _write(args.out, head, summary, scores, outcomes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
