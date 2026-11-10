"""답 보기 줄(#652) — followup 의 "• " 줄을 FE 가 버튼으로 그린다."""

from app.chat import options


def test_질문_아래_빈_줄을_두고_보기를_한_줄에_하나씩_단다() -> None:
    assert options.with_options("어느 쪽이 좋으세요?", ["입문", "실전"], 190) == (
        "어느 쪽이 좋으세요?\n\n• 입문\n• 실전"
    )


def test_질문이_없으면_보기_줄만() -> None:
    assert options.with_options(None, ["입문", "실전"], 190) == "• 입문\n• 실전"
    assert options.with_options("  ", ["입문"], 190) == "• 입문"


def test_길면_뒤_보기부터_빼고_보기_글은_자르지_않는다() -> None:
    text = options.with_options("질문?", ["가" * 10, "나" * 10, "다" * 10], 30)
    assert text == "질문?\n\n• " + "가" * 10 + "\n• " + "나" * 10
    # 보기가 하나도 안 들어가면 질문만
    assert options.with_options("질문?", ["가" * 40], 30) == "질문?"


def test_빈_보기와_겹친_보기는_버린다() -> None:
    assert options.with_options("질문?", ["입문", " ", "입문", ""], 190) == (
        "질문?\n\n• 입문"
    )
    assert options.with_options(None, [], 190) is None


def test_받침은_한글_마지막_글자와_숫자_읽는_소리로_본다() -> None:
    assert options.has_object_final("데미안")
    assert not options.has_object_final("칩 워")
    assert options.has_object_final("AI 2041")
    assert not options.has_object_final("1984 2")
    assert not options.has_object_final("Python")
