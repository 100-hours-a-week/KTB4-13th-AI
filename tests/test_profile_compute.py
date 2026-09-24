"""⑥ 취향 프로필 계산 테스트 — 취향 벡터, 태그 가중치, 판 번호. DB 없이 돈다."""

import math

import pytest

from app.profile import compute
from app.profile.compute import Profile


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def test_취향_벡터는_가중평균을_길이_1로_맞춘_것이다() -> None:
    # 구매(3)한 책이 담기(1)한 책보다 세 배 세게 끈다.
    result = compute.centroid([([1.0, 0.0], 3), ([0.0, 1.0], 1)])

    assert result is not None
    assert math.isclose(_norm(result), 1.0)
    assert math.isclose(result[0] / result[1], 3.0)


def test_가중치가_0_이하인_재료는_취향_벡터에_넣지_않는다() -> None:
    # 2.0점 이하 리뷰(−2)는 카테고리 점수만 깎고 취향 벡터에는 넣지 않는다(명세 ⑥).
    result = compute.centroid([([1.0, 0.0], 1), ([0.0, 1.0], -2), ([0.0, 1.0], 0)])

    assert result == [1.0, 0.0]


@pytest.mark.parametrize(
    "parts",
    [
        [],
        [([1.0, 0.0], -2)],
        [([0.0, 0.0], 1)],  # 방향이 없는 벡터는 쓸 수 없다
    ],
)
def test_쓸_재료가_없으면_취향_벡터는_없다(parts: list) -> None:
    assert compute.centroid(parts) is None


def test_좋아한_책이_이력에도_있으면_큰_가중치_하나만_쓴다() -> None:
    # 온보딩 책은 나의 도서관에도 담겨 좋아한 책(2)과 담기(1)로 두 번 들어올 수 있다.
    weights = compute.book_weights([1, 2, 3], {1: 1, 2: 3, 4: 2}, set())

    assert weights == {1: 2, 2: 3, 3: 2, 4: 2}


def test_싫다고_한_책은_좋아한_책이어도_뺀다() -> None:
    # 2.0점 이하 리뷰(−2)는 빼고, 중립(0)은 좋아한 책 그대로 둔다.
    weights = compute.book_weights([1, 2], {1: -2, 2: 0}, {1})

    assert weights == {2: 2}


def test_사고_나서_싫다고_한_책도_뺀다() -> None:
    # 구매(3)와 1점 리뷰(−2) — 이력 가중치는 큰 값 3 이지만 싫다는 신호가 이긴다.
    weights = compute.book_weights([], {1: 3, 2: 3}, {1})

    assert weights == {2: 3}


def test_라벨은_몇_개를_고르든_비중_합이_같다() -> None:
    one = compute.label_parts([[1.0, 0.0]])
    three = compute.label_parts([[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]])

    assert [w for _, w in one] == [compute.LABELS_TOTAL_WEIGHT]
    assert sum(w for _, w in three) == pytest.approx(compute.LABELS_TOTAL_WEIGHT)
    assert len({w for _, w in three}) == 1


def test_고른_라벨이_없으면_재료도_없다() -> None:
    assert compute.label_parts([]) == []


def test_태그_가중치는_온보딩_태그와_카테고리_점수를_합친다() -> None:
    weights = compute.tag_weights(["힐링", "에세이"], [], {"에세이": 3, "기타": -2})

    assert weights == {"힐링": 1, "에세이": 4, "기타": -2}


def test_온보딩_카테고리는_카탈로그_분류로_풀어_이력_점수에_더한다() -> None:
    # 여행은 지리(핵심)로 풀린다. 이름 그대로("여행")는 넣지 않는다 — ④는 책의 분류로 찾는다.
    weights = compute.tag_weights([], ["여행"], {"지리": 3})

    assert weights == {"지리": 3 + compute.ONBOARDING_CORE_WEIGHT}


def test_합이_0인_태그는_뺀다() -> None:
    assert compute.tag_weights([], [], {"에세이": 0}) == {}


def _profile(centroid=None, tags=None, cold_start=False) -> Profile:
    return Profile(
        centroid=[1.0, 0.0] if centroid is None else centroid,
        tag_weights={"힐링": 1} if tags is None else tags,
        cold_start=cold_start,
    )


def test_처음_만들면_판_번호는_1이다() -> None:
    assert compute.next_version(None, _profile()) == 1


def test_바뀐_게_없으면_판_번호는_그대로다() -> None:
    assert compute.next_version((_profile(), 4), _profile()) == 4


@pytest.mark.parametrize(
    "changed",
    [
        _profile(centroid=[0.0, 1.0]),
        _profile(tags={"힐링": 2}),
        _profile(cold_start=True),
    ],
)
def test_순위에_영향을_주는_값이_바뀌면_판_번호가_오른다(changed: Profile) -> None:
    assert compute.next_version((_profile(), 4), changed) == 5


def test_저장_정밀도_차이만_있으면_바뀐_것으로_보지_않는다() -> None:
    # DB 는 4바이트 실수로 저장해 읽어 오면 끝자리가 다르다. 이걸 바뀐 것으로 보면 호출마다 판이 오른다.
    stored = _profile(centroid=[0.1, 0.2])
    computed = _profile(centroid=[0.1 + 1e-12, 0.2])

    assert compute.next_version((stored, 4), computed) == 4
