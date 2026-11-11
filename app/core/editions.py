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
# 원작을 다른 꼴로 옮긴 판본(그래픽노블·만화·각색)의 표시(#691). 『듄』 그래픽노블은 저자 칸이 "프랭크 허버트, …, 케빈 J.
# 앤더슨 각색, 라울 앨런, …"이라 첫 저자로 묶으면 소설과 한 묶음이 된다. 저자 칸의 역할 말과 소개글로만 안다.
# "그림"은 넣지 않는다 — 삽화가 든 소설·그림책은 판본마다 "그림"이 붙어 있어 원작과 다른 꼴이라는 뜻이 아니다.
# 소개글의 "웹툰", "만화로도 만들어진"도 넣지 않는다 — 원작 소설의 소개글에 흔하다(평가 DB 한국문학 "웹툰" 881권)
_ADAPTED_AUTHOR = re.compile(r"각색|만화")
_ADAPTED_TEXT = re.compile(r"그래픽\s?노블|만화로\s?(옮|만나|읽|그려|그린|보)")


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


def is_adaptation(author: str | None, description: str | None) -> bool:
    """원작을 그래픽노블·만화로 옮긴 판본인가. 저자 칸이나 소개글에 표시가 있을 때만 안다."""
    return bool(_ADAPTED_AUTHOR.search(author or "")) or bool(
        _ADAPTED_TEXT.search(description or "")
    )
