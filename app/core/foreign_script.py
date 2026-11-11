"""③⑦ 답에 섞인 다른 나라 글자를 거른다(#717).

모델이 한국어 낱말 하나를 같은 뜻의 다른 나라 말로 바꿔 쓰는 일이 있다. ⑦(gpt-5.4-mini) 평가 기록 5,822답 중
4번("այժմ 합계는 22,000원입니다", "সেখানে에서 결제하시면 됩니다", "리눅스 명령어 사전은 हट었습니다"), 11/11 dev 와
같은 설정으로 30번 중 1번이었다. ③(gpt-4o-mini) 평가에도 "위로를 주는 पुस्तक입니다", "경제를 легко 이해할 수
있는"이 있었다. ③ 카드 프롬프트의 "반드시 한국어로만 답하라"로는 막히지 않는다.

거르는 글자는 한국어 책 서비스의 답에 나올 일이 없는 것만이다: 키릴, 아르메니아, 히브리, 아랍, 인도계(데바나가리·
벵골 등), 타이, 라오, 티베트, 미얀마, 조지아, 에티오피아, 크메르. 한자와 가나는 책 제목과 저자에 실제로 쓰여서
(『雨の日』, 川原仁珠) 거르지 않는다. 그리스 문자도 수식(α, π)에 쓰여 거르지 않는다.

섞인 낱말은 거의 한국어 낱말 하나를 대신한다. 따로 떨어진 낱말("այժմ 합계는")은 빼도 문장이 선다. 한국어에 붙은
낱말("সেখানে에서")은 빼면 문장이 깨져서 그 문장을 뺀다. 문장이 다 빠지면 글자만 지운다(빈 답보다 낫다).
"""

import re

# 한국어 답에 나올 일이 없는 글자. U+1100~11FF(한글 자모)는 넣지 않는다
_FOREIGN = re.compile(
    "["
    "Ѐ-࿿"  # 키릴부터 티베트까지(아르메니아·히브리·아랍·인도계·타이·라오 포함)
    "က-ჿ"  # 미얀마, 조지아
    "ሀ-᎟"  # 에티오피아
    "ក-៿"  # 크메르
    "ﬓ-﷿"  # 아르메니아·히브리·아랍 합자
    "ﹰ-ﻼ"  # 아랍 표시형
    "]+"
)
# 책 제목. 제목에 든 글자는 그 책의 이름이라 건드리지 않는다
_TITLE = re.compile(r"《[^》]*》|『[^』]*』|「[^」]*」|〈[^〉]*〉")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_MARK = re.compile(r" +(?=[.,!?。])")


def _runs(text: str) -> list[re.Match[str]]:
    """책 제목 밖에 있는 다른 나라 글자 덩어리."""
    titles = [m.span() for m in _TITLE.finditer(text)]
    return [
        m
        for m in _FOREIGN.finditer(text)
        if not any(start <= m.start() < end for start, end in titles)
    ]


def _attached(text: str, run: re.Match[str]) -> bool:
    """글자 덩어리가 앞뒤 낱말(한글·영문·숫자)에 붙어 있나."""
    before = text[run.start() - 1] if run.start() > 0 else ""
    after = text[run.end()] if run.end() < len(text) else ""
    return before.isalnum() or after.isalnum()


def _cut(text: str, runs: list[re.Match[str]]) -> str:
    """글자 덩어리만 지우고 띄어쓰기를 다듬는다."""
    for run in reversed(runs):
        text = text[: run.start()] + text[run.end() :]
    return _SPACE_BEFORE_MARK.sub("", _SPACES.sub(" ", text)).strip()


def _sentence(sentence: str) -> str:
    runs = _runs(sentence)
    if not runs:
        return sentence
    if any(_attached(sentence, run) for run in runs):
        return ""
    return _cut(sentence, runs)


def has_foreign(text: str) -> bool:
    return bool(_runs(text))


def without_foreign(text: str) -> str:
    """다른 나라 글자를 거른 글. 그런 글자가 없으면 그대로 돌려준다.

    줄 구조(목록, 답 보기 "• " 줄)는 지킨다. 거른 줄만 문장을 다시 잇고, 다 빠진 줄은 뺀다.
    """
    if not has_foreign(text):
        return text
    lines: list[str] = []
    for line in text.split("\n"):
        if not has_foreign(line):
            lines.append(line)
            continue
        kept = [s for s in map(_sentence, _SENTENCE_END.split(line.strip())) if s]
        if kept:
            lines.append(" ".join(kept))
    cleaned = "\n".join(lines).strip()
    if cleaned:
        return cleaned
    return "\n".join(
        cut for line in text.split("\n") if (cut := _cut(line, _runs(line)))
    )
