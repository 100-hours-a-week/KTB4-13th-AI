"""① 검색 평가 실행기(evals/search/run.py) 테스트(#495). DB 와 앱 없이 돈다."""

import json
import subprocess
from pathlib import Path

import pytest

from evals.search import run
from evals.search.scoring import Book

_CASES = {case["id"]: case for case in run.load_bundle()["cases"]}


def test_사례를_종류와_번호로_고르고_없는_이름이면_멈춘다() -> None:
    cases = list(_CASES.values())
    assert {c["kind"] for c in run.select(cases, kinds=["author"])} == {"author"}
    assert [c["id"] for c in run.select(cases, ids=["filter-02"])] == ["filter-02"]
    with pytest.raises(ValueError, match="autor"):
        run.select(cases, kinds=["autor"])


def test_평가_DB_가_아니면_돌리지_않는다() -> None:
    assert run.refusal("bookjeok_ai_eval", allow_any=False) is None
    assert "_eval" in run.refusal("bookjeok_ai", allow_any=False)
    assert run.refusal("bookjeok_ai", allow_any=True) is None


def test_요청은_위_top_권을_필터와_함께_한_번에_받는다() -> None:
    assert run.request(_CASES["filter-01"], 20) == {
        "query": "사랑",
        "filters": {"category": "소설"},
        "size": 20,
    }
    assert run.request(_CASES["nonsense-1"], 20)["filters"] == {}


def test_잰_코드의_커밋을_적고_app_을_고친_채면_그렇다고_붙인다(tmp_path: Path) -> None:
    assert run.code_label(tmp_path) == tmp_path.name  # git 이 아니면 폴더 이름

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "vector.py").write_text("EF_SEARCH = 1000\n")
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    assert "고친" not in run.code_label(tmp_path)
    (tmp_path / "app" / "vector.py").write_text("EF_SEARCH = 200\n")
    assert run.code_label(tmp_path).endswith("(app 을 고친 채)")


def _shelf() -> dict[int, Book]:
    book = Book(
        book_id=1,
        isbn="9780000000001",
        title=_CASES["title-001"]["query"],
        author=_CASES["author-01"]["query"],
        category="한국문학",
        pub_year=2020,
        price=10000,
    )
    return {1: book}


def test_골라서_돌리고_표와_사례마다의_결과를_낸다(tmp_path: Path, capsys) -> None:
    def search(requests: list[dict]) -> list[dict]:
        ok = {"status": 200, "degraded": None, "book_ids": [1], "seconds": 0.1}
        failed = {"status": 500, "degraded": None, "book_ids": [], "seconds": 0.1}
        return [failed if r["query"] == "ㅁㄴㅇㄹㅍ" else ok for r in requests]

    out = tmp_path / "results" / "r.json"
    ids = "title-001,author-01,filter-01,nonsense-1"
    argv = ["--ids", ids, "--yes", "--out", str(out)]
    assert run.main(argv, search=search, facts=lambda ids: _shelf()) == 0

    printed = capsys.readouterr().out
    assert "| title | 1 | 100.0% | 100.0% | 100.0% | 1.000 | 0 |" in printed
    assert "위 10권 중 그 저자의 책 0.10" in printed
    assert "조건을 어긴 책 0권(0건)" in printed
    assert "재지 못한 사례 1건" in printed
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [s["id"] for s in saved["scores"]] == ids.split(",")
    assert saved["scores"][0]["book_ids"] == [1]
    assert saved["summary"]["kinds"]["title"]["hit_1"] == 1.0


