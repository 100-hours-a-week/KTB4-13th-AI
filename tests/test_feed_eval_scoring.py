"""④⑥ 평가 묶음의 채점(evals/feed/scoring.py) 테스트(#392). DB 없이 돈다."""

from evals.feed.scoring import (
    Book,
    Response,
    matches,
    score,
    summarize,
)

INTERESTS = {
    "f-novel": {"label": "소설 분야", "by": "field", "field": "소설"},
    "cooking": {"label": "요리", "by": "kdc", "kdc": ["594"]},
    "mystery": {
        "label": "추리",
        "by": "words",
        "words": ["추리", "미스터리"],
        "rough": True,
    },
    "a-한강": {"label": "한강의 책", "by": "author", "author": "한강"},
    "children": {"label": "어린이책", "by": "reader", "reader": ["7"]},
    "c-thin": {"label": "200쪽 이하", "by": "pages", "max": 200},
    "c-recent": {"label": "2024년 이후", "by": "pub_year", "min": 2024},
    "c-cheap": {"label": "만 원 이하", "by": "price", "max": 10000},
}
KINDS = {
    "one-topic": {"group": "typical", "about": "주제 하나"},
    "topics-2": {"group": "guard", "about": "주제 둘"},
    "dislike": {"group": "guard", "about": "읽지 않는 주제"},
    "owned": {"group": "guard", "about": "가진 책"},
    "cold": {"group": "typical", "about": "재료 없음"},
    "pile": {"group": "guard", "about": "책이 쌓임"},
}


def _book(isbn: str = "9780000000001", **over) -> Book:
    base = {
        "isbn": isbn,
        "title": "제목",
        "author": "저자",
        "category": "경제학",
        "kdc": "320.1",
        "reader": "0",
        "pages": 300,
        "pub_year": 2020,
        "price": 15000,
        "text": "제목 소개글",
    }
    return Book(**{**base, **over})


def _case(kind: str = "one-topic", **expect) -> dict:
    return {
        "id": f"{kind}-01",
        "kind": kind,
        "about": "",
        "user_id": 1,
        "user": {
            "onboarding": {"categories": [], "tags": [], "liked_isbns": []},
            "history": {"purchased_isbns": [], "library_isbns": [], "reviews": []},
            "memories": [],
        },
        "expect": expect,
    }


def _response(isbns: list[str], **over) -> Response:
    base = {
        "profile_status": 200,
        "feed_status": 200,
        "cold_start": False,
        "degraded": None,
        "isbns": tuple(isbns),
        "seconds": 0.05,
    }
    return Response(**{**base, **over})


def _shelf(*books: Book) -> dict[str, Book]:
    return {book.isbn: book for book in books}


# ── 책 한 권이 관심사에 맞는가 ──────────────────────────────────────────────


def test_분야는_책의_분류가_그_분야의_핵심_분류일_때_맞다() -> None:
    assert matches(INTERESTS["f-novel"], _book(category="한국문학")) is True
    assert matches(INTERESTS["f-novel"], _book(category="경제학")) is False
    assert matches(INTERESTS["f-novel"], _book(category=None)) is False


def test_세부_분류는_번호가_그_숫자로_시작할_때_맞고_번호가_없으면_모른다() -> None:
    assert matches(INTERESTS["cooking"], _book(kdc="594.5")) is True
    assert matches(INTERESTS["cooking"], _book(kdc="598.1")) is False
    # 번호가 없는 책을 "아님"으로 세면, 번호가 붙은 책이 많은 쪽이 유리해진다.
    assert matches(INTERESTS["cooking"], _book(kdc=None)) is None


def test_낱말은_제목이나_소개글에_있으면_맞고_대소문자를_가리지_않는다() -> None:
    sf = {"by": "words", "words": ["SF"], "label": "SF", "rough": True}

    assert matches(INTERESTS["mystery"], _book(text="밀실 미스터리의 걸작")) is True
    assert matches(INTERESTS["mystery"], _book(text="따뜻한 가족 이야기")) is False
    assert matches(sf, _book(text="한국 sf 문학의 오늘")) is True


def test_작가는_이름이_다른_이름의_일부일_때는_맞지_않다() -> None:
    assert matches(INTERESTS["a-한강"], _book(author="지은이: 한강")) is True
    assert matches(INTERESTS["a-한강"], _book(author="한강 지음 ; 김철수 옮김")) is True
    assert matches(INTERESTS["a-한강"], _book(author="한강희")) is False
    assert matches(INTERESTS["a-한강"], _book(author="김한강")) is False
    assert matches(INTERESTS["a-한강"], _book(author=None)) is False


