"""분야 이름만 말한 요청을 도서관에서 많이 읽힌 순서로 답한다(#514).

"소설 추천해줘", "그냥 에세이", "추리소설 아무거나"처럼 분야 이름만 있고 비교할 내용이 없는 요청이다. 분야 이름이 그대로
검색어가 되면 ① 검색이 제목에 그 낱말이 든 책을 앞에 둔다(11/11 dev: "추리소설" → 『고가 사부로 단편 추리소설』·『추리소설
속 트릭의 비밀』·『추리소설 세계명단편』, "그냥 소설" → 『소설』 두 권). 소개글 벡터로 찾아도 『소설창작론』처럼 그 분야에
관한 책이 나온다(10/19). 그래서 이런 요청은 검색하지 않고, 그 분야에서 많이 읽힌 책을 후보로 쓴다.

내용이 있는 요청("우울할 때 읽을 소설", "반전 있는 추리소설")은 여기 오지 않는다. 지금처럼 검색한다(parse 가 None).

데이터는 함께 대출 그래프와 같은 정보나루 도서별 이용 분석 원본(usage.jsonl)이다. 파일에는 책마다 ISBN 과 꼬리표(분야·나라·
청소년·세부 장르)만 대출 많은 순서로 담는다. 대출 건수와 정보나루 낱말 원문은 담지 않는다(app/jobs/build_popular_books.py).
"""

import gzip
import json
import logging
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.search import books, filters
from app.search.schemas import SearchFilters

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

# --- 꼬리표(파일을 만들 때 매긴다) -------------------------------------------------------------------------------

# KDC 문학(8xx)의 셋째 자리 → 분야. 둘째 자리는 나라다. 희곡·일기 같은 나머지는 쓰지 않는다.
_LITERATURE_FORMS = {"1": "시", "3": "소설", "4": "수필"}
_LITERATURE_ORIGINS = {
    "1": "한국",
    "2": "중국",
    "3": "일본",
    "4": "영미",
    "5": "독일",
    "6": "프랑스",
}
# 자기계발은 문학 밖이다. 처세(199)와 성공학(325.2)에 몰려 있다(11/11 원본: 325.2 1,351권·199.1 573권, 위는
# 『타이탄의 도구들』·『그릿』·『신경 끄기의 기술』).
_SELF_HELP = re.compile(r"^(199|325\.2)")
# ISBN 부가기호 첫 자리. 7 아동, 6 초등 학습참고서, 5 중고등 학습참고서는 이 갈래에 넣지 않는다 — 어른이 "소설 추천해줘"에
# 동화를 받으면 안 된다(그래프의 '남의 목록 수'로 줄 세우면 위가 『꽁꽁꽁 캠핑』·『흔한남매』였다). 4 는 청소년이다.
_CHILD_ADDITIONS = frozenset("765")
_TEEN_ADDITION = "4"

# 세부 장르는 정보나루가 책마다 뽑은 낱말(무게 있음)로 매긴다. 낱말이 통째로 같을 때만 무게를 더한다 — 일부만 보면
# "용"이 『용의자 X의 헌신』에 걸려 판타지가 됐다(11/11 시험). 무게 합이 SUBGENRE_MIN_WEIGHT 이상이면 그 장르다.
# 11/11 원본에서 소설 5,899권 중 추리 116·로맨스 59·SF 31·역사 18·판타지 10권.
SUBGENRE_WORDS: dict[str, frozenset[str]] = {
    "추리": frozenset(
        {"추리", "살인", "탐정", "범인", "미스터리", "형사", "살인사건", "용의자", "수사", "트릭", "범죄", "스릴러"}
    ),
    "SF": frozenset(
        {"우주", "로봇", "외계", "외계인", "행성", "인공지능", "미래", "과학", "SF", "안드로이드", "지구", "시간여행"}
    ),
    "판타지": frozenset({"마법", "판타지", "마법사", "왕국", "드래곤", "요정", "마녀", "마왕", "전설"}),
    "로맨스": frozenset({"사랑", "연애", "로맨스", "첫사랑", "결혼", "연인"}),
    "역사": frozenset({"조선", "역사", "왕", "고려", "전쟁", "임진왜란", "궁궐", "세종", "역사소설"}),
}  # fmt: skip
SUBGENRE_MIN_WEIGHT = 20.0


