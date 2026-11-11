"""BE 재고를 AI v_products 에 맞추는 부분(app/jobs/sync_from_be.py, #711) 시험.

SEARCH_TEST_DATABASE_URL 이 있을 때만 돈다(트랜잭션 안에서 하고 되돌린다). BE 는 가짜다.
"""

import pytest

from app.jobs import sync_from_be as sync
from tests.test_sync_from_be_convert import _be_product
from tests.test_sync_from_be_write import _DB_URL, _in_tx

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_IN_STOCK = 880000001
_SOLD_OUT_AI = 880000002


class _StockBe:
    """BE 재고만 흉내 낸다. available 은 상품 번호 → 보유 − 예약. 모르는 상품은 품절(0)로 본다 — 로컬 평가 DB 에
    원래 있던 재고 0 상품이 시험 결과에 섞이지 않게 한다."""

    def __init__(self, available: dict[int, int]) -> None:
        self.available = available

    def sold_out(self) -> dict[int, int]:
        return {pid: n for pid, n in self.available.items() if n <= 0}

    def available_by_ids(self, ids: list[int]) -> dict[int, int]:
        return {pid: self.available.get(pid, 0) for pid in ids}


async def _seed(conn) -> None:
    # AI 에 재고 있는 상품 하나(5), 재고 없는 상품 하나(0)
    await sync.upsert_products(
        conn,
        [
            sync.to_product(_be_product(id=_IN_STOCK, book_id=990000001)),
            sync.to_product(
                _be_product(
                    id=_SOLD_OUT_AI,
                    book_id=990000002,
                    on_hand_quantity=1,
                    reserved_quantity=1,
                )
            ),
        ],
    )


async def _stock(conn, pid: int) -> int:
    return await conn.fetchval(
        "SELECT stock_quantity FROM v_products WHERE id = $1", pid
    )


def test_BE에서_품절된_상품은_AI_재고를_0으로_고친다() -> None:
    be = _StockBe({_IN_STOCK: 0, _SOLD_OUT_AI: 0})

    async def body(conn):
        await _seed(conn)
        summary = await sync.sync_stock(conn, be)
        return summary, await _stock(conn, _IN_STOCK)

    summary, stock = _in_tx(body)

    assert stock == 0
    assert summary.sold_out == 1 and summary.restocked == 0


def test_BE에_다시_들어온_상품은_AI_재고를_BE_수로_고친다() -> None:
    # AI 는 0인데 BE 에는 재고가 있다
    be = _StockBe({_IN_STOCK: 5, _SOLD_OUT_AI: 3})

    async def body(conn):
        await _seed(conn)
        summary = await sync.sync_stock(conn, be)
        return summary, await _stock(conn, _SOLD_OUT_AI)

    summary, stock = _in_tx(body)

    assert stock == 3
    assert summary.sold_out == 0 and summary.restocked == 1


def test_있음·없음이_같으면_재고_수가_달라도_고치지_않는다() -> None:
    # AI 는 재고를 0보다 큰지로만 읽는다. 숫자까지 맞추면 첫 실행에 210만 개를 다시 쓴다(개발 BE 기준)
    be = _StockBe({_IN_STOCK: 17, _SOLD_OUT_AI: 0})

    async def body(conn):
        await _seed(conn)
        before = await conn.fetchval(
            "SELECT ctid::text FROM v_products WHERE id = $1", _IN_STOCK
        )
        summary = await sync.sync_stock(conn, be)
        after = await conn.fetchval(
            "SELECT ctid::text FROM v_products WHERE id = $1", _IN_STOCK
        )
        return summary, before, after, await _stock(conn, _IN_STOCK)

    summary, before, after, stock = _in_tx(body)

    assert (summary.sold_out, summary.restocked) == (0, 0)
    assert before == after and stock == 5
