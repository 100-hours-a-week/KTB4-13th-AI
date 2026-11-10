"""③ 챗봇 평가 실행기(evals/chat/run.py)가 카드의 ISBN 으로 책 번호를 채우는지 본다. DB 없이 돈다."""

import asyncio

from evals.chat.run import fill_book_ids


class _Conn:
    """책 표 대신 ISBN 끝 세 자리를 책 번호로 돌려준다. 9780000000999 는 책 표에 없다."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []

    async def fetch(self, sql: str, isbns: list[str]) -> list[dict]:
        self.asked.append(isbns)
        return [
            {"isbn13": isbn, "book_id": int(isbn[-3:])}
            for isbn in isbns
            if isbn != "9780000000999"
        ]


def test_응답_카드에는_book_id가_없어_ISBN으로_채운다() -> None:
    cards = [
        {"book_id": None, "isbn": "9780000000012"},
        {"book_id": None, "isbn": "9780000000999"},
        {"book_id": 7, "isbn": "9780000000007"},
        {"book_id": None, "isbn": None},
    ]
    conn = _Conn()

    asyncio.run(fill_book_ids(conn, cards))

    assert [c["book_id"] for c in cards] == [12, None, 7, None]
    # 이미 책 번호가 있는 카드(옛 응답)와 ISBN 이 없는 카드는 묻지 않는다
    assert conn.asked == [["9780000000012", "9780000000999"]]


def test_채울_카드가_없으면_책_표를_묻지_않는다() -> None:
    conn = _Conn()

    asyncio.run(fill_book_ids(conn, [{"book_id": 3, "isbn": "9780000000003"}]))

    assert conn.asked == []
