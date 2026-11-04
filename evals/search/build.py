"""① 검색 평가 묶음(cases.json)을 평가 DB 에서 뽑는다(#495).

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.build --catalog-built 2026-10-13

같은 책 표와 같은 시드면 같은 묶음이 나온다. 책 표를 읽기만 하고 LLM 은 부르지 않는다. 고르는 규칙은
README 에 있다.
"""

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg

from app.core import editions, products
from app.feed.interest_picks import has_author

CASES_PATH = Path(__file__).with_name("cases.json")
VERSION = 3
# 위 몇 권을 보는지. 사례마다 /search 에 이 권수를 한 번 요청한다
TOP = 20
# 책을 고르는 순서를 정하는 값. 바꾸면 다른 묶음이 나온다
SEED = "495"
TITLES = 300
TITLE_CHARS = (2, 40)
AUTHORS = 30
# 저자의 책이 평가 DB 에 이만큼은 있어야 위 10권이 모두 그 저자의 책일 수 있다
AUTHOR_MIN_BOOKS = 10
# 시드 순서로 앞에서부터 이만큼만 읽어 고른다. 드문 표기 변형이 모자라면 늘린다
POOL = 20_000

# 표기를 바꾸는 방법과 사례 수. 이 순서로 채운다. 드문 방법(부제가 있거나 끝에 권수가 붙은 제목)부터 채워야
# 흔한 방법이 그 책을 먼저 가져가지 않는다.
CHANGES = {
    "no-subtitle": 20,  # 부제(':' 뒤)를 뺀다
    "volume-roman": 13,  # 끝의 권수 숫자를 로마 숫자로(2 → Ⅱ, #241)
    "volume-circled": 12,  # 끝의 권수 숫자를 동그라미 숫자로(2 → ②, #200)
    "case": 25,  # 영문 대소문자를 바꾼다
    "nospace": 30,  # 띄어쓰기를 없앤다
}
# 부제를 빼고 남은 제목이 이보다 짧으면(띄어쓰기·문장부호 빼고) 쓰지 않는다. "정보"처럼 짧으면 그 제목의
# 책이 너무 많아, 부제 없이 찾는 일을 잰다고 보기 어렵다.
MIN_MAIN_TITLE = 4

_VOLUME = re.compile(r"^(.*\S)\s+(\d{1,2})$")
_ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# 평가 DB 의 저자 칸은 지은이·그린이·옮긴이를 쉼표로 이어 적는다("한강, 데버라 스미스"). 가운뎃점으로 잇기도 한다.
_AUTHOR_SPLIT = re.compile(r"[,;·]")
# 한글 이름은 성으로 시작하는 두–네 글자만 받는다. 편집, 기획 같은 낱말이 이름으로 잡히지 않게 한다.
_KOREAN_NAME = re.compile(r"^[가-힣]{2,4}$")
_SURNAMES = frozenset(
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민진나지엄채원천방공현함"
    "변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예경봉사부가복태목형피두감음빈동온호범좌팽승간"
)
# 띄어 쓴 한글 낱말 둘–넷은 외국 이름으로 본다("무라카미 하루키", "알랭 드 보통").
_FOREIGN_NAME = re.compile(r"^[가-힣]+(?: [가-힣]+){1,3}$")
# 사람이 아닌 저자(단체, 편집진)이거나 역할 말이 붙은 이름
_NOT_PERSON = re.compile(
    "편집|기획|연구|출판|위원|협회|학회|재단|센터|교육|학원|시험|고시|교재|콘텐츠|스튜디오|미디어|아카데미|그룹"
    "|컴퍼니|작가|미상|그림|지음|옮김|엮음|감수"
)


def change_title(title: str, change: str) -> str | None:
    """제목의 표기를 바꾼 검색어. 그 방법으로 바꿀 수 없는 제목이면 None."""
    if change == "nospace":
        query = "".join(title.split())
    elif change == "no-subtitle":
        query = title.split(":", 1)[0].strip()
        if len(editions.title_key(query)) < MIN_MAIN_TITLE:
            return None
    elif change in ("volume-roman", "volume-circled"):
        match = _VOLUME.match(title)
        digits = _ROMAN if change == "volume-roman" else _CIRCLED
        if not match or not 1 <= int(match[2]) <= len(digits):
            return None
        query = f"{match[1]} {digits[int(match[2]) - 1]}"
    elif change == "case":
        # 소문자가 하나라도 있으면 모두 대문자로, 없으면 모두 소문자로. 영문만 바꾼다.
        flip = str.upper if re.search("[a-z]", title) else str.lower
        query = "".join(flip(c) if c.isascii() else c for c in title)
    else:
        raise ValueError(f"모르는 바꾸는 방법: {change}")
    return query if query != title else None


