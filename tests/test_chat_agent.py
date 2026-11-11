"""③ 책 추천 에이전트(CHAT_AGENT) — 세 갈래 후보와 판단 호출. DB·LLM 은 가짜로 바꿔 끼운다.

설계(11/10): 첫 호출이 검색어·떠올린 제목·기준 책을 내고, 검색(A)·제목 검색(B)·기준 책의 함께 대출·마니아(C)를 나란히
모아 판단 호출이 카드·질문·한 번 더 찾기 중 하나를 고른다. 여기서 보는 것:
- 스위치를 끄면 1단계 프롬프트와 카드 생성이 그대로다
- 그래프에서 온 책의 이유는 관계 사실("함께 읽은 책")이고, 목록으로 알 수 없는 말("많이 읽은")은 빠진다
- 함께 읽은 책을 물으면 그 목록에서만 고른다
- 한 번 더 찾기는 턴마다 한 번이고, 질문이면 카드 없이 followup 으로 묻는다
"""

import asyncio
import json
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import lanes, picking, reading_graph, recall
from app.chat.shown import ShownBook
from app.main import app
from app.routers import chat
from app.search.schemas import SearchFilters

client = TestClient(app)

SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}
ANCHOR_ISBN = "9791111111111"


def _usage(
    isbn: str, name: str, authors: str, **lists: list[tuple[str, str, str]]
) -> str:
    response = {"book": {"isbn13": isbn, "bookname": name, "authors": authors}}
    for field, books in lists.items():
        response[field] = [
            {"book": {"isbn13": i, "bookname": n, "authors": a}} for i, n, a in books
        ]
    return json.dumps(
        {"isbn13": isbn, "resp": {"response": response}}, ensure_ascii=False
    )


HUB = ("9790000000009", "불편한 편의점", "김호연")
CHIP_WAR = ("9790000000001", "칩 워", "크리스 밀러")
AI_2041 = ("9790000000002", "AI 2041", "리카이푸")
NEXT_CHIP = ("9790000000003", "반도체 제국의 미래", "정인성")
OTHER_HESSE = ("9790000000004", "수레바퀴 아래서", "헤르만 헤세")


def _graph() -> reading_graph.ReadingGraph:
    lines = [
        _usage(
            ANCHOR_ISBN,
            "AI 지도책",
            "케이트 크로퍼드 지음",
            coLoanBooks=[CHIP_WAR, HUB],
            maniaRecBooks=[AI_2041],
        ),
        _usage(*CHIP_WAR, coLoanBooks=[NEXT_CHIP, HUB], maniaRecBooks=[OTHER_HESSE]),
        _usage("9790000000010", "아몬드", "손원평", coLoanBooks=[HUB]),
        _usage("9790000000011", "파친코", "이민진", coLoanBooks=[HUB]),
    ]
    return reading_graph.from_data(reading_graph.build(lines))


def _row(
    book_id: int, isbn: str, title: str, author: str, description: str = ""
) -> dict:
    return {
        "book_id": book_id,
        "isbn": isbn,
        "title": title,
        "author": author,
        "publisher": "출판사",
        "price": 18000,
        "in_stock": True,
        "cover_url": None,
        "category": "경제학",
        "pub_year": 2023,
        "description": description,
    }


CATALOG = {
    CHIP_WAR[0]: _row(201, *CHIP_WAR, "반도체 패권 경쟁의 역사를 다룬다."),
    AI_2041[0]: _row(202, *AI_2041, "인공지능이 바꿀 20년 뒤 열 가지 이야기."),
    NEXT_CHIP[0]: _row(203, *NEXT_CHIP, "반도체 산업의 미래."),
    HUB[0]: _row(204, *HUB, "편의점 이야기."),
    OTHER_HESSE[0]: _row(205, *OTHER_HESSE, "소년의 성장."),
}
SEARCHED = {
    "book_id": 301,
    "isbn": "9790000000301",
    "title": "반도체 입문",
    "author": "김반도",
    "publisher": "출판사",
    "price": 15000,
    "in_stock": True,
    "cover_url": None,
    "description": "반도체 산업 입문서.",
    "match_score": 70,
    "popularity": 0,
}


@pytest.fixture(autouse=True)
def agent_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHAT_AGENT", "true")
    monkeypatch.setattr(reading_graph, "HUB_TOP", 1)
    reading_graph.install(_graph())

    async def _rows(isbns) -> list[dict]:
        return [dict(CATALOG[i]) for i in sorted(set(isbns)) if i in CATALOG]

    async def _scores(
        conn, candidates, user_id, spec, query_vector=None, book_to_book=False
    ) -> None:
        for c in candidates:
            c["match_score"], c["popularity"] = 40, 0

    async def _no_vector(conn, turn):
        return None

    async def _shown(cards):
        return [
            ShownBook(rank, 101, "AI 지도책", "케이트 크로퍼드", "AI 의 숨은 비용.")
            for rank, isbn in cards
            if isbn == ANCHOR_ISBN
        ]

    class _NoPool:
        def acquire(self):
            return nullcontext()

    monkeypatch.setattr(lanes, "_rows_by_isbns", _rows)
    monkeypatch.setattr(chat, "_attach_match_scores", _scores)
    monkeypatch.setattr(chat, "_anchor_vector", _no_vector)
    monkeypatch.setattr(chat, "_shown_books", _shown)
    monkeypatch.setattr(chat.db, "get_pool", lambda: _NoPool())
    yield
    reading_graph.install(None)


def _use_search(monkeypatch: pytest.MonkeyPatch, *books: dict) -> list:
    calls = []

    async def _fake(spec, exclude_book_ids, user_id, owned_book_ids=None) -> list[dict]:
        calls.append(spec)
        return [dict(b) for b in books]

    monkeypatch.setattr(chat, "get_candidates", _fake)
    return calls


RECALL_MARK = "책을 많이 아는 서점 직원"


def _use_model(
    monkeypatch: pytest.MonkeyPatch,
    spec_patch: dict,
    *judged: dict,
    recalled: list[dict] | None = None,
) -> list[str]:
    """1단계는 spec_patch, 떠올린 제목 호출은 recalled, 판단 호출은 judged 를 차례로 준다. 판단 호출 프롬프트를 돌려준다."""
    prompts: list[str] = []
    answers = list(judged)

    def _dispatch(prompt_value) -> AIMessage:
        text = prompt_value.to_messages()[0].content
        if "조건(spec) 갱신기" in text:
            return AIMessage(content=json.dumps(spec_patch, ensure_ascii=False))
        if RECALL_MARK in text:
            return AIMessage(
                content=json.dumps({"titles": recalled or []}, ensure_ascii=False)
            )
        assert "셋 중 하나를 골라 action" in text, "판단 호출이 아닌 다른 호출"
        prompts.append(text)
        return AIMessage(content=json.dumps(answers.pop(0), ensure_ascii=False))

    monkeypatch.setattr(chat, "get_chat_model", lambda **_: RunnableLambda(_dispatch))
    return prompts