@dataclass(frozen=True)
class Entry:
    """많이 읽힌 책 한 권의 꼬리표."""

    isbn: str
    kind: str  # 소설·수필·시·자기계발
    origin: str | None  # 문학의 나라. 문학이 아니거나 모르면 None
    teen: bool
    subgenres: frozenset[str]


def tag(response: dict) -> tuple[Entry, int] | None:
    """정보나루 도서별 이용 분석 응답 하나 → (꼬리표, 대출 건수). 이 갈래에 쓰지 않는 책이면 None.

    대출 건수는 줄 세우는 데만 쓰고 파일에는 담지 않는다.
    """
    book = response.get("book") or {}
    isbn = (book.get("isbn13") or "").strip()
    if not isbn:
        return None
    addition = (book.get("addition_symbol") or "").strip()[:1]
    if addition in _CHILD_ADDITIONS:
        return None
    kdc = (book.get("class_no") or "").strip()
    kind, origin = _kind_of(kdc)
    if kind is None:
        return None
    try:
        loans = int(book.get("loanCnt") or 0)
    except (TypeError, ValueError):
        loans = 0
    words = _keyword_weights(response.get("keywords") or [])
    subgenres = (
        frozenset(
            name
            for name, vocabulary in SUBGENRE_WORDS.items()
            if sum(w for word, w in words.items() if word in vocabulary)
            >= SUBGENRE_MIN_WEIGHT
        )
        if kind == "소설"
        else frozenset()
    )
    return Entry(isbn, kind, origin, addition == _TEEN_ADDITION, subgenres), loans


def _kind_of(kdc: str) -> tuple[str | None, str | None]:
    literature = re.match(r"^8(\d)(\d)", kdc)
    if literature:
        country, form = literature.groups()
        kind = _LITERATURE_FORMS.get(form)
        return kind, _LITERATURE_ORIGINS.get(country) if kind else None
    if _SELF_HELP.match(kdc):
        return "자기계발", None
    return None, None


def _keyword_weights(keywords: list) -> dict[str, float]:
    weights: dict[str, float] = {}
    for item in keywords:
        keyword = (item or {}).get("keyword") or {}
        try:
            weights[str(keyword["word"])] = float(keyword["weight"])
        except (KeyError, TypeError, ValueError):
            continue
    return weights


def build(lines: Iterable[str]) -> dict:
    """usage.jsonl 의 줄들로 파일 내용을 만든다. 한 줄 = {"isbn13", "resp": 정보나루 응답}. 같은 ISBN 은 뒤의 것을 쓴다."""
    tagged: dict[str, tuple[Entry, int]] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        response = (record.get("resp") or {}).get("response") or {}
        found = tag(response)
        if found is not None:
            tagged[found[0].isbn] = found
    # 대출 많은 순, 같으면 ISBN 순(다시 만들어도 같은 파일)
    ranked = sorted(tagged.values(), key=lambda pair: (-pair[1], pair[0].isbn))
    return {
        "version": FORMAT_VERSION,
        "books": [
            [e.isbn, e.kind, e.origin, e.teen, sorted(e.subgenres)] for e, _ in ranked
        ],
    }


@dataclass(frozen=True)
class PopularBooks:
    entries: tuple[Entry, ...]  # 많이 읽힌 순


