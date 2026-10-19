"""③ 평가의 긴 이유 지표(evals/chat/reason_long.py, #503)와 일부 질문만 채점하기. 가짜 응답으로 본다.

긴 이유가 책 소개를 옮긴 것을 옮겼다고, 요청을 짚은 것을 짚었다고 세는지 본다. 지표가 틀리면 프롬프트를
고친 효과를 거꾸로 읽는다.
"""

from evals.chat import grade, reason_long

_DESCRIPTION = (
    "평범한 직장인이 퇴근 후에 그림을 배우며 발견한 빛나는 순간들을 기록한 에세이."
)
_BOOKS = {
    "1": {
        "title": "물감을 사야 해서",
        "has_description": True,
        "description": _DESCRIPTION,
    },
    "2": {"title": "토지 1", "has_description": False, "description": ""},
}


def _card(book_id: int, reason_long: str | None) -> dict:
    return {
        "book_id": book_id,
        "reason_short": "한 줄 이유",
        "reason_long": reason_long,
    }


def _turn(semantic: str, cards: list[dict], msg: str = "추천해줘") -> dict:
    return {
        "msg": msg,
        "status": 200,
        "sec": 6.0,
        "spec": {"intent": "semantic", "filters": {}, "semantic": semantic},
        "reply": "골라봤어요.",
        "followup": None,
        "degraded": False,
        "cards": cards,
    }


def _case(no: int = 1) -> dict:
    return {
        "no": no,
        "kind": "situation",
        "about": "가짜",
        "turns": ["추천해줘"],
        "expect": {"action": "recommend"},
    }


def _common(*runs: dict, books: dict = _BOOKS) -> dict:
    cases_file = {
        "version": 1,
        "kinds": {"situation": {"about": "가짜"}},
        "cases": [_case()],
    }
    raw = {"meta": {}, "books": books, "runs": [{"1": [turn]} for turn in runs]}
    return grade.summarize(cases_file, raw)["common"]


def test_요청_낱말은_조사를_떼고_앞_두_글자로_뽑고_흔한_말은_뺀다() -> None:
    assert reason_long.request_words("퇴근길에 가볍게 읽을 에세이") == [
        "퇴근",
        "가볍",
        "에세",
    ]
    assert reason_long.request_words("SQL 입문서 추천") == ["sql", "입문"]
    assert reason_long.request_words("책을 추천해줘") == []
    assert reason_long.request_words(None) == []


def test_책_소개를_옮긴_긴_이유를_센다() -> None:
    copied = _turn("퇴근길에 볼 에세이", [_card(1, _DESCRIPTION)])
    linked = _turn(
        "퇴근길에 볼 에세이",
        [_card(1, "퇴근길에 볼 책을 찾으셨죠. 그림을 배우는 직장인 이야기예요.")],
    )

    common = _common(copied, linked)

    assert common["reason_long_measured"] is True
    assert (common["reason_long_cards"], common["reason_long_copied"]) == (2, 1)


def test_책_소개에_없는_요청_낱말을_짚었는지_따로_센다() -> None:
    semantic = "면접 준비할 때 볼 에세이"
    summary_like = _turn(semantic, [_card(1, "그림을 배우는 직장인의 에세이입니다.")])
    linked = _turn(
        semantic, [_card(1, "면접 준비로 지친 저녁에 읽기 좋은 에세이예요.")]
    )

    common = _common(summary_like, linked)

    # "에세"는 책 소개에도 있어 둘 다 짚은 것으로 세지만, 책 소개에 없는 "면접"·"준비"는 한쪽만 짚었다.
    assert common["reason_long_mentions_request"] == 2
    assert (
        common["reason_long_own_word_cards"],
        common["reason_long_mentions_own_word"],
    ) == (2, 1)


def test_말하지_않았고_책_소개에도_없는_분위기를_센다() -> None:
    # 조건 문장의 "가벼운"은 사용자가 말하지 않은 말이다(1단계가 지어 넣음).
    echoed = _turn(
        "가벼운 에세이",
        [_card(1, "가벼운 에세이를 찾는 분께 잘 어울리는 책이에요.")],
        msg="에세이 추천해줘",
    )
    said = _turn(
        "가벼운 에세이",
        [_card(1, "가벼운 에세이를 찾으셨죠. 퇴근 후 그림 이야기예요.")],
        msg="가벼운 에세이 추천해줘",
    )

    common = _common(echoed, said)

    assert (common["reason_long_cards"], common["reason_long_unsaid_mood"]) == (2, 1)


def test_책_소개가_있는데_긴_이유가_빈_카드만_센다() -> None:
    # 책 소개가 없는 책(2)의 빈 긴 이유는 #407 대로라 세지 않는다.
    common = _common(_turn("소설", [_card(1, None), _card(2, None)]))

    assert (common["reason_long_cards"], common["reason_long_missing"]) == (0, 1)


def test_원본에_책_소개가_없으면_긴_이유_지표를_재지_않는다() -> None:
    old_books = {"1": {"title": "물감을 사야 해서", "has_description": True}}

    common = _common(_turn("소설", [_card(1, "긴 이유")]), books=old_books)

    assert common["reason_long_measured"] is False
    assert "reason_long_copied" not in common


def test_일부_질문만_골라_센다() -> None:
    cases_file = {
        "version": 1,
        "kinds": {"situation": {"about": "가짜"}},
        "cases": [_case(1), _case(2)],
    }
    raw = {
        "meta": {},
        "books": _BOOKS,
        "runs": [
            {"1": [_turn("소설", [_card(1, "긴 이유")])], "2": [_turn("소설", [])]}
        ],
    }

    summary = grade.summarize(cases_file, raw, only=frozenset({1}))

    assert (summary["cases"], summary["passed"], summary["only"]) == (1, 1, [1])