def _post(message: str, *, shown: bool = True) -> dict:
    body = {
        "user_id": 1,
        "consented": True,
        "spec": SPEC,
        "message": message,
        "recent_turns": [{"role": "user", "text": "AI 책 추천해줘"}] if shown else [],
        "exclude_isbns": [],
        "image_ref": None,
    }
    if shown:
        body["context_cards"] = [{"rank": 1, "isbn": ANCHOR_ISBN}]
    res = client.post("/recommendations/chat", json=body)
    assert res.status_code == 200, res.text
    return res.json()["data"]


def test_보여_준_책에서_이어_새_조건을_말하면_그_책의_함께_대출에서도_고른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "반도체 산업", "anchor_card": 1},
        {
            "action": "cards",
            "cards": [
                {
                    "book_id": 201,
                    "reason_short": "반도체 패권 경쟁을 다뤄요.",
                    "reason_long": "반도체 산업이 궁금하시다면 패권 경쟁의 역사부터 볼 수 있어요.",
                    "match_basis": [{"label": "주제", "detail": "반도체"}],
                }
            ],
            "reply": "반도체 산업을 넓게 보는 책이에요. 『반도체 제국의 미래』로 옆으로 넓혀 보셔도 좋아요.",
        },
    )

    data = _post("반도체 산업이 궁금해")

    listing = prompts[0].split("책 목록:\n", 1)[1]
    assert "칩 워 - 크리스 밀러 [『AI 지도책』 함께 읽은 책]" in listing
    assert "반도체 입문 - 김반도 [검색]" in listing
    # 기준 책의 마니아 목록도 같이 온다. 허브(불편한 편의점)는 후보에도 이웃 줄에도 없다
    assert "AI 2041" in listing
    assert "불편한 편의점" not in listing
    assert "이 책의 함께 읽은 책 『반도체 제국의 미래』" in listing
    [card] = data["cards"]
    assert card["title"] == "칩 워"
    assert card["reason_short"] == "반도체 패권 경쟁을 다뤄요."
    assert card["match_basis"][0]["label"] == "함께 읽은 책"
    assert "도서관 정보나루" in card["match_basis"][0]["detail"]
    # 두 갈래 제안은 답 보기로 나가고(#652), 같은 이웃 책을 짚은 모델 문장은 말풍선에서 빠진다
    assert data["reply"] == "반도체 산업을 넓게 보는 책이에요."
    assert "• 『칩 워』 함께 읽은 책 보여줘" in data["followup"]


def test_소개글_없는_그래프_책은_관계_사실을_이유로_쓰고_많이_빌렸다는_말은_뺀다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(CATALOG, CHIP_WAR[0], _row(201, *CHIP_WAR))
    _use_search(monkeypatch)
    _use_model(
        monkeypatch,
        {"semantic": "반도체 산업", "anchor_card": 1},
        {
            "action": "cards",
            "cards": [
                {
                    "book_id": 201,
                    "reason_short": "지어낸 이유",
                    "reason_long": "지어낸 긴 이유",
                }
            ],
            "reply": "『칩 워』는 많이 읽은 책이에요. 반도체를 넓게 볼 수 있어요.",
        },
    )

    data = _post("반도체 산업이 궁금해")

    [card] = data["cards"]
    assert (
        card["reason_short"]
        == "『AI 지도책』을 읽은 사람들이 함께 읽은 책이에요(도서관 정보나루)."
    )
    assert card["reason_long"] is None
    assert [b["label"] for b in card["match_basis"]] == ["함께 읽은 책"]
    # 목록으로 알 수 없는 말("많이 읽은")은 빠지고, 두 갈래 제안은 코드가 카드 아래 답 보기로 단다(#652)
    assert data["reply"] == "반도체를 넓게 볼 수 있어요."
    assert data["followup"] == (
        "• 『칩 워』 함께 읽은 책 보여줘\n• 『칩 워』와 비슷한 책 더 보여줘"
    )


def test_함께_읽은_책을_물으면_검색하지_않고_그_목록에서만_고른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "AI 지도책 함께 읽은 책", "anchor_card": 1, "relation": "co"},
        {
            "action": "cards",
            "cards": [{"book_id": 201}],
            "reply": "함께 읽은 책이에요.",
        },
    )

    data = _post("이 책 읽은 사람들은 또 뭐 읽었어?")

    assert searches == []
    listing = prompts[0].split("책 목록:\n", 1)[1]
    assert "칩 워" in listing
    assert "AI 2041" not in listing
    assert "함께 읽은 책에서 고른다" in prompts[0]
    assert [c["title"] for c in data["cards"]] == ["칩 워"]
    # 관계를 물었으니 소개글이 있어도 한 줄 이유는 관계 사실이다
    assert data["cards"][0]["reason_short"].startswith(
        "『AI 지도책』을 읽은 사람들이 함께 읽은 책"
    )


def test_모델이_기준_책을_못_짚어도_카드가_한_장이면_지시어로_채운다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "AI 지도책 함께 읽은 책"},
        {
            "action": "cards",
            "cards": [{"book_id": 201}],
            "reply": "함께 읽은 책이에요.",
        },
    )

    data = _post("이 책 읽은 사람들은 또 뭐 읽었어?")

    assert searches == []
    assert "칩 워 - 크리스 밀러 [『AI 지도책』 함께 읽은 책]" in prompts[0]
    assert [c["title"] for c in data["cards"]] == ["칩 워"]


@pytest.mark.parametrize(
    ("message", "ranks", "anchor", "relation"),
    [
        ("이런 책 더 없어?", [1], 1, "similar"),
        ("이런 책 더 없어?", [1, 2, 3], None, None),
        ("2번 빌린 사람들은 뭐 읽었어?", [1, 2, 3], 2, "co"),
        ("이 책 읽은 사람들은 또 뭐 읽었어?", [1], 1, "co"),
        ("이거 같이 읽을 만한 책은?", [1], 1, "co"),
        ("2번 본 사람들은 또 뭐 읽었어?", [1, 2, 3], 2, "co"),
        ("두 번째랑 비슷한 거", [1, 2, 3], 2, "similar"),
        ("9번 같은 책", [1, 2, 3], None, None),
        ("반도체 산업이 궁금해", [1], None, None),
    ],
)
def test_기준_책과_관계는_메시지_낱말로_채운다(
    message, ranks, anchor, relation
) -> None:
    plan = lanes.fill_plan(lanes.EMPTY_PLAN, message, ranks)
    assert (plan.anchor, plan.relation) == (anchor, relation)


