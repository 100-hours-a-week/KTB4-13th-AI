"""다듬은 제목 색인의 SQL 식(match_shelf._KEY_TITLE)이 판정의 파이썬(match_shelf.title_key)과 같은 글자를 내는가(#615).

실제 PostgreSQL 이 있어야 돈다. 둘이 어긋나면 판정하면 맞는 책이 검색 후보에 못 든다. 이 문제를 고치려고 만든
색인이라, 같은 일을 하는지 DB 에서 직접 견준다.
"""

import asyncio
import os

import asyncpg
import pytest

from app.photo import match_shelf

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_TITLES = [
    "모던 자바스크립트 Deep Dive",
    "(시험, 생활, 교양 상식으로 나눠서 배우는) 통계학 대백과 사전",
    "(언어 자료 처리를 위한)Perl",
    "  (괄호 앞 띄어쓰기) 제목",
    "클린 코드 : 애자일 소프트웨어 장인 정신",
    "Effective Java=이펙티브 자바",
    "토비의 스프링 - 3.1",
    "(1) 첫째 : (2) 둘째",
    "(주)",
    "ＡＩ　시대의　글쓰기",  # 전각 로마자·전각 띄어쓰기
    "해리 포터와 마법사의 돌 1",
    "C++ 프로그래밍",
    "빅데이터·AI 입문",
    "",
]


def test_SQL_식과_파이썬이_같은_글자를_낸다() -> None:
    sql = (
        "SELECT "
        + match_shelf._KEY_TITLE.replace("b.title", "t")
        + " FROM unnest($1::text[]) WITH ORDINALITY AS u(t, n) ORDER BY n"
    )

    async def _go() -> list[str]:
        conn = await asyncpg.connect(_DB_URL)
        try:
            return [r[0] for r in await conn.fetch(sql, _TITLES)]
        finally:
            await conn.close()

    assert asyncio.run(_go()) == [match_shelf.title_key(t) for t in _TITLES]
