"""⑤ 평가 묶음의 코드 채점(evals/extraction/scoring.py) 테스트(#341). LLM 없이 돈다.

채점기가 틀리면 프롬프트를 고칠 때 엉뚱한 방향으로 간다. 그래서 "무엇을 실패로 보는지"를 하나씩 고정한다.
뜻이 같은지(뽑으면 안 되는 취향을 다른 말로 뽑았는지, 종류가 맞는지)는 코드로 볼 수 없어 여기서
채점하지 않고 표시만 한다. LLM 채점은 뒤 단계다.
"""

import pytest

from evals.extraction.scoring import Outcome, score

_CID = "eval_x-01"


def _case(extractions=(), *, must_not_extract=(), forbidden=(), kind="request") -> dict:
    return {
        "id": "x-01",
        "kind": kind,
        "request": {"conversation_id": _CID},
        "expect": {
            "nothing_found": not extractions,
            "extractions": [
                {
                    "type": type_,
                    "value": value,
                    "keywords": list(keywords),
                    "stated": False,
                }
                for type_, value, keywords in extractions
            ],
            "must_not_extract": list(must_not_extract),
            "forbidden": list(forbidden),
        },
    }


def _got(
    type_="topic", value="쿠버네티스 입문서를 찾음", confidence=0.7, **over
) -> dict:
    item = {
        "type": type_,
        "value": value,
        "confidence": confidence,
        "vector": [0.1, 0.2],
        "dim": 2,
        "source_conversation_id": _CID,
    }
    item.update(over)
    return item


def _ok(*items) -> Outcome:
    return Outcome(
        status=200,
        data={"extractions": list(items), "nothing_found": not items},
        seconds=1.0,
    )


_KUBE = ("topic", "쿠버네티스 입문서를 찾음", ("쿠버네티스",))


# --- 뽑아야 하는 대화 ---


def test_기대한_취향의_찾는_말이_든_문장을_뽑았으면_통과다() -> None:
    result = score(_case([_KUBE]), _ok(_got(value="쿠버네티스를 다룬 입문서를 찾음")))

    assert result.passed
    assert result.failures == ()
    assert (result.expected, result.matched, result.extracted) == (1, 1, 1)


def test_찾는_말은_하나만_들어_있어도_되고_띄어쓰기와_대소문자를_보지_않는다() -> None:
    case = _case([("topic", "SF 소설을 좋아함", ("에스에프", "SF 소설"))])

    assert score(case, _ok(_got(value="sf소설을 즐겨 읽음"))).passed


def test_뽑아야_하는데_안_뽑았으면_실패다() -> None:
    result = score(_case([_KUBE]), _ok())

    assert not result.passed
    assert result.failures == ("missed",)
    assert (result.expected, result.matched, result.extracted) == (1, 0, 0)


def test_뽑았지만_찾는_말이_든_문장이_없으면_실패다() -> None:
    result = score(_case([_KUBE]), _ok(_got(value="개발 책을 찾음")))

    assert result.failures == ("missed",)


def test_기대한_취향이_둘이면_둘_다_있어야_통과다() -> None:
    case = _case([_KUBE, ("mood", "위로가 되는 책을 찾음", ("위로",))])

    result = score(case, _ok(_got()))

    assert result.failures == ("missed",)
    assert (result.expected, result.matched) == (2, 1)


def test_기대에_없는_취향을_더_뽑은_것은_실패가_아니다() -> None:
    # 사용자가 한 말에 근거가 있으면 괜찮다(README). 근거가 있는지는 LLM 채점이 본다
    result = score(_case([_KUBE]), _ok(_got(), _got(value="얇은 책을 찾음")))

    assert result.passed
    assert result.extra == 1


def test_종류가_기대와_다른_것은_실패가_아니고_개수만_센다() -> None:
    # 종류가 맞는지는 LLM 채점의 몫이다(#341 기준 4). 코드는 참고용으로 센다
    result = score(_case([_KUBE]), _ok(_got(type_="condition")))

    assert result.passed
    assert result.type_matched == 0


# --- 뽑으면 안 되는 대화 ---


def test_뽑을_것이_없는_대화에서_안_뽑았으면_통과다() -> None:
    assert score(_case(), _ok()).passed