def test_모델이_낸_기준_책과_관계는_메시지에_근거가_있으면_그대로_쓴다() -> None:
    plan = lanes.Plan(anchor=3, relation="co")
    assert lanes.fill_plan(plan, "3번 읽은 사람들은 뭐 읽어?", [1, 2, 3]) == plan
    # 메시지가 다른 번호를 말했으면 메시지를 따른다
    assert lanes.fill_plan(plan, "1번이랑 비슷한 거", [1, 2, 3]).anchor == 1


def test_놓친_조건이_있으면_새_검색어로_한_번만_다시_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "반도체 공급망"},
        {"action": "search_again", "missed": "공급망", "query": "반도체 공급망 위기"},
        {"action": "search_again", "query": "또 찾기"},
    )

    data = _post("반도체 공급망 책", shown=False)

    assert [s.semantic for s in searches] == ["반도체 공급망", "반도체 공급망 위기"]
    assert len(prompts) == 2
    assert "이미 한 번 다시 찾았다" in prompts[1]
    # 두 번째도 다시 찾자고 하면 받지 않는다. 고른 카드가 없으니 못 찾음 안내로 간다
    assert data["cards"] == []
    assert data["followup"]


def test_후보가_두_갈래로_갈리면_카드_없이_묻는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)
    _use_model(
        monkeypatch,
        {"semantic": "AI"},
        {
            "action": "ask",
            "reply": "AI 책이 두 쪽으로 나뉘어요.",
            "question": "기술 입문 쪽인가요, 사회 영향 쪽인가요?",
        },
    )

    data = _post("AI 책 읽고 싶은데 뭘 읽을지 모르겠어", shown=False)

    assert data["cards"] == []
    assert data["reply"] == "AI 책이 두 쪽으로 나뉘어요."
    assert data["followup"] == "기술 입문 쪽인가요, 사회 영향 쪽인가요?"


def test_두_갈래로_물을_때_답_보기를_질문_아래_줄로_단다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)
    _use_model(
        monkeypatch,
        {"semantic": "AI"},
        {
            "action": "ask",
            "reply": "AI 책이 두 쪽으로 나뉘어요.",
            "question": "기술 입문 쪽인가요, 사회 영향 쪽인가요?",
            "options": ["AI 기술 입문서 보여줘", "AI 사회 영향 책 보여줘"],
        },
    )

    data = _post("AI 책 읽고 싶은데 뭘 읽을지 모르겠어", shown=False)

    # FE 가 "• " 줄을 버튼으로 그린다(#652)
    assert data["followup"] == (
        "기술 입문 쪽인가요, 사회 영향 쪽인가요?\n\n"
        "• AI 기술 입문서 보여줘\n• AI 사회 영향 책 보여줘"
    )


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        (["입문서 보여줘", "실전서 보여줘"], ("입문서 보여줘", "실전서 보여줘")),
        # 세 개 넘게 오면 두 방향 몫만
        (["가", "나", "다"], ("가", "나")),
        # 하나뿐이거나, 길어서(잘린 말은 보낼 수 없다) 버리고 하나 남으면 보기 없이 묻는다
        (["입문서 보여줘"], ()),
        (["입문서 보여줘", "가" * 21], ()),
        (["같은 말", "같은 말"], ()),
        ("입문서", ()),
        (None, ()),
    ],
)
def test_물을_때_답_보기는_두_개만_받는다(raw, kept) -> None:
    answer = {"action": "ask", "question": "어느 쪽?", "options": raw}
    assert picking.parse(answer, may_ask=True, may_search=True).options == kept


def test_카드_턴_말풍선_끝_질문과_두_갈래_제안은_카드_아래로_간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)
    _use_model(
        monkeypatch,
        {"semantic": "반도체 산업", "anchor_card": 1},
        {
            "action": "cards",
            "cards": [{"book_id": 201, "reason_short": "반도체 패권 경쟁을 다뤄요."}],
            "reply": "반도체 산업을 넓게 보는 책이에요. 역사 쪽이 좋으세요, 기술 쪽이 좋으세요?",
        },
    )

    data = _post("반도체 산업이 궁금해")

    assert data["reply"] == "반도체 산업을 넓게 보는 책이에요."
    assert data["followup"] == (
        "역사 쪽이 좋으세요, 기술 쪽이 좋으세요?\n\n"
        "• 『칩 워』 함께 읽은 책 보여줘\n• 『칩 워』와 비슷한 책 더 보여줘"
    )


def test_같은_저자_책은_한_장까지다(monkeypatch: pytest.MonkeyPatch) -> None:
    same_author = {
        **SEARCHED,
        "book_id": 302,
        "isbn": "9790000000302",
        "title": "반도체 심화",
    }
    _use_search(monkeypatch, SEARCHED, same_author)
    _use_model(
        monkeypatch,
        {"semantic": "반도체"},
        {
            "action": "cards",
            "cards": [
                {"book_id": 301, "reason_short": "가"},
                {"book_id": 302, "reason_short": "나"},
            ],
        },
    )

    data = _post("반도체 책", shown=False)

    assert [c["title"] for c in data["cards"]] == ["반도체 입문"]


def test_떠올린_책에_소개글이_없으면_확인된_칸으로만_이유를_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch)
    titled = _row(401, "9790000000401", "클린 코드", "로버트 C. 마틴 지음")

    async def _find(title):
        return dict(titled) if title.title == "클린 코드" else None

    monkeypatch.setattr(lanes, "_find_titled", _find)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "개발자 필독서"},
        {"action": "cards", "cards": [{"book_id": 401, "reason_short": "지어낸 내용"}]},
        recalled=[{"title": "클린 코드", "author": "로버트 마틴"}],
    )

    data = _post("개발자라면 꼭 읽어야 할 책", shown=False)

    assert "클린 코드 - 로버트 C. 마틴 지음 [떠올린 제목]" in prompts[0]
    # 소개글이 없다고 적지 않고 확인된 정보를 둔다(#86)
    assert "확인된 정보: 출판사, 2023년, 경제학" in prompts[0]
    assert "소개글 없음" not in prompts[0]
    [card] = data["cards"]
    assert card["reason_short"] == "로버트 C. 마틴의 2023년 경제학 책이에요."
    assert "찾으신" not in card["reason_short"]
    assert card["reason_long"] is None


def test_판단_호출이_실패하면_검색과_떠올린_책으로만_규칙_카드를_낸다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)

    def _dispatch(prompt_value) -> AIMessage:
        text = prompt_value.to_messages()[0].content
        if "조건(spec) 갱신기" in text:
            return AIMessage(content='{"semantic": "반도체", "anchor_card": 1}')
        return AIMessage(content="JSON 아님")

    monkeypatch.setattr(chat, "get_chat_model", lambda **_: RunnableLambda(_dispatch))

    data = _post("반도체 산업이 궁금해")

    assert data["degraded"] is True
    assert [c["title"] for c in data["cards"]] == ["반도체 입문"]


