"""③ 카드가 없을 때의 되묻기 문장(_no_card_response, #283).

LLM·DB 없이 spec과 메시지만으로 만든다. 라우터에 끼워 보는 테스트는
tests/test_chat_router.py에 있다.
"""

from app.chat.schemas import Spec, SpecExact
from app.routers import chat
from app.search.schemas import SearchFilters


def _spec(**overrides) -> Spec:
    base = {
        "intent": "semantic",
        "exact": SpecExact(title=None, author=None, publisher=None),
        "filters": SearchFilters(),
        "semantic": None,
        "anchor_book": None,
        "exclude": [],
    }
    base.update(overrides)
    return Spec(**base)


def test_걸린_조건을_이름으로_짚고_따라_말할_예시를_준다() -> None:
    spec = _spec(
        semantic="경제 입문서",
        filters=SearchFilters(price_max=20000, pub_year_from=2020),
    )

    reply, followup = chat._no_card_response(
        spec, "2만원 이하 2020년 이후 경제", had_candidates=False
    )

    assert reply == "조건에 맞는 책을 아직 못 찾았어요."
    assert "2만원 이하, 2020년 이후 조건이 걸려 있어요." in followup
    assert '"가격은 상관없어요" / "출간연도는 상관없어요"' in followup


def test_같은_종류의_조건_예시는_한_번만_준다() -> None:
    spec = _spec(semantic="책", filters=SearchFilters(price_min=5000, price_max=10000))

    _, followup = chat._no_card_response(spec, "책", had_candidates=False)

    assert followup.count("가격은 상관없어요") == 1


def test_분야_출판사_조건도_짚는다() -> None:
    spec = _spec(
        semantic="책",
        filters=SearchFilters(category="소설"),
        exact=SpecExact(title=None, author=None, publisher="민음사"),
    )

    _, followup = chat._no_card_response(spec, "책", had_candidates=False)

    assert "소설, 민음사 책" in followup


def test_재고_조건은_짚지_않는다() -> None:
    # 챗봇은 재고 조건을 쓰지 않는다(#319). 옛 대화에서 넘어온 spec에 남아 있어도 묻지 않는다.
    spec = _spec(
        semantic="책",
        filters=SearchFilters(category="소설", in_stock_only=True),
    )

    _, followup = chat._no_card_response(spec, "책", had_candidates=False)

    assert "소설 조건이 걸려 있어요." in followup
    assert "재고" not in followup


def test_금액은_만원_단위로_떨어지면_만원으로_쓴다() -> None:
    assert chat._won(20000) == "2만원"
    assert chat._won(12500) == "12,500원"


def test_조건도_검색어도_없으면_분야를_고르게_한다() -> None:
    reply, followup = chat._no_card_response(
        _spec(), "책 추천해줘", had_candidates=False
    )

    assert reply == "추천해 드리려면 조금 더 알아야 해요."
    assert "소설, 에세이, 인문, 경제경영, 자기계발, 과학" in followup
    assert '"경제경영 책 추천해줘"처럼' in followup
    assert "제목·저자" in followup
    assert "걸린 조건" not in followup


def test_검색어가_없으면_조건이_걸려_있어도_주제부터_묻고_조건은_알려만_준다() -> None:
    # 조건을 풀어도 찾을 게 없다. 조건을 넓히자고 묻는 건 원인과 다르다.
    spec = _spec(filters=SearchFilters(pub_year_from=2020))

    reply, followup = chat._no_card_response(
        spec, "2020년 이후 책", had_candidates=False
    )

    assert reply == "추천해 드리려면 조금 더 알아야 해요."
    assert followup.startswith("어떤 분야가 좋으세요?")
    assert "(지금 걸린 조건: 2020년 이후)" in followup
    assert "넓혀서" not in followup


def test_검색어는_있는데_못_찾았으면_짧은_말은_그대로_짚는다() -> None:
    spec = _spec(semantic="가볍고 잔잔한 분위기의 어린이 책")

    _, followup = chat._no_card_response(spec, "어린왕자", had_candidates=False)

    assert followup.startswith('"어린왕자"만으로는 찾기 어려워요.')
    assert "제목이나 저자를 알려 주시거나" in followup


def test_검색어는_있는데_못_찾았으면_긴_말은_따라_쓰지_않는다() -> None:
    spec = _spec(semantic="판타지")
    long_message = "요즘 지하철에서 읽기 좋은 가볍고 재미있는 판타지 소설 추천해줘"

    _, followup = chat._no_card_response(spec, long_message, had_candidates=False)

    assert followup.startswith("말씀하신 내용만으로는 찾기 어려워요.")
    assert long_message not in followup


def test_후보가_있었는데_카드가_비면_조건을_탓하지_않는다() -> None:
    spec = _spec(semantic="판타지", filters=SearchFilters(price_max=10000))

    reply, followup = chat._no_card_response(spec, "판타지", had_candidates=True)

    assert reply == "추천을 만들지 못했어요."
    assert followup == "같은 요청을 한 번만 다시 말씀해 주시겠어요?"


def test_최저가_0은_조건이_아니라서_말하지_않는다() -> None:
    spec = _spec(semantic="책", filters=SearchFilters(price_min=0))

    assert chat._active_conditions(spec) == []
