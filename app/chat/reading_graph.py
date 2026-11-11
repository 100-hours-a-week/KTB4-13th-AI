"""③ 함께 대출·마니아 목록 — 정보나루 도서별 이용 분석에서 만든 한 걸음 그래프.

정보나루는 책마다 "이 책을 빌린 사람들이 함께 빌린 책"(함께 대출)과 "이 분야 마니아에게 추천하는 책"(마니아) 목록을 각
10권씩 준다. 횟수도, 순서의 기준도 밝히지 않는다. 그래서 이렇게 쓴다(11/06·11/10 결정).

- 목록에 있으면 연결 하나다. 순서로 점수를 매기지 않는다
- 방향을 살린다. "A 의 목록에 B 가 있다"만 A 의 이웃이다. 남의 목록에 내가 든 것은 내 이웃이 아니다. 양쪽을 합쳤더니
  드문 책의 목록에 든 인기 책이 그 드문 책을 이웃으로 끌어왔다(파친코 → 대출 4회 책, 11/04 점검)
- 함께 대출과 마니아는 따로 둔다. 함께 대출은 같이 읽는 책(다음 권, 같이 공부하는 책, 그냥 같이 많이 빌린 책)이고
  비슷한 책이 아니다. 마니아는 같은 분야 책 쪽이지만 정보나루가 고른 기준은 모른다. 다독자 목록은 쓰지 않는다
- 판본은 작품 하나로 센다(work_key)
- 한 걸음만 본다. 여러 걸음 걷기는 11/04 시험에서 ③ 의 약한 문항을 못 고쳤다
- 인기 허브: 작품마다 남의 목록 몇 개에 들었는지 세어 둔다. 많이 든 책(불편한 편의점 같은)은 어느 책 목록에나 있어
  그 책에 대해 알려 주는 것이 없다. 이웃을 고를 때 덜 든 책부터 고르고, 가장 많이 든 책들은 뺀다. 관계를 물은 턴에
  허브 아닌 이웃이 모자라면 허브로 채운다(app/chat/lanes.py neighbor_books) — 허브는 상위 100작품이라 그래프가 커지면
  사피엔스처럼 그 책과 진짜 가까운 책도 든다

파일은 app/jobs/build_reading_graph.py 가 usage.jsonl 에서 만든다. 서버는 켜질 때 메모리에 올린다(목록 받은 5.8만 작품
약 110MB, 조회 마이크로초). 파일이 없으면 None 이고, 챗봇은 그래프 없이 돈다.
"""

import gzip
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

Kind = Literal["co", "mania"]
KINDS: tuple[Kind, ...] = ("co", "mania")
# 정보나루 응답의 목록 칸
SOURCE_LISTS: dict[str, Kind] = {"coLoanBooks": "co", "maniaRecBooks": "mania"}
FORMAT_VERSION = 1
# 남의 목록에 가장 많이 든 작품 이만큼을 허브로 보고 후보와 이웃에서 뺀다. 11/04 점검의 허브 정의(연결 합 상위 100)와
# 같은 수다. 그때 상위 5권 중 허브가 22.6% 에서 6.2% 로 줄었다
HUB_TOP = 100

_NON_KEY = re.compile(r"[^0-9a-z가-힣]+")
_SUBTITLE_SPLIT = re.compile(r"\s*[:=]\s*| - ")
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_AUTHOR_SEPARATOR = re.compile(r"[;,/]")
_AUTHOR_ROLE_SUFFIX = re.compile(
    r"\s*(지음|옮김|엮음|그림|글|저|역|편|감수|지은이|옮긴이|원작|각색)\s*$"
)


def _key(text: str) -> str:
    return _NON_KEY.sub("", unicodedata.normalize("NFKC", text).lower())


def _title_part(title: str | None) -> str:
    """부제와 괄호 말을 뗀 제목. 괄호 말을 떼고 남는 것이 없으면 괄호만 지운 제목이다.

    11/04 그래프는 "(이상한 과자 가게) 전천당"처럼 "(" 로 시작하는 제목을 빈 제목으로 만들어, 같은 저자의 다른 책이
    한 작품으로 합쳐졌다(9,684권). 앞 괄호 말을 떼면 "전천당"이 남는다. "페인트 (양장)", "데미안 (큰글자책)"처럼
    뒤에 붙은 판본 표시도 떼야 한 작품이 된다.
    """
    head = _SUBTITLE_SPLIT.split(title or "")[0]
    return _key(_PARENTHETICAL.sub(" ", head)) or _key(head)