def test_독자_대상과_숫자_조건은_값이_없으면_모른다() -> None:
    assert matches(INTERESTS["children"], _book(reader="7")) is True
    assert matches(INTERESTS["children"], _book(reader="0")) is False
    assert matches(INTERESTS["children"], _book(reader=None)) is None
    assert matches(INTERESTS["c-thin"], _book(pages=200)) is True
    assert matches(INTERESTS["c-thin"], _book(pages=201)) is False
    assert matches(INTERESTS["c-thin"], _book(pages=None)) is None
    assert matches(INTERESTS["c-recent"], _book(pub_year=2024)) is True
    assert matches(INTERESTS["c-recent"], _book(pub_year=2023)) is False
    assert matches(INTERESTS["c-cheap"], _book(price=10000)) is True
    assert matches(INTERESTS["c-cheap"], _book(price=10001)) is False


# ── 사용자 한 명의 위 20권 ─────────────────────────────────────────────────


def test_나와야_하는_관심사마다_맞는_책을_세고_가장_적은_쪽을_낸다() -> None:
    shelf = _shelf(
        _book("1", kdc="594.5"),
        _book("2", kdc="594.1"),
        _book("3", text="추리 소설"),
        _book("4"),
    )
    got = score(
        _case("topics-2", show=["cooking", "mystery"]),
        _response(["1", "2", "3", "4"]),
        shelf,
        INTERESTS,
    )

    assert got.ok is True
    assert got.returned == 4
    assert got.show == {"cooking": 2, "mystery": 1}
    assert got.least == 1
    assert got.missing == 0
    # 4번 책은 요리도 추리도 아니다.
    assert got.none == 1
    assert got.unsure == 0


def test_한_권도_안_나온_관심사를_센다() -> None:
    got = score(
        _case("topics-2", show=["cooking", "mystery"]),
        _response(["1"]),
        _shelf(_book("1", kdc="594.5")),
        INTERESTS,
    )

    assert got.least == 0
    assert got.missing == 1


def test_맞는지_알_수_없는_책은_아닌_책과_따로_센다() -> None:
    # 세부 분류 번호가 없는 책은 요리책인지 알 수 없다. "어느 관심사도 아닌 책"에 넣으면 숫자가 부풀려진다.
    got = score(
        _case(show=["cooking"]),
        _response(["1", "2"]),
        _shelf(_book("1", kdc=None), _book("2", kdc="911")),
        INTERESTS,
    )

    assert got.show == {"cooking": 0}
    assert got.unknown == {"cooking": 1}
    assert got.none == 1
    assert got.unsure == 1


def test_책_표에서_찾지_못한_책은_알_수_없는_책이다() -> None:
    got = score(_case(show=["cooking"]), _response(["없는책"]), _shelf(), INTERESTS)

    assert got.returned == 1
    assert got.show == {"cooking": 0}
    assert got.unknown == {"cooking": 1}
    assert got.none == 0
    assert got.unsure == 1


def test_API가_실패했거나_기능을_줄여_답했으면_잰_것으로_치지_않는다() -> None:
    shelf = _shelf(_book("1", kdc="594.5"))
    case = _case(show=["cooking"])

    assert score(case, _response(["1"], feed_status=500), shelf, INTERESTS).ok is False
    assert (
        score(case, _response(["1"], profile_status=400), shelf, INTERESTS).ok is False
    )
    # 벡터를 못 써 규칙 점수만으로 답한 목록은 재려는 것이 아니다.
    assert (
        score(case, _response(["1"], degraded="rule_only"), shelf, INTERESTS).ok
        is False
    )
    # 취향이 있는 사용자인데 개인화하지 않은 목록이 왔다. 프로필이 쓰이지 않은 것이다.
    assert score(case, _response(["1"], cold_start=True), shelf, INTERESTS).ok is False


def test_늘면_안_되는_관심사와_기울어야_하는_것을_센다() -> None:
    shelf = _shelf(
        _book("1", kdc="594.5", pages=120), _book("2", text="추리", pages=400)
    )
    got = score(
        _case("dislike", show=["cooking"], avoid=["mystery"], toward=["c-thin"]),
        _response(["1", "2"]),
        shelf,
        INTERESTS,
    )

    assert got.avoid == {"mystery": 1}
    assert got.toward == {"c-thin": 1}
    # 추리 책은 요리가 아니다. 나와야 하는 관심사 기준으로만 "아닌 책"을 센다.
    assert got.none == 1


