"""③ 대화로 좁혀 가기 평가(evals/chat_dialog, #530)의 시나리오 모양과 채점 함수. LLM·DB 는 부르지 않는다."""

from evals.chat_dialog import grade
from evals.chat_dialog.run import _body, load_scenarios


def test_시나리오는_번호가_겹치지_않고_첫마디와_원하는_것과_조건이_있다() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) == 10
    assert len({s["no"] for s in scenarios}) == 10
    for s in scenarios:
        assert s["opening"] and s["want"] and s["must"]
        assert len(s["opening"]) <= 200


def test_조건을_모두_맞아야_성공이다() -> None:
    picked = {"done": True, "judge": {"met": [True, True]}}
    assert grade.succeeded(picked, 2) is True
    assert grade.succeeded({**picked, "judge": {"met": [True, False]}}, 2) is False
    assert grade.succeeded({**picked, "judge": {"met": [True]}}, 2) is False
    assert grade.succeeded({**picked, "done": False}, 2) is False
    assert grade.succeeded({**picked, "judge": {"error": "x"}}, 2) is False


def test_말풍선이_카드_제목을_늘어놓는지_본다() -> None:
    turn = {
        "reply": "『비전공자가 배우는 파이썬』을 추천합니다.",
        "cards": [{"title": "(쉽게 배우는) 비전공자가 배우는 파이썬"}],
    }
    assert grade.lists_titles(turn) is True
    assert grade.ends_with_recommend(turn) is True
    quiet = {
        "reply": "처음 배우는 분께 맞는 결로 골랐어요.",
        "cards": [{"title": "비전공자가 배우는 파이썬"}],
    }
    assert grade.lists_titles(quiet) is False
    assert grade.ends_with_recommend(quiet) is False


def test_물은_문장을_뽑아_같은_질문_반복을_센다() -> None:
    turn = {
        "reply": "세 권이에요.\n\n1번처럼 시, 2번처럼 에세이 중 어느 쪽이 끌리세요?",
        "followup": None,
    }
    assert grade.asks(turn) is True
    assert (
        grade.question_of(turn) == "1번처럼 시, 2번처럼 에세이 중 어느 쪽이 끌리세요?"
    )
    assert grade.question_of({"reply": "골랐어요.", "followup": None}) is None


def test_요청은_BE_대화방_경로처럼_쇼핑_칸을_싣는다() -> None:
    visible = [
        {
            "book_id": 1,
            "isbn": "9780000000001",
            "title": "책",
            "price": 15000,
            "match_score": 30,
        }
    ]
    body = _body("2번", {"intent": "semantic"}, [], [1], visible, "eval-1")
    assert body["context_cards"] == [
        {"isbn": "9780000000001", "rank": 1, "match_score": 30}
    ]
    assert body["offers"] == [
        {"isbn": "9780000000001", "title": "책", "price": 15000, "in_stock": True}
    ]
    assert body["idempotency_key"]
    assert body["user_context"] == {"logged_in": True}