def test_뽑을_것이_없는_대화에서_하나라도_뽑으면_실패다() -> None:
    result = score(_case(), _ok(_got()))

    assert result.failures == ("extracted",)


def test_넣으면_안_되는_말이_뽑은_문장에_있으면_실패다() -> None:
    case = _case([("mood", "위로가 되는 책을 찾음", ("위로",))], forbidden=["우울증"])

    result = score(
        case, _ok(_got(type_="mood", value="우울증 때문에 위로가 되는 책을 찾음"))
    )

    assert result.failures == ("forbidden",)


def test_넣으면_안_되는_말은_띄어쓰기가_달라도_잡는다() -> None:
    case = _case([_KUBE], forbidden=["만 원"])

    result = score(case, _ok(_got(value="2만원 이하의 쿠버네티스 책을 찾음")))

    assert result.failures == ("forbidden",)


def test_뽑으면_안_되는_취향이_적힌_대화는_LLM_채점이_필요하다고_표시한다() -> None:
    # "추리소설을 좋아함"을 "추리물을 즐김"으로 뽑았는지는 코드로 알 수 없다
    case = _case([_KUBE], must_not_extract=["추리소설을 좋아함"])

    result = score(case, _ok(_got()))

    assert result.passed
    assert result.needs_llm


def test_뽑으면_안_되는_취향을_글자_그대로_뽑았으면_코드도_실패로_본다() -> None:
    case = _case([_KUBE], must_not_extract=["추리소설을 좋아함"])

    result = score(case, _ok(_got(), _got(value="추리 소설을 좋아함.")))

    assert result.failures == ("extracted",)


# --- 응답 자체가 잘못된 경우 ---


@pytest.mark.parametrize("status", [400, 500, 503, 504])
def test_200이_아니면_실패이고_다른_것은_보지_않는다(status) -> None:
    result = score(_case([_KUBE]), Outcome(status=status, data=None, seconds=0.5))

    assert result.failures == ("error",)
    assert result.extracted == 0


def test_200이_아니면_본문에_무엇이_있든_실패다() -> None:
    # 뽑을 것이 없는 대화에 503 이 오면 "안 뽑았으니 통과"로 세면 안 된다
    outcome = Outcome(503, {"extractions": [], "nothing_found": True}, 0.5)

    assert score(_case(), outcome).failures == ("error",)


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(_got(type_="genre"), id="모르는_종류"),
        pytest.param(_got(value=""), id="빈_문장"),
        pytest.param(_got(value="쿠버네티스 " + "가" * 200), id="200자_초과"),
        pytest.param(_got(confidence=0.4), id="신뢰도_0.5_미만"),
        pytest.param(_got(confidence=1.2), id="신뢰도_1_초과"),
        pytest.param(_got(dim=3), id="벡터_길이와_dim이_다름"),
        pytest.param(_got(vector=[]), id="빈_벡터"),
        pytest.param(_got(source_conversation_id="다른 대화"), id="출처가_요청과_다름"),
        pytest.param({"type": "topic", "value": "쿠버네티스"}, id="칸이_빠짐"),
    ],
)
def test_응답의_모양이_명세와_다르면_실패다(item) -> None:
    result = score(_case([_KUBE]), _ok(item))

    assert "format" in result.failures


def test_nothing_found가_목록과_어긋나면_모양_실패다() -> None:
    outcome = Outcome(
        status=200, data={"extractions": [_got()], "nothing_found": True}, seconds=1.0
    )

    assert "format" in score(_case([_KUBE]), outcome).failures


def test_실패가_여럿이면_모두_적는다() -> None:
    case = _case([_KUBE], forbidden=["우울증"])

    result = score(case, _ok(_got(value="우울증에 좋은 책을 찾음", confidence=0.1)))

    assert set(result.failures) == {"format", "missed", "forbidden"}


# --- 세기·어디에·핵심어(#424). 통과에는 쓰지 않고 맞은 개수만 센다 ---


def _labeled(strength=1, keyword=("쿠버네티스",), applies_to=("프로그래밍", "IT")):
    case = _case([_KUBE])
    case["expect"]["extractions"][0].update(
        strength=strength, keyword=list(keyword), applies_to=list(applies_to)
    )
    return case


