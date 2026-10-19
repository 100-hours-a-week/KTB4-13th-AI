"""③ 긴 이유가 책 소개를 그대로 옮겼는지 재는 어림(#503).

긴 이유(reason_long)는 도서 상세 페이지에 책 소개와 같이 보인다. 긴 이유가 책 소개를 옮기면 같은 글이 두 번 보인다
(운영 실측: 『물감을 사야 해서, 퇴사는 잠시 미뤘습니다』의 긴 이유가 책 소개와 한 낱말만 달랐다).

띄어쓰기와 기호를 지운 두 글에서, 긴 이유의 이어진 8글자 묶음 가운데 책 소개에도 있는 것의 몫을 잰다.
가장 긴 공통 부분 문자열로 재면 낱말 하나만 빼고 옮긴 글이 거기서 반으로 끊겨 낮게 나온다. 묶음으로 세면
빠진 낱말 근처의 묶음만 빠진다. 8글자는 한국어로 낱말 두세 개라, "코딩 테스트"·"이 책은"처럼 짧게 겹치는 말은
세지 않는다.

③ 카드 생성(app/routers/chat.py)이 옮긴 긴 이유를 버릴 때와 평가 채점(evals/chat/grade.py)이 같은 잣대를 쓴다.
"""

import re

COPY_NGRAM = 8
# 긴 이유의 묶음 중 이만큼 이상이 책 소개에 있으면 옮긴 글로 본다. 평가 원본(10/16, 긴 이유 1,469개)에서
# 0.5 이상은 2.9%였고, 하나씩 읽어 보면 책 소개의 문장을 이어 붙이고 몇 낱말만 바꾼 글이었다.
COPIED_SHARE_MIN = 0.5

_NON_WORD = re.compile(r"[\W_]+")


def _squash(text: str | None) -> str:
    return _NON_WORD.sub("", (text or "").lower())


def copied_share(text: str | None, source: str | None) -> float:
    """text 의 8글자 묶음 중 source 에도 있는 것의 몫(0~1). text 가 8글자보다 짧으면 0."""
    squashed, origin = _squash(text), _squash(source)
    grams = [
        squashed[i : i + COPY_NGRAM] for i in range(len(squashed) - COPY_NGRAM + 1)
    ]
    if not grams or not origin:
        return 0.0
    in_source = {
        origin[i : i + COPY_NGRAM] for i in range(len(origin) - COPY_NGRAM + 1)
    }
    return sum(g in in_source for g in grams) / len(grams)
