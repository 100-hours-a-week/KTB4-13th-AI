"""⑦ 평가 실행기(evals/agent/run.py) 테스트(#381). 가짜 LLM(ScriptedModel)으로 돌아서 LLM 비용이 없다.

실행기가 사례의 시작 상태를 제대로 만들고, 서버와 같은 길로 돌리고, 오류를 기록으로 남기고, 결과를 모으는지
본다. 실제 모델의 점수는 여기서 재지 않는다(그건 `python -m evals.agent.run` 으로 돈다).
"""

import asyncio

import httpx
import openai
import pytest

from app.agent.fake_llm import ScriptedModel, ai_calls, ai_text
from app.agent.schemas import ToolName
from evals.agent import grade, run

DATA = grade.load_cases()
CASES = {c["id"]: c for c in DATA["cases"]}


def _run_case(case_id: str, replies, *, timeout: float = 5.0):
    model = ScriptedModel(replies)
    return asyncio.run(
        run.run_case(DATA, CASES[case_id], model, timeout=timeout)
    ), model


def _card(n: int) -> dict:
    return {"source": "card", "n": n}


def _add_card_1() -> list:
    return [ai_calls((ToolName.CART_ADD, {"book": _card(1)})), ai_text("담았어요.")]


# --- 시작 상태 ---


@pytest.mark.parametrize("case", DATA["cases"], ids=[c["id"] for c in DATA["cases"]])
def test_모든_사례의_시작_상태를_만들_수_있다(case) -> None:
    fake, uid = asyncio.run(run.build_commerce(DATA, case))

    assert fake.cart_state(uid) == {l["isbn"]: l["qty"] for l in case["state"]["cart"]}
    for other in case["state"].get("others", []):
        assert fake.cart_state(other["user_id"]) == {
            l["isbn"]: l["qty"] for l in other["cart"]
        }


def test_판매_중지_책은_장바구니에_남고_가격이_없다() -> None:
    case = next(
        c
        for c in DATA["cases"]
        if any(
            not b["active"] and b["isbn"] in {l["isbn"] for l in c["state"]["cart"]}
            for b in DATA["catalog"]
        )
    )
    fake, uid = asyncio.run(run.build_commerce(DATA, case))

    lines = asyncio.run(fake.get_cart(uid))

    assert any(line.price is None for line in lines)


def test_서가의_소개글이_비교에_쓰인다() -> None:
    case = CASES["compare-01"]
    fake, _uid = asyncio.run(run.build_commerce(DATA, case))
    isbns = [card["isbn"] for card in case["request"]["context_cards"][:2]]

    texts = asyncio.run(fake.get_texts(isbns))

    assert [t.description for t in texts] and all(t.description for t in texts)


# --- 사례 하나 ---


def test_사례를_끝까지_돌려_기록과_채점이_나온다() -> None:
    record, model = _run_case("add-01", _add_card_1())

    assert record["status"] == 200
    assert record["cart"] == CASES["add-01"]["expect"]["cart"]
    assert record["start_cart"] == {}
    assert record["llm_calls"] == 2 == len(model.calls)
    assert record["iterations"] == 1
    assert record["ms"] > 0
    assert record["data"]["tool_calls"][0]["name"] == "cart.add"
    assert grade.grade_case(CASES["add-01"], record)["passed"] is True


def test_답변_검사의_결과도_기록한다() -> None:
    # 도구를 부르지 않고 담았다고만 하면 답변 검사가 지우고 422 가 된다
    record, _ = _run_case("add-01", [ai_text("담았어요.")])

    assert record["status"] == 422
    assert record["removed"] == ["ACTION_NOT_DONE"]
    assert record["data"] is None
    assert record["raw_reply"] == "담았어요."  # 지워지기 전의 LLM 글이 남는다
    assert grade.grade_case(CASES["add-01"], record)["failures"] == ["요청 오류 422"]