def test_제목을_콕_집은_요청은_지금처럼_카드_생성으로_간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact = {**SEARCHED, "title": "토지", "author": "박경리"}
    _use_search(monkeypatch, exact)
    sent: list[str] = []

    def _dispatch(prompt_value) -> AIMessage:
        text = prompt_value.to_messages()[0].content
        sent.append(text)
        if "조건(spec) 갱신기" in text:
            return AIMessage(content='{"intent": "exact", "exact": {"title": "토지"}}')
        return AIMessage(
            content='{"cards": [{"book_id": 301, "reason_short": "이유"}], "reply": "찾았어요."}'
        )

    monkeypatch.setattr(chat, "get_chat_model", lambda **_: RunnableLambda(_dispatch))

    data = _post("토지 찾아줘", shown=False)

    assert not any("셋 중 하나를 골라 action" in text for text in sent)
    assert [c["title"] for c in data["cards"]] == ["토지"]


def test_스위치를_켜면_1단계에_에이전트_칸을_싣고_끄면_그대로다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _prompt() -> str:
        sent: list[str] = []

        async def _invoke(chain, inputs):
            sent.append(chat.SPEC_PROMPT.format_messages(**inputs)[0].content)
            return {}

        monkeypatch.setattr(chat, "invoke_chain", _invoke)
        import asyncio

        asyncio.run(chat._ask_spec_patch("책", chat.Spec.model_validate(SPEC), []))
        return sent[0]

    assert chat.AGENT_RULE in _prompt()
    monkeypatch.setenv("CHAT_AGENT", "false")
    chat.get_settings.cache_clear()
    off = _prompt()
    assert "titles" not in off
    assert (
        off
        == chat.SPEC_PROMPT.format_messages(
            recent_turns="(최근 대화 없음)",
            shown="(없음)",
            spec_json=json.dumps(
                chat.Spec.model_validate(SPEC).model_dump(), ensure_ascii=False
            ),
            message="책",
            ask_rule="",
        )[0].content
    )


# --- 작은 규칙 ---------------------------------------------------------------


def test_1단계_칸은_모양이_틀리면_버린다() -> None:
    assert (
        lanes.parse_plan({"anchor_card": True, "relation": "다른 것"})
        == lanes.EMPTY_PLAN
    )
    assert lanes.parse_plan({"anchor_card": 2}).anchor == 2
    assert lanes.parse_plan(
        {"anchor_title": {"title": "데미안", "author": "헤세"}}
    ).anchor == lanes.Title("데미안", "헤세")
    assert lanes.parse_plan({"anchor_card": 0}).anchor is None
    # 4o 는 기준 책 제목을 글자로 냈다(11/10 실험 1). 글자도 받는다
    assert lanes.parse_plan({"anchor_title": "데미안"}).anchor == lanes.Title(
        "데미안", None
    )
    # 카드 번호와 제목을 같이 내면 번호가 먼저이고, 제목은 named 에 남는다
    both = lanes.parse_plan({"anchor_card": 2, "anchor_title": "코스모스"})
    assert (both.anchor, both.named) == (2, lanes.Title("코스모스", None))


def test_떠올린_제목은_저자_있는_칸만_겹치지_않게_받는다() -> None:
    assert lanes.parse_titles(
        [
            {"title": "코스모스", "author": "칼 세이건"},
            {"title": ""},
            "문자열",
            {"title": "코스모스", "author": "칼 세이건"},
        ]
    ) == (lanes.Title("코스모스", "칼 세이건"),)
    assert recall.parse({"titles": "아님"}) == ()
    assert recall.parse("JSON 아님") == ()


def test_사용자_조건은_다른_갈래_책에도_건다() -> None:
    book = {
        "price": 18000,
        "pub_year": 2023,
        "category": "경제학",
        "publisher": "(주)부키",
    }
    assert lanes.passes(book, SearchFilters(price_max=20000), None)
    assert not lanes.passes(book, SearchFilters(price_max=15000), None)
    assert not lanes.passes(book, SearchFilters(pub_year_from=2024), None)
    assert not lanes.passes(
        {**book, "pub_year": None}, SearchFilters(pub_year_to=2024), None
    )
    assert lanes.passes(book, SearchFilters(), "부키")
    assert not lanes.passes(book, SearchFilters(), "창비")
    assert not lanes.passes(book, SearchFilters(category="한국문학"), None)


def test_같은_책이_여러_갈래에서_오면_꼬리표를_합친다() -> None:
    a = {"book_id": 1, "title": "칩 워", "author": "크리스 밀러", "isbn": "1"}
    b = {
        "book_id": 2,
        "title": "칩 워 (리커버)",
        "author": "크리스 밀러 지음",
        "isbn": "2",
        "sources": [("co", "AI 지도책")],
    }
    c = {
        "book_id": 3,
        "title": "다른 책",
        "author": "누군가",
        "isbn": "3",
        "sources": [("title", None)],
    }

    merged = lanes.merge([a], [b, c], graph=None)

    assert [m["book_id"] for m in merged] == [1, 3]
    assert merged[0]["sources"] == [("search", None), ("co", "AI 지도책")]


@pytest.mark.parametrize(
    ("kind", "anchor", "reason"),
    [
        (
            "co",
            "AI 지도책",
            "『AI 지도책』을 읽은 사람들이 함께 읽은 책이에요(도서관 정보나루).",
        ),
        (
            "co",
            "데미안",
            "『데미안』을 읽은 사람들이 함께 읽은 책이에요(도서관 정보나루).",
        ),
        (
            "co",
            "파이썬",
            "『파이썬』을 읽은 사람들이 함께 읽은 책이에요(도서관 정보나루).",
        ),
        (
            "co",
            "코스모스",
            "『코스모스』를 읽은 사람들이 함께 읽은 책이에요(도서관 정보나루).",
        ),
        # 숫자로 끝나면 읽는 소리로: 2041(이천사십일) → 을, 1984(천구백팔십사) → 를
        (
            "co",
            "AI 2041",
            "『AI 2041』을 읽은 사람들이 함께 읽은 책이에요(도서관 정보나루).",
        ),
        (
            "co",
            "1984",
            "『1984』를 읽은 사람들이 함께 읽은 책이에요(도서관 정보나루).",
        ),
        ("mania", "데미안", "『데미안』의 정보나루 마니아 추천 목록에 있는 책이에요."),
    ],
)
def test_관계_이유는_받침에_맞는_조사를_쓴다(
    kind: str, anchor: str, reason: str
) -> None:
    assert picking.relation_reason(kind, anchor) == reason


def test_관계_이유는_기준_책_제목이_길어도_80자_안이다() -> None:
    for kind in ("co", "mania"):
        assert len(picking.relation_reason(kind, "아주 " * 60 + "긴 제목")) <= 80


