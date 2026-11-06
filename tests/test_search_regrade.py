"""새 라벨 사람 재채점(evals/searchlab/regrade.py) 테스트. DB 없이 돈다."""

import pytest

from evals.searchlab import regrade


def test_출처_파일_이름에서_비중을_읽고_기준선은_3대1이다() -> None:
    assert regrade.weight_of("m060-b1000-w13-screen") == "13"
    assert regrade.weight_of("base-rel") == "31"


def test_책이_나온_칸의_비중을_모아_묶음을_만든다() -> None:
    sources = [
        {"d-1": {"c01": {"isbn": "a", "ranks": {"m060-b500-w31-rel": 3}}}},
        {
            "d-1": {
                "c01": {"isbn": "a", "ranks": {"m060-b500-w13-rel": 5}},
                "c02": {"isbn": "b", "ranks": {"base-screen": 1}},
            }
        },
    ]
    assert regrade.groups(sources) == {("d-1", "a"): "13+31", ("d-1", "b"): "31"}


def test_묶음마다_최소_권수를_주고_전체는_정한_권수다() -> None:
    grouped = {("d", f"a{i}"): "13" for i in range(5)}
    grouped |= {("d", f"b{i}"): "31" for i in range(300)}
    picked = regrade.pick(grouped, size=100, least=10)
    assert len(picked) == 100 and len(set(picked)) == 100
    assert sum(isbn.startswith("a") for _, isbn in picked) == 5
    assert picked == regrade.pick(grouped, size=100, least=10)


def test_시트에는_점수도_출처도_없고_체크한_점수를_읽는다() -> None:
    book = {"title": "별", "author": "가", "category": "문학", "intro": "소개"}
    text, key = regrade.sheet([("d-1", "a")], {"d-1": "별 이야기"}, {"a": book})
    assert key == {"r001": {"case": "d-1", "isbn": "a"}}
    assert regrade.read_marks(text) == {}
    marked = text.replace("- [ ] 1 조금", "- [x] 1 조금")
    assert regrade.read_marks(marked) == {"r001": 1}
    twice = marked.replace("- [ ] 2 맞음", "- [x] 2 맞음")
    with pytest.raises(ValueError, match="둘 이상"):
        regrade.read_marks(twice)


def test_가중_카파() -> None:
    assert regrade.weighted_kappa([0, 1, 2], [0, 1, 2]) == 1.0
    # 점수가 서로 독립이면 0 이다
    assert regrade.weighted_kappa([0, 0, 2, 2], [0, 2, 0, 2]) == pytest.approx(0.0)
    # 1 차이는 2 차이보다 덜 깎인다
    close = regrade.weighted_kappa([0, 1, 2, 2], [0, 2, 2, 2])
    far = regrade.weighted_kappa([0, 0, 2, 2], [0, 2, 2, 2])
    assert close > far


def test_일치도는_세션_점수와_사람_점수를_견주고_문턱_밑이면_보류한다() -> None:
    key = {f"r{i}": {"case": "d", "isbn": str(i)} for i in range(4)}
    labels = {"d": {"0": 0, "1": 1, "2": 2, "3": 2}}
    same = regrade.score({"r0": 0, "r1": 1, "r2": 2, "r3": 2}, key, labels)
    assert same["가중 κ"] == 1.0 and same["서술형 판정"] == "진행"
    apart = regrade.score({"r0": 2, "r1": 1, "r2": 0, "r3": 0}, key, labels)
    assert apart["서술형 판정"] == "보류"
