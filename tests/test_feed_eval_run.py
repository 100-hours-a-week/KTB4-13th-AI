"""④⑥ 평가 묶음의 실행기(evals/feed/run.py) 테스트(#392). DB 도 서버도 쓰지 않는다.

실제로 서버에 붙는 부분(measure)은 책 211만 권이 든 평가용 DB 가 있어야 해서 여기서 돌리지 않는다.
사용자를 고르고, 차례로 부르고, 채점해서 표로 내는 흐름을 가짜 서버로 본다.
"""

import asyncio
import json
from collections import Counter

import pytest

from evals.feed import run as runner
from evals.feed.scoring import Book, Response

BUNDLE = runner.load_bundle()
CASES = runner.load_cases(BUNDLE)
BY_ID = {case["id"]: case for case in CASES}


def _book(isbn: str) -> Book:
    return Book(
        isbn=isbn,
        title=f"책 {isbn}",
        author="저자",
        category="한국문학",
        kdc="813.7",
        reader="0",
        pages=300,
        pub_year=2020,
        price=15000,
        text="추리 소설",
    )


class FakeServer:
    """부른 순서를 적어 두고, 사용자마다 정해진 책 세 권을 돌려준다."""

    def __init__(self) -> None:
        self.called: list[str] = []
        self.asked: list[set[str]] = []

    async def target(self, case: dict) -> Response:
        self.called.append(case["id"])
        cold = bool(case["expect"].get("cold_start"))
        isbns = tuple(f"{case['user_id']}{n}" for n in range(3))
        return Response(200, 200, cold, None, isbns, 0.01)

    async def facts(self, isbns: set[str]) -> dict[str, Book]:
        self.asked.append(set(isbns))
        return {isbn: _book(isbn) for isbn in isbns}


# ── 묶음 읽기와 사용자 고르기 ──────────────────────────────────────────────


def test_사용자마다_자기_종류의_묶음을_붙여_읽는다() -> None:
    assert len(CASES) == len(BUNDLE["cases"])
    assert {case["group"] for case in CASES} == {"typical", "guard"}
    assert BY_ID["one-field-01"]["group"] == "typical"
    assert BY_ID["dislike-01"]["group"] == "guard"


def test_종류나_번호나_묶음으로_고른다() -> None:
    assert {c["kind"] for c in runner.select(CASES, kinds=["dislike", "author"])} == {
        "dislike",
        "author",
    }
    assert [c["id"] for c in runner.select(CASES, ids=["pile-02", "cold-01"])] == [
        "pile-02",
        "cold-01",
    ]
    assert all(c["group"] == "typical" for c in runner.select(CASES, group="typical"))
    assert len(runner.select(CASES)) == len(CASES)


def test_묶음에_없는_이름으로_고르면_돌리지_않고_알린다() -> None:
    # 오타로 0명을 돌리고 "문제없음"으로 읽히는 일을 막는다.
    with pytest.raises(ValueError, match="dislik"):
        runner.select(CASES, kinds=["dislik"])
    with pytest.raises(ValueError, match="pile-99"):
        runner.select(CASES, ids=["pile-99"])
    with pytest.raises(ValueError, match="hard"):
        runner.select(CASES, group="hard")


# ── 돌리기 ────────────────────────────────────────────────────────────────


def test_고른_사용자를_한_번씩_부르고_책_정보는_한_번에_묻는다() -> None:
    server = FakeServer()
    picked = runner.select(CASES, ids=["one-field-01", "one-field-02"])

    scores = asyncio.run(
        runner.run(picked, BY_ID, server.target, server.facts, BUNDLE["interests"])
    )

    assert server.called == ["one-field-01", "one-field-02"]
    assert [s.case_id for s in scores] == ["one-field-01", "one-field-02"]
    # 책 정보를 사용자마다 따로 물으면 큰 표를 사용자 수만큼 훑는다. 받은 책을 모아 한 번만 묻는다.
    assert len(server.asked) == 1
    assert len(server.asked[0]) == 6