def test_판단_답은_쓸_수_없는_갈래면_카드로_본다() -> None:
    again = {"action": "search_again", "query": "새 검색어"}
    assert picking.parse(again, may_ask=True, may_search=True).action == "search_again"
    assert picking.parse(again, may_ask=True, may_search=False).action == "cards"
    assert (
        picking.parse({"action": "search_again"}, may_ask=True, may_search=True).action
        == "cards"
    )
    ask = {"action": "ask", "question": "어느 쪽?"}
    assert picking.parse(ask, may_ask=True, may_search=True).action == "ask"
    assert picking.parse(ask, may_ask=False, may_search=True).action == "cards"
    assert (
        picking.parse({"action": "ask"}, may_ask=True, may_search=True).action
        == "cards"
    )


def test_말풍선은_action_을_보고_나서_보낸다() -> None:
    assert picking.streams_reply('{"act') is None
    assert picking.streams_reply('{"action": "cards", "re') is True
    assert picking.streams_reply('{"action": "search_again"') is False


def test_함께_대출_책을_비슷한_책이라_한_문장은_뺀다() -> None:
    reply = "좋은 선택이에요. 『칩 워』와 비슷한 책도 있어요. 『AI 2041』도 비슷한 결이에요."
    assert (
        picking.without_overclaims(reply, {"칩 워"})
        == "좋은 선택이에요. 『AI 2041』도 비슷한 결이에요."
    )
    assert picking.without_overclaims("다음에 읽을 책이에요.", set()) is None


@pytest.mark.parametrize(
    ("text", "over"),
    [
        ("많은 이들에게 깊은 울림을 준답니다.", True),
        ("많은 이가 찾는 책이에요.", True),
        ("많은 사람이 읽었어요.", True),
        ("많은 이야기를 담고 있어요.", False),
        ("짧고 깊은 울림이 있어요.", False),
    ],
)
def test_목록으로_알_수_없는_말을_가린다(text: str, over: bool) -> None:
    assert picking.overclaims(text) is over


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("co", "3권을 채워라"),
        ("similar", "3권을 채워라"),
        (None, ""),
    ],
)
def test_관계를_물은_턴은_목록_책으로_카드를_채우라고_한다(
    relation: str | None, expected: str
) -> None:
    rule = picking.prompt_inputs(
        recent_turns="",
        message="",
        intro="",
        listing="",
        limit=3,
        may_ask=True,
        may_search=True,
        relation=relation,
    )["relation_rule"]
    assert expected in rule
    assert "{" not in rule


def test_제목_안의_문장부호에서는_문장을_자르지_않는다() -> None:
    reply = (
        "파이썬 입문서예요. 다음엔 『파이썬 배워! 크롤러 DIY하다!』를 보세요. 좋아요!"
    )
    assert picking.sentences(reply) == [
        "파이썬 입문서예요.",
        "다음엔 『파이썬 배워! 크롤러 DIY하다!』를 보세요.",
        "좋아요!",
    ]


def test_카드에_없는_책을_제안한_문장은_통째로_빠진다() -> None:
    reply = "파이썬 입문서예요. 다음엔 『파이썬 배워! 크롤러 DIY하다!』를 보세요."
    assert (
        chat._without_unshown_titles(reply, ["파이썬 입문"], picking.sentences)
        == "파이썬 입문서예요."
    )


def test_형식_예시를_베낀_근거_칸은_뺀다() -> None:
    basis = [
        {"label": "근거 이름", "detail": "반도체"},
        {"label": "주제", "detail": "그 근거의 구체적 내용"},
        {"label": "주제", "detail": "반도체 산업"},
    ]
    assert picking.without_placeholder_basis(basis) == [
        {"label": "주제", "detail": "반도체 산업"}
    ]


def test_비슷한_책을_더_원하면_기준_책_목록을_검색보다_앞에_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "AI 교양서"},
        {
            "action": "cards",
            "cards": [{"book_id": 202}],
            "reply": "같은 분야 책이에요.",
        },
    )

    data = _post("이런 책 더 없어?")

    listing = prompts[0].split("책 목록:\n", 1)[1]
    assert listing.index("AI 2041") < listing.index("반도체 입문")
    # 비슷한 책은 마니아 목록에서만 데려온다
    assert "칩 워" not in listing
    assert (
        data["cards"][0]["reason_short"]
        == "『AI 지도책』의 정보나루 마니아 추천 목록에 있는 책이에요."
    )


# --- 11/10 실험 1(gpt-4o)에서 드러난 것 ------------------------------------------


def test_제목으로_짚은_기준_책을_글자로_내도_그_책의_함께_읽은_책을_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)
    named: list[lanes.Title] = []

    async def _find(title, graph):
        named.append(title)
        return _row(
            101, ANCHOR_ISBN, "AI 지도책", "케이트 크로퍼드 지음", "AI 의 숨은 비용."
        )

    monkeypatch.setattr(lanes, "_find_named", _find)
    _use_model(
        monkeypatch,
        # 4o 가 실제로 낸 모양: 제목을 글자로, 저자 없이
        {"anchor_title": "AI 지도책", "relation": "co"},
        {
            "action": "cards",
            "cards": [{"book_id": 201}],
            "reply": "함께 읽은 책이에요.",
        },
    )

    data = _post("AI 지도책 읽은 사람들은 또 뭐 읽어?", shown=False)

    assert named == [lanes.Title("AI 지도책", None)]
    assert searches == []
    assert [c["title"] for c in data["cards"]] == ["칩 워"]


class _Conn:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def fetch(self, sql, ids):
        return [r for r in self.rows if r["book_id"] in ids]


class _Pool:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def acquire(self):
        return _Conn(self.rows)


def test_제목이_같은_책이_여럿이면_함께_읽은_목록이_있는_작품을_고른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.photo import match_cover

    other = _row(501, "9790000000501", "AI 지도책", "다른 사람")
    listed = _row(502, ANCHOR_ISBN, "AI 지도책", "케이트 크로퍼드 지음")

    async def _match(conn, text):
        scored = [
            match_cover.Scored(
                SimpleNamespace(book_id=b["book_id"]), True, False, False
            )
            for b in (other, listed)
        ]
        return match_cover.Match("candidates", scored, "text")

    monkeypatch.setattr(match_cover, "match", _match)
    monkeypatch.setattr(lanes.db, "get_pool", lambda: _Pool([other, listed]))

    found = asyncio.run(lanes._find_named(lanes.Title("AI 지도책", None), _graph()))

    assert found["book_id"] == 502


def test_기준_책을_찾다_실패하면_기준_책_없이_간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(title, graph):
        raise TimeoutError

    monkeypatch.setattr(lanes, "_find_named", _fail)
    plan = lanes.Plan(anchor=lanes.Title("데미안", None))
    assert asyncio.run(lanes.resolve_anchor(plan, [], [], _graph())) is None