def first_author(author: str | None) -> str:
    """저자 칸의 첫 이름."""
    return _AUTHOR_SPLIT.split(author or "", maxsplit=1)[0].strip()


def person_like(name: str) -> bool:
    """사람 이름으로 보이는가. 단체(편집부, 연구소)나 역할 말이 든 이름은 아니다."""
    if _NOT_PERSON.search(name):
        return False
    if _KOREAN_NAME.match(name):
        return name[0] in _SURNAMES
    return bool(_FOREIGN_NAME.match(name))


# 흔한 낱말 다섯에 조건 넷(분야, 출간연도, 가격, 셋 다)을 붙인다. 분야는 ① 요청이 받는 온보딩 값만 쓴다(#219).
# (낱말, 분야, (출간연도 부터, 까지), (가격 부터, 까지)). None 은 그쪽 끝을 열어 둔다는 뜻이다.
FILTERS = (
    ("사랑", "소설", (2020, None), (None, 12000)),
    ("여행", "여행", (2010, 2015), (15000, 25000)),
    ("마음", "에세이", (None, 2010), (10000, None)),
    ("역사", "역사", (2018, 2022), (None, 20000)),
    ("행복", "자기계발", (2015, None), (12000, 18000)),
)
# 없는 말(#548). (소분류, 검색어). 자판을 아무렇게나 친 것처럼 만들었다. 소분류는 자음·모음만(jamo), 쓰지 않는
# 음절(syllable), 영문(latin), 한글을 영문 자판으로 친 것(layout), 기호·숫자(symbol), 섞은 것(mixed)이다.
# 앞의 다섯은 버전 2 의 것 그대로다. "0000", "aaaa" 처럼 제목에 그 글자가 든 책이 있어 키워드로 결과가 나오는 것도 둔다.
NONSENSE = (
    ("jamo", "ㅁㄴㅇㄹㅍ"),
    ("jamo", "ㅋㅌㅊ ㅍㅎㄱ"),
    ("syllable", "뷁쒧퀣"),
    ("latin", "qzxjvk wpfh"),
    ("jamo", "ㄷㄱㅅㅂ ㅈㄷㄱ"),
    ("jamo", "ㅁㄴㅇㄹ"),
    ("jamo", "ㅋㅋㅋㅋㅋ"),
    ("jamo", "ㅎㅎㅎ"),
    ("jamo", "ㅇㅇ"),
    ("jamo", "ㄱㄴㄷㄹ"),
    ("jamo", "ㅠㅠㅠㅠ"),
    ("jamo", "ㅜㅜ"),
    ("jamo", "ㅏㅑㅓㅕ"),
    ("jamo", "ㅗㅛㅜㅠ"),
    ("syllable", "뷁"),
    ("syllable", "쀍쒧"),
    ("syllable", "퉯뷁"),
    ("syllable", "궰뛁쀍"),
    ("syllable", "햏햏"),
    ("syllable", "뭙뭙뭙"),
    ("syllable", "쭳쭳"),
    ("syllable", "깳깳깳깳"),
    ("syllable", "뛟"),
    ("syllable", "뾸쀍뺡"),
    ("syllable", "가나다라마바사"),
    ("syllable", "아야어여오요"),
    ("latin", "asdf"),
    ("latin", "asdfasdf"),
    ("latin", "qwerty"),
    ("latin", "zxcvbnm"),
    ("latin", "jjjjjj"),
    ("latin", "aaaa"),
    ("latin", "sdfsdfsdf"),
    ("latin", "lkjhgf"),
    ("latin", "qwer asdf"),
    ("latin", "xkcd zzq"),
    ("latin", "fjdksla fjdk"),
    ("layout", "dkssudgktpdy"),
    ("layout", "rkskek"),
    ("layout", "wkdtjr"),
    ("layout", "qkrwldms"),
    ("layout", "dlrjtdms"),
    ("symbol", "!!!???"),
    ("symbol", "..."),
    ("symbol", "^^;;"),
    ("symbol", "1234567"),
    ("symbol", "0000"),
    ("symbol", "@#$%"),
    ("mixed", "ㅁㄴㅇㄹ 1234"),
    ("mixed", "ㅂㅈㄷㄱ asdf"),
)
# 키워드 결과가 0건인 있는 말(#548). (소분류, 검색어). 사람이 찾는 책이 있는 말인데 키워드 검색이 0건이라 벡터
# 결과에만 기댄다. 없는 말을 자르는 규칙을 넣을 때 이 말들이 비지 않는지 지키려고 둔다. 1권 이상 나오면 맞다.
# 소분류는 제목 오타(typo), 로마자로 쓴 제목·저자(romanized), 제목 초성(chosung), 상황을 구어로 말한 것(colloquial).
# 평가 DB 에서 키워드 결과가 0건인 것만 골랐다(10/22). 책 표가 바뀌면 키워드에 걸리는 것이 생길 수 있다.
NO_KEYWORD = (
    ("typo", "데미얀"),
    ("typo", "채식쥬의자"),
    ("typo", "아몬듣"),
    ("typo", "사피언스ㅡ"),
    ("typo", "해리퍼터"),
    ("typo", "어린왕쟈님"),
    ("typo", "룬의아이들 겨울의 적쟈"),
    ("typo", "역행쟈"),
    ("typo", "원씽ㅇ"),
    ("romanized", "haripota"),
    ("romanized", "ingan sil-gyeok"),
    ("romanized", "tolseutoi"),
    ("romanized", "eorin wangja"),
    ("romanized", "chaesikjuuija"),
    ("romanized", "sonyeoni onda"),
    ("chosung", "ㅎㄹㅍㅌ"),
    ("chosung", "ㄷㅁㅇ"),
    ("chosung", "ㅅㄴㅇ ㅇㄷ"),
    ("chosung", "ㅇㄹㅇㅈ"),
    ("chosung", "ㅊㅅㅈㅇㅈ"),
    ("colloquial", "머리 식힐 거"),
    ("colloquial", "잠 안 올 때 볼 거"),
    ("colloquial", "출퇴근길에 가볍게 볼 거"),
    ("colloquial", "선물용으로 괜찮은 거"),
    ("colloquial", "주말에 뒹굴면서 볼 거"),
    ("colloquial", "비행기에서 볼 거"),
    ("colloquial", "입문용으로 괜찮은 거"),
    ("colloquial", "애기 재울 때 읽어 줄 거"),
    ("colloquial", "친구 생일에 줄 만한 거"),
    ("colloquial", "시험 끝나고 볼 거"),
)
# 서술형 검색어(#502). 맞는 책이 여럿이라 나온 책마다 매긴 점수(labels.json)로 잰다. 사람이 쓴 목록 그대로 쓴다.
# (소분류, 검색어, 출처). 출처는 handoff(인계 문서의 예시), chat-eval(③ 챗봇 평가 질문을 검색창에 칠 말로 줄임),
# written(직접 씀)이다. long 은 조건이 여럿인 20자 이상의 검색어다.
DESCRIPTIVE = (
    ("topic", "우주의 기원에 관한 책", "handoff"),
    ("topic", "조선 시대 궁중 음식", "handoff"),
    ("topic", "인공지능이 바꿀 미래 사회", "chat-eval"),
    ("topic", "한국사를 재미있게 풀어 쓴 책", "chat-eval"),
    ("topic", "기후 위기와 환경", "written"),
    ("topic", "컴퓨터 역사 속 해커 이야기", "chat-eval"),
    ("topic", "재테크 기초", "chat-eval"),
    ("mood", "마음이 편해지는 책", "handoff"),
    ("mood", "위로가 되는 에세이", "chat-eval"),
    ("mood", "여운이 남는 소설", "chat-eval"),
    ("mood", "따뜻한 소설", "chat-eval"),
    ("mood", "소름 돋는 추리 소설", "chat-eval"),
    ("mood", "기분 좋아지는 책", "chat-eval"),
    ("mood", "술술 읽히는 가벼운 소설", "chat-eval"),
    ("audience", "초등학생이 읽을 과학책", "handoff"),
    ("audience", "중학생 코딩 입문서", "chat-eval"),
    ("audience", "7살 아이 그림책", "chat-eval"),
    ("audience", "어른이 읽는 판타지 소설", "chat-eval"),
    ("audience", "사춘기 자녀를 둔 부모", "written"),
    ("audience", "비전공자를 위한 컴퓨터 기초", "chat-eval"),
    ("situation", "비 오는 날 읽을 책", "handoff"),
    ("situation", "퇴근길에 가볍게 읽을 소설", "chat-eval"),
    ("situation", "잠이 안 올 때 읽는 책", "chat-eval"),
    ("situation", "번아웃이 왔을 때", "chat-eval"),
    ("situation", "이직을 고민할 때", "written"),
    ("situation", "주말에 카페에서 읽을 소설", "chat-eval"),
    ("long", "개발자가 알아야 할 만큼만 쉽게 설명한 네트워크 책", "chat-eval"),
    ("long", "수식을 쉬운 말로 풀어 주는 파이썬 통계 입문서", "chat-eval"),
    ("long", "팀 프로젝트에서 의견 충돌을 줄이는 협업과 대화법", "chat-eval"),
    ("long", "열등감을 건강하게 다스리고 싶을 때 읽을 쉬운 철학책", "chat-eval"),
    ("long", "어려움을 이겨 내는 이야기가 담긴 어린이 그림책", "chat-eval"),
    ("long", "편의점에서 일하는 여자가 주인공인 일본 소설", "chat-eval"),
    ("long", "회사에서 발표할 때 말을 조리 있게 하는 법", "chat-eval"),
    ("long", "엄마가 된 뒤의 마음을 솔직하게 쓴 에세이", "written"),
    ("mixed", "초등학생이 읽을 우주 이야기", "written"),
    ("mixed", "비 오는 날 읽기 좋은 잔잔한 소설", "written"),
    ("mixed", "직장인을 위한 쉬운 경제 공부", "written"),
    ("mixed", "한국 작가가 쓴 SF 소설", "chat-eval"),
    ("mixed", "마음이 지칠 때 읽는 짧은 시집", "written"),
    ("mixed", "아이와 함께 읽는 공룡 그림책", "written"),
)

