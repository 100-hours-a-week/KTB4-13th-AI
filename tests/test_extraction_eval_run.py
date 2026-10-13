"""⑤ 평가 실행기(evals/extraction/run.py) 테스트(#341). 진짜 LLM 은 부르지 않는다.

실행기는 돈이 드는 호출을 여러 번 한다. 그래서 "몇 번 부르는지 먼저 알려 주고, 허락 없이는 부르지
않는다"를 테스트로 고정한다.
"""

import asyncio
import json
import logging

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.extraction import service
from evals.extraction import run
from evals.extraction.scoring import Outcome

_CASES = run.load_cases()
_BY_ID = {case["id"]: case for case in _CASES}


def _ideal(request: dict) -> Outcome:
    """기대값 그대로 답하는 가짜 API."""
    case = next(c for c in _CASES if c["request"] == request)
    items = [
        {
            "type": want["type"],
            "value": want["value"],
            "confidence": 0.8,
            "vector": [0.1, 0.2],
            "dim": 2,
            "source_conversation_id": request["conversation_id"],
        }
        for want in case["expect"]["extractions"]
    ]
    return Outcome(200, {"extractions": items, "nothing_found": not items}, 1.0)


def _target(answer=_ideal):
    calls: list[str] = []

    async def call(request: dict) -> Outcome:
        calls.append(request["conversation_id"])
        return answer(request)

    return call, calls


def _run(cases, answer=_ideal, runs=1):
    target, calls = _target(answer)
    scores = asyncio.run(run.run(cases, target, runs))
    return run.summarize(scores, runs), calls


# --- 묶음과 채점이 서로 맞는지 ---


def test_묶음_전체를_기대대로_답하면_전부_통과한다() -> None:
    # 기대 문장이 자기 핵심어를 담지 않거나 금지어를 담으면, 맞게 뽑아도 통과할 수 없는 대화가 된다
    summary, calls = _run(_CASES)

    assert len(calls) == len(_CASES) == 80
    assert summary["total"] == {"cases": 80, "runs": 80, "passed": 80, "pass_rate": 1.0}
    assert summary["failures"] == {}


def test_아무것도_안_뽑으면_뽑을_것이_없는_대화만_통과한다() -> None:
    def nothing(request: dict) -> Outcome:
        return Outcome(200, {"extractions": [], "nothing_found": True}, 1.0)

    summary, _calls = _run(_CASES, nothing)

    expect_nothing = sum(c["expect"]["nothing_found"] for c in _CASES)
    assert summary["total"]["passed"] == expect_nothing
    assert summary["failures"] == {"missed": 80 - expect_nothing}


# --- 고르기 ---


def test_종류로_고른다() -> None:
    picked = run.select(_CASES, kinds=["none", "onetime"])

    assert {c["kind"] for c in picked} == {"none", "onetime"}
    assert len(picked) == 9


def test_번호로_고른다() -> None:
    picked = run.select(_CASES, ids=["request-01", "none-01"])

    assert [c["id"] for c in picked] == ["request-01", "none-01"]


def test_묶음으로_고른다() -> None:
    assert len(run.select(_CASES, group="guard")) == 11


@pytest.mark.parametrize(
    "chosen",
    [{"kinds": ["없는종류"]}, {"ids": ["request-999"]}, {"group": "없는묶음"}],
)
def test_없는_것을_고르면_조용히_넘어가지_않는다(chosen) -> None:
    # 오타로 0개를 돌리고 "100%"를 보는 일이 없게 한다
    with pytest.raises(ValueError):
        run.select(_CASES, **chosen)


# --- 돌리기와 집계 ---


def test_대화마다_정한_횟수만큼_차례로_부른다() -> None:
    cases = run.select(_CASES, ids=["request-01", "none-01"])

    _summary, calls = _run(cases, runs=3)

    assert calls == ["eval_request-01"] * 3 + ["eval_none-01"] * 3


def test_종류별과_묶음별로_통과율을_낸다() -> None:
    cases = run.select(_CASES, kinds=["none", "injection"])

    summary, _calls = _run(cases, runs=2)

    assert summary["by_kind"]["none"] == {
        "group": "typical",
        "cases": 6,
        "runs": 12,
        "passed": 12,
        "pass_rate": 1.0,
    }
    assert summary["by_group"]["guard"]["runs"] == 4
    assert summary["calls"] == 16


