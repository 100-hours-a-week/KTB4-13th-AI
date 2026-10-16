"""③ 카드 생성 프롬프트의 형식 예시가 답으로 새어 나오지 않게 한다(#410).

JSON 형식 예시에 `"detail": "잔잔함"`이 들어 있었더니, 모델이 사용자가 말하지 않았는데도 답과 이유에
"잔잔한 분위기"를 썼다(평가: LLM을 부른 턴의 32%). 예시를 설명으로 바꾸면 3.6%로 줄었다(10/12 실험).
프롬프트 글자가 그대로 나가는지는 test_chat_router.py 가 보고, 여기서는 예시에 분위기 낱말이 없는지만 지킨다.
"""

import pytest

from app.routers import chat

# 형식 예시에 있으면 모델이 베끼는 낱말. 사용자가 말하지 않은 분위기·톤이다.
MOOD_WORDS = ("잔잔", "따뜻", "가벼운", "편안", "재미있", "감동")


def _prompt() -> str:
    return chat.CARD_PROMPT.format(intro="아래는 후보 목록이다.", limit=3, listing="")


@pytest.mark.parametrize("word", MOOD_WORDS)
def test_카드_프롬프트에_분위기_낱말_예시가_없다(word: str) -> None:
    assert word not in _prompt()


def test_match_basis_형식_예시는_설명으로_적는다() -> None:
    # 같은 JSON 안의 reason_short·reason_long 칸처럼 값 자리에 설명을 쓴다.
    assert '"label": "근거 이름", "detail": "그 근거의 구체적 내용"' in _prompt()
