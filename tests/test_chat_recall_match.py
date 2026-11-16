"""떠올린 제목을 책 표의 책에 붙이는 규칙(app/chat/recall_match.py, #737) 테스트.

책 이름과 저자는 11/12 평가 DB 실측(서술형 40문항, gpt-4o)에서 그대로 가져왔다. 놓쳤던 진짜 책은 붙고, 모델이 지어낸
제목은 이름이 같은 다른 진짜 책에 붙지 않는지를 본다.
"""

import asyncio

import pytest

from app.chat import recall_match
from app.photo import catalog, match_cover

_next_id = iter(range(1, 10_000))


def _book(
    title: str,
    author: str | None,
    publisher: str | None = None,
    *,
    pub_year: int | None = 2020,
    in_stock: bool = True,
) -> catalog.Book:
    return catalog.Book(
        book_id=next(_next_id),
        isbn=None,
        title=title,
        author=author,
        publisher=publisher,
        pub_year=pub_year,
        price=15000,
        in_stock=in_stock,
        cover_url="https://img/1.jpg",
    )


# --- 놓쳤던 진짜 책 -------------------------------------------------------------------------------------------


def test_제목과_저자가_맞는_책이_여러_묶음이어도_판본_하나를_고른다() -> None:
    # 출판사가 다르고 첫 저자 표기도 달라 판본 묶음이 셋으로 갈린다. 표지 규칙은 후보로 넘긴다
    books = [
        _book("어린 왕자", "생텍쥐페리, 모윤희, 심수근", "두산동아", pub_year=2009),
        _book("어린 왕자", "앙투안 드 생텍쥐페리, 김향수", "웅진씽크빅", pub_year=2015),
        _book(
            "어린 왕자", "앙투안 드 생텍쥐페리, 심영아", "웅진씽크빅", in_stock=False
        ),
    ]
    strict = match_cover.decide(recall_match._cover("어린 왕자", "생텍쥐페리"), books)
    assert strict.kind == "candidates"

    found = recall_match.decide("어린 왕자", "생텍쥐페리", books)

    assert [b.title for b in found] == ["어린 왕자"] * 3
    assert found[0] == books[1]  # 살 수 있는 새 판


def test_로마자_제목도_제목과_저자가_맞으면_붙인다() -> None:
    books = [
        _book("1Q84", "무라카미 하루키, 양윤옥", "문학동네", pub_year=2009),
        _book("1Q84", "무라카미 하루키, 양윤옥", "문학동네", pub_year=2010),
    ]

    found = recall_match.decide("1Q84", "무라카미 하루키", books)

    assert found[0] == books[1]


@pytest.mark.parametrize(
    ("title", "recalled_author", "catalog_author"),
    [
        ("그리고 아무도 없었다", "애거사 크리스티", "애거서 크리스티, 김남주"),
        ("반지의 제왕", "존 로널드 루엘 톨킨", "J.R.R. 톨킨, 김보원, 김번, 이미애"),
        ("비폭력 대화", "마셜 로젠버그", "마셜 B. 로젠버그, 캐서린 한"),
        ("시간의 주름", "마들렌 렝글", "매들렌 렝글"),
        ("종이달", "가쿠타 미쓰요", "가쿠다 미쓰요"),
        (
            "그리고 아무도 없었다",
            "애거사 크리스티",
            "애거서 크리스티 (Agatha Christie)",
        ),
    ],
)
def test_제목이_같고_여러_낱말_이름의_마지막_낱말이_같으면_같은_사람으로_본다(
    title: str, recalled_author: str, catalog_author: str
) -> None:
    book = _book(title, catalog_author)

    assert recall_match.decide(title, recalled_author, [book]) == [book]


def test_이름이_그대로_맞는_묶음이_하나면_표지_규칙대로_그_묶음만_준다() -> None:
    loose = _book("반지의 제왕", "J.R.R. 톨킨", pub_year=2024)
    exact = _book("반지의 제왕", "존 로널드 루엘 톨킨", pub_year=2002)

    found = recall_match.decide("반지의 제왕", "존 로널드 루엘 톨킨", [loose, exact])

    assert found == [exact]


def test_여러_묶음에서_고를_때_이름이_그대로_맞는_판본이_마지막_낱말만_맞는_판본보다_앞이다() -> (
    None
):
    # "루엘 톨킨"은 이름 끝이 같아 표지 규칙에서도 같은 사람이지만 첫 저자 키가 달라 묶음이 갈린다
    loose = _book("반지의 제왕", "J.R.R. 톨킨", pub_year=2024)
    exact_old = _book("반지의 제왕", "존 로널드 루엘 톨킨", pub_year=2002)
    exact_new = _book("반지의 제왕", "루엘 톨킨", pub_year=2010)

    found = recall_match.decide(
        "반지의 제왕", "존 로널드 루엘 톨킨", [loose, exact_old, exact_new]
    )

    assert found == [exact_new, exact_old, loose]


