"""검색 시간 회차 이어 돌리기(evals/searchlab/rounds.py) 테스트(#595). AWS·DB 없이 돈다."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evals.searchlab import rounds

PLANS = Path(rounds.__file__).parent / "plans"


def test_이름에_exp_가_없는_DB_는_재부팅하지_않는다() -> None:
    assert rounds.refusal("bookjeok-ai-exp") is None
    assert "재부팅하지 않는다" in rounds.refusal("bookjeok-ai-prod")
    assert "재부팅하지 않는다" in rounds.refusal("bookjeok-ai-dev")


def test_같은_설정을_두_번_재면_몇_번째인지_붙인다(tmp_path) -> None:
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {"name": "p", "rounds": [{"label": "a"}, {"label": "b"}, {"label": "a"}]}
        )
    )
    plan = rounds.load_plan(path)
    assert [r["label"] for r in plan["rounds"]] == ["a-1", "b-1", "a-2"]
    assert plan["states"] == ["first", "repeat"]
    assert plan["warmup"] is True
    assert plan["clear"] == "reboot"


def test_저장소의_계획은_읽히고_A1_은_앞뒤를_뒤집은_순서다() -> None:
    for path in PLANS.glob("*.json"):
        plan = rounds.load_plan(path)
        labels = [r["label"] for r in plan["rounds"]]
        assert len(set(labels)) == len(labels), path.name
    a1 = rounds.load_plan(PLANS / "a1-ef.json")
    values = [r["ef_search"] for r in a1["rounds"]]
    assert values == [100, 200, 400, 1000, 1000, 400, 200, 100]
    a5 = rounds.load_plan(PLANS / "a5-eic.json")
    assert [r["effective_io_concurrency"] for r in a5["rounds"]] == [16, 64, 64, 16]


def test_처음_조회만_섞고_데우고_반복_조회는_그대로() -> None:
    plan = {"name": "p", "warmup": True, "kinds": ["title"], "limit": 5}
    rnd = {"label": "ef100", "ef_search": 100, "random_page_cost": 1.1}
    first = rounds.measure_args(plan, rnd, 3, "first")
    assert first[:4] == ["--label", "p/ef100", "--state", "first"]
    assert "--warmup" in first
    assert first[first.index("--order-seed") + 1] == "p-3"
    assert first[first.index("--ef-search") + 1] == "100"
    assert first[first.index("--random-page-cost") + 1] == "1.1"
    assert "--effective-io-concurrency" not in first
    assert first[first.index("--kinds") + 1] == "title"
    assert "--pgss" in first
    repeat = rounds.measure_args(plan, rnd, 3, "repeat")
    assert "--warmup" not in repeat and "--order-seed" not in repeat
    assert "--any-db" in rounds.measure_args(
        {"name": "p", "warmup": False, "any_db": True}, rnd, 1, "first"
    )


class _Aws:
    def __init__(self, metric_error: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.metric_error = metric_error

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "cloudwatch":
            name = args[args.index("--metric-name") + 1]
            if name == self.metric_error:
                raise subprocess.CalledProcessError(254, "aws", stderr="AccessDenied")
            return json.dumps(
                {
                    "Datapoints": [
                        {"Timestamp": "t2", "Average": 2.0},
                        {"Timestamp": "t1", "Average": 1.0, "Maximum": 3.0},
                    ]
                }
            )
        return "{}"


def test_재부팅은_기다림까지_정지_시작은_멈춤도_기다린다() -> None:
    aws = _Aws()
    rounds.clear_cache(aws, "x-exp", "reboot")
    assert [c[:2] for c in aws.calls] == [
        ["rds", "reboot-db-instance"],
        ["rds", "wait"],
    ]
    aws = _Aws()
    rounds.clear_cache(aws, "x-exp", "stop-start")
    assert [c[1] for c in aws.calls] == [
        "stop-db-instance",
        "wait",
        "start-db-instance",
        "wait",
    ]
    assert aws.calls[1][2] == "db-instance-stopped"
    with pytest.raises(ValueError):
        rounds.clear_cache(_Aws(), "x-exp", "flush")


def test_지표는_시간순으로_읽고_하나가_막혀도_나머지는_읽는다() -> None:
    aws = _Aws(metric_error="CPUCreditBalance")
    start = datetime(2026, 11, 6, tzinfo=UTC)
    got = rounds.metrics(
        aws,
        "AWS/RDS",
        ("DBInstanceIdentifier", "x-exp"),
        ["ReadIOPS", "CPUCreditBalance"],
        start,
        start,
    )
    assert got["ReadIOPS"] == [
        {"t": "t1", "avg": 1.0, "max": 3.0},
        {"t": "t2", "avg": 2.0, "max": None},
    ]
    assert "error" in got["CPUCreditBalance"][0]


def _clock():
    t = [datetime(2026, 11, 6, 9, 0, tzinfo=UTC)]

    def now():
        t[0] += timedelta(seconds=30)
        return t[0]

    return now


def test_회차마다_비우고_다시_켜졌는지_보고_두_상태를_잰다() -> None:
    aws = _Aws()
    boots = iter(["b0", "b1", "b1", "b2"])
    measured: list[list[str]] = []
    written: list[dict] = []
    plan = {
        "name": "p",
        "rounds": [{"label": "a"}, {"label": "b"}],
        "states": ["first", "repeat"],
        "warmup": True,
        "clear": "reboot",
    }
    rounds.run_plan(
        plan,
        url="postgresql://x/db_eval",
        db_instance="x-exp",
        aws=aws,
        measure=measured.append,
        started=lambda _url: next(boots),
        bench=lambda: 0.5,
        now=_clock(),
        write=written.append,
        ec2_instance="i-test",
    )
    assert [m[1] for m in measured] == ["p/a", "p/a", "p/b", "p/b"]
    assert [w["db_started"] for w in written] == ["b1", "b2"]
    assert written[0]["cleared"] == "reboot"
    assert set(written[0]["rds"]) == set(rounds.RDS_METRICS)
    assert set(written[0]["ec2"]) == set(rounds.EC2_METRICS)
    assert sum(c[1] == "reboot-db-instance" for c in aws.calls) == 2


def test_다시_켜지지_않았으면_그_회차에서_멈춘다() -> None:
    plan = {
        "name": "p",
        "rounds": [{"label": "a"}],
        "states": ["first"],
        "warmup": False,
        "clear": "reboot",
    }
    with pytest.raises(RuntimeError, match="다시 켜지지 않았다"):
        rounds.run_plan(
            plan,
            url="u",
            db_instance="x-exp",
            aws=_Aws(),
            measure=lambda _a: None,
            started=lambda _url: "same",
            bench=lambda: 0.5,
            now=_clock(),
            write=lambda _l: None,
        )


def test_DB_를_주지_않으면_재부팅도_지표도_없이_잰다() -> None:
    aws = _Aws()
    written: list[dict] = []
    plan = {
        "name": "p",
        "rounds": [{"label": "a"}],
        "states": ["first"],
        "warmup": False,
        "clear": "reboot",
    }
    rounds.run_plan(
        plan,
        url="u",
        db_instance=None,
        aws=aws,
        measure=lambda _a: None,
        started=lambda _url: "b0",
        bench=lambda: 0.5,
        now=_clock(),
        write=written.append,
    )
    assert aws.calls == []
    assert written[0]["cleared"] is None and "rds" not in written[0]