def test_카드가_여럿인데_어느_책인지_말하지_않고_함께_읽은_책을_물으면_번호를_묻는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)

    async def _two(cards):
        return [
            ShownBook(1, 101, "AI 지도책", "케이트 크로퍼드", "AI 의 숨은 비용."),
            ShownBook(2, 102, "아몬드", "손원평", "감정을 모르는 소년."),
        ]

    monkeypatch.setattr(chat, "_shown_books", _two)
    # 4o 는 실행마다 다른 카드를 찍었다(2번, 1번). 판단 호출은 부르지 않아야 한다(judged 없음)
    _use_model(monkeypatch, {"anchor_card": 1, "relation": "co"})
    body = {
        "user_id": 1,
        "consented": True,
        "spec": SPEC,
        "message": "이거 읽은 사람들은 또 뭐 봐?",
        "recent_turns": [{"role": "user", "text": "책 추천해줘"}],
        "exclude_isbns": [],
        "image_ref": None,
        "context_cards": [
            {"rank": 1, "isbn": ANCHOR_ISBN},
            {"rank": 2, "isbn": "9790000000010"},
        ],
    }

    res = client.post("/recommendations/chat", json=body)

    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert searches == []
    assert data["cards"] == []
    assert data["reply"] == chat._WHICH_CARD_REPLY
    # 보여 준 책마다 답 보기를 단다. 누르면 제목과 "함께 읽"으로 그 카드의 함께 읽은 책 갈래로 간다(#652)
    assert data["followup"] == (
        f"{chat._WHICH_CARD_QUESTION}\n\n"
        "• 『AI 지도책』 함께 읽은 책 보여줘\n• 『아몬드』 함께 읽은 책 보여줘"
    )


def test_이_작가_말고면_기준_책_저자의_책은_빼고_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same_author = {
        **SEARCHED,
        "book_id": 302,
        "isbn": "9790000000302",
        "title": "AI 의 숨은 얼굴",
        "author": "케이트 크로퍼드 지음",
    }
    _use_search(monkeypatch, same_author, SEARCHED)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "AI 교양서", "relation": "similar"},
        {
            "action": "cards",
            "cards": [{"book_id": 202}],
            "reply": "다른 작가 책이에요.",
        },
    )

    data = _post("이 작가 말고 다른 작가 책으로 비슷한 거")

    listing = prompts[0].split("책 목록:\n", 1)[1]
    assert "AI 의 숨은 얼굴" not in listing
    assert "반도체 입문" in listing
    assert [c["title"] for c in data["cards"]] == ["AI 2041"]


@pytest.mark.parametrize(
    ("message", "plan", "ranks", "titles", "anchor", "relation", "ask"),
    [
        # 카드 3장 앞의 "다른 거"는 다른 책을 원하는 말이다(4o 는 1번과 비슷한 책으로 읽었다)
        (
            "다른 거 보여줘",
            lanes.Plan(anchor=1, relation="similar"),
            [1, 2, 3],
            {},
            None,
            None,
            False,
        ),
        # 어느 카드인지 말하지 않았으면 모델이 찍은 카드를 버리고 번호를 묻는다
        (
            "이거 읽은 사람들은 또 뭐 봐?",
            lanes.Plan(anchor=2, relation="co"),
            [1, 2, 3],
            {},
            None,
            None,
            True,
        ),
        ("읽은 사람들은 또 뭐 읽어?", lanes.EMPTY_PLAN, [1, 2], {}, None, None, True),
        # 번호를 말했으면 "다른 책"이어도 그 카드에서 출발하는 말이다(고친 뒤 실험 1에서 이 문장이 깨졌다)
        (
            "1번 독자들이 좋아한 다른 책",
            lanes.Plan(anchor=1, relation="co"),
            [1, 2, 3],
            {},
            1,
            "co",
            False,
        ),
        # 카드가 한 장이면 그 책에서 이어 묻는 말도 받는다
        ("반도체 산업이 궁금해", lanes.Plan(anchor=1), [1], {}, 1, None, False),
        (
            "반도체 산업이 궁금해",
            lanes.Plan(anchor=2),
            [1, 2, 3],
            {},
            None,
            None,
            False,
        ),
        # 카드 제목을 말했으면 그 카드다
        (
            "아몬드랑 비슷한 거 더",
            lanes.EMPTY_PLAN,
            [1, 2, 3],
            {2: "아몬드"},
            2,
            "similar",
            False,
        ),
        # 모델이 근거 없는 카드와 함께 제목도 냈으면 제목을 쓴다
        (
            "코스모스 읽은 사람들은 뭘 같이 읽었어?",
            lanes.Plan(anchor=2, relation="co", named=lanes.Title("코스모스", None)),
            [1, 2, 3],
            {2: "아몬드"},
            lanes.Title("코스모스", None),
            "co",
            False,
        ),
        # 모델이 제목으로 짚은 책이 보여 준 카드면 카드 번호로 바꾼다(ISBN 으로 그래프를 찾는다)
        (
            "데미안 같은 책 더",
            lanes.Plan(anchor=lanes.Title("데미안", None)),
            [1, 2],
            {1: "데미안"},
            1,
            "similar",
            False,
        ),
    ],
)
def test_모델이_짚은_카드는_메시지에_근거가_있을_때만_쓴다(
    message, plan, ranks, titles, anchor, relation, ask
) -> None:
    filled = lanes.fill_plan(plan, message, ranks, titles)
    assert (filled.anchor, filled.relation, filled.ask_card) == (anchor, relation, ask)


def test_이_작가는_지시어이고_말고가_붙으면_저자를_뺀다() -> None:
    filled = lanes.fill_plan(
        lanes.EMPTY_PLAN, "이 작가 말고 다른 작가 책으로 비슷한 거", [1]
    )
    assert (filled.anchor, filled.relation, filled.other_author) == (1, "similar", True)
    assert not lanes.fill_plan(lanes.EMPTY_PLAN, "이 책 같은 거 더", [1]).other_author


def test_떠올린_제목_하나가_실패해도_나머지로_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _find(title):
        if title.title == "느린 책":
            raise TimeoutError
        return _row(401, "9790000000401", title.title, title.author)

    monkeypatch.setattr(lanes, "_find_titled", _find)

    books = asyncio.run(
        lanes.titled_books(
            [
                lanes.Title("느린 책", "누군가"),
                lanes.Title("클린 코드", "로버트 C. 마틴"),
            ]
        )
    )

    assert [b["title"] for b in books] == ["클린 코드"]


def test_떠올린_제목_호출이_늦으면_버린다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "RECALL_GRACE_SECONDS", 0.01)

    async def _slow():
        await asyncio.sleep(1)
        return (lanes.Title("코스모스", "칼 세이건"),)

    async def _run():
        return await chat._recalled_within_grace(asyncio.create_task(_slow()))

    assert asyncio.run(_run()) == ()