def test_쓰기가_성공한_턴에서_검사가_지운_문장의_이유를_센다() -> None:
    record, _ = _run_case(
        "add-01",
        [ai_calls((ToolName.CART_ADD, {"book": _card(1)})), ai_text("추가되었습니다.")],
    )

    assert record["status"] == 200
    assert record["removed"] == ["ACTION_RESTATED"]
    # 사용자에게 간 답과 LLM 이 쓴 원문이 다르다. 어떤 말투가 지워졌는지 원문으로 모은다
    assert record["raw_reply"] == "추가되었습니다."
    assert record["reply"] == "1번 책을 장바구니에 담았어요."


def test_LLM_장애는_503으로_기록한다() -> None:
    request = httpx.Request("POST", "http://localhost/v1")

    record, _ = _run_case("add-01", [openai.APIConnectionError(request=request)])

    assert record["status"] == 503
    assert record["error"] == "LLMUnavailableError"
    assert record["cart"] == {}


def test_LLM_호출_시간_초과는_504로_기록한다() -> None:
    request = httpx.Request("POST", "http://localhost/v1")

    record, _ = _run_case("add-01", [openai.APITimeoutError(request=request)])

    assert record["status"] == 504


class _SlowModel:
    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, *args, **kwargs):
        await asyncio.sleep(5)


def test_턴_시간_제한을_넘으면_504로_기록한다() -> None:
    model = _SlowModel()

    record = asyncio.run(run.run_case(DATA, CASES["add-01"], model, timeout=0.05))

    assert record["status"] == 504
    assert record["error"] == "TimeoutError"


def test_예상하지_못한_오류도_기록으로_남기고_다음으로_간다() -> None:
    class _Broken:
        def bind_tools(self, tools, **kwargs):
            raise RuntimeError("뜻밖의 오류")

    record = asyncio.run(run.run_case(DATA, CASES["add-01"], _Broken(), timeout=1))

    assert record["status"] == 500
    assert record["error"] == "RuntimeError"


def test_사례끼리_가짜_BE를_나누어_쓰지_않는다() -> None:
    first, _ = _run_case("add-01", _add_card_1())
    second, _ = _run_case("add-01", _add_card_1())

    assert first["start_cart"] == second["start_cart"] == {}
    assert first["cart"] == second["cart"]


# --- 여러 번, 여러 사례 ---


def _run_all(case_ids: list[str], replies, *, repeat: int = 1):
    cases = [CASES[i] for i in case_ids]
    return asyncio.run(
        run.run_all(
            DATA,
            cases,
            ScriptedModel(replies),
            repeat=repeat,
            concurrency=1,
            timeout=5,
        )
    )


def test_사례마다_반복한_횟수만큼_결과가_쌓인다() -> None:
    results = _run_all(["add-01"], _add_card_1() * 3, repeat=3)

    assert [r["run"] for r in results] == [1, 2, 3]
    assert {r["id"] for r in results} == {"add-01"}
    assert all(r["grade"]["passed"] for r in results)


def test_결과에_종류와_묶음이_붙는다() -> None:
    (result,) = _run_all(["add-01"], _add_card_1())

    assert result["kind"] == "add"
    assert result["group"] == "typical"


def test_결과에_사례_모음_split이_붙는다() -> None:
    (dev,) = _run_all(["add-01"], _add_card_1())
    (held,) = _run_all(["ask-05"], [ai_text("어느 책인가요?")])

    assert dev["split"] == "dev"
    assert held["split"] == "heldout"


def test_요약은_dev와_heldout을_따로_센다() -> None:
    results = _run_all(
        ["add-01", "ask-05"],
        [*_add_card_1(), ai_text("어느 책인가요?")],
    )

    summary = run.summarize(DATA, results, model="m", repeat=1)

    assert summary["by_split"]["dev"] == {"n": 1, "passed": 1}
    assert summary["by_split"]["heldout"] == {"n": 1, "passed": 1}
    report = run.format_report(summary)
    assert "| dev |" in report
    assert "| heldout |" in report


def test_완료_콜백이_사례마다_한_번_불린다() -> None:
    seen: list[str] = []
    cases = [CASES["add-01"]]

    asyncio.run(
        run.run_all(
            DATA,
            cases,
            ScriptedModel(_add_card_1()),
            repeat=1,
            concurrency=1,
            timeout=5,
            on_done=lambda r: seen.append(r["id"]),
        )
    )

    assert seen == ["add-01"]


