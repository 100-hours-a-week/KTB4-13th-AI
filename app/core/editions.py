"""같은 책의 다른 판본을 한 키로 묶는다.

카탈로그는 판본·권마다 book_id 가 달라 같은 책이 목록에 여러 번 나온다(#281). 저자 표기도 "지은이: 한강",
"J.K. 롤링 지음 ;강동혁 옮김"처럼 들쭉날쭉해서 역할 말을 빼고 첫 저자만 본다. 챗봇의 판본 묶기
(app/routers/chat.py)와 같은 규칙이다.
"""

import re

# 역할 접두어는 콜론이 있을 때만, 접미어는 앞에 공백이 있을 때만 뗀다 — 안 그러면 이름 속 글자("글", "저")가
# 같이 잘린다
_AUTHOR_ROLE_PREFIX = re.compile(
    r"^(지은이|저자|글쓴이|글|그림|그린이|원작|옮긴이|역자|엮은이|편저)\s*[:：]\s*"
)
_AUTHOR_ROLE_SUFFIX = re.compile(r"\s+(지음|옮김|엮음|그림|글|저|역|편)\s*$")
_AUTHOR_SEPARATOR = re.compile(r"[;,]")
_NON_WORD = re.compile(r"[\W_]+")


def title_key(title: str | None) -> str:
    """띄어쓰기와 문장부호만 다른 제목을 같은 것으로 본다."""
    return _NON_WORD.sub("", (title or "").casefold())


def author_key(author: str | None) -> str:
    """저자 칸의 첫 저자. "지은이:" 같은 역할 말을 떼고 띄어쓰기와 문장부호를 뺀다."""
    first = _AUTHOR_SEPARATOR.split(author or "")[0].strip()
    first = _AUTHOR_ROLE_SUFFIX.sub("", _AUTHOR_ROLE_PREFIX.sub("", first))
    return _NON_WORD.sub("", first.casefold())


def edition_key(title: str | None, author: str | None) -> tuple[str, str] | None:
    """같은 책의 다른 판본을 한 키로. 제목이나 저자가 비면 None — 다른 책일 수 있어 묶지 않는다."""
    key = (title_key(title), author_key(author))
    return key if all(key) else None