@pytest.mark.parametrize(
    ("message", "points"),
    [
        ("2번 읽은 사람들은 또 뭐 읽었어?", True),
        ("두 번째 거랑 같이 읽을 만한 책", True),
        ("이거 좋았는데 반도체 쪽으로", True),
        ("데미안 읽은 사람들은 또 뭐 읽어?", True),
        ("우주에 대해 처음 읽기 좋은 교양서", False),
        ("반도체 산업 이해하고 싶어", False),
    ],
)
def test_책을_짚는_말이면_떠올린_제목을_부르지_않는다(message, points) -> None:
    assert lanes.points_at_book(message) is points


@pytest.mark.parametrize(
    ("message", "recall"),
    [
        ("책 추천해줘", False),
        ("아무거나 추천해 줄래?", False),
        ("2만원 이하 책 추천해줘", False),
        ("2번 읽은 사람들은 또 뭐 읽었어?", False),
        ("요즘 읽을 만한 소설 추천해줘", True),
        ("머리 식힐 책 하나 추천해줘", True),
        ("반도체 산업 이해하고 싶어", True),
    ],
)
def test_주제_말이_없으면_떠올린_제목을_부르지_않는다(
    message: str, recall: bool
) -> None:
    assert chat._may_recall(message) is recall


def _picked(title: str, co: list[str], mania: list[str]) -> dict:
    return {"title": title, "neighbors": {"co": co, "mania": mania}}


CO = "함께 읽은 책 보여줘"


@pytest.mark.parametrize(
    ("picked", "proposed"),
    [
        (
            [_picked("칩 워", ["반도체 제국의 미래"], ["수레바퀴 아래서"])],
            [f"『칩 워』 {CO}", "『칩 워』와 비슷한 책 더 보여줘"],
        ),
        ([_picked("데미안", ["싯다르타"], [])], [f"『데미안』 {CO}"]),
        # 받침이 있으면 "과"
        ([_picked("데미안", [], ["인간 실격"])], ["『데미안』과 비슷한 책 더 보여줘"]),
        ([_picked("데미안", [], [])], []),
        # 이웃이 모두 카드에 나온 책이면 다음 카드를 기준 책으로 짚는다
        (
            [
                _picked("데미안", ["싯다르타"], []),
                _picked("싯다르타", ["유리알 유희"], []),
            ],
            [f"『싯다르타』 {CO}"],
        ),
        # 숫자는 읽는 소리로 받침을 본다(2041 → 이천사십일)
        (
            [_picked("AI 2041", ["더 커밍 웨이브"], ["더 커밍 웨이브", "넥서스"])],
            [f"『AI 2041』 {CO}", "『AI 2041』과 비슷한 책 더 보여줘"],
        ),
        # 마니아 목록이 함께 읽은 책뿐이면 같은 책을 낼 보기를 두 번 달지 않는다(11/10 실험 2·3)
        (
            [_picked("AI 2041", ["더 커밍 웨이브"], ["더 커밍 웨이브"])],
            [f"『AI 2041』 {CO}"],
        ),
        # 카드 제목에 부제가 붙어도 이웃 줄(부제를 뗀 제목)과 같은 책이면 카드에 나온 책으로 본다. 보기는 부제를 뗀 제목이다
        (
            [
                _picked("데미안", ["싯다르타"], []),
                _picked("싯다르타 : 인도의 시", ["유리알 유희"], []),
            ],
            [f"『싯다르타』 {CO}"],
        ),
    ],
)
def test_두_갈래_제안은_고른_책의_이웃_줄로_답_보기를_만든다(picked, proposed) -> None:
    assert picking.proposal_options(picked) == proposed


@pytest.mark.parametrize(
    ("message", "relation"),
    [
        (f"『데미안』 {CO}", "co"),
        ("『데미안』과 비슷한 책 더 보여줘", "similar"),
    ],
)
def test_제안_보기를_누르면_그_카드의_그_갈래로_간다(
    message: str, relation: str
) -> None:
    plan = lanes.fill_plan(lanes.EMPTY_PLAN, message, [1, 2], {1: "칩 워", 2: "데미안"})
    assert plan.anchor == 2
    assert plan.relation == relation


@pytest.mark.parametrize(
    ("similarity", "part"),
    [
        (0.87, 0.625),
        (0.90, 1.0),
        (0.95, 1.0),
        # 목록에 든 책은 이웃 책 가운데 값(0.854 → 0.425)보다 낮게 매기지 않는다. 벡터가 없어도(None) 같다
        (0.854, 0.425),
        (0.814, 0.425),
        (0.70, 0.425),
        (None, 0.425),
    ],
)
def test_관계_턴의_유사도는_책끼리_구간으로_펴고_목록_책의_바닥을_둔다(
    similarity, part
) -> None:
    assert chat._book_similarity_part(similarity) == pytest.approx(part)


def test_짚은_기준_책의_목록이_없고_검색어도_없으면_사용자_말로_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)

    async def _none(title, graph):
        return None

    monkeypatch.setattr(lanes, "_find_named", _none)
    _use_model(
        monkeypatch,
        {"anchor_title": "어디에도 없는 책", "relation": "co"},
        {
            "action": "cards",
            "cards": [{"book_id": 301, "reason_short": "반도체 산업 입문서예요."}],
            "reply": "찾아봤어요.",
        },
    )

    data = _post("어디에도 없는 책 읽은 사람들은 또 뭐 읽어?", shown=False)

    assert [s.semantic for s in searches] == [
        "어디에도 없는 책 읽은 사람들은 또 뭐 읽어?"
    ]
    assert [c["title"] for c in data["cards"]] == ["반도체 입문"]


def test_이웃_책_DB_조회가_실패하면_그_갈래만_빼고_검색_책으로_고른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)

    async def _down(isbns) -> list[dict]:
        raise TimeoutError

    # 기준 책 이웃(neighbor_books)과 이웃 줄(attach_neighbor_lines) 둘 다 이 조회를 쓴다
    monkeypatch.setattr(lanes, "_rows_by_isbns", _down)
    prompts = _use_model(
        monkeypatch,
        {"semantic": "AI 교양서"},
        {
            "action": "cards",
            "cards": [{"book_id": 301, "reason_short": "반도체 산업 입문서예요."}],
            "reply": "입문서예요.",
        },
    )

    data = _post("이런 책 더 없어?")

    listing = prompts[0].split("책 목록:\n", 1)[1]
    assert "AI 2041" not in listing
    assert [c["title"] for c in data["cards"]] == ["반도체 입문"]


