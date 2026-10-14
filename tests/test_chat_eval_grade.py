"""③ 평가 채점 규칙(evals/chat/grade.py, #322). 가짜 응답으로 본다. DB·LLM 없이 돈다.

채점기가 틀리면 프롬프트를 고친 효과를 거꾸로 읽는다. 통과로 세면 안 되는 응답을 통과로 세지
않는지를 주로 본다.
"""

from evals.chat import grade

_BOOKS = {
    "1": {
        "isbn": "9790000000001",
        "title": "불편한 편의점 2",
        "author": "김호연",
        "publisher": "나무옆의자",
        "pub_year": 2022,
        "price": 14000,
        "has_description": True,
    },
    "2": {
        "isbn": "9790000000002",
        "title": "토지 1",
        "author": "박경리",
        "publisher": "다산책방",
        "pub_year": 2012,
        "price": 25000,
        "has_description": False,
    },
    "3": {
        "isbn": "9790000000003",
        "title": "토지 1",
        "author": "박경리 지음",
        "publisher": "마로니에북스",
        "pub_year": 2012,
        "price": 18000,
        "has_description": True,
    },
}


def _card(book_id: int, reason_long: str | None = "긴 이유") -> dict:
    return {
        "book_id": book_id,
        "reason_short": "한 줄 이유",
        "reason_long": reason_long,
    }


def _turn(msg: str = "추천해줘", cards: list[dict] | None = None, **over) -> dict:
    turn = {
        "msg": msg,
        "status": 200,
        "sec": 6.0,
        "spec": {"intent": "semantic", "filters": {}, "semantic": "위로되는 소설"},
        "reply": "골라봤어요.",
        "followup": None,
        "degraded": False,
        "cards": cards or [],
    }
    turn.update(over)
    return turn


def _case(turns: int = 1, **expect) -> dict:
    return {
        "no": 1,
        "kind": "situation",
        "about": "가짜",
        "turns": ["추천해줘"] * turns,
        "expect": {"action": "recommend", **expect},
    }


def test_추천을_기대한_질문은_카드가_있어야_통과다() -> None:
    assert grade.grade_case(_case(), [_turn(cards=[_card(1)])], _BOOKS) == []
    assert grade.grade_case(_case(), [_turn()], _BOOKS) == ["카드 0장"]


def test_되묻기를_기대한_질문은_카드가_없고_되묻는_문장이_있어야_통과다() -> None:
    case = _case(action="clarify")

    assert (
        grade.grade_case(case, [_turn(followup="어떤 분야가 좋으세요?")], _BOOKS) == []
    )
    assert grade.grade_case(case, [_turn(cards=[_card(1)])], _BOOKS) == ["되묻지 않음"]
    assert grade.grade_case(case, [_turn()], _BOOKS) == ["되묻지 않음"]


def test_요청이_실패했거나_턴이_모자라면_다른_검사_없이_실패다() -> None:
    case = _case(turns=2)

    assert grade.grade_case(case, [_turn(cards=[_card(1)])], _BOOKS) == ["요청 오류"]
    failed = [_turn(cards=[_card(1)]), _turn(status=500)]
    assert grade.grade_case(case, failed, _BOOKS) == ["요청 오류"]


def test_기준_책은_띄어쓰기가_달라도_잡는다() -> None:
    case = _case(cards={"title_none": ["불편한편의점"]})

    assert grade.grade_case(case, [_turn(cards=[_card(1)])], _BOOKS) == [
        "기준 책이 카드에 나옴"
    ]


def test_제목이_딱_같을_때만_잡는_검사는_제목에_든_낱말로는_안_잡는다() -> None:
    # "아몬드"를 빼 달라고 "아몬드 케이크 만들기"까지 빼면 안 된다.
    case = _case(cards={"title_none_exact": ["불편한 편의점"]})

    assert grade.grade_case(case, [_turn(cards=[_card(1)])], _BOOKS) == []


