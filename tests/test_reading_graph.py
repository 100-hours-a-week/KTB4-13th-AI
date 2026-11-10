"""③ 함께 대출·마니아 그래프(app/chat/reading_graph.py)와 파일 만들기. DB 없이 돈다."""

import gzip
import json

import pytest

from app.chat import reading_graph


def _book(isbn: str, name: str, authors: str) -> dict:
    return {"book": {"isbn13": isbn, "bookname": name, "authors": authors}}


def _line(isbn: str, name: str, authors: str, **lists: list[dict]) -> str:
    response = {"book": {"isbn13": isbn, "bookname": name, "authors": authors}}
    response.update(lists)
    return json.dumps(
        {"isbn13": isbn, "resp": {"response": response}}, ensure_ascii=False
    )


@pytest.mark.parametrize(
    ("title", "author", "key"),
    [
        ("아몬드 :손원평 장편소설", "지은이: 손원평", "아몬드|손원평"),
        # 11/04 버그 1: "(" 로 시작하는 제목이 빈 제목이 돼 같은 저자 책이 합쳐졌다
        (
            "(이상한 과자 가게) 전천당",
            "지은이: 히로시마 레이코",
            "전천당|히로시마레이코",
        ),
        # 11/04 버그 2: 괄호 역할 말·붙은 역할 말이 안 떼어져 같은 책이 갈렸다
        (
            "사피엔스",
            "유발 하라리 (원작), 다니엘 카사나브 (그림)",
            "사피엔스|유발하라리",
        ),
        ("사피엔스", "원작·각색: 유발 하라리 ;옮긴이: 김명주", "사피엔스|유발하라리"),
        ("페인트 (양장)", "이희영 (지은이)", "페인트|이희영"),
        ("파친코 :이민진 장편소설", "이민진 지음 ;이미정 옮김", "파친코|이민진"),
    ],
)
def test_작품_키는_부제_앞_괄호_말_역할_말을_뗀다(
    title: str, author: str, key: str
) -> None:
    assert reading_graph.work_key(title, author) == key


def test_제목이나_저자가_비면_작품_키가_없다() -> None:
    assert reading_graph.work_key("", "손원평") is None
    assert reading_graph.work_key("아몬드", "") is None
    # 괄호뿐인 제목은 괄호만 지운 글자를 쓴다
    assert (
        reading_graph.work_key("(전천당)", "히로시마 레이코") == "전천당|히로시마레이코"
    )


def _graph() -> reading_graph.ReadingGraph:
    lines = [
        _line(
            "1",
            "데미안",
            "헤르만 헤세 지음",
            coLoanBooks=[
                _book("9", "불편한 편의점", "김호연"),
                _book("2", "수레바퀴 아래서", "헤르만 헤세"),
            ],
            maniaRecBooks=[
                _book("3", "죄와 벌", "도스토옙스키"),
                _book("1", "데미안 :큰글자", "헤르만 헤세"),
            ],
            readerRecBooks=[_book("4", "다독자 책", "누군가")],
        ),
        # 같은 작품의 다른 판본도 목록을 받았다. 작품 하나로 합친다
        _line(
            "11",
            "데미안 (양장)",
            "지은이: 헤르만 헤세",
            coLoanBooks=[_book("5", "인간 실격", "다자이 오사무")],
        ),
        _line(
            "6", "아몬드", "손원평", coLoanBooks=[_book("9", "불편한 편의점", "김호연")]
        ),
        _line(
            "7", "파친코", "이민진", coLoanBooks=[_book("9", "불편한 편의점", "김호연")]
        ),
        # 받는 중에 덜 쓰인 줄은 건너뛴다
        '{"isbn13": "8", "resp": ',
    ]
    return reading_graph.from_data(reading_graph.build(lines))


def test_목록은_종류마다_따로_방향을_살려_작품으로_묶는다() -> None:
    graph = _graph()
    demian = graph.work_of("1")
    assert graph.work_of("11") == demian

    def titles(work: int, kind: str) -> set[str]:
        return {graph.keys[w].split("|")[0] for w in graph.neighbors(work, kind)}

    assert titles(demian, "co") == {"불편한편의점", "수레바퀴아래서", "인간실격"}
    # 자기 자신(다른 판본)은 이웃이 아니다. 다독자 목록은 쓰지 않는다
    assert titles(demian, "mania") == {"죄와벌"}
    # 방향: 데미안 목록에 수레바퀴가 있다고 수레바퀴의 이웃에 데미안이 생기지 않는다
    assert graph.neighbors(graph.work_of("2"), "co") == ()
    assert not graph.has_lists(graph.work_of("2"))


def test_남의_목록에_든_횟수를_세고_허브를_빼고_덜_든_책부터_고른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reading_graph, "HUB_TOP", 1)
    graph = _graph()
    hub = graph.work_of("9")
    assert graph.listed["co"][hub] == 3
    assert graph.hubs == frozenset({hub})

    picked = graph.pick(graph.work_of("1"), "co", 3)
    assert hub not in picked
    assert {graph.keys[w].split("|")[0] for w in picked} == {
        "수레바퀴아래서",
        "인간실격",
    }
    # usable 이 아니라고 한 작품은 건너뛴다
    skipped = graph.work_of("2")
    assert skipped not in graph.pick(
        graph.work_of("1"), "co", 3, lambda w: w != skipped
    )


def test_ISBN_을_모르는_판본은_제목_저자로_작품을_찾는다() -> None:
    graph = _graph()
    assert graph.work_of(
        "999", "데미안 :헤르만 헤세 장편소설", "헤르만 헤세"
    ) == graph.work_of("1")
    assert graph.work_of("999", "없는 책", "누군가") is None


def test_파일로_쓰고_읽으면_같은_그래프다(tmp_path) -> None:
    lines = [
        _line(
            "1",
            "데미안",
            "헤르만 헤세",
            coLoanBooks=[_book("2", "수레바퀴 아래서", "헤르만 헤세")],
        )
    ]
    path = tmp_path / "graph.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(reading_graph.build(lines), f)

    graph = reading_graph.load(path)

    assert graph is not None
    assert len(graph.neighbors(graph.work_of("1"), "co")) == 1
    assert graph.isbns[graph.work_of("2")] == ("2",)


def test_파일이_없거나_형식이_다르면_None_이다(tmp_path) -> None:
    assert reading_graph.load(tmp_path / "없음.json.gz") is None
    path = tmp_path / "old.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"version": 0}, f)
    assert reading_graph.load(path) is None