def test_설정이_없으면_에이전트가_켜진다(monkeypatch: pytest.MonkeyPatch) -> None:
    # tests/conftest.py 는 예전 경로를 보려고 CHAT_AGENT=false 를 넣는다. 운영처럼 값이 없을 때를 본다
    monkeypatch.delenv("CHAT_AGENT", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    assert get_settings().chat_agent is True


def test_SSE면_판단_호출의_말풍선을_먼저_보내고_done_에_카드를_싣는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_search(monkeypatch, SEARCHED)
    _use_model(
        monkeypatch,
        {"semantic": "반도체 산업"},
        {
            "action": "cards",
            "cards": [{"book_id": 301, "reason_short": "반도체 산업 입문서예요."}],
            "reply": "산업 흐름부터 잡기 좋은 책이에요.",
        },
    )
    body = {
        "user_id": 1,
        "consented": True,
        "spec": SPEC,
        "message": "반도체 산업 책 추천해줘",
        "recent_turns": [],
        "exclude_isbns": [],
        "image_ref": None,
    }

    res = client.post(
        "/recommendations/chat", json=body, headers={"Accept": "text/event-stream"}
    )

    assert res.status_code == 200
    events = []
    for block in res.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    assert [name for name, _ in events] == ["delta", "done"]
    done = events[1][1]["data"]
    assert events[0][1]["text"] == done["reply"]
    assert [c["title"] for c in done["cards"]] == ["반도체 입문"]


# 마니아 목록 없는 기준 책의 "비슷한 책 더"(#654). 그래프의 아몬드는 함께 대출 줄만 있고 마니아 줄이 없다
ALMOND_ISBN = "9790000000010"
NEAR = _row(
    401,
    "9790000000401",
    "말 없는 소년",
    "정소년",
    "감정을 잘 느끼지 못하는 소년의 성장.",
)


def _post_almond(monkeypatch: pytest.MonkeyPatch, message: str) -> dict:
    """보여 준 카드 1장이 『아몬드』인 대화."""

    async def _shown(cards):
        return [
            ShownBook(rank, 102, "아몬드", "손원평", "감정을 못 느끼는 소년.")
            for rank, isbn in cards
            if isbn == ALMOND_ISBN
        ]

    monkeypatch.setattr(chat, "_shown_books", _shown)
    body = {
        "user_id": 1,
        "consented": True,
        "spec": SPEC,
        "message": message,
        "recent_turns": [{"role": "user", "text": "내 취향에 맞는 책 추천해줘"}],
        "exclude_isbns": [],
        "image_ref": None,
        "context_cards": [{"rank": 1, "isbn": ALMOND_ISBN}],
    }
    res = client.post("/recommendations/chat", json=body)
    assert res.status_code == 200, res.text
    return res.json()["data"]


def _use_near(monkeypatch: pytest.MonkeyPatch, books: list[dict] | None) -> list:
    calls = []

    async def _fake(anchor):
        calls.append(anchor)
        return (
            None
            if books is None
            else [{**b, "sources": [("search", None)]} for b in books]
        )

    monkeypatch.setattr(lanes, "near_books", _fake)
    return calls


# 1단계가 앞 턴 주제("내 취향")를 검색어로 남긴 채 기준 책을 짚는다 — dev 에서 다른 주제가 나온 턴(11/11)
OLD_TOPIC = {"semantic": "편안한 위로 에세이", "anchor_card": 1, "relation": "similar"}


def test_마니아_목록_없는_책의_비슷한_책은_앞_턴_주제로_찾지_않고_그_책과_가까운_책에서_고른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)
    near_calls = _use_near(monkeypatch, [NEAR])
    prompts = _use_model(
        monkeypatch,
        OLD_TOPIC,
        {
            "action": "cards",
            "cards": [
                {
                    "book_id": 401,
                    "reason_short": "감정을 잘 못 느끼는 소년의 성장 이야기예요.",
                    "reason_long": "감정을 잘 느끼지 못하는 소년이 자라는 이야기예요.",
                }
            ],
            "reply": "감정을 다룬 성장소설이에요.",
        },
    )

    data = _post_almond(monkeypatch, "『아몬드』와 비슷한 책 더 보여줘")

    assert searches == []
    assert [a.title for a in near_calls] == ["아몬드"]
    listing = prompts[0].split("책 목록:\n", 1)[1]
    assert "말 없는 소년" in listing
    assert "반도체 입문" not in listing
    assert [c["title"] for c in data["cards"]] == ["말 없는 소년"]


def test_기준_책_벡터가_없으면_앞_턴_주제_대신_기준_책_제목으로_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)
    _use_near(monkeypatch, None)
    _use_model(
        monkeypatch,
        OLD_TOPIC,
        {"action": "cards", "cards": [{"book_id": 301}], "reply": "찾은 책이에요."},
    )

    _post_almond(monkeypatch, "『아몬드』와 비슷한 책 더 보여줘")

    assert [(s.intent, s.semantic, s.exact.title) for s in searches] == [
        ("semantic", "아몬드", None)
    ]


def test_마니아_목록이_있는_책의_비슷한_책은_지금처럼_마니아_목록과_검색에서_고른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searches = _use_search(monkeypatch, SEARCHED)
    near_calls = _use_near(monkeypatch, [NEAR])
    prompts = _use_model(
        monkeypatch,
        {"semantic": "AI 교양서", "anchor_card": 1, "relation": "similar"},
        {"action": "cards", "cards": [{"book_id": 202}], "reply": "AI 이야기예요."},
    )

    _post("AI 지도책이랑 비슷한 책 더 보여줘")

    assert near_calls == []
    assert [s.semantic for s in searches] == ["AI 교양서"]
    listing = prompts[0].split("책 목록:\n", 1)[1]
    assert listing.index("AI 2041") < listing.index("반도체 입문")


def test_가까운_책은_기준_책과_그_판본을_빼고_검색_꼬리표를_단다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        102: _row(102, ALMOND_ISBN, "아몬드", "손원평"),
        103: _row(103, "9790000000103", "아몬드 (양장)", "손원평"),
        401: NEAR,
    }

    class _Conn:
        async def fetchval(self, sql, book_id):
            assert book_id == 102
            return "[0.1, 0.2]"

        async def fetch(self, sql, ids):
            return [rows[i] for i in ids if i in rows]

    class _Pool:
        def acquire(self):
            return nullcontext(_Conn())

    async def _nearest(conn, query_vector, filters, limit):
        assert query_vector == [0.1, 0.2]
        return [102, 103, 401]

    monkeypatch.setattr(lanes.db, "get_pool", lambda: _Pool())
    monkeypatch.setattr(lanes.vector, "search_ids", _nearest)
    anchor = lanes.Anchor(book_id=102, title="아몬드", author="손원평", work=None)

    books = asyncio.run(lanes.near_books(anchor))

    assert [b["book_id"] for b in books] == [401]
    assert books[0]["sources"] == [("search", None)]


def test_가까운_책은_기준_책_벡터가_없으면_None(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def fetchval(self, sql, book_id):
            return None

    class _Pool:
        def acquire(self):
            return nullcontext(_Conn())

    monkeypatch.setattr(lanes.db, "get_pool", lambda: _Pool())
    anchor = lanes.Anchor(book_id=102, title="아몬드", author="손원평", work=None)

    assert asyncio.run(lanes.near_books(anchor)) is None