def test_가격과_출간연도는_카드_한_장이라도_어기면_실패다() -> None:
    cards = [_card(1), _card(2)]

    assert grade.grade_case(
        _case(cards={"price_max": 20000}), [_turn(cards=cards)], _BOOKS
    ) == ["가격 초과"]
    assert grade.grade_case(
        _case(cards={"year_from": 2020}), [_turn(cards=cards)], _BOOKS
    ) == ["연도 미달"]


def test_제목이_같은_카드가_둘이면_같은_책_중복이다() -> None:
    case = _case(cards={"distinct_titles": True})

    assert grade.grade_case(case, [_turn(cards=[_card(2), _card(3)])], _BOOKS) == [
        "같은 책 중복"
    ]


def test_앞_턴에서_본_책이_마지막_턴에_다시_나오면_실패다() -> None:
    case = _case(turns=2, cards={"not_shown_before": True})
    first = _turn(cards=[_card(1)])

    assert grade.grade_case(case, [first, _turn(cards=[_card(1)])], _BOOKS) == [
        "본 책이 다시 나옴"
    ]
    assert grade.grade_case(case, [first, _turn(cards=[_card(3)])], _BOOKS) == []


def test_권수를_정했는데_카드가_더_많으면_실패다() -> None:
    case = _case(cards={"max_cards": 1})

    assert grade.grade_case(case, [_turn(cards=[_card(1), _card(3)])], _BOOKS) == [
        "권수 초과"
    ]
    assert grade.grade_case(case, [_turn(cards=[_card(1)])], _BOOKS) == []


def test_보여_준_책_중에서_골라야_하는데_새_책을_내면_실패다() -> None:
    case = _case(turns=2, cards={"only_shown_before": True})
    first = _turn(cards=[_card(1), _card(3)])

    assert grade.grade_case(case, [first, _turn(cards=[_card(2)])], _BOOKS) == [
        "보여 준 책 밖에서 고름"
    ]
    assert grade.grade_case(case, [first, _turn(cards=[_card(3)])], _BOOKS) == []


def test_책소개_없는_책에_긴_이유가_있으면_실패다() -> None:
    case = _case(cards={"reason_long_needs_description": True})

    assert grade.grade_case(case, [_turn(cards=[_card(2)])], _BOOKS) == [
        "책소개 없는 책에 긴 이유"
    ]
    assert (
        grade.grade_case(case, [_turn(cards=[_card(2, reason_long=None)])], _BOOKS)
        == []
    )
    # 책소개가 있는 책의 긴 이유는 그대로 둔다.
    assert grade.grade_case(case, [_turn(cards=[_card(3)])], _BOOKS) == []


def test_턴별_조건_검사는_그_턴의_응답_spec을_본다() -> None:
    case = _case(
        turns=2,
        spec=[
            {"turn": 1, "path": "filters.price_max", "equals": 20000},
            {"turn": 2, "path": "filters.price_max", "equals": None},
        ],
    )
    held = _turn(
        cards=[_card(1)], spec={"filters": {"price_max": 20000}, "semantic": "소설"}
    )
    released = _turn(
        cards=[_card(3)], spec={"filters": {"price_max": None}, "semantic": "소설"}
    )

    assert grade.grade_case(case, [held, released], _BOOKS) == []
    assert grade.grade_case(case, [held, held], _BOOKS) == [
        "조건 불일치(2턴 filters.price_max)"
    ]


def test_톤을_바꿔_달라고_했는데_조건_문장이_그대로면_실패다() -> None:
    case = _case(turns=2, semantic={"changes_at": 2})
    same = [_turn(cards=[_card(1)]), _turn(cards=[_card(3)])]
    changed = [
        _turn(cards=[_card(1)]),
        _turn(
            cards=[_card(3)], spec={"filters": {}, "semantic": "무겁지 않은 위로 소설"}
        ),
    ]

    assert grade.grade_case(case, same, _BOOKS) == ["톤 미반영"]
    assert grade.grade_case(case, changed, _BOOKS) == []