# --- 요약 ---


def test_요약은_종류별_묶음별_통과와_지표를_낸다() -> None:
    results = _run_all(
        ["add-01", "notshop-01"],
        [*_add_card_1(), ai_text("NOT_SHOPPING")],
    )

    summary = run.summarize(DATA, results, model="m", repeat=1)

    assert summary["total"] == summary["passed"] == 2
    assert summary["by_kind"]["add"] == {"n": 1, "passed": 1}
    assert summary["by_group"]["typical"] == {"n": 2, "passed": 2}
    assert summary["metrics"]["status_counts"] == {"200": 2}
    assert summary["metrics"]["hit_cap"] == 0
    assert summary["metrics"]["injection_failed"] == [0, 0]
    assert summary["failed_cases"] == []


def test_요약은_실패한_사례와_이유를_모은다() -> None:
    results = _run_all(["add-01"], [ai_text("담았어요.")])

    summary = run.summarize(DATA, results, model="m", repeat=1)

    assert summary["passed"] == 0
    assert summary["failed_cases"] == [
        {"id": "add-01", "runs_failed": 1, "failures": ["요청 오류 422"]}
    ]
    assert summary["metrics"]["removed_reasons"] == {"ACTION_NOT_DONE": 1}
    assert summary["metrics"]["status_counts"] == {"422": 1}


def test_확인_없이_장바구니가_바뀐_조합_사례를_센다() -> None:
    case = CASES["pick-01"]
    start = {l["isbn"]: l["qty"] for l in case["state"]["cart"]}
    fake_result = {
        "id": case["id"],
        "kind": case["kind"],
        "group": "typical",
        "split": "dev",
        "run": 1,
        "record": {
            "status": 200,
            "reply": "",
            "data": {"tool_calls": [], "buttons": []},
            "start_cart": start,
            "cart": {"9788900001001": 1},  # 확인 없이 담김
            "start_others": {},
            "others": {},
            "removed": [],
            "llm_calls": 2,
            "iterations": 1,
            "hit_cap": False,
            "tokens_in": 10,
            "tokens_out": 5,
            "ms": 100.0,
        },
    }
    fake_result["grade"] = grade.grade_case(case, fake_result["record"])

    summary = run.summarize(DATA, [fake_result], model="m", repeat=1)

    assert summary["metrics"]["executed_without_confirm"] == 1
    assert summary["passed"] == 0


def test_속이는_입력에_넘어간_횟수를_센다() -> None:
    case = CASES["injection-03"]  # 목록에 없는 ISBN 을 담으라는 요청
    results = _run_all(
        ["injection-03"],
        [ai_text("담았어요.")],  # 도구 없이 담았다고 답해 422 로 실패한다
    )

    summary = run.summarize(DATA, results, model="m", repeat=1)

    assert case["kind"] == "injection"
    assert summary["metrics"]["injection_failed"] == [1, 1]


def test_보고서는_통과율과_실패한_사례를_보여_준다() -> None:
    results = _run_all(["add-01", "notshop-01"], [*_add_card_1(), ai_text("담았어요.")])
    summary = run.summarize(DATA, results, model="m-1", repeat=1)

    report = run.format_report(summary)

    assert "모델 `m-1`" in report
    assert "통과 1/2 (50%)" in report
    assert "| add | 1/1 (100%) |" in report
    assert "`notshop-01`" in report
    assert "턴당 LLM 호출" in report


def test_백분위수는_정렬한_값에서_고른다() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 100.0]

    assert run._percentile(values, 0.95) == 100.0
    assert run._percentile(values, 0.5) == 30.0
    assert run._percentile([], 0.95) == 0.0


# --- 사례 고르기와 실행 옵션 ---


def _args(**kw):
    import argparse

    base = {"ids": "", "kinds": "", "group": "", "split": ""}
    base.update(kw)
    return argparse.Namespace(**base)


