"""책 제목의 권 번호를 다룬다(#615). 표지 규칙과 책장 규칙이 같이 쓴다.

판 표시("개정판", "3/E", "2nd Edition")는 권 번호가 아니라 버린다. 판 표시를 권 번호로 붙이면 제목이 같은 책을
못 찾는다. 반대로 진짜 권 번호를 빼고 찾으면 카탈로그에 없는 2권 사진을 1권으로 확정한다. 그래서 모양으로 가르고,
권 번호면 끝까지 붙여 찾는다.
"""

import re

from app.core import editions

# 권 번호 "1"은 표지에 적혀 있어도 카탈로그 제목에는 없는 경우가 많다
FIRST_VOLUMES = frozenset({"1", "i", "ⅰ", "상", "上"})
# 흔한 권 번호 모양: 숫자(연도판 "2027" 포함), 상·중·하
_SIMPLE = re.compile(r"\d{1,4}|상|중|하|上|中|下")
# 로마 숫자 권 번호와 아라비아 숫자. 표지는 "II", 카탈로그는 "2"나 "Ⅱ"로 적기도 한다(title_key 가 소문자로 바꾼다)
_ROMAN = {
    **{
        r: str(n)
        for n, r in enumerate(
            ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"], 1
        )
    },
    **{chr(0x2170 + n - 1): str(n) for n in range(1, 11)},
}
# 판 표시. 숫자가 있어도 권 번호가 아니다
_EDITION_MARK = re.compile(
    r"판|에디션|edition|\bed\.?(?!\w)|/e\b|\d+\s*(st|nd|rd|th)\b", re.IGNORECASE
)
_DIGITS = re.compile(r"\d+")


def is_edition_mark(text: str | None) -> bool:
    return bool(text) and bool(_EDITION_MARK.search(text))


def keys(volume: str | None) -> set[str]:
    """권 번호의 여러 표기("2"·"ii"·"ⅱ"). 판 표시나 숫자 없는 글자면 빈 집합이다.

    "PART 4 01"처럼 흔하지 않은 모양도 숫자가 있으면 권 표기로 보고 통째로 붙여 찾는다. 버리면 권마다 카탈로그 제목이
    같은 책(『전지적 독자 시점』)의 다른 권을 확정한다. 표지 시험의 틀린 확정 4건이 전부 이것이었다(#615).
    """
    if not volume or is_edition_mark(volume):
        return set()
    key = editions.title_key(volume)
    if key in _ROMAN:
        return {key, _ROMAN[key], *(r for r, n in _ROMAN.items() if n == _ROMAN[key])}
    if _SIMPLE.fullmatch(key):
        return {key, *(r for r, n in _ROMAN.items() if n == key)}
    if _DIGITS.search(key):
        return {key}
    return set()


def numbers(text: str | None) -> set[int]:
    """글자 속 숫자들. "01"과 "1"을 같게 본다."""
    return {int(n) for n in _DIGITS.findall(text or "")}


def compatible(read: str | None, catalog_title: str) -> bool:
    """읽은 글자의 숫자와 카탈로그 제목의 숫자가 어긋나지 않는가. 한쪽에 숫자가 없으면 어긋나지 않은 것으로 본다."""
    a, b = numbers(read), numbers(catalog_title)
    return not a or not b or bool(a & b)