def test_서술형은_매긴_점수로_위_10권의_nDCG_를_소분류별로도_낸다(capsys) -> None:
    labels = json.loads(
        Path(run.__file__).with_name("labels.json").read_text(encoding="utf-8")
    )["labels"]
    isbn = next(i for i, grade in labels["descriptive-01"].items() if grade == 2)
    book = Book(
        book_id=1,
        isbn=isbn,
        title="책",
        author=None,
        category=None,
        pub_year=None,
        price=None,
    )

    def search(requests: list[dict]) -> list[dict]:
        return [{"status": 200, "degraded": None, "book_ids": [1, 2], "seconds": 0.1}]

    argv = ["--ids", "descriptive-01", "--yes"]
    assert run.main(argv, search=search, facts=lambda ids: {1: book}) == 0

    printed = capsys.readouterr().out
    # 2점 책이 1등이고, 책 표에 없는 2번은 점수 없는 책이다. 이상값(위 10권이 모두 2점)으로 나눠 3 / 13.63.
    # nDCG′ 는 점수 없는 책을 빼고 재도 2점 책 하나라 같고, 10권 중 점수가 있는 책은 1권(10%)이다
    assert "| descriptive | 1 | 0.220 | 0.220 | 10.0% | 1.0 | 0.0 | 1 |" in printed
    assert "| ↳ topic | 1 | 0.220 |" in printed


def test_yes_가_없으면_부르지_않고_모르는_종류나_코드_폴더면_멈춘다() -> None:
    def search(requests: list[dict]) -> list[dict]:
        raise AssertionError("부르면 안 된다")

    assert run.main(["--kinds", "author"], search=search, facts=dict) == 0
    assert run.main(["--kinds", "autor", "--yes"], search=search, facts=dict) == 2
    assert run.main(["--code", "/없는/폴더", "--yes"], search=search, facts=dict) == 2


def _book(book_id: int, isbn: str | None = None, author: str | None = None) -> Book:
    return Book(
        book_id=book_id,
        isbn=isbn,
        title=f"책{book_id}",
        author=author,
        category=None,
        pub_year=None,
        price=None,
    )


def test_첫_화면은_인기순_위_12권으로_받고_12권을_본다(capsys) -> None:
    sent = []

    def search(requests: list[dict]) -> list[dict]:
        sent.extend(requests)
        return [
            {
                "status": 200,
                "degraded": None,
                "book_ids": list(range(1, 13)),
                "seconds": 0.1,
            }
        ]

    shelf = {i: _book(i, author="전순선" if i == 12 else None) for i in range(1, 13)}
    assert (
        run.main(
            ["--ids", "author-01", "--screen", "--yes"],
            search=search,
            facts=lambda ids: shelf,
        )
        == 0
    )

    assert sent == [{"query": "전순선", "filters": {}, "size": 12, "sort": "popular"}]
    printed = capsys.readouterr().out
    # 12번째 책이 그 저자의 책이라 12권을 보면 1/12, 10권만 보면 0이다
    assert "popular 정렬, 위 12권 기준" in printed
    assert "위 12권 중 그 저자의 책 0.08" in printed


def test_첫_화면의_제목은_1등과_12등_안을_본다(capsys) -> None:
    def search(requests: list[dict]) -> list[dict]:
        return [{"status": 200, "degraded": None, "book_ids": [1], "seconds": 0.1}]

    title = run.load_bundle()["cases"][0]
    shelf = {
        1: Book(
            book_id=1,
            isbn=None,
            title=title["expect"]["titles"][0],
            author=None,
            category=None,
            pub_year=None,
            price=None,
        )
    }
    assert (
        run.main(
            ["--ids", title["id"], "--screen", "--yes"],
            search=search,
            facts=lambda ids: shelf,
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "| 종류 | 사례 | 1등 안 | 12등 안 | MRR |" in printed


def test_값을_바꾸는_이름은_이름_값_꼴이어야_한다() -> None:
    assert run.parse_sets(["keyword_weight=1", " min_coverage = 0.55 "]) == {
        "keyword_weight": "1",
        "min_coverage": "0.55",
    }
    for bad in ("keyword_weight", "=1", "keyword_weight="):
        with pytest.raises(ValueError, match="이름=값"):
            run.parse_sets([bad])