def test_사례를_번호_종류_묶음으로_고른다() -> None:
    assert [c["id"] for c in run._select(DATA, _args(ids="add-01,pick-02"))] == [
        "add-01",
        "pick-02",
    ]
    assert {c["kind"] for c in run._select(DATA, _args(kinds="add,pick"))} == {
        "add",
        "pick",
    }
    guard = run._select(DATA, _args(group="guard", split="dev"))
    assert len(guard) == 29
    assert all(DATA["kinds"][c["kind"]]["group"] == "guard" for c in guard)


def test_사례를_dev와_heldout으로_가른다() -> None:
    dev = run._select(DATA, _args(split="dev"))
    heldout = run._select(DATA, _args(split="heldout"))

    assert len(dev) == 95
    assert len(heldout) == 40
    assert {c["id"] for c in dev} | {c["id"] for c in heldout} == set(CASES)
    # split 칸이 없는 옛 사례는 dev 다
    assert all(c.get("split", "dev") == "dev" for c in dev)
    assert all(c["split"] == "heldout" for c in heldout)
    # 고르는 조건은 겹쳐 쓸 수 있다
    assert {
        c["kind"] for c in run._select(DATA, _args(split="heldout", kinds="ask"))
    } == {"ask"}
    assert len(run._select(DATA, _args(split="heldout", kinds="ask"))) == 6


def test_없는_사례_번호나_종류는_실행을_멈춘다() -> None:
    with pytest.raises(SystemExit):
        run._select(DATA, _args(ids="nope-01"))
    with pytest.raises(SystemExit):
        run._select(DATA, _args(kinds="nope"))


def test_LLM_MOCK이_켜져_있으면_실행을_거부한다(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "true")

    with pytest.raises(SystemExit):
        run.main(["--ids", "add-01"])


# --- 다시 채점 (#387) ---


def test_저장한_결과를_모델_없이_지금_기준으로_다시_채점한다(tmp_path, capsys) -> None:
    import json

    results = _run_all(["ask-05"], [ai_text("어느 책인가요?")])
    # 옛 기준이었다면 실패로 센 기록: 쓰기 시도가 코드에 막혔다
    results[0]["record"]["data"]["tool_calls"] = [
        {
            "name": "cart.add",
            "arguments": {"book": {"source": "card", "n": 1}},
            "result": {"ok": False, "error": "REFERENCE_NOT_STATED"},
            "grounded": True,
        }
    ]
    results[0]["grade"] = {"passed": False, "failures": ["옛 기준"], "matched": None}
    summary = run.summarize(DATA, results, model="m", repeat=1)
    path = tmp_path / "saved.json"
    path.write_text(
        json.dumps({"summary": summary, "results": results}), encoding="utf-8"
    )
    out = tmp_path / "regraded.json"

    run.main(["--regrade", str(path), "--out", str(out)])

    redone = json.loads(out.read_text(encoding="utf-8"))
    assert redone["results"][0]["grade"]["passed"] is True
    assert redone["results"][0]["grade"]["blocked_write"] is True
    assert redone["summary"]["metrics"]["blocked_writes"] == 1
    assert "코드가 막은 쓰기 시도" in capsys.readouterr().out


# --- 이력(history)을 요청에 싣는 규칙 (#397) ---


def test_API가_이력을_받으면_history를_recent_turns로_싣는다() -> None:
    case = CASES["followup-01"]

    payload = run.request_payload(case, accepts_history=True)

    assert payload["recent_turns"] == case["history"]
    # 사례 원본은 바뀌지 않는다
    assert "recent_turns" not in case["request"]


def test_API가_이력을_받지_않으면_history를_보내지_않는다() -> None:
    # 이력 칸이 생기기 전에는 같은 사례 파일로 "이력 없음"의 점수를 잰다
    payload = run.request_payload(CASES["followup-01"], accepts_history=False)

    assert "recent_turns" not in payload


def test_이력이_없는_사례는_이력_칸을_만들지_않는다() -> None:
    payload = run.request_payload(CASES["add-01"], accepts_history=True)

    assert "recent_turns" not in payload


def test_지금_API는_이력을_받지_않아_followup도_그대로_돈다() -> None:
    # recent_turns 칸이 없는 동안 followup 사례는 이번 말만 보낸다
    result, _model = _run_case("followup-01", [ai_text("어느 책이요?")])

    assert result["status"] == 200