def test_주제를_바꿨는데_옛_주제가_남으면_실패다() -> None:
    case = _case(semantic={"has": ["주식"], "lacks": ["위로"]})

    assert grade.grade_case(case, [_turn(cards=[_card(1)])], _BOOKS) == [
        "주제 전환 무시"
    ]


def _raw(runs: list[list[dict]]) -> dict:
    return {"meta": {}, "books": _BOOKS, "runs": [{"1": turns} for turns in runs]}


def _cases_file(case: dict) -> dict:
    return {"version": 1, "kinds": {"situation": {"about": "가짜"}}, "cases": [case]}


def test_요약은_회차별_통과_수와_카드_0장을_센다() -> None:
    raw = _raw([[_turn(cards=[_card(1)])], [_turn()], [_turn(cards=[_card(3)])]])

    summary = grade.summarize(_cases_file(_case()), raw)

    assert summary["passed_per_run"] == [1, 0, 1]
    assert (summary["passed"], summary["total"]) == (2, 3)
    assert (summary["zero_cards"], summary["zero_cards_total"]) == (1, 3)
    assert summary["zero_cards_by_case"] == {"1": 1}
    assert summary["fail_reasons"] == {"카드 0장": 1}


def test_기록만_하는_질문은_통과율에서_빼고_출처별로_따로_센다() -> None:
    bug = {**_case(), "no": 1, "source": "bug"}
    chatlog = {**_case(), "no": 2, "source": "chatlog"}
    observe = {
        **_case(),
        "no": 3,
        "source": "chatlog",
        "expect": {"action": "observe", "ungraded": ["미정"]},
    }
    cases_file = {
        "version": 3,
        "kinds": {"situation": {"about": "가짜"}},
        "sources": {"bug": {"about": "가짜"}, "chatlog": {"about": "가짜"}},
        "cases": [bug, chatlog, observe],
    }
    raw = {
        "meta": {},
        "books": _BOOKS,
        "runs": [{"1": [_turn(cards=[_card(1)])], "2": [_turn()], "3": [_turn()]}],
    }

    summary = grade.summarize(cases_file, raw)

    assert (summary["passed"], summary["total"]) == (1, 2)
    assert summary["observed"] == [3]
    assert summary["by_source"]["bug"]["rate"] == 100.0
    assert summary["by_source"]["chatlog"]["total"] == 1


def test_끊긴_회차는_빼고_채점할_수_있다() -> None:
    raw = _raw([[_turn(cards=[_card(1)])], [_turn(degraded=True)]])

    summary = grade.summarize(_cases_file(_case()), raw, frozenset({2}))

    assert summary["runs"] == 1
    assert summary["skipped_runs"] == [2]
    assert (summary["passed"], summary["total"]) == (1, 1)


def test_말하지_않은_분위기와_새어_나온_낱말을_센다() -> None:
    leaked = _turn(
        msg="소설 추천해줘",
        cards=[_card(1)],
        spec={"filters": {}, "semantic": "가벼운 소설"},
        reply="잔잔한 소설을 골라봤어요.",
    )
    said = _turn(
        msg="가벼운 소설 추천해줘",
        cards=[_card(3)],
        spec={"filters": {}, "semantic": "가벼운 소설"},
    )

    common = grade.summarize(_cases_file(_case()), _raw([[leaked], [said]]))["common"]

    assert (common["llm_turns"], common["unsaid_mood_turns"]) == (2, 1)
    assert common["leak_word_turns"] == 1


def test_LLM을_안_부른_턴은_분위기_지표의_분모에서_뺀다() -> None:
    # 규칙이 바로 되물은 턴과 LLM 이 끊긴 턴에는 지어낼 문장이 없다.
    instant = _turn(sec=0.01, followup="어떤 책을 말씀하시는 건가요?")
    down = _turn(degraded=True)

    common = grade.summarize(_cases_file(_case()), _raw([[instant], [down]]))["common"]

    assert common["llm_turns"] == 0
    assert common["degraded_turns_per_run"] == [0, 1]
