from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.jobs import replicate_be


def test_KST_naive_시각을_UTC로_변환한다() -> None:
    source = datetime(2026, 10, 2, 9, 30, 15, tzinfo=UTC).replace(tzinfo=None)

    assert replicate_be.target_utc(source) == datetime(
        2026, 10, 2, 0, 30, 15, tzinfo=UTC
    )


def test_Postgres_커서를_KST_naive로_복원한다() -> None:
    stored = datetime(2026, 10, 2, 0, 30, 15, tzinfo=UTC)

    expected = datetime(2026, 10, 2, 9, 30, 15, tzinfo=UTC).replace(tzinfo=None)
    assert replicate_be.source_naive(stored) == expected


def test_이미_KST인_커서는_벽시각을_유지한다() -> None:
    stored = datetime(2026, 10, 2, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))

    expected = datetime(2026, 10, 2, 9, 30, tzinfo=UTC).replace(tzinfo=None)
    assert replicate_be.source_naive(stored) == expected


def test_cancel_테이블이_있으면_부분취소_수량을_집계한다() -> None:
    query = replicate_be._purchases_source(True)

    assert "SUM(canceled_quantity)" in query
    assert "MAX(canceled_at)" in query
    assert "LEAST(oi.quantity" in query


def test_cancel_테이블이_없으면_현재_상태로_전체취소를_보존한다() -> None:
    query = replicate_be._purchases_source(False)

    assert "oi.status = 'CANCELED'" in query
    assert "THEN oi.quantity ELSE 0" in query
