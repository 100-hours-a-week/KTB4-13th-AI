"""평가 결과를 짝지어 판정할 때 쓰는 계산(evals/stats.py, #752). DB·LLM 없이 돈다."""

import pytest

from evals import stats


def test_차이가_모두_0이면_구간도_0이다() -> None:
    assert stats.bootstrap([0.0, 0.0, 0.0], seed=1) == stats.Interval(
        0.0, 0.0, 0.0, 1.0
    )


def test_차이가_모두_같으면_구간은_그_값_하나다() -> None:
    gap = stats.bootstrap([0.2] * 30, seed=1)
    assert gap.mean == pytest.approx(0.2)
    assert gap.low == pytest.approx(0.2)
    assert gap.high == pytest.approx(0.2)
    assert gap.p == 0.0


def test_짝지은_사례가_없으면_멈춘다() -> None:
    with pytest.raises(ValueError):
        stats.bootstrap([], seed=1)


def test_시드가_같으면_같은_구간이_나온다() -> None:
    diffs = [0.3, -0.1, 0.2, 0.0, 0.5, -0.2, 0.1, 0.0, 0.05, -0.05]
    assert stats.bootstrap(diffs, seed=7) == stats.bootstrap(diffs, seed=7)


def test_V3_판정과_같은_계산이다() -> None:
    # V3 브랜치의 판정 코드(검색 시드 556)에 같은 값을 넣어 나온 숫자다. numpy 의 난수 뽑는 법이 바뀌면 여기서 걸린다
    gap = stats.bootstrap([0.3, -0.1, 0.2, 0.0, 0.5, -0.2, 0.1, 0.0], seed=556)
    assert gap.mean == pytest.approx(0.1)
    assert gap.low == pytest.approx(-0.0375)
    assert gap.high == pytest.approx(0.25)
    assert gap.p == pytest.approx(0.2206)


def test_대부분_좋아지면_구간이_0보다_위고_반반이면_0을_걸친다() -> None:
    better = stats.bootstrap([0.1] * 40 + [-0.05] * 10, seed=1)
    assert better.low > 0
    mixed = stats.bootstrap([0.1, -0.1] * 25, seed=1)
    assert mixed.low < 0 < mixed.high


@pytest.mark.parametrize(
    ("lost", "gained", "p"),
    [
        (0, 0, 1.0),
        (1, 1, 1.0),
        (6, 0, 2 / 64),
        (5, 0, 2 / 32),
        (8, 2, 2 * (1 + 10 + 45) / 1024),
    ],
)
def test_McNemar_는_한쪽만_맞힌_사례로_양쪽_p_를_낸다(
    lost: int, gained: int, p: float
) -> None:
    assert stats.mcnemar(lost, gained) == pytest.approx(p)
    assert stats.mcnemar(gained, lost) == pytest.approx(p)


def test_McNemar_는_음수_사례_수를_받지_않는다() -> None:
    with pytest.raises(ValueError):
        stats.mcnemar(-1, 3)


def test_Holm_은_작은_p_부터_보고_처음_못_넘으면_뒤는_모두_못_넘는다() -> None:
    # 0.01 < 0.05/3 은 넘고, 다음 0.03 은 0.05/2 = 0.025 를 못 넘으니 0.04 도 못 넘는다
    assert stats.holm({"가": 0.01, "나": 0.04, "다": 0.03}) == {
        "가": True,
        "나": False,
        "다": False,
    }
    assert stats.holm({"가": 0.01, "나": 0.02}) == {"가": True, "나": True}
    assert stats.holm({"가": 0.06}) == {"가": False}


def test_Judged_가_어느_쪽이든_문턱_밑이면_라벨을_보충한다() -> None:
    assert stats.enough_labels(0.95, 0.91)
    assert stats.enough_labels(0.9, 0.9)
    assert not stats.enough_labels(0.95, 0.898)
    with pytest.raises(ValueError):
        stats.enough_labels()
