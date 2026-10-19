"""④ 관심사별 목록을 시간 예산 안에서 뽑기(app/feed/interest_fetch.py) 테스트(#451).

뽑기 함수는 가짜로 바꿔 순서와 대신하기만 본다. 마지막 테스트만 실제 PostgreSQL 이 필요하다.
"""

import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app.feed import interest_fetch, interest_picks
from app.feed.interest_fetch import Job
from app.feed.interest_picks import Pick

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")


def _picked(name: str, kind: str = "pick") -> list[Pick]:
    return [Pick(0, None, f"{kind}:{name}", None, None, 1.0)]


class _Fakes:
    """뽑기 함수들을 가짜로 바꾼다. slow 에 든 관심사는 조회가 시간을 넘긴다."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        slow: frozenset[str] = frozenset(),
        popular_slow: bool = False,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.lefts: list = []
        self.popular: list[tuple[str, object]] = []
        self.anchors: dict[str, tuple[int, ...]] = {}

        def picker(kind: str):
            async def fake(conn, name, *args, limit, left, **kwargs):
                left()  # 실제 뽑기처럼 조회 전에 남은 시간을 받는다
                self.lefts.append(left)
                self.calls.append((kind, name))
                if "anchors" in kwargs:
                    self.anchors[f"{kind}:{name}"] = kwargs["anchors"]
                if name in slow:
                    raise TimeoutError
                return _picked(name, kind)

            return fake

        async def popular(conn, name, vector, *, by_name, limit, timeout, anchors=()):
            self.popular.append((name, vector))
            self.anchors[f"popular:{name}"] = anchors
            if popular_slow:
                raise TimeoutError
            return _picked(name, "popular")

        monkeypatch.setattr(interest_picks, "pick", picker("pick"))
        monkeypatch.setattr(interest_picks, "pick_author", picker("author"))
        monkeypatch.setattr(interest_picks, "pick_keyword", picker("keyword"))
        monkeypatch.setattr(interest_picks, "pick_popular", popular)


class _FakeConn:
    @asynccontextmanager
    async def transaction(self):
        yield


def _fetch(jobs: list[Job], seconds: float = 3.0) -> interest_fetch.Fetched:
    return asyncio.run(interest_fetch.fetch(_FakeConn(), jobs, seconds=seconds))


_JOBS = [
    Job("요리", 10, [1.0], by_name=True),
    Job("작가:한강", 5),
    Job("키워드:고양이", 5, [1.0], by_name=True),
]


def test_관심사_종류에_맞는_뽑기를_넘긴_순서대로_하나의_예산으로_부른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(monkeypatch)

    fetched = _fetch(_JOBS)

    assert fakes.calls == [
        ("pick", "요리"),
        ("author", "작가:한강"),
        ("keyword", "키워드:고양이"),
    ]
    assert len(set(map(id, fakes.lefts))) == 1
    assert fetched.picks["요리"] == _picked("요리")
    assert fetched.substituted == ()


def test_시간을_넘긴_관심사는_그_분류의_인기_있는_책으로_대신하고_알려_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(monkeypatch, slow=frozenset({"요리"}))

    fetched = _fetch(_JOBS)

    assert fakes.popular == [("요리", [1.0])]
    assert fetched.picks["요리"] == _picked("요리", "popular")
    assert fetched.picks["작가:한강"] == _picked("작가:한강", "author")
    assert fetched.substituted == ("요리",)


def test_예산을_다_쓰면_남은_관심사는_조회하지_않고_바로_대신한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _Fakes(monkeypatch)

    fetched = _fetch(_JOBS, seconds=0.0)

    assert fakes.calls == []
    assert [name for name, _ in fakes.popular] == [job.name for job in _JOBS]
    assert fetched.substituted == tuple(job.name for job in _JOBS)


def test_대신할_책마저_시간을_넘기면_비우고_다른_관심사가_채우게_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Fakes(monkeypatch, slow=frozenset({"요리"}), popular_slow=True)

    fetched = _fetch(_JOBS)

    assert fetched.picks["요리"] == []
    assert fetched.substituted == ("요리",)


def test_검색어가_없는_관심사는_조회하지_않고_대신한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 이름 벡터를 못 읽은 경우다. 밤에 다시 뽑도록 대신한 관심사로 알린다
    fakes = _Fakes(monkeypatch)

    fetched = _fetch([Job("요리", 10, None, by_name=True)])

    assert fakes.calls == []
    assert fakes.popular == [("요리", None)]
    assert fetched.substituted == ("요리",)


@pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)
def test_조회가_시간을_넘겨도_바깥_트랜잭션은_깨지지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_pick(conn, name, vector, *, by_name, limit, left, anchors=()):
        await conn.fetch("SELECT pg_sleep(1)", timeout=left())
        return []

    monkeypatch.setattr(interest_picks, "pick", slow_pick)
    # 대신할 책은 실제 조회로 읽되, 테스트 책이 없는 분류라 빈 목록이다
    monkeypatch.setattr(interest_picks, "classes_of", lambda _name: ("쀍분류",))

    async def run():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            fetched = await interest_fetch.fetch(
                conn, [Job("요리", 10, [1.0], by_name=True)], seconds=0.2
            )
            # 시간을 넘긴 조회는 저장점까지만 되돌려, 같은 트랜잭션에서 계속 쓸 수 있다
            alive = await conn.fetchval("SELECT 1")
            return fetched, alive
        finally:
            await tx.rollback()
            await conn.close()

    fetched, alive = asyncio.run(run())

    assert fetched.substituted == ("요리",)
    assert fetched.picks["요리"] == []
    assert alive == 1


def test_책_벡터_평균으로_뽑는_관심사는_내_책_번호를_뽑기와_대신할_책에_넘긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 매칭 점수를 가장 비슷한 내 책과의 유사도로 매기려고 넘긴다(#483)
    fakes = _Fakes(monkeypatch, slow=frozenset({"역사"}))

    _fetch(
        [
            Job("요리", 10, [1.0], anchors=(11, 12)),
            Job("역사", 10, [1.0], anchors=(13,)),
        ]
    )

    assert fakes.anchors == {
        "pick:요리": (11, 12),
        "pick:역사": (13,),
        "popular:역사": (13,),
    }
