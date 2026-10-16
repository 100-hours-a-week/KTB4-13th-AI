"""④⑥ 평가용 가상 사용자 묶음(evals/feed/cases.json)의 형식 검사(#390). DB 없이 돈다.

묶음을 돌려 채점하는 것은 책 211만 권이 든 평가용 DB 가 있어야 해서 CI 에서 하지 않는다. 여기서는 묶음 자체가
깨지지 않았는지만 본다. 파일을 고치다 형식이 어긋나면 실행기가 그 사용자를 건너뛰거나 틀리게 채점한다.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.core import categories
from app.core.config import get_settings
from app.core.history import DISLIKED_MAX_RATING
from app.extraction.rules import MAX_VALUE_CHARS
from app.extraction.rules import PREFERENCE_TYPES as MEMORY_TYPES
from app.feed.schemas import MAX_SIZE, FeedRequest
from app.profile.schemas import USED_LIKED_BOOKS, ProfileRequest
from evals.feed.run import memory_request

_PATH = Path(__file__).resolve().parents[1] / "evals" / "feed" / "cases.json"

# typical 은 지금도 잘 나와야 하는 평범한 사용자, guard 는 지금 틀리기 쉬운 사용자다. 결과를 따로 낸다.
GROUPS = {"typical", "guard"}
# 관심사마다 "맞는 책"을 가르는 방법과, 그 방법에 꼭 있어야 하는 칸
TRUTHS = {
    "field": "field",  # 책의 분류가 그 온보딩 분야의 핵심 분류에 든다
    "kdc": "kdc",  # 도서관 세부 분류 번호가 이 숫자들로 시작한다
    "words": "words",  # 제목이나 소개글에 이 낱말이 있다(거친 기준)
    "author": "author",  # 저자 칸에 이 이름이 있다
    "reader": "reader",  # 독자 대상 부호
    "pages": "max",  # 쪽수가 이 값 이하
    "pub_year": "min",  # 출간연도가 이 값 이상
    "price": "max",  # 정가가 이 값 이하
}
EXPECT_KEYS = {
    "show",
    "avoid",
    "toward",
    "compare_to",
    "never_owned",
    "editions",
    "cold_start",
    "pile",
}
# 온보딩 분야 가운데 어린이는 책의 분류로 가를 수 없어 대응표에 없다(#228). 화면에는 있는 값이다.
ONBOARDING_CATEGORIES = {*categories.ONBOARDING_TO_CATALOG, "어린이"}

_DATA = json.loads(_PATH.read_text(encoding="utf-8"))
_INTERESTS = _DATA["interests"]
_KINDS = _DATA["kinds"]
_CASES = _DATA["cases"]
_BY_ID = {case["id"]: case for case in _CASES}


def _group(case: dict) -> str:
    return _KINDS[case["kind"]]["group"]


def _books(case: dict) -> list[str]:
    user = case["user"]
    return [
        *user["onboarding"]["liked_isbns"],
        *user["history"]["purchased_isbns"],
        *user["history"]["library_isbns"],
    ]


def test_묶음에_버전과_책_표의_정보가_있다() -> None:
    # 사용자를 고치면 전후 숫자를 견줄 수 없다. 버전을 올려 결과에 함께 적는다.
    assert isinstance(_DATA["version"], int) and _DATA["version"] >= 1
    # 책은 이 책 표에서 골랐다. 책 표가 달라지면 같은 묶음이어도 숫자를 견줄 수 없다.
    assert set(_DATA["catalog"]) == {"built", "books", "with_vectors"}
    assert 0 < _DATA["catalog"]["with_vectors"] <= _DATA["catalog"]["books"]
    # 위 몇 권을 볼지. 홈 추천이 한 번에 줄 수 있는 권수를 넘으면 한 번의 요청으로 받을 수 없다.
    assert 1 <= _DATA["top"] <= MAX_SIZE


def test_사용자_번호와_사례_번호는_겹치지_않는다() -> None:
    assert len(_BY_ID) == len(_CASES)
    assert len({case["user_id"] for case in _CASES}) == len(_CASES)
    for case in _CASES:
        assert case["id"].rsplit("-", 1)[0] == case["kind"]
        assert set(case) == {"id", "kind", "about", "user_id", "user", "expect"}
        assert case["about"].strip()


def test_종류마다_묶음과_설명이_있고_사례가_둘_이상이다() -> None:
    counts = Counter(case["kind"] for case in _CASES)

    assert set(counts) == set(_KINDS)
    assert min(counts.values()) >= 2
    for kind in _KINDS.values():
        assert set(kind) == {"group", "about"}
        assert kind["group"] in GROUPS
        assert kind["about"].strip()


def test_지금_잘_나와야_하는_사용자가_넉넉하다() -> None:
    # 틀리는 사용자만 모아 재면, 그쪽을 고치느라 멀쩡하던 사용자가 나빠지는 것을 놓친다.
    typical = sum(_group(case) == "typical" for case in _CASES)

    assert typical >= 40
    assert typical / len(_CASES) >= 0.25


@pytest.mark.parametrize("key", list(_INTERESTS))
def test_관심사마다_맞는_책을_가르는_기준이_있다(key: str) -> None:
    interest = _INTERESTS[key]
    by = interest["by"]

    assert interest["label"].strip()
    assert by in TRUTHS
    assert set(interest) - {"rough"} == {"label", "by", TRUTHS[by]}
    value = interest[TRUTHS[by]]
    if by == "field":
        # 실행기가 분야의 핵심 분류를 서버의 대응표에서 읽는다. 대응표에 없는 이름이면 맞는 책이 0권이 된다.
        assert value in categories.ONBOARDING_TO_CATALOG
    elif by in ("kdc", "words", "reader"):
        assert value and all(isinstance(v, str) and v.strip() == v and v for v in value)
        if by == "kdc":
            assert all(v.isdigit() and 2 <= len(v) <= 3 for v in value)
    elif by == "author":
        assert value.strip() == value and value
    else:
        assert isinstance(value, int) and value > 0
    # 낱말로 가르는 기준은 거칠다고 표시해 둔다. 결과 표가 이 숫자를 따로 보여 준다.
    assert interest.get("rough", False) is (by == "words")


def test_쓰이지_않는_관심사가_없다() -> None:
    used = {
        key
        for case in _CASES
        for name in ("show", "avoid", "toward")
        for key in case["expect"].get(name, ())
    }

    assert used == set(_INTERESTS)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_사용자는_서버가_받는_요청으로_만들_수_있다(case: dict) -> None:
    user = case["user"]
    assert set(user) == {"onboarding", "history", "memories"}
    assert set(user["onboarding"]) == {"categories", "tags", "liked_isbns"}
    assert set(user["history"]) == {"purchased_isbns", "library_isbns", "reviews"}

    assert set(user["onboarding"]["categories"]) <= ONBOARDING_CATEGORIES
    assert set(user["onboarding"]["tags"]) <= set(categories.ONBOARDING_TAGS)
    # 넘는 책은 서버가 조용히 잘라 쓴다. 묶음이 뜻한 사용자와 다른 사용자를 재게 된다.
    assert len(user["onboarding"]["liked_isbns"]) <= USED_LIKED_BOOKS
    assert len(_books(case)) == len(set(_books(case)))

    for memory in user["memories"]:
        # 취향 기억은 ⑤가 뽑은 것이다. ⑤가 낼 수 있는 종류와 길이여야 한다. 세기·어디에·핵심어·저장한
        # 때의 범위는 아래 서버의 요청 검사가 본다(#422).
        assert set(memory) == {
            "type",
            "value",
            "strength",
            "applies_to",
            "keyword",
            "created_at",
        }
        assert memory["type"] in MEMORY_TYPES
        assert 0 < len(memory["value"]) <= MAX_VALUE_CHARS
        assert memory["value"] == memory["value"].strip()
    # 묶음의 순서가 말한 순서다. 마지막 말을 따르는 규칙을 잴 때 순서가 어긋나면 다른 것을 잰다.
    saved = [memory["created_at"] for memory in user["memories"]]
    assert saved == sorted(saved) and len(saved) == len(set(saved))

    # 서버의 요청 검사를 그대로 통과해야 한다. 벡터는 실행기가 붙이므로 여기서는 자리만 채운다.
    dim = get_settings().embedding_dim
    ProfileRequest.model_validate(
        {
            "user_id": case["user_id"],
            "idempotency_key": f"eval_{case['id']}",
            "onboarding": user["onboarding"],
            "memories": memory_request(
                user["memories"], [[0.0] * dim] * len(user["memories"]), dim
            ),
            "history": user["history"],
        },
        strict=False,
    )
    FeedRequest.model_validate(
        {
            "user_id": case["user_id"],
            "surface": "home",
            "size": _DATA["top"],
            "purchased_isbns": user["history"]["purchased_isbns"],
            "library_isbns": user["history"]["library_isbns"],
        }
    )
    # 리뷰는 산 책에만 달 수 있다(BE 의 규칙).
    assert {review["isbn"] for review in user["history"]["reviews"]} <= set(
        user["history"]["purchased_isbns"]
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_볼_것이_적혀_있고_있는_관심사만_가리킨다(case: dict) -> None:
    expect = case["expect"]

    assert set(expect) <= EXPECT_KEYS
    # 나와야 하는 관심사가 있거나, 개인화하지 않은 목록이 나와야 하는 사용자다.
    assert bool(expect.get("show")) != bool(expect.get("cold_start"))
    for name in ("show", "avoid", "toward"):
        keys = expect.get(name, [])
        assert len(keys) == len(set(keys))
        assert set(keys) <= set(_INTERESTS)
    # 나와야 하는 관심사가 늘면 안 되는 관심사이기도 하면 채점할 수 없다.
    assert not set(expect.get("show", ())) & set(expect.get("avoid", ()))
    for flag in ("never_owned", "editions", "cold_start"):
        assert expect.get(flag, True) is True


def test_견줄_짝은_마지막_취향_기억만_뺀_같은_사용자다() -> None:
    # 기억 하나를 더했을 때 무엇이 달라지는지를 본다. 다른 것이 같이 다르면 그 기억 때문인지 알 수 없다.
    paired = [case for case in _CASES if "compare_to" in case["expect"]]

    assert paired
    for case in paired:
        twin = _BY_ID[case["expect"]["compare_to"]]
        assert _group(twin) == "typical"
        assert twin["user"]["onboarding"] == case["user"]["onboarding"]
        assert twin["user"]["history"] == case["user"]["history"]
        assert twin["user"]["memories"] == case["user"]["memories"][:-1]


def test_읽지_않는다는_기억은_그_끝맺음으로_쓴다() -> None:
    # ⑤는 읽지 않는 것을 "…은 읽지 않음"으로 끝낸다(프롬프트의 문장 쓰는 법). 다르게 쓰면 다른 것을 잰다.
    avoiding = [
        case
        for case in _CASES
        if case["expect"].get("avoid") and case["user"]["memories"]
    ]

    assert avoiding
    for case in avoiding:
        assert case["user"]["memories"][-1]["value"].endswith("읽지 않음")
        assert "compare_to" in case["expect"]


def test_낮은_별점을_준_사용자는_서버가_싫어한다고_보는_점수를_줬다() -> None:
    rated = [case for case in _CASES if case["user"]["history"]["reviews"]]

    assert rated
    for case in rated:
        assert all(
            review["rating"] <= float(DISLIKED_MAX_RATING)
            for review in case["user"]["history"]["reviews"]
        )
        assert case["expect"].get("avoid")


def test_가진_책을_보는_사용자는_가진_책이_있다() -> None:
    owning = [case for case in _CASES if case["expect"].get("never_owned")]

    assert owning
    for case in owning:
        history = case["user"]["history"]
        assert history["purchased_isbns"] and history["library_isbns"]


def test_책이_쌓인_사용자는_적은_권수만큼_좋아한_책이_있고_권수마다_여럿이다() -> None:
    # 권수가 같은 사용자끼리 위 20권이 얼마나 겹치는지를 본다. 한 명뿐이면 견줄 상대가 없다.
    piles = [case for case in _CASES if "pile" in case["expect"]]
    sizes = Counter(case["expect"]["pile"] for case in piles)

    assert piles and min(sizes.values()) >= 2
    books: list[str] = []
    for case in piles:
        liked = case["user"]["onboarding"]["liked_isbns"]
        assert len(liked) == case["expect"]["pile"]
        books += liked
    # 사용자끼리 같은 책을 좋아하면 추천이 겹치는 것이 당연해진다. 권수가 같은 사용자들은 책이 겹치지 않는다.
    for size in sizes:
        same = [
            book
            for case in piles
            if case["expect"]["pile"] == size
            for book in case["user"]["onboarding"]["liked_isbns"]
        ]
        assert len(same) == len(set(same))