def from_data(data: dict) -> PopularBooks:
    if data.get("version") != FORMAT_VERSION:
        raise ValueError(f"인기 책 파일 형식 {data.get('version')} 은 읽을 수 없다")
    return PopularBooks(
        tuple(
            Entry(isbn, kind, origin, bool(teen), frozenset(subgenres))
            for isbn, kind, origin, teen, subgenres in data["books"]
        )
    )


def load(path: str | Path) -> PopularBooks | None:
    """파일(gzip JSON)을 읽는다. 없거나 못 읽으면 None — 분야만 말한 요청도 지금처럼 검색한다."""
    file = Path(path)
    if not file.is_file():
        logger.warning(
            "인기 책 파일이 없어 분야만 말한 요청도 검색으로 찾는다: %s", file
        )
        return None
    try:
        with gzip.open(file, "rt", encoding="utf-8") as f:
            index = from_data(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception(
            "인기 책 파일을 못 읽어 분야만 말한 요청도 검색으로 찾는다: %s", file
        )
        return None
    logger.info("인기 책 파일을 읽었다: %d권", len(index.entries))
    return index


_index: PopularBooks | None = None


def install(index: PopularBooks | None) -> None:
    """서버가 켜질 때 읽은 파일을 둔다. 테스트는 작은 목록을 끼운다."""
    global _index
    _index = index


def get() -> PopularBooks | None:
    return _index


# --- 요청 해석 ---------------------------------------------------------------------------------------------------

_KIND_WORDS = {
    "소설": "소설", "소설책": "소설", "장편": "소설", "장편소설": "소설", "단편소설": "소설", "문학소설": "소설",
    "에세이": "수필", "에세이집": "수필", "수필": "수필", "수필집": "수필", "산문": "수필", "산문집": "수필",
    "시": "시", "시집": "시",
    "자기계발": "자기계발", "자기계발서": "자기계발", "자계서": "자기계발",
}  # fmt: skip
# 세부 장르 낱말은 소설을 뜻한다("추리소설", "미스터리 추천해줘")
_SUBGENRE_WORDS = {
    "추리": "추리", "추리소설": "추리", "미스터리": "추리", "미스테리": "추리", "스릴러": "추리",
    "탐정소설": "추리", "범죄소설": "추리",
    "sf": "SF", "sf소설": "SF", "에스에프": "SF", "공상과학": "SF", "과학소설": "SF",
    "판타지": "판타지", "판타지소설": "판타지",
    "로맨스": "로맨스", "로맨스소설": "로맨스", "연애소설": "로맨스",
    "역사소설": "역사",
}  # fmt: skip
_ORIGIN_WORDS = {
    "한국": "한국", "국내": "한국", "일본": "일본", "중국": "중국", "영미": "영미", "미국": "영미", "영국": "영미",
    "독일": "독일", "프랑스": "프랑스",
}  # fmt: skip
_TEEN_WORDS = frozenset({"청소년"})
# 내용을 더하지 않는 말. 이것만 더 있으면 분야만 말한 요청이다. "요즘"·"최근"은 넣지 않는다 — 많이 읽힌 책은 몇 년 된
# 책이 많아서, 새 책을 원하는 말은 지금처럼 검색한다(연도를 말했으면 1단계가 연도 조건으로 옮겨 여기 걸리지 않는다).
_FILLER = frozenset(
    {
        "추천", "책", "책들", "도서", "작품", "작품들", "그냥", "아무", "아무거나", "아무거", "재밌는", "재미있는",
        "좋은", "괜찮은", "인기", "인기있는", "있는", "유명한", "읽을", "읽을만한", "만한", "볼만한", "하나", "좀",
        "몇", "권", "것", "거", "장르", "종류", "위주", "명작", "대표작", "베스트셀러",
    }
)  # fmt: skip
# "추리 소설", "역사 소설"처럼 띄어 쓴 세부 장르를 붙인다
_SPACED_GENRE = re.compile(
    r"(추리|과학|연애|로맨스|판타지|역사|탐정|범죄|장편|단편|sf)\s+소설"
)
_SEPARATORS = re.compile(r"[·/,|&+~()\[\]\"'“”‘’!?.]")
# 낱말 끝의 조사. 떼어 낸 말이 사전에 있을 때만 뗀다 — "에세이"의 "이"를 떼면 안 된다
_PARTICLE = re.compile(r"(으로|이나|에서|이랑|로|을|를|은|는|이|가|도|만|의|나|랑|요)$")


@dataclass(frozen=True)
class Ask:
    """분야만 말한 요청이 고른 분야. 빈 칸은 가리지 않는다는 뜻이다."""

    kinds: frozenset[str]
    subgenres: frozenset[str] = frozenset()
    origins: frozenset[str] = frozenset()
    teen: bool = False

    def matches(self, entry: Entry) -> bool:
        return (
            entry.kind in self.kinds
            and (not self.subgenres or bool(entry.subgenres & self.subgenres))
            and (not self.origins or entry.origin in self.origins)
            and (not self.teen or entry.teen)
        )


def parse(text: str | None) -> Ask | None:
    """검색 문장이 분야 이름(과 내용 없는 말)뿐이면 그 분야. 다른 말이 하나라도 있으면 None — 지금처럼 검색한다."""
    normalized = _SPACED_GENRE.sub(
        r"\1소설", _SEPARATORS.sub(" ", (text or "").lower())
    )
    kinds: set[str] = set()
    subgenres: set[str] = set()
    origins: set[str] = set()
    teen = False
    for word in normalized.split():
        word = _known(word)
        if word is None:
            return None
        if word in _KIND_WORDS:
            kinds.add(_KIND_WORDS[word])
        elif word in _SUBGENRE_WORDS:
            subgenres.add(_SUBGENRE_WORDS[word])
        elif word in _ORIGIN_WORDS:
            origins.add(_ORIGIN_WORDS[word])
        elif word in _TEEN_WORDS:
            teen = True
    if subgenres:
        kinds.add("소설")
    if not kinds:
        return None
    if "소설" not in kinds and (subgenres or origins):
        # "일본 에세이"는 원본에 나라 꼬리표가 있지만 수필은 576권뿐이라 나라로 가르지 않는다. 세부 장르는 소설만 있다
        origins.clear()
    return Ask(frozenset(kinds), frozenset(subgenres), frozenset(origins), teen)


def _known(word: str) -> str | None:
    for candidate in (word, _PARTICLE.sub("", word)):
        if candidate and (
            candidate in _KIND_WORDS
            or candidate in _SUBGENRE_WORDS
            or candidate in _ORIGIN_WORDS
            or candidate in _TEEN_WORDS
            or candidate in _FILLER
            or candidate.startswith("추천")
        ):
            return candidate
    return None


def answers(text: str | None) -> bool:
    """이 검색 문장을 인기 책으로 답할 수 있나. 파일이 없으면 아니다(지금처럼 검색·묻기)."""
    return _index is not None and parse(text) is not None


# --- 후보 -------------------------------------------------------------------------------------------------------

# 파일에서 먼저 뽑는 ISBN 수. 서점에 없는 책·가격 조건·이미 보여 준 책으로 빠지는 만큼 넉넉히 뽑는다
# (11/11 원본 6.4만 권 중 평가 DB 서점에 있는 책 6.18만 권).
ISBN_LIMIT = 300
# DB 에서 가져오는 후보 수. 이 안에서 취향 순으로 세우거나(취향 벡터가 있는 사용자) 많이 읽힌 순서에 기대어 뽑는다(없는
# 사용자). 맨 위 10권만 쓰면 취향을 모르는 사람은 모두 같은 책을 받았다(11/11 실측: 넓은 소설 요청 5번 중 5번
# 『불편한 편의점』).
FETCH_LIMIT = 200
QUERY_TIMEOUT_SECONDS = 1.0
# 뽑을 때 순위 r 의 무게는 1/√r 이다. 위쪽 책이 더 자주 나오되 200위 안 책도 나온다(1위가 10권에 들 확률 약 30%).
_RANK_WEIGHT_POWER = 0.5


def sample(pool: list, size: int, rng: random.Random | None = None) -> list:
    """많이 읽힌 순의 pool 에서 size 개를 뽑는다. 위쪽일수록 잘 뽑히고, 뽑힌 것은 순서를 섞어 돌려준다.

    무게를 지수 분포 열쇠로 바꿔 작은 순으로 고른다(Efraimidis–Spirakis, 무게 비례·중복 없는 뽑기). 섞는 까닭: 카드
    LLM 은 목록 앞쪽 책을 고르는 버릇이 있어(11/10 카드 후보 실험), 많이 읽힌 순서대로 주면 뽑아도 1~2위로 쏠린다.
    """
    if len(pool) <= size:
        return list(pool)
    rng = rng or random.Random()
    keys = [
        rng.expovariate(1.0) * (rank**_RANK_WEIGHT_POWER)
        for rank in range(1, len(pool) + 1)
    ]
    chosen = [pool[i] for i in sorted(range(len(pool)), key=keys.__getitem__)[:size]]
    rng.shuffle(chosen)
    return chosen


_SQL = """
SELECT b.book_id
FROM unnest($1::text[]) WITH ORDINALITY AS ranked(isbn, pos)
JOIN v_books b ON b.isbn13 = ranked.isbn
WHERE TRUE{filters}
ORDER BY ranked.pos, b.book_id
LIMIT $2
"""


def isbns_for(index: PopularBooks, ask: Ask, limit: int = ISBN_LIMIT) -> list[str]:
    """그 분야의 많이 읽힌 책 ISBN, 많이 읽힌 순."""
    found: list[str] = []
    for entry in index.entries:
        if ask.matches(entry):
            found.append(entry.isbn)
            if len(found) >= limit:
                break
    return found


async def fetch(
    conn: asyncpg.Connection, isbns: list[str], search_filters: SearchFilters
) -> list[dict]:
    """ISBN 순서대로 우리 서점의 책을 검색 결과와 같은 모양으로 가져온다. 가격·출간연도 조건을 건다.

    분야 조건(category)은 걸지 않는다. 분야는 꼬리표로 이미 골랐다. 카탈로그 분류로 "에세이"를 걸면 수필집·에세이
    분류만 남아서, 많이 읽힌 수필 대부분('한국문학' 110권·'일본문학·기타 아시아문학' 41권, 11/11 평가 DB 앞 175권)이 빠진다.
    """
    if not isbns:
        return []
    without_category = search_filters.model_copy(update={"category": None})
    where, params = filters.build_where(without_category, first_param=3)
    rows = await conn.fetch(
        _SQL.format(filters=where),
        isbns,
        FETCH_LIMIT,
        *params,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    return await books.fetch(conn, [r["book_id"] for r in rows])


# --- 카드 아래 보기 --------------------------------------------------------------------------------------------

# 분야만 넓게 말한 소설 요청에 카드와 같이 붙이는 질문과 보기. 고르면 다음 턴도 분야만 말한 요청이라 이 갈래로 좁혀진다
# (보기마다 원본에 1,300권 넘게 있는 나라와, 100권 넘게 있는 추리를 둔다. SF 는 31권이라 넣지 않는다).
NOVEL_QUESTION = "어떤 소설이 끌리세요?"
NOVEL_OPTIONS = ("추리·스릴러", "한국 소설", "일본 소설", "영미 소설")


def question(ask: Ask | None) -> tuple[str, tuple[str, ...]] | None:
    """카드 아래 붙일 (질문, 보기). 이미 좁힌 요청이면 None."""
    if ask is None or ask.kinds != {"소설"} or ask.subgenres or ask.origins:
        return None
    return NOVEL_QUESTION, NOVEL_OPTIONS
