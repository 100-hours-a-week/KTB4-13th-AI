"""비동기 홈 목록 부하 측정(evals/feed/async_load.py) 테스트(#603). 서버·DB·AWS 없이 돈다.

서버에 붙어 끝까지 도는 것은 로컬에서 손으로 확인했다(PR 본문). 서비스를 내리는 길은 실험 서버에서 처음 돈다.
"""

import asyncio
import json

import pytest

from evals.feed import async_load


def _cases() -> list[dict]:
    return json.loads(async_load.CASES_PATH.read_text(encoding="utf-8"))["cases"]


def test_취향_기억_벡터_용도는_앱과_같다() -> None:
    from app.extraction.service import MEMORY_VECTOR_PURPOSE

    assert async_load.MEMORY_VECTOR_PURPOSE == MEMORY_VECTOR_PURPOSE


def test_재료가_있는_가상_사용자만_앞에서부터() -> None:
    users = async_load.pick_users(_cases(), 31)
    assert len(users) == 31
    assert not any(c["expect"].get("cold_start") for c in users)
    with pytest.raises(ValueError, match="가상 사용자"):
        async_load.pick_users(_cases(), 1000)


def test_요청은_평가_실행기와_같은_모양() -> None:
    case = _cases()[0]
    memories = async_load.memory_body([{"keyword": "요리"}], [[0.1, 0.2]])
    body = async_load.profile_body(case, memories, "seq31")
    assert body["user_id"] == case["user_id"]
    assert body["idempotency_key"] == f"load_seq31_{case['id']}"
    assert body["memories"] == [{"keyword": "요리", "vector": [0.1, 0.2], "dim": 2}]
    assert set(body) == {
        "user_id",
        "idempotency_key",
        "onboarding",
        "memories",
        "history",
    }


def test_이름에_exp_가_없으면_내리지_않고_실험_DB_가_아니면_돌리지_않는다() -> None:
    assert async_load.refusal_service("bookjeok-exp-cluster", "exp-ai-service") is None
    assert "내리지 않는다" in async_load.refusal_service(
        "prod-ecs-cluster", "prod-ai-service"
    )
    assert "내리지 않는다" in async_load.refusal_service(
        "exp-cluster", "prod-ai-service"
    )
    assert async_load.refusal_db("postgresql://u:p@h/bookjeok_ai_eval") is None
    assert "실험용이 아니다" in async_load.refusal_db("postgresql://u:p@h/bookjeok_ai")


def test_목록이_다_만들어진_때를_사용자마다_보낸_때부터_잰다() -> None:
    left = iter([{1, 2}, {2}, set()])
    ticks = iter([10.0, 11.0, 12.5, 14.0])

    async def pending() -> set[int]:
        return next(left)

    got = asyncio.run(
        async_load.wait_drained(
            pending, {1: 10.0, 2: 10.5}, poll=0, clock=lambda: next(ticks)
        )
    )
    assert got == {1: 2.5, 2: 3.5}


def test_한도를_넘기면_안_만들어진_사용자는_None() -> None:
    ticks = iter([0.0, 1.0, 5.0])

    async def pending() -> set[int]:
        return {2}

    got = asyncio.run(
        async_load.wait_drained(
            pending, {1: 0.0, 2: 0.0}, limit=3, poll=0, clock=lambda: next(ticks)
        )
    )
    assert got == {1: 1.0, 2: None}


def test_검색_시간은_보내기_전과_보내는_동안을_나눠_견준다() -> None:
    prober = async_load.Prober(lambda *_: None, ["q"], 1.0)
    # 20개 중 95% 자리(19번째)는 위에서 두 번째 값이다
    prober.samples = [("before", 0.1, 200)] * 18 + [("before", 0.2, 200)] * 2
    prober.samples += [("during", 0.1, 200)] * 18 + [("during", 0.3, 200)] * 2
    prober.samples += [("during", 9.0, 500)]  # 실패한 검색은 시간에 넣지 않고 따로 센다
    got = prober.summary()
    assert got["before"]["p95"] == 0.2
    assert got["during"]["p95"] == 0.3
    assert got["during"]["errors"] == 1
    assert got["p95_ratio"] == 1.5
    assert got["after"]["n"] == 0


def test_할_일만_보여_주고_보내지_않는다(capsys, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/bookjeok_ai_eval")
    code = async_load.main(
        ["--base-url", "http://x:8000", "--pattern", "burst", "--users", "10"]
    )
    assert code == 0
    assert "--yes" in capsys.readouterr().out


def test_내리려면_실험_서비스_이름이_있어야_한다(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/bookjeok_ai_eval")
    args = [
        "--base-url",
        "http://x:8000",
        "--pattern",
        "burst",
        "--users",
        "10",
        "--stop-after",
        "120",
    ]
    assert async_load.main(args) == 2
    assert async_load.main([*args, "--cluster", "prod", "--service", "prod-ai"]) == 2