def test_짝이_있는_사용자를_고르면_짝도_부르되_채점은_고른_사용자만_한다() -> None:
    server = FakeServer()
    picked = runner.select(CASES, ids=["dislike-01"])
    twin = BY_ID["dislike-01"]["expect"]["compare_to"]

    scores = asyncio.run(
        runner.run(picked, BY_ID, server.target, server.facts, BUNDLE["interests"])
    )

    assert set(server.called) == {"dislike-01", twin}
    assert [s.case_id for s in scores] == ["dislike-01"]
    # 짝의 목록에서도 같은 관심사를 셌다.
    assert scores[0].twin_show is not None
    assert scores[0].same_as_twin == 0


def test_고른_목록에_없는_짝이_실패해도_재지_못한_사용자로_알린다() -> None:
    from evals.feed.scoring import summarize

    server = FakeServer()
    picked = runner.select(CASES, ids=["dislike-01"])
    twin = BY_ID["dislike-01"]["expect"]["compare_to"]

    async def twin_fails(case: dict) -> Response:
        if case["id"] == twin:
            return Response(200, 500, None, None, (), 0.01)
        return await server.target(case)

    scores = asyncio.run(
        runner.run(picked, BY_ID, twin_fails, server.facts, BUNDLE["interests"])
    )
    text = runner.render(
        summarize(scores, BUNDLE["kinds"], BUNDLE["interests"]), BUNDLE["top"]
    )

    # 짝은 채점 대상이 아니라서, 여기서 막지 않으면 실패가 어디에도 드러나지 않는다.
    assert scores[0].ok is False
    assert "재지 못한 사용자 1명" in text


def test_짝도_같이_고르면_두_번_부르지_않는다() -> None:
    server = FakeServer()
    twin = BY_ID["dislike-01"]["expect"]["compare_to"]
    picked = runner.select(CASES, ids=["dislike-01", twin])

    asyncio.run(
        runner.run(picked, BY_ID, server.target, server.facts, BUNDLE["interests"])
    )

    assert sorted(server.called) == sorted(["dislike-01", twin])


def test_가진_책의_정보도_같이_묻는다() -> None:
    # 다른 판본을 세려면 가진 책의 제목을 알아야 한다. 가진 책은 목록에 나오지 않으니 따로 물어야 한다.
    server = FakeServer()
    picked = runner.select(CASES, ids=["owned-01"])
    history = BY_ID["owned-01"]["user"]["history"]

    asyncio.run(
        runner.run(picked, BY_ID, server.target, server.facts, BUNDLE["interests"])
    )

    assert {*history["purchased_isbns"], *history["library_isbns"]} <= server.asked[0]


# ── 어느 DB 에 붙는가 ─────────────────────────────────────────────────────


def test_평가용이_아닌_DB_에는_돌리지_않는다() -> None:
    # 실행기는 가상 사용자의 프로필을 DB 에 쓴다. 개발용이나 운영 DB 에 쓰면 안 된다.
    assert runner.refusal("bookjeok_ai_eval", allow_any=False) is None
    assert "bookjeok_ai_dev" in runner.refusal("bookjeok_ai_dev", allow_any=False)
    assert runner.refusal("bookjeok_ai_dev", allow_any=True) is None


def test_DB_주소에서_이름만_꺼낸다() -> None:
    # 주소에는 계정과 비밀번호가 들어 있을 수 있어 그대로 찍지 않는다.
    assert runner.database_name("postgresql://user:pw@host:5432/bookjeok_ai_eval") == (
        "bookjeok_ai_eval"
    )
    assert runner.database_name("postgresql://localhost/bookjeok_ai_eval?x=1") == (
        "bookjeok_ai_eval"
    )


# ── 결과 표 ──────────────────────────────────────────────────────────────


def _run_all(ids: list[str]):
    server = FakeServer()
    picked = runner.select(CASES, ids=ids)
    return asyncio.run(
        runner.run(picked, BY_ID, server.target, server.facts, BUNDLE["interests"])
    )


def test_결과_표에_종류와_묶음이_나오고_거친_기준은_표시된다() -> None:
    from evals.feed.scoring import summarize

    scores = _run_all(
        ["one-field-01", "one-topic-01", "dislike-01", "pile-01", "pile-02"]
    )
    text = runner.render(
        summarize(scores, BUNDLE["kinds"], BUNDLE["interests"]), BUNDLE["top"]
    )

    assert "| one-field |" in text
    assert "| **typical 합계** |" in text
    # 추리는 낱말로 가르는 거친 기준이다. 표에서 알아볼 수 있어야 한다.
    assert "| one-topic † |" in text
    assert "짝과 견줌" in text
    assert "책이 쌓인 사용자" in text


