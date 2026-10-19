"""⑦ 쇼핑 에이전트가 책의 글(제목, 소개글)을 읽는 경계(#363).

가격과 재고는 BE(commerce.py)가 주인이고, 소개글은 AI 서버가 가진 카탈로그(복제 테이블)가 주인이다.
출처가 달라서 Protocol 을 따로 둔다. 운영은 AI DB 의 책 표(catalog_db.py), 로컬과 시험은 메모리 가짜
(commerce_fake.py)를 쓴다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class BookText:
    isbn: str
    title: str
    # 소개글이 없는 책이 있다(null). 비어 있어도 같다
    description: str | None


@runtime_checkable
class CatalogGateway(Protocol):
    async def get_texts(self, isbns: Sequence[str]) -> list[BookText]:
        """ISBN마다 제목과 소개글을 돌려준다. 모르는 책은 결과에서 빠진다."""
        ...
