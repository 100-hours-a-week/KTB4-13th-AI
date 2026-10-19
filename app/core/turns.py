"""③⑦이 같이 받는 최근 대화(recent_turns)를 요청 검사 전에 다듬는다(#402, #490).

명세: 최대 20턴(넘으면 최근 20턴만), 한 줄 200자. 한 줄이 200자를 넘으면 400 대신 뒤쪽 200자만 쓴다.
⑦ 의 답변에는 길이 상한이 없어서 호출자가 그대로 실으면 넘을 수 있고, 챗봇 입구는 ③ 검사를 먼저 하므로 ③ 이
400 으로 거절하면 긴 답 다음 턴 전체가 갈래와 상관없이 실패한다. 참고용 문맥이라 잘라도 손해가 적고, 제안과
질문("~담을까요?")은 말 끝에 있어서 뒤쪽을 남긴다.
"""

MAX_RECENT_TURNS = 20
MAX_TURN_CHARS = 200


def _tail_text(item: object) -> object:
    """한 줄의 text 가 상한을 넘으면 뒤쪽만 남긴다. 형식이 틀린 항목은 그대로 두어 strict 검사가 400 으로 거절한다."""
    if (
        isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and len(item["text"]) > MAX_TURN_CHARS
    ):
        return {**item, "text": item["text"][-MAX_TURN_CHARS:]}
    return item


def tail_turns(value: object) -> object:
    """field_validator(mode="before")에서 부른다. null 은 빈 목록, 줄 수가 넘으면 최근 것만, 긴 줄은 뒤쪽만."""
    if value is None:
        return []
    if isinstance(value, list):
        return [_tail_text(item) for item in value[-MAX_RECENT_TURNS:]]
    return value