def test_책_표_제목_앞_괄호_말을_떼고_견준다() -> None:
    book = _book("(그림으로 보는) 시간의 역사", "스티븐 호킹, 김동광", "까치")

    assert recall_match.decide("시간의 역사", "스티븐 호킹", [book]) == [book]


def test_제목_앞에_붙인_저자_이름을_뗀다() -> None:
    assert (
        recall_match.title_without_author("파올로 코엘료의 연금술사", "파올로 코엘료")
        == "연금술사"
    )
    assert (
        recall_match.title_without_author("파올로코엘료의 연금술사", "파올로 코엘료")
        == "연금술사"
    )
    assert recall_match.title_without_author("연금술사", "파올로 코엘료") is None
    # 저자 이름뿐인 제목은 떼지 않는다
    assert recall_match.title_without_author("한강의", "한강") is None


def test_저자_이름이_든_진짜_제목은_떼기_전에_그대로_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _book("클라우스 슈밥의 제4차 산업혁명", "클라우스 슈밥")
    queries: list[str] = []

    async def _search(conn, title):
        queries.append(title)
        return [real]

    monkeypatch.setattr(match_cover, "search", _search)

    found = asyncio.run(
        recall_match.match(None, "클라우스 슈밥의 제4차 산업혁명", "클라우스 슈밥")
    )

    assert found == [real]
    assert queries == ["클라우스 슈밥의 제4차 산업혁명"]


def test_제목_그대로_못_찾으면_저자_이름을_떼고_한_번_더_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = _book("연금술사", "파울로 코엘료, 최정수", "문학동네")
    queries: list[str] = []

    async def _search(conn, title):
        queries.append(title)
        return [book] if title == "연금술사" else []

    monkeypatch.setattr(match_cover, "search", _search)

    found = asyncio.run(
        recall_match.match(None, "파올로 코엘료의 연금술사", "파올로 코엘료")
    )

    assert found == [book]
    assert queries == ["파올로 코엘료의 연금술사", "연금술사"]


# --- 지어낸 제목이 다른 진짜 책에 붙으면 안 된다 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "recalled_author", "catalog_author"),
    [
        # 지어낸 제목과 같은 제목의 진짜 책이 책 표에 있지만 저자가 다른 사람이다
        ("우주 탐험", "스티븐 호킹", "케이티 데이니스, 피터 앨런"),
        ("고양이를 부탁해", "베르나르 베르베르", "정재은, 박지성, 김현정, 이언희"),
        ("결정적 순간의 리더십", "존 맥스웰", "고현숙"),
        ("스피치의 기술", "데일 카네기", "피터 데스버그, 이시훈, 정의철"),
        ("공룡 대탐험", "애덤 히비", "로맹 아미오"),
        # 마지막 낱말이 한 글자면 가르지 않는다
        ("별이 빛나는 밤에", "오리나 던", "이후"),
        # 한 낱말 이름은 통째로 같아야 한다
        ("어떤 책", "김영하", "김영아"),
        # 단체 이름은 마지막 낱말이 같아도 다른 곳이다(11/12 실측에서 붙었던 것)
        ("팥죽 할멈과 호랑이", "대교출판 편집부", "키움북스 편집부"),
        ("궁중음식 이야기", "한국 궁중음식연구회", "서울 궁중음식연구회"),
        ("달의 바다", "마크 해던", "정한아"),
    ],
)
def test_제목이_같아도_저자가_다른_사람이면_붙이지_않는다(
    title: str, recalled_author: str, catalog_author: str
) -> None:
    assert (
        recall_match.decide(title, recalled_author, [_book(title, catalog_author)])
        == []
    )


def test_저자가_맞아도_제목이_다르면_붙이지_않는다() -> None:
    # 테리 프래쳇은 책 표에 있지만 『나이트 메이지』는 없다(모델이 지어낸 제목)
    books = [
        _book("멋진 징조들", "테리 프래쳇, 닐 게이먼"),
        _book("디스크월드", "테리 프래쳇"),
    ]

    assert recall_match.decide("나이트 메이지", "테리 프래쳇", books) == []


def test_앞_괄호_말을_떼도_저자가_다르면_붙이지_않는다() -> None:
    book = _book("(그림으로 보는) 시간의 역사", "스티븐 호킹, 김동광")

    assert recall_match.decide("시간의 역사", "서금석", [book]) == []


def test_C샵_표시가_다른_책은_마지막_낱말이_같아도_붙이지_않는다() -> None:
    book = _book("C 프로그래밍 언어", "브라이언 W. 커니핸, 데니스 M. 리치")

    assert recall_match.decide("C# 프로그래밍 언어", "데니스 리치", [book]) == []