def _counts(result) -> tuple[int, ...]:
    return (
        result.labeled,
        result.sign_matched,
        result.strength_matched,
        result.keyword_matched,
        result.where_labeled,
        result.where_matched,
    )


def test_세기와_핵심어와_어디에가_맞으면_모두_센다() -> None:
    got = _got(strength=1, keyword="쿠버네티스", applies_to=["프로그래밍"])

    assert _counts(score(_labeled(), _ok(got))) == (1, 1, 1, 1, 1, 1)


def test_세기는_부호와_크기를_따로_센다() -> None:
    got = _got(strength=2, keyword="쿠버네티스", applies_to=["프로그래밍"])

    result = score(_labeled(strength=1), _ok(got))

    assert (result.sign_matched, result.strength_matched) == (1, 0)


def test_세기가_정수가_아니면_맞지_않다() -> None:
    result = score(_labeled(), _ok(_got(strength=True)))

    assert (result.sign_matched, result.strength_matched) == (0, 0)


def test_핵심어는_받아들이는_말_하나와_같아야_하고_띄어쓰기와_대소문자는_보지_않는다() -> (
    None
):
    case = _labeled(keyword=("쿠버네티스", "K8s 입문"))

    assert score(case, _ok(_got(keyword="k8s입문"))).keyword_matched == 1
    # 받아들이는 말을 담고 있어도 같지 않으면 맞지 않다. 문장을 그대로 적은 것을 잡는다
    assert (
        score(case, _ok(_got(keyword="쿠버네티스 입문서를 찾음"))).keyword_matched == 0
    )


@pytest.mark.parametrize(
    ("names", "matched"),
    [
        (["프로그래밍"], 1),
        (["프로그래밍", "IT"], 1),
        (["프로그래밍", "AI"], 0),  # 하나라도 받아들이는 이름 밖이면 틀림
        ([], 0),  # 맞는 이름이 있는데 고르지 않음
        ("프로그래밍", 0),  # 목록이 아님
    ],
)
def test_어디에는_고른_이름이_모두_받아들이는_이름_안에_있어야_맞다(
    names, matched
) -> None:
    assert score(_labeled(), _ok(_got(applies_to=names))).where_matched == matched


def test_맞는_이름이_없는_취향은_아무것도_고르지_않아야_맞다() -> None:
    case = _labeled(applies_to=())

    assert score(case, _ok(_got(applies_to=[]))).where_matched == 1
    assert score(case, _ok(_got(applies_to=["취미"]))).where_matched == 0


def test_어디에는_적어_둔_취향만_센다() -> None:
    case = _labeled()
    del case["expect"]["extractions"][0][
        "applies_to"
    ]  # 주제가 아닌 취향은 어디에가 없다

    result = score(case, _ok(_got(strength=1, keyword="쿠버네티스")))

    assert (result.labeled, result.where_labeled) == (1, 0)


def test_세_칸을_적지_않은_기대_취향과_못_찾은_취향은_세지_않는다() -> None:
    # 조건은 아직 세 칸을 적지 않았다(#427)
    assert score(_case([_KUBE]), _ok(_got(strength=1))).labeled == 0
    assert score(_labeled(), _ok(_got(value="파이썬 입문서를 찾음"))).labeled == 0


def test_세_칸이_없는_응답은_찾았어도_맞지_않은_것으로_세고_통과는_그대로다() -> None:
    # 지금 서버는 세 칸을 내지 않는다(#426 에서 생긴다). 세 칸은 통과에 쓰지 않는다
    result = score(_labeled(), _ok(_got()))

    assert result.passed
    assert _counts(result) == (1, 0, 0, 0, 1, 0)


def test_찾은_문장이_여럿이면_하나라도_맞으면_맞다() -> None:
    first = _got(value="쿠버네티스 입문서를 찾음", strength=-2, keyword="입문서")
    second = _got(value="쿠버네티스를 좋아함", strength=1, keyword="쿠버네티스")

    result = score(_labeled(), _ok(first, second))

    assert (result.sign_matched, result.keyword_matched) == (1, 1)
