"""⑤ 평가용 고정 대화 묶음(evals/extraction/cases.json)의 형식 검사(#341). DB·LLM 없이 돈다.

묶음을 돌려 채점하는 것은 LLM 비용 때문에 CI 에서 하지 않는다. 여기서는 묶음 자체가 깨지지 않았는지만 본다.
파일을 고치다 형식이 어긋나면 채점기가 그 대화를 조용히 건너뛰거나 틀리게 채점한다.
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.core import categories
from app.extraction.rules import (
    MAX_EXTRACTIONS,
    MAX_VALUE_CHARS,
    MIN_CONDITION_STRENGTH,
    condition_keyword,
)
from app.extraction.rules import PREFERENCE_TYPES as TYPES
from app.extraction.schemas import parse_request

_PATH = Path(__file__).resolve().parents[1] / "evals" / "extraction" / "cases.json"

# 종류와 문장 최대 길이는 서버가 거르는 규칙(app/extraction/rules.py)과 같은 값을 쓴다. 따로 적어 두면
# 묶음의 기대값과 서버의 규칙이 어긋나도 모른다.
# typical 은 흔한 대화, guard 는 드물지만 틀리면 안 되는 대화다. 채점 결과를 따로 낸다.
GROUPS = {"typical", "guard"}

_DATA = json.loads(_PATH.read_text(encoding="utf-8"))
_KINDS = _DATA["kinds"]
_CASES = _DATA["cases"]
_IDS = [case["id"] for case in _CASES]

# 세기·어디에·핵심어를 적은 기대 취향(#424). 조건도 #427 부터 적는다.
_LABELED = [
    (case["id"], item) for case in _CASES for item in case["expect"]["extractions"]
]
# 명세 ⑤의 핵심어 최대 길이
_MAX_KEYWORD_CHARS = 100


def _in_group(group: str) -> list[dict]:
    return [case for case in _CASES if _KINDS[case["kind"]]["group"] == group]


def _to_extract(cases: list[dict]) -> int:
    return sum(not case["expect"]["nothing_found"] for case in cases)


def test_묶음에_버전이_있다() -> None:
    # 대화를 고치면 전후 숫자를 견줄 수 없다. 버전을 올려 결과에 함께 적는다.
    assert isinstance(_DATA["version"], int) and _DATA["version"] >= 1


def test_대화_번호는_겹치지_않는다() -> None:
    assert len(_IDS) == len(set(_IDS))


def test_종류마다_묶음과_설명이_있고_대화가_둘_이상이다() -> None:
    counts = Counter(case["kind"] for case in _CASES)

    assert set(counts) == set(_KINDS)
    assert min(counts.values()) >= 2
    for kind in _KINDS.values():
        assert set(kind) == {"group", "about"}
        assert kind["group"] in GROUPS
        assert kind["about"].strip()


def test_흔한_대화는_뽑을_것이_있는_쪽이_많되_없는_쪽도_넉넉하다() -> None:
    # 추천을 부탁하는 대화가 대부분이라 뽑을 것이 있는 쪽이 많다. 그래도 없는 쪽이 얇으면
    # "무엇이든 뽑는" 프롬프트가 높은 점수를 받는다.
    typical = _in_group("typical")
    to_extract = _to_extract(typical)

    assert to_extract * 2 > len(typical)
    assert (len(typical) - to_extract) * 5 >= len(typical)


def test_지키는_대화가_여덟_개_이상이다() -> None:
    assert len(_in_group("guard")) >= 8


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_대화_하나의_형식(case: dict) -> None:
    assert set(case) == {"id", "kind", "about", "request", "expect"}
    assert case["id"].startswith(case["kind"] + "-")
    assert case["about"].strip()

    # 요청은 API 가 받는 모양 그대로다. 채점기는 이것을 그대로 보낸다.
    req = parse_request(case["request"])
    assert req is not None
    assert req.consented is True
    assert req.conversation_id == f"eval_{case['id']}"
    assert req.conversation[0].role == "user"
    assert {turn.role for turn in req.conversation} <= {"user", "assistant"}

    expect = case["expect"]
    assert set(expect) == {
        "nothing_found",
        "extractions",
        "must_not_extract",
        "forbidden",
    }
    assert isinstance(expect["nothing_found"], bool)
    # 뽑을 것이 없다는 대화에는 기대 문장이 없고, 있다는 대화에는 하나 이상 있다.
    assert expect["nothing_found"] == (not expect["extractions"])
    # 서버는 대화 하나에서 이 개수까지만 남긴다. 그보다 많이 기대하면 통과할 수 없는 대화가 된다.
    assert len(expect["extractions"]) <= MAX_EXTRACTIONS

    for item in expect["extractions"]:
        # 세기·핵심어는 모두, 어디에는 주제에만 있다(명세 ⑤).
        labels = {"strength", "keyword"}
        if item["type"] == "topic":
            labels.add("applies_to")
            # 꼭 고를 이름은 주제에만 있고, 적지 않아도 된다(#486).
            if "applies_to_must" in item:
                labels.add("applies_to_must")
        assert set(item) == {"type", "value", "keywords", "stated", *labels}
        assert item["type"] in TYPES
        assert 0 < len(item["value"]) <= MAX_VALUE_CHARS
        # 찾는 말은 뽑은 문장에 하나라도 들어 있는지 코드로 보는 데 쓴다. 비어 있으면 무엇이든 통과한다.
        assert item["keywords"] and all(k.strip() for k in item["keywords"])
        # 사용자가 직접 말한 취향인지, 부탁한 내용에서 드러난 취향인지.
        assert isinstance(item["stated"], bool)

    for key in ("must_not_extract", "forbidden"):
        assert isinstance(expect[key], list)
        assert all(isinstance(v, str) and v.strip() for v in expect[key])


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_넣으면_안_되는_말은_기대_문장에도_없다(case: dict) -> None:
    # 기대 문장이 금지한 말을 담고 있으면 그 대화는 어떻게 뽑아도 통과할 수 없다.
    expect = case["expect"]
    for item in expect["extractions"]:
        assert not [word for word in expect["forbidden"] if word in item["value"]]


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_기대_문장은_자기_찾는_말을_담고_있다(case: dict) -> None:
    # 찾는 말이 기대 문장에도 없으면 찾는 말을 잘못 적은 것이다.
    for item in case["expect"]["extractions"]:
        assert [word for word in item["keywords"] if word in item["value"]]


@pytest.mark.parametrize(("case_id", "item"), _LABELED, ids=[i for i, _ in _LABELED])
def test_세기_어디에_핵심어의_형식(case_id: str, item: dict) -> None:
    strength = item["strength"]
    # 서버는 0 과 피함의 부탁(-1)을 내지 않는다. 기대에 적으면 맞힐 수 없다.
    assert type(strength) is int and strength in {-3, -2, 1, 2, 3}
    # 크기는 부탁만 했으면 1, 직접 말했으면 2 이상이다.
    assert (abs(strength) >= 2) == item["stated"]

    keywords = item["keyword"]
    assert keywords and all(0 < len(k.strip()) <= _MAX_KEYWORD_CHARS for k in keywords)
    # 핵심어는 뜻을 뒤집지 않는 말로 적는다. 피함은 세기의 부호로 나타낸다.
    assert not [k for k in keywords if "않" in k or "싫" in k]

    if "applies_to" in item:
        names = item["applies_to"]
        # 어디에는 서버 안 카테고리·세부 카테고리 목록의 이름이다(#423).
        assert not set(names) - set(categories.NAME_TEXTS)
        assert len(names) == len(set(names))
        # 제목만 말한 취향은 어디에도 넣지 않는다(#395).
        if any(k.startswith("『") for k in keywords):
            assert names == []

    if "applies_to_must" in item:
        must = item["applies_to_must"]
        # 받아들이는 이름 가운데 하나는 꼭 골라야 하는 이름이다. 그 밖의 이름이면 맞힐 수 없다(#486).
        assert must and len(must) == len(set(must))
        assert set(must) <= set(item["applies_to"])

    if item["type"] == "condition":
        # 조건은 정해진 꼴로 적고, 늘 지킨다고 말한 것만 뽑는다(#427).
        assert all(condition_keyword(k) == k for k in keywords)
        assert strength >= MIN_CONDITION_STRENGTH
