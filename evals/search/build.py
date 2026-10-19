"""① 검색 평가 묶음(cases.json)을 평가 DB 에서 뽑는다(#495).

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.build --catalog-built 2026-10-13

같은 책 표와 같은 시드면 같은 묶음이 나온다. 책 표를 읽기만 하고 LLM 은 부르지 않는다. 고르는 규칙은
README 에 있다.
"""

import re

from app.core import editions

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