# 벡터와 상품(가격)이 있는 책을 시드 순서로. ISBN 으로 순서를 정해 책 번호가 바뀌어도 같은 순서가 나온다.
_POOL_SQL = f"""
SELECT b.isbn13, b.title, b.author
FROM v_books b
WHERE char_length(b.title) BETWEEN $2 AND $3
  AND EXISTS (SELECT 1 FROM book_embeddings e WHERE e.book_id = b.book_id)
  AND {products.price_sql("b")} IS NOT NULL
ORDER BY md5($1 || ':' || b.isbn13), b.isbn13
LIMIT $4
"""
# 저자 칸에 이름이 든 책의 후보. 글자 조각 색인으로 모은 뒤 has_author 로 이름이 통째로 든 책만 센다.
_AUTHOR_SQL = "SELECT author FROM v_books WHERE $1 <% author"


def _case(case_id: str, kind: str, query: str, **fields: Any) -> dict[str, Any]:
    return {"id": case_id, "kind": kind, "query": query, **fields}


def sample(pool: Sequence[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
    """책 목록(시드 순서)에서 제목 사례와 표기 변형 사례를 고른다. 제목이 같은 책은 한 권만 쓴다.

    표기 변형은 제목 표본에서 먼저 고르고, 모자라면 표본 뒤의 책을 같은 순서로 쓴다. 책 한 권은 한 번만 쓴다.
    """
    seen: set[str] = set()
    books = []
    for row in pool:
        key = editions.title_key(row["title"])
        if key and key not in seen:
            seen.add(key)
            books.append(row)
    if len(books) < TITLES:
        raise ValueError(f"제목이 다른 책이 {len(books)}권뿐이다(필요 {TITLES})")
    titles = [
        _case(f"title-{i:03d}", "title", row["title"], isbn=row["isbn13"])
        | {"expect": {"titles": [row["title"]]}}
        for i, row in enumerate(books[:TITLES], start=1)
    ]

    used: set[int] = set()
    variants = []
    for change, quota in CHANGES.items():
        picked = 0
        for index, row in enumerate(books):
            if picked == quota:
                break
            query = None if index in used else change_title(row["title"], change)
            if query is None:
                continue
            used.add(index)
            picked += 1
            # 부제를 뺀 검색어는 부제 없는 제목의 책이 나와도 맞다
            expect = (
                [row["title"], query] if change == "no-subtitle" else [row["title"]]
            )
            variants.append(
                _case(f"title-variant-{len(variants) + 1:03d}", "title-variant", query)
                | {"change": change, "isbn": row["isbn13"]}
                | {"of": titles[index]["id"] if index < TITLES else None}
                | {"expect": {"titles": expect}}
            )
        if picked < quota:
            raise ValueError(
                f"{change} 사례가 {picked}건뿐이다(필요 {quota}). POOL 을 늘린다"
            )
    return titles, variants


async def pick_authors(
    conn: asyncpg.Connection, pool: Sequence[Mapping[str, Any]]
) -> list[dict]:
    """책 목록(시드 순서)의 첫 저자 가운데 사람 이름이고 책이 AUTHOR_MIN_BOOKS 권 이상인 사람."""
    names: list[str] = []
    seen: set[str] = set()
    for row in pool:
        name = first_author(row["author"])
        if name in seen or not person_like(name):
            continue
        seen.add(name)
        rows = await conn.fetch(_AUTHOR_SQL, name)
        if sum(has_author(r["author"], name) for r in rows) >= AUTHOR_MIN_BOOKS:
            names.append(name)
            if len(names) == AUTHORS:
                break
    if len(names) < AUTHORS:
        raise ValueError(f"저자가 {len(names)}명뿐이다(필요 {AUTHORS}). POOL 을 늘린다")
    return [
        _case(f"author-{i:02d}", "author", name, expect={"author": name})
        for i, name in enumerate(names, start=1)
    ]


def _between(low_key: str, high_key: str, bounds: tuple) -> dict[str, int]:
    return {
        k: v for k, v in zip((low_key, high_key), bounds, strict=True) if v is not None
    }


def fixed_cases() -> list[dict]:
    """필터, 없는 말, 키워드 0건인 있는 말, 서술형의 사례. 표본을 뽑지 않고 위의 목록 그대로 만든다."""
    cases = []
    for word, category, years, price in FILTERS:
        one = (
            {"category": category},
            _between("pub_year_from", "pub_year_to", years),
            _between("price_min", "price_max", price),
        )
        for filters in (*one, one[0] | one[1] | one[2]):
            cases.append(
                _case(f"filter-{len(cases) + 1:02d}", "filter", word, filters=filters)
            )
    cases += [
        _case(f"nonsense-{i}", "nonsense", query, group=group)
        for i, (group, query) in enumerate(NONSENSE, start=1)
    ]
    cases += [
        _case(f"no-keyword-{i:02d}", "no-keyword", query, group=group)
        for i, (group, query) in enumerate(NO_KEYWORD, start=1)
    ]
    return cases + [
        _case(f"descriptive-{i:02d}", "descriptive", query, facet=facet, source=source)
        for i, (facet, query, source) in enumerate(DESCRIPTIVE, start=1)
    ]


def dump(bundle: Mapping[str, Any]) -> str:
    """사례를 한 줄에 하나씩 쓴다. 사례를 고친 diff 를 줄 단위로 볼 수 있다."""

    def one(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = ["{"]
    lines += [f" {one(k)}: {one(v)}," for k, v in bundle.items() if k != "cases"]
    cases = ",\n".join(f"  {one(case)}" for case in bundle["cases"])
    lines.append(f' "cases": [\n{cases}\n ]')
    return "\n".join([*lines, "}"]) + "\n"


async def build(url: str, built: str) -> dict[str, Any]:
    conn = await asyncpg.connect(url)
    try:
        pool = await conn.fetch(_POOL_SQL, SEED, *TITLE_CHARS, POOL)
        titles, variants = sample(pool)
        authors = await pick_authors(conn, pool)
        catalog = {
            "built": built,
            "books": await conn.fetchval("SELECT count(*) FROM v_books"),
            "with_vectors": await conn.fetchval("SELECT count(*) FROM book_embeddings"),
        }
    finally:
        await conn.close()
    return {
        "version": VERSION,
        "top": TOP,
        "seed": SEED,
        "catalog": catalog,
        "cases": [*titles, *variants, *authors, *fixed_cases()],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="① 검색 평가 묶음을 뽑는다")
    parser.add_argument(
        "--catalog-built", required=True, help="책 표를 만든 날. 예: 2026-10-13"
    )
    parser.add_argument("--out", default=str(CASES_PATH), help="쓸 파일")
    args = parser.parse_args(argv)

    from app.core.config import get_settings

    url = get_settings().database_url
    name = urlsplit(url).path.lstrip("/")
    if not name.endswith("_eval"):
        # 다른 책 표에서 뽑으면 평가 DB 에 없는 책이 묶음에 들어간다
        print(f"DB '{name}' 는 평가용(이름이 _eval 로 끝남)이 아니다", file=sys.stderr)
        return 2
    bundle = asyncio.run(build(url, args.catalog_built))
    Path(args.out).write_text(dump(bundle), encoding="utf-8")
    print(f"DB {name} 에서 사례 {len(bundle['cases'])}건을 뽑아 {args.out} 에 썼다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
