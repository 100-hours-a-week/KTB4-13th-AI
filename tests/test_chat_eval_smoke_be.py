"""③ BE 경유 점검(evals/chat/smoke_be.py, #504)의 요청 모양과 결과 읽기. 네트워크 없이 돈다.

요청 모양이 BE 의 ChatRecommendationRequest 와 어긋나면 점검이 400 으로 끝나 500 을 못 본다.
"""

from evals.chat import smoke_be

# BE ChatRecommendationRequest 의 칸(camelCase)과 spec 에 꼭 있어야 하는 키(isSpecValid)
BE_REQUEST_KEYS = {"consented", "spec", "message", "recentTurns", "excludeBookIds"}
BE_REQUIRED_SPEC_KEYS = {
    "intent",
    "exact",
    "filters",
    "semantic",
    "anchor_book",
    "exclude",
}


def test_요청은_BE가_받는_칸과_spec_키를_다_갖춘다() -> None:
    body = smoke_be.build_body("토지 찾아줘")

    assert set(body) == BE_REQUEST_KEYS
    assert BE_REQUIRED_SPEC_KEYS <= set(body["spec"])
    assert body["consented"] is False
    assert body["recentTurns"] == [] and body["excludeBookIds"] == []


def test_보내는_문장은_BE_한도_안이다() -> None:
    assert len(smoke_be.MESSAGES) == 5
    for message in smoke_be.MESSAGES:
        assert message.strip() and len(message) <= 200


def test_200_응답은_카드와_긴_이유가_빈_카드를_센다() -> None:
    payload = {
        "data": {
            "cards": [
                {"title": "토지 1", "reasonLong": None},
                {"title": "토지 2", "reasonLong": "긴 이유"},
            ]
        }
    }

    result = smoke_be.read_result("토지 찾아줘", 200, payload, 3.21)

    assert result["cards"] == 2
    assert result["null_reason_long"] == 1
    assert result["titles"] == ["토지 1", "토지 2"]
    assert result["error"] is None


def test_실패_응답은_본문을_남기고_요약은_5xx를_센다() -> None:
    failed = smoke_be.read_result(
        "클린 코드 있어?", 500, {"result": "ERROR", "data": None}, 1.0
    )
    ok = smoke_be.read_result("데미안", 200, {"data": {"cards": []}}, 1.0)
    down = smoke_be.read_result("데미안", 0, {"raw": "URLError"}, 60.0)

    assert failed["cards"] == 0
    assert failed["error"] == {"result": "ERROR", "data": None}
    assert smoke_be.summarize([failed, ok, down]) == {
        "sent": 3,
        "ok": 1,
        "server_errors": 1,
        "unauthorized": 0,
        "unreachable": 1,
    }