def _author_part(author: str | None) -> str:
    """첫 저자 이름. 역할 말을 어떤 꼴로 붙여도 뗀다.

    정보나루 저자 칸은 "지은이: 유발 하라리 ;옮긴이: 조현욱", "원작·각색: 유발 하라리", "유발 하라리 (원작), 다니엘
    카사나브 (그림)", "이희영 (지은이)", "이민진 지음 ;이미정 옮김"처럼 들쭉날쭉하다. 11/04 그래프는 "()"와 붙은 역할
    말을 못 떼 같은 책이 갈렸다(사피엔스, 파친코, 페인트).
    """
    first = _AUTHOR_SEPARATOR.split(author or "")[0]
    first = re.split(r"[:：]", first)[-1]
    first = _PARENTHETICAL.sub(" ", first)
    first = _AUTHOR_ROLE_SUFFIX.sub("", first.strip())
    first = first.split("·")[0]
    return _key(first)


def work_key(title: str | None, author: str | None) -> str | None:
    """같은 작품의 판본을 한 키로 묶는다. 제목이나 저자가 비면 None — 다른 책일 수 있어 묶지 않는다.

    정보나루 책 이름("아몬드 :손원평 장편소설")과 우리 책 표 제목에 같이 쓴다.
    """
    title_part, author_part = _title_part(title), _author_part(author)
    if not title_part or not author_part:
        return None
    return f"{title_part}|{author_part}"


@dataclass(frozen=True)
class ReadingGraph:
    """작품 번호로 다루는 한 걸음 그래프. 작품 번호는 파일 안에서만 뜻이 있다."""

    keys: tuple[str, ...]  # 작품 번호 → work_key(키가 없는 작품은 "")
    by_isbn: dict[str, int]
    by_key: dict[str, int]
    lists: dict[Kind, dict[int, tuple[int, ...]]]  # 작품 → 그 작품의 목록에 든 작품들
    listed: dict[Kind, tuple[int, ...]]  # 작품 → 남의 목록 몇 개에 들었나
    hubs: frozenset[int]
    isbns: dict[int, tuple[str, ...]]  # 작품 → 정보나루에서 본 판본 ISBN

    def work_of(
        self, isbn: str | None, title: str | None = None, author: str | None = None
    ) -> int | None:
        """책의 작품 번호. ISBN 으로 먼저 찾고, 그 판본을 정보나루에서 못 봤으면 제목·저자로 찾는다."""
        if isbn and isbn in self.by_isbn:
            return self.by_isbn[isbn]
        key = work_key(title, author)
        return self.by_key.get(key) if key else None

    def has_lists(self, work: int) -> bool:
        return any(work in self.lists[kind] for kind in KINDS)

    def neighbors(self, work: int, kind: Kind) -> tuple[int, ...]:
        """이 작품의 목록에 든 작품들(한 걸음). 순서에는 뜻이 없다."""
        return self.lists[kind].get(work, ())

    def is_hub(self, work: int) -> bool:
        return work in self.hubs

    def pick(
        self,
        work: int,
        kind: Kind,
        limit: int,
        usable: Callable[[int], bool] = lambda _work: True,
    ) -> list[int]:
        """이웃 중 허브를 빼고 남의 목록에 덜 든 작품부터 limit 개. usable 이 아니라고 한 작품은 건너뛴다."""
        ranked = sorted(
            (w for w in self.neighbors(work, kind) if not self.is_hub(w) and usable(w)),
            key=lambda w: (self.listed[kind][w], w),
        )
        return ranked[:limit]