def test_재지_못한_사용자가_있으면_표에서_알린다() -> None:
    from evals.feed.scoring import summarize

    async def failing(case: dict) -> Response:
        return Response(200, 500, None, None, (), 0.01)

    server = FakeServer()
    picked = runner.select(CASES, ids=["one-field-01"])
    scores = asyncio.run(
        runner.run(picked, BY_ID, failing, server.facts, BUNDLE["interests"])
    )
    text = runner.render(
        summarize(scores, BUNDLE["kinds"], BUNDLE["interests"]), BUNDLE["top"]
    )

    assert "재지 못한 사용자 1명" in text


# ── 명령 ─────────────────────────────────────────────────────────────────


def test_yes_없이는_부르지_않고_몇_명을_돌릴지만_알린다(capsys) -> None:
    server = FakeServer()

    code = runner.main(["--kinds", "dislike"], target=server.target, facts=server.facts)

    out = capsys.readouterr().out
    assert code == 0
    assert server.called == []
    # 읽지 않는 주제 10명과, 견줄 짝 10명
    assert "사용자 10명" in out and "짝 10명" in out
    assert "--yes" in out


def test_yes_를_붙이면_돌리고_표를_낸다(capsys) -> None:
    server = FakeServer()

    code = runner.main(
        ["--ids", "one-field-01,cold-01", "--yes"],
        target=server.target,
        facts=server.facts,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert server.called == ["one-field-01", "cold-01"]
    assert "| one-field |" in out and "| cold |" in out


def test_없는_이름을_주면_돌리지_않고_끝낸다(capsys) -> None:
    server = FakeServer()

    code = runner.main(
        ["--kinds", "nope", "--yes"], target=server.target, facts=server.facts
    )

    assert code == 2
    assert server.called == []
    assert "nope" in capsys.readouterr().err


def test_out_을_주면_사용자마다의_결과를_파일로_남긴다(tmp_path, capsys) -> None:
    server = FakeServer()
    path = tmp_path / "result.json"

    runner.main(
        ["--ids", "one-field-01", "--yes", "--out", str(path)],
        target=server.target,
        facts=server.facts,
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["bundle_version"] == BUNDLE["version"]
    # 어느 책 표로 쟀는지가 같이 남아야 숫자를 견줄 수 있다.
    assert saved["catalog"] == BUNDLE["catalog"]
    assert saved["scores"][0]["case_id"] == "one-field-01"
    assert len(saved["scores"][0]["isbns"]) == 3
    assert "kinds" in saved["summary"]


# ── 기준선 ───────────────────────────────────────────────────────────────


def test_out_의_폴더가_없으면_만든다(tmp_path, capsys) -> None:
    # 다 돌린 뒤에 폴더가 없어 저장하지 못하면 결과가 사라진다.
    server = FakeServer()
    path = tmp_path / "results" / "after.json"

    code = runner.main(
        ["--ids", "one-field-01", "--yes", "--out", str(path)],
        target=server.target,
        facts=server.facts,
    )

    assert code == 0
    assert json.loads(path.read_text(encoding="utf-8"))["scores"]


def test_기준선은_지금_묶음으로_잰_것이다() -> None:
    # 사용자를 고치고 기준선을 다시 재지 않으면, 바꾼 뒤의 숫자를 낡은 기준선과 견주게 된다.
    baseline = json.loads(
        runner.CASES_PATH.with_name("baseline.json").read_text(encoding="utf-8")
    )
    kinds = baseline["summary"]["kinds"]

    assert baseline["bundle_version"] == BUNDLE["version"]
    assert baseline["catalog"] == BUNDLE["catalog"]
    assert {kind: row["users"] for kind, row in kinds.items()} == dict(
        Counter(case["kind"] for case in CASES)
    )
    # 재지 못한 사용자가 있는 기준선은 견줄 기준이 못 된다.
    assert all(row["measured"] == row["users"] for row in kinds.values())
