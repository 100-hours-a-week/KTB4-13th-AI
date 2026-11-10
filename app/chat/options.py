"""③ 답 보기 줄(#652) — followup 안에서 "• "로 시작하는 줄은 사용자가 그대로 보낼 말이다.

FE 가 이 줄을 버튼으로 그리고, 누르면 그 글을 사용자 메시지로 보낸다(FE #148). BE 는 ③ 응답의 buttons 에 화면 이동
버튼만 받고 그 밖에는 턴을 실패로 처리해서, 새 버튼 칸 대신 BE 가 손대지 않고 넘기는 followup 글에 보기를 싣는다.
보기 줄을 못 그리는 화면에서는 그대로 목록 글로 보인다.

보기는 사용자의 말이다. 누르면 그 글이 다음 턴의 메시지가 되므로 다음 턴의 해석(1단계, 함께 읽은 책·비슷한 책 낱말)이
알아듣는 말로 쓴다. 번호("2번")만 쓰지 않는다 — 그 말에는 찾을 낱말이 없다.
"""

from collections.abc import Sequence

BULLET = "• "


def with_options(
    text: str | None, options: Sequence[str], max_chars: int
) -> str | None:
    """text 아래에 보기 줄을 붙인다. max_chars 를 넘으면 뒤 보기부터 뺀다. 보기만 있어도 된다(text 가 비면 보기 줄만).

    보기가 하나도 안 들어가면 text 만 돌려준다. 둘 다 없으면 None. 보기 글은 자르지 않는다 — 잘린 말은 보낼 수 없다.
    """
    head = (text or "").strip()
    picked = list(dict.fromkeys(o.strip() for o in options if o and o.strip()))
    for count in range(len(picked), 0, -1):
        listed = "\n".join(BULLET + option for option in picked[:count])
        joined = f"{head}\n\n{listed}" if head else listed
        if len(joined) <= max_chars:
            return joined
    return head or None


def has_object_final(word: str) -> bool:
    """마지막 글자에 받침이 있는가(을/를, 과/와 고르기). 숫자는 읽는 소리로 본다(영·일·삼·육·칠·팔은 받침)."""
    last = word.rstrip()[-1:]
    if "가" <= last <= "힣":
        return (ord(last) - ord("가")) % 28 != 0
    return last in "013678"