def test_짝이_있으면_같은_관심사를_짝의_목록에서도_세고_겹치는_책을_센다() -> None:
    shelf = _shelf(
        _book("1", kdc="594.5"),
        _book("2", kdc="594.5"),
        _book("3", text="추리"),
        _book("4", text="추리"),
    )
    got = score(
        _case(
            "dislike", show=["cooking"], avoid=["mystery"], compare_to="one-topic-01"
        ),
        _response(["1", "3", "4"]),
        shelf,
        INTERESTS,
        twin=_response(["1", "2"]),
    )

    assert got.show == {"cooking": 1}
    assert got.twin_show == {"cooking": 2}
    assert got.avoid == {"mystery": 2}
    assert got.twin_avoid == {"mystery": 0}
    assert got.same_as_twin == 1


def test_짝을_재지_못했으면_그_사용자도_재지_못한_것으로_친다() -> None:
    # 짝의 목록이 비면 짝의 책이 0권으로 세어져, 기억을 더한 뒤가 오히려 나아진 것처럼 보인다.
    shelf = _shelf(_book("1", kdc="594.5"))
    case = _case("dislike", show=["cooking"], compare_to="one-topic-01")
    mine = _response(["1"])

    for broken in (
        _response([], feed_status=500),
        _response([], profile_status=400),
        _response(["1"], degraded="rule_only"),
        # 짝은 취향 기억이 남아 있는 사용자다. 개인화하지 않은 목록이 왔다면 프로필이 쓰이지 않은 것이다.
        _response(["1"], cold_start=True),
    ):
        got = score(case, mine, shelf, INTERESTS, twin=broken)

        assert got.ok is False
        assert got.twin_show is None
        assert got.same_as_twin is None


def test_짝이_없으면_짝의_숫자는_비어_있다() -> None:
    got = score(
        _case(show=["cooking"]), _response(["1"]), _shelf(_book("1")), INTERESTS
    )

    assert got.twin_show is None
    assert got.same_as_twin is None


def test_가진_책이_나온_권수와_같은_제목의_다른_판본을_센다() -> None:
    case = _case("owned", show=["f-novel"], never_owned=True, editions=True)
    case["user"]["history"]["purchased_isbns"] = ["1"]
    case["user"]["history"]["library_isbns"] = ["2"]
    shelf = _shelf(
        _book("1", title="데미안"),
        _book("2", title="어린 왕자"),
        _book("3", title="데미안 "),
        _book("4", title="데 미 안"),
        _book("5", title="수레바퀴 아래서"),
    )
    got = score(case, _response(["1", "3", "4", "5"]), shelf, INTERESTS)

    assert got.owned == 1
    # 3번과 4번은 띄어쓰기만 다른 같은 제목이다. 가진 책 자신(1번)은 판본으로 세지 않는다.
    assert got.editions == 2


def test_가진_책을_보지_않는_사용자는_그_칸이_비어_있다() -> None:
    got = score(
        _case(show=["cooking"]), _response(["1"]), _shelf(_book("1")), INTERESTS
    )

    assert got.owned is None
    assert got.editions is None


def test_재료가_없는_사용자는_개인화하지_않은_목록이_와야_맞다() -> None:
    case = _case("cold", cold_start=True)

    assert score(case, _response(["1"], cold_start=True), _shelf(), INTERESTS).ok
    assert not score(case, _response(["1"], cold_start=False), _shelf(), INTERESTS).ok


# ── 종류별로 모으기 ───────────────────────────────────────────────────────


def _scores(*items):
    return [
        score(case, response, shelf, INTERESTS, twin=twin)
        for case, response, shelf, twin in items
    ]


def test_종류마다_사용자_수와_평균을_낸다() -> None:
    shelf = _shelf(_book("1", kdc="594.5"), _book("2", text="추리"), _book("3"))
    first = _case("topics-2", show=["cooking", "mystery"])
    second = {**_case("topics-2", show=["cooking", "mystery"]), "id": "topics-2-02"}
    summary = summarize(
        _scores(
            (first, _response(["1", "2", "3"], seconds=0.1), shelf, None),
            (second, _response(["1", "3"], seconds=0.3), shelf, None),
        ),
        KINDS,
        INTERESTS,
    )
    row = summary["kinds"]["topics-2"]

    assert row["group"] == "guard"
    assert row["users"] == 2
    assert row["measured"] == 2
    # 가장 적게 나온 관심사: 첫째 사용자는 1권, 둘째는 0권
    assert row["least_mean"] == 0.5
    assert row["missing_users"] == 1
    assert row["none_mean"] == 1.0
    assert row["seconds_median"] == 0.2
    # 추리는 낱말로 가르는 거친 기준이다.
    assert row["rough"] is True