def build(lines: Iterable[str]) -> dict:
    """usage.jsonl 의 줄들로 그래프 파일 내용을 만든다. 한 줄 = {"isbn13", "resp": 정보나루 응답}."""
    keys: list[str] = []
    by_key: dict[str, int] = {}
    by_isbn: dict[str, int] = {}

    def work_for(isbn: str, name: str, authors: str) -> int:
        if isbn in by_isbn:
            return by_isbn[isbn]
        key = work_key(name, authors)
        if key is not None and key in by_key:
            work = by_key[key]
        else:
            work = len(keys)
            keys.append(key or "")
            if key is not None:
                by_key[key] = work
        by_isbn[isbn] = work
        return work

    lists: dict[Kind, dict[int, set[int]]] = {kind: {} for kind in KINDS}
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = ((record.get("resp") or {}).get("response")) or {}
        book = response.get("book") or {}
        isbn = book.get("isbn13") or record.get("isbn13")
        if not isbn:
            continue
        source = work_for(isbn, book.get("bookname", ""), book.get("authors", ""))
        for field, kind in SOURCE_LISTS.items():
            for item in response.get(field) or []:
                neighbor = (item or {}).get("book") or {}
                if not neighbor.get("isbn13"):
                    continue
                target = work_for(
                    neighbor["isbn13"],
                    neighbor.get("bookname", ""),
                    neighbor.get("authors", ""),
                )
                if target != source:
                    lists[kind].setdefault(source, set()).add(target)
            # 목록을 받은 책은 이웃이 0권이어도 "받은 책"으로 남긴다(빈 목록과 안 받은 책을 가른다)
            lists[kind].setdefault(source, set())

    listed = {kind: [0] * len(keys) for kind in KINDS}
    for kind in KINDS:
        for targets in lists[kind].values():
            for target in targets:
                listed[kind][target] += 1
    return {
        "version": FORMAT_VERSION,
        "source": "도서관 정보나루 도서별 이용 분석(usageAnalysisList) — 함께 대출·마니아 추천 목록",
        "keys": keys,
        "isbns": by_isbn,
        "lists": {
            kind: {str(work): sorted(targets) for work, targets in lists[kind].items()}
            for kind in KINDS
        },
        "listed": listed,
    }


def from_data(data: dict) -> ReadingGraph:
    if data.get("version") != FORMAT_VERSION:
        raise ValueError(f"그래프 파일 형식 {data.get('version')} 은 읽을 수 없다")
    keys = tuple(data["keys"])
    listed = {kind: tuple(data["listed"][kind]) for kind in KINDS}
    total = [sum(listed[kind][w] for kind in KINDS) for w in range(len(keys))]
    hubs = sorted(range(len(keys)), key=lambda w: -total[w])[:HUB_TOP]
    isbns: dict[int, list[str]] = {}
    for isbn, work in data["isbns"].items():
        isbns.setdefault(work, []).append(isbn)
    return ReadingGraph(
        keys=keys,
        by_isbn=dict(data["isbns"]),
        by_key={key: work for work, key in enumerate(keys) if key},
        lists={
            kind: {int(w): tuple(ts) for w, ts in data["lists"][kind].items()}
            for kind in KINDS
        },
        listed=listed,
        hubs=frozenset(w for w in hubs if total[w] > 0),
        isbns={work: tuple(sorted(found)) for work, found in isbns.items()},
    )


def load(path: str | Path) -> ReadingGraph | None:
    """그래프 파일(gzip JSON)을 읽는다. 없거나 못 읽으면 None — 챗봇은 그래프 없이 돈다."""
    file = Path(path)
    if not file.is_file():
        logger.warning("함께 대출 그래프 파일이 없어 그래프 갈래 없이 돈다: %s", file)
        return None
    try:
        with gzip.open(file, "rt", encoding="utf-8") as f:
            graph = from_data(json.load(f))
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception(
            "함께 대출 그래프 파일을 못 읽어 그래프 갈래 없이 돈다: %s", file
        )
        return None
    logger.info(
        "함께 대출 그래프를 읽었다: 작품 %d, 목록 있는 작품 %d",
        len(graph.keys),
        len(graph.lists["co"]),
    )
    return graph


_graph: ReadingGraph | None = None


def install(graph: ReadingGraph | None) -> None:
    """서버가 켜질 때 읽은 그래프를 둔다. 테스트는 작은 그래프를 끼운다."""
    global _graph
    _graph = graph


def get() -> ReadingGraph | None:
    return _graph
