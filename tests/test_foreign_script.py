"""답에 섞인 다른 나라 글자 거르기(app/core/foreign_script.py, #717) 테스트.

문장은 평가 기록에서 실제로 나온 것이다(⑦ gpt-5.4-mini, ③ gpt-4o-mini).
"""

import pytest

from app.core.foreign_script import has_foreign, without_foreign


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 따로 떨어진 낱말은 그 낱말만 뺀다(아르메니아어 '이제', 러시아어 '쉽게', 아랍어 '날마다의')
        (
            "수량을 2권으로 줄였습니다. այժմ 합계는 22,000원입니다.",
            "수량을 2권으로 줄였습니다. 합계는 22,000원입니다.",
        ),
        (
            "경제를 легко 이해할 수 있는 입문서입니다.",
            "경제를 이해할 수 있는 입문서입니다.",
        ),
        (
            "작은 여유를 다뤄요. اليومية 웃음과 여유가 따뜻해요.",
            "작은 여유를 다뤄요. 웃음과 여유가 따뜻해요.",
        ),
        # 한국어에 붙은 낱말은 그 문장을 뺀다(벵골어 '거기', 힌디어 '빼')
        (
            "《처음 만나는 파이썬》 주문 화면으로 보냈어요. সেখানে에서 결제하시면 됩니다.",
            "《처음 만나는 파이썬》 주문 화면으로 보냈어요.",
        ),
        (
            "리눅스 명령어 사전은 हट었습니다. 합계는 30,000원입니다.",
            "합계는 30,000원입니다.",
        ),
        # 질문 뒤에 홀로 붙은 낱말(아르메니아어 '지금')
        (
            "어느 책의 수량을 몇 권으로 바꿔드릴까요? հիմա",
            "어느 책의 수량을 몇 권으로 바꿔드릴까요?",
        ),
    ],
)
def test_섞인_낱말을_거른다(text: str, expected: str) -> None:
    assert has_foreign(text)
    assert without_foreign(text) == expected


def test_문장이_다_빠지면_글자만_지운다() -> None:
    # 빈 말풍선보다 낫다. 이 경우만 문장이 조금 어색할 수 있다
    assert without_foreign("위로를 주는 पुस्तक입니다.") == "위로를 주는 입니다."


def test_다른_나라_글자뿐이면_빈_글이다() -> None:
    assert without_foreign("հիմա") == ""


def test_답_보기_줄은_줄마다_거른다() -> None:
    text = "어느 쪽이 좋으세요?\n• 소설 쪽\n• поколения 쪽"

    assert without_foreign(text) == "어느 쪽이 좋으세요?\n• 소설 쪽\n• 쪽"


def test_거를_것이_없는_줄은_띄어쓰기까지_그대로다() -> None:
    text = "장바구니에 1권이 담겨 있습니다.\n\n1번째:  《책》 1권  \n합계 легко"

    assert (
        without_foreign(text)
        == "장바구니에 1권이 담겨 있습니다.\n\n1번째:  《책》 1권  \n합계"
    )


@pytest.mark.parametrize(
    "text",
    [
        "『Война и мир』를 담았어요.",  # 책 제목 안의 글자는 그 책의 이름이다
        "《雨の日》은 川原仁珠의 책이에요.",  # 한자·가나는 제목·저자에 쓰인다
        "SF와 SQL 입문서예요. α와 π도 나와요.",  # 영문·그리스 문자는 거르지 않는다
        "장바구니에 담았어요.",
    ],
)
def test_거르지_않는_글은_그대로다(text: str) -> None:
    assert not has_foreign(text)
    assert without_foreign(text) == text
