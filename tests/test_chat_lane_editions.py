"""③ 후보 갈래의 판본 고르기(app/chat/lanes.py) 테스트(#691).

떠올린 제목·기준 책 이웃이 같은 책의 판본 중 그래픽노블·만화판을 카드로 내지 않는지 본다. 11/11 dev 시험에서 SF
추천의 『듄』 카드가 그래픽노블 3권이었고 그대로 담겨 주문 화면까지 갔다.
"""

from app.chat import lanes

_NOVEL_AUTHOR = "프랭크 허버트, 김승욱"
_COMIC_AUTHOR = "프랭크 허버트, 브라이언 허버트, 케빈 J. 앤더슨 각색, 라울 앨런"


def _row(
    book_id: int,
    author: str,
    *,
    description: str = "소개글",
    in_stock: bool = True,
    pub_year: int = 2021,
) -> dict:
    return {
        "book_id": book_id,
        "title": "듄",
        "author": author,
        "description": description,
        "in_stock": in_stock,
        "pub_year": pub_year,
    }


def test_떠올린_제목이_고른_판본이_그래픽노블이면_같은_묶음의_소설판을_낸다() -> None:
    comic = _row(1, _COMIC_AUTHOR, description="『듄』의 그래픽노블 3권", pub_year=2025)
    novel = _row(2, _NOVEL_AUTHOR, pub_year=2021)

    assert lanes._plain_edition([comic, novel]) is novel


def test_원작_꼴_판본이_여럿이면_소개글_있고_살_수_있는_새_판을_낸다() -> None:
    comic = _row(1, _COMIC_AUTHOR, pub_year=2025)
    old = _row(2, _NOVEL_AUTHOR, pub_year=2016)
    no_text = _row(3, _NOVEL_AUTHOR, description="", pub_year=2024)
    new = _row(4, _NOVEL_AUTHOR, pub_year=2021)

    assert lanes._plain_edition([comic, old, no_text, new]) is new


def test_고른_판본이_원작_꼴이면_표지_규칙이_고른_그대로다() -> None:
    picked = _row(1, _NOVEL_AUTHOR, pub_year=2016)
    newer = _row(2, _NOVEL_AUTHOR, pub_year=2025)

    assert lanes._plain_edition([picked, newer]) is picked


def test_각색판뿐이면_버리지_않고_그대로_낸다() -> None:
    comic = _row(1, _COMIC_AUTHOR)

    assert lanes._plain_edition([comic]) is comic


def test_기준_책_이웃도_각색판보다_원작_꼴_판본을_고른다() -> None:
    comic = _row(1, _COMIC_AUTHOR, pub_year=2025)
    novel = _row(2, _NOVEL_AUTHOR, description="", pub_year=2016)

    # 각색판에 소개글이 있고 더 새 판이어도 원작 꼴이 먼저다
    assert lanes._best_edition([comic, novel]) is novel
