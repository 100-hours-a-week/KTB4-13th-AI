"""같은 책의 다른 판본을 한 키로 묶는다.

카탈로그는 판본·권마다 book_id 가 달라 같은 책이 목록에 여러 번 나온다(#281). 저자 표기도 "지은이: 한강",
"J.K. 롤링 지음 ;강동혁 옮김"처럼 들쭉날쭉해서 역할 말을 빼고 첫 저자만 본다. 챗봇의 판본 묶기
(app/routers/chat.py)와 같은 규칙이다.
"""

import re
import unicodedata

# 역할 접두어는 콜론이 있을 때만, 접미어는 앞에 공백이 있을 때만 뗀다 — 안 그러면 이름 속 글자("글", "저")가
# 같이 잘린다
_AUTHOR_ROLE_PREFIX = re.compile(
    r"^(지은이|저자|글쓴이|글|그림|그린이|원작|옮긴이|역자|엮은이|편저)\s*[:：]\s*"
)
_AUTHOR_ROLE_SUFFIX = re.compile(r"\s+(지음|옮김|엮음|그림|글|저|역|편)\s*$")
_AUTHOR_SEPARATOR = re.compile(r"[;,]")
_NON_WORD = re.compile(r"[\W_]+")
# 로마자 바로 뒤의 # 이나 ++ 는 제목의 일부다(C#·C++·F#). title_key 는 문장부호와 함께 지워서 "C# 프로그래밍"과
# "C 프로그래밍"이 같은 키가 된다. "머신러닝+딥러닝"의 + 처럼 낱말 사이에 끼운 것은 표기마다 달라 보지 않는다
_LANGUAGE_MARK = re.compile(r"[a-z](?:\+\+|#)")


def language_marks(title: str | None) -> tuple[str, ...]:
    """제목 속 "c#"·"c++" 같은 언어 이름 표시. 둘이 다르면 키가 같아도 다른 책이다."""
    text = unicodedata.normalize("NFKC", title or "").replace("♯", "#").casefold()
    return tuple(sorted(_LANGUAGE_MARK.findall(text)))


def title_key(title: str | None) -> str:
    """띄어쓰기와 문장부호만 다른 제목을 같은 것으로 본다."""
    return _NON_WORD.sub("", (title or "").casefold())


def first_author(author: str | None) -> str:
    """저자 칸의 첫 저자 이름. "지은이:"·"지음" 같은 역할 말만 떼고 표기는 그대로 둔다(검색어로 쓴다)."""
    first = _AUTHOR_SEPARATOR.split(author or "")[0].strip()
    return _AUTHOR_ROLE_SUFFIX.sub("", _AUTHOR_ROLE_PREFIX.sub("", first)).strip()


def author_key(author: str | None) -> str:
    """저자 칸의 첫 저자. "지은이:" 같은 역할 말을 떼고 띄어쓰기와 문장부호를 뺀다."""
    return _NON_WORD.sub("", first_author(author).casefold())


def edition_key(title: str | None, author: str | None) -> tuple[str, str] | None:
    """같은 책의 다른 판본을 한 키로. 제목이나 저자가 비면 None — 다른 책일 수 있어 묶지 않는다."""
    key = (title_key(title), author_key(author))
    return key if all(key) else None