def test_재지_못한_사용자는_평균에서_빼고_따로_센다() -> None:
    shelf = _shelf(_book("1", kdc="594.5"))
    ok = _case(show=["cooking"])
    failed = {**_case(show=["cooking"]), "id": "one-topic-02"}
    summary = summarize(
        _scores(
            (ok, _response(["1"]), shelf, None),
            (failed, _response([], feed_status=500), shelf, None),
        ),
        KINDS,
        INTERESTS,
    )
    row = summary["kinds"]["one-topic"]

    assert row["users"] == 2
    assert row["measured"] == 1
    assert row["least_mean"] == 1.0
    assert row["rough"] is False


def test_짝이_있는_종류는_짝과의_차이를_낸다() -> None:
    shelf = _shelf(
        _book("1", kdc="594.5"), _book("2", kdc="594.5"), _book("3", text="추리")
    )
    case = _case("dislike", show=["cooking"], avoid=["mystery"], compare_to="x")
    summary = summarize(
        _scores((case, _response(["1", "3"]), shelf, _response(["1", "2"]))),
        KINDS,
        INTERESTS,
    )
    row = summary["kinds"]["dislike"]

    assert row["show_mean"] == 1.0
    assert row["twin_show_mean"] == 2.0
    assert row["avoid_mean"] == 1.0
    assert row["twin_avoid_mean"] == 0.0
    assert row["same_as_twin_mean"] == 1.0


def test_짝을_재지_못한_사용자는_짝과의_차이에_넣지_않는다() -> None:
    shelf = _shelf(_book("1", kdc="594.5"), _book("2", kdc="594.5"))
    fine = _case("dislike", show=["cooking"], compare_to="x")
    broken = {**_case("dislike", show=["cooking"], compare_to="y"), "id": "dislike-02"}
    summary = summarize(
        _scores(
            (fine, _response(["1"]), shelf, _response(["1", "2"])),
            (broken, _response(["1", "2"]), shelf, _response([], feed_status=500)),
        ),
        KINDS,
        INTERESTS,
    )
    row = summary["kinds"]["dislike"]

    assert row["users"] == 2
    assert row["measured"] == 1
    # 짝이 실패한 사용자가 섞이면 (2 + 0) / 2 = 1.0 이 된다.
    assert row["twin_show_mean"] == 2.0
    assert row["show_mean"] == 1.0


def test_책이_쌓인_사용자는_권수가_같은_사용자끼리_겹치는_책을_낸다() -> None:
    def pile(case_id: str, size: int) -> dict:
        return {**_case("pile", show=["cooking"], pile=size), "id": case_id}

    shelf = _shelf(*(_book(str(n)) for n in range(1, 9)))
    summary = summarize(
        _scores(
            (pile("pile-01", 5), _response(["1", "2", "3"]), shelf, None),
            (pile("pile-02", 5), _response(["2", "3", "4"]), shelf, None),
            (pile("pile-03", 10), _response(["5", "6"]), shelf, None),
            (pile("pile-04", 10), _response(["5", "6"]), shelf, None),
        ),
        KINDS,
        INTERESTS,
    )
    piles = summary["kinds"]["pile"]["piles"]

    assert piles["5"] == {"users": 2, "overlap_mean": 2.0, "distinct": 4}
    assert piles["10"] == {"users": 2, "overlap_mean": 2.0, "distinct": 2}


def test_가진_책과_재료_없는_사용자의_결과를_낸다() -> None:
    owned = _case("owned", show=["f-novel"], never_owned=True, editions=True)
    owned["user"]["history"]["purchased_isbns"] = ["1"]
    cold = _case("cold", cold_start=True)
    shelf = _shelf(_book("1", title="데미안"), _book("2", title="데미안"))
    summary = summarize(
        _scores(
            (owned, _response(["1", "2"]), shelf, None),
            (cold, _response(["2"], cold_start=True), shelf, None),
        ),
        KINDS,
        INTERESTS,
    )

    assert summary["kinds"]["owned"]["owned_users"] == 1
    assert summary["kinds"]["owned"]["editions_mean"] == 1.0
    assert summary["kinds"]["cold"]["measured"] == 1


def test_묶음별로도_모으되_전체를_합친_숫자는_내지_않는다() -> None:
    # 가상 사용자는 실제 사용자의 분포를 따르지 않아서 전체 평균은 뜻이 없다(README 의 한계).
    shelf = _shelf(_book("1", kdc="594.5"))
    summary = summarize(
        _scores((_case(show=["cooking"]), _response(["1"]), shelf, None)),
        KINDS,
        INTERESTS,
    )

    assert set(summary) == {"kinds", "groups"}
    assert summary["groups"]["typical"]["users"] == 1
    assert "guard" not in summary["groups"]