def test_돌릴_때마다_결과가_달라진_대화를_알려_준다() -> None:
    # 같은 대화에 뽑았다 안 뽑았다 하는 것이 ⑤의 가장 큰 문제였다(#373 연결 확인)
    turn = {"n": 0}

    def flaky(request: dict) -> Outcome:
        turn["n"] += 1
        if request["conversation_id"] == "eval_request-01" and turn["n"] % 2 == 0:
            return Outcome(200, {"extractions": [], "nothing_found": True}, 1.0)
        return _ideal(request)

    cases = run.select(_CASES, ids=["request-01", "none-01"])

    summary, _calls = _run(cases, flaky, runs=4)

    assert summary["unstable"] == ["request-01"]
    assert summary["by_kind"]["request"]["passed"] == 2
    assert summary["failures"] == {"missed": 2}


def test_실패한_호출은_오류로_세고_나머지는_계속_돈다() -> None:
    def broken(request: dict) -> Outcome:
        if request["conversation_id"] == "eval_request-01":
            return Outcome(503, None, 0.2)
        return _ideal(request)

    cases = run.select(_CASES, ids=["request-01", "none-01"])

    summary, calls = _run(cases, broken)

    assert len(calls) == 2
    assert summary["failures"] == {"error": 1}
    assert summary["total"]["passed"] == 1


def test_걸린_시간의_중앙값과_가장_긴_값을_낸다() -> None:
    times = iter([1.0, 2.0, 9.0])

    def timed(request: dict) -> Outcome:
        ideal = _ideal(request)
        return Outcome(ideal.status, ideal.data, next(times))

    summary, _calls = _run(run.select(_CASES, kinds=["onetime"]), timed)

    assert summary["seconds"] == {"median": 2.0, "max": 9.0}


# --- 돈이 드는 호출을 허락 없이 하지 않는다 ---


def test_yes_없이는_계획만_알려_주고_한_번도_부르지_않는다(capsys) -> None:
    target, calls = _target()

    code = run.main(["--kinds", "request", "--runs", "3"], target=target)

    out = capsys.readouterr().out
    assert code == 0
    assert calls == []
    assert "대화 18개 × 3회 = LLM 호출 54번" in out
    assert "--yes" in out


def test_yes를_붙이면_돌리고_표를_낸다(capsys) -> None:
    target, calls = _target()

    code = run.main(["--ids", "request-01,none-01", "--yes"], target=target)

    out = capsys.readouterr().out
    assert code == 0
    assert len(calls) == 2
    assert "request" in out and "100%" in out


def test_도는_동안_줄인_로그를_끝나면_되돌린다(capsys) -> None:
    target, _calls = _target()

    run.main(["--ids", "none-01", "--yes"], target=target)

    assert logging.root.manager.disable == logging.NOTSET


def test_결과를_파일로_남긴다(tmp_path, capsys) -> None:
    target, _calls = _target()
    path = tmp_path / "result.json"

    run.main(["--ids", "request-01", "--yes", "--out", str(path)], target=target)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["summary"]["total"]["runs"] == 1
    assert saved["bundle_version"] == run.load_bundle()["version"]
    assert saved["runs"][0]["case_id"] == "request-01"
    assert saved["runs"][0]["values"] == ["쿠버네티스 입문서를 찾음"]


def test_없는_종류를_주면_부르지_않고_끝난다(capsys) -> None:
    target, calls = _target()

    assert run.main(["--kinds", "없는종류", "--yes"], target=target) == 2
    assert calls == []


# --- 서버를 같은 프로세스에서 부르는 진짜 대상. LLM 과 임베딩만 가짜다 ---


def test_API를_그대로_거쳐_응답과_시간을_받는다(monkeypatch) -> None:
    reply = json.dumps(
        {
            "extractions": [
                {
                    "type": "topic",
                    "value": "쿠버네티스 입문서를 찾음",
                    "confidence": 0.7,
                    "evidence": "쿠버네티스 처음 배우는데 입문서 추천해줘",
                    "owner": "user",
                    "basis": "requested",
                }
            ]
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(
        service,
        "get_chat_model",
        lambda: RunnableLambda(lambda _p: AIMessage(content=reply)),
    )

    async def _embed(texts, purpose):
        return [[0.1, 0.2] for _ in texts], 2, "fake"

    monkeypatch.setattr(service.embedding, "embed", _embed)

    outcome = asyncio.run(run.api_target()(_BY_ID["request-01"]["request"]))

    assert outcome.status == 200
    assert outcome.seconds > 0
    assert [item["value"] for item in outcome.data["extractions"]] == [
        "쿠버네티스 입문서를 찾음"
    ]
