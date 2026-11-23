"""서술형 라벨 사람 재채점(evals/search/regrade.py) 테스트(#751). DB 없이 돈다."""

import json
from pathlib import Path

import pytest

from evals.search import label, regrade

BOOK = {"title": "별", "author": "가", "category": "문학", "intro": "소개"}


def test_시트에는_점수도_묶음도_없고_체크한_점수를_읽는다() -> None:
    picked = [{"case": "d-1", "isbn": "a", "pool": "처음"}]
    text, key = regrade.sheet(picked, {"d-1": "별 이야기"}, {"a": BOOK}, prefix="b")
    assert key == {"b001": {"case": "d-1", "isbn": "a", "pool": "처음"}}
    assert "처음" not in text
    assert regrade.read_marks(text) == {}
    marked = text.replace("- [ ] 1 조금", "- [x] 1 조금")
    assert regrade.read_marks(marked) == {"b001": 1}
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
    # 손으로 셈한 값: 관찰 불일치 0.25, 기대 불일치 0.5
    assert regrade.weighted_kappa([0, 0, 2, 2], [0, 1, 2, 1]) == pytest.approx(0.5)


def _sheet(pool: str, grades: dict[str, int]) -> tuple[dict, dict]:
    key = {
        f"{pool}{n}": {"case": "d", "isbn": isbn, "pool": pool}
        for n, isbn in enumerate(grades)
    }
    marks = {number: grades[item["isbn"]] for number, item in key.items()}
    return marks, key


def test_묶음별로_세션과_견주고_묶음_크기로_가중해_합친다() -> None:
    labels = {"d": {"0": 0, "1": 1, "2": 2, "3": 2, "4": 0, "5": 2}}
    same = _sheet("처음", {"0": 0, "1": 1, "2": 2})
    apart = _sheet("나중", {"3": 0, "4": 2, "5": 2})
    result = regrade.score([same, apart], labels, {"처음": 300, "나중": 100})
    first = result["묶음"]["처음"]["가중 κ"]
    later = result["묶음"]["나중"]["가중 κ"]
    assert first == 1.0
    assert result["묶음"]["나중"]["2점과 0점으로 갈린 수"] == 2
    assert result["합친 가중 κ"] == pytest.approx((300 * first + 100 * later) / 400)
    assert result["다시"] is None


def test_합친_값이_문턱_밑이면_서술형_판정을_보류한다() -> None:
    labels = {"d": {"0": 0, "1": 1, "2": 2, "3": 2}}
    apart = _sheet("처음", {"0": 2, "1": 1, "2": 0, "3": 0})
    assert regrade.score([apart], labels, {"처음": 838})["서술형 판정"] == "보류"
    same = _sheet("처음", {"0": 0, "1": 1, "2": 2, "3": 2})
    assert regrade.score([same], labels, {"처음": 838})["서술형 판정"] == "진행"


def test_다시_매긴_시트는_처음_매긴_사람_점수와_견주고_합친_값에_넣지_않는다() -> None:
    labels = {"d": {"0": 0, "1": 2}}
    first = _sheet("처음", {"0": 0, "1": 2})
    again = _sheet("다시", {"0": 1, "1": 2})
    result = regrade.score([first, again], labels, {"처음": 838})
    assert set(result["묶음"]) == {"처음"}
    assert result["합친 가중 κ"] == 1.0
    assert result["다시"]["매긴 수"] == 2
    assert result["다시"]["표"] == {(0, 1): 1, (2, 2): 1}


def test_묶음_크기는_라벨_파일의_채점_기록에서_읽는다() -> None:
    sizes = regrade.pool_sizes(
        label.load_labels(), label.load_labels(regrade.CONFIRM_LABELS_PATH)
    )
    assert sizes == {"처음": 838, "나중": 579, "확인용": 507}


def test_두_라벨_파일을_합쳐도_사례가_겹치지_않고_검색어가_모두_있다() -> None:
    merged = regrade.load_labels()
    assert len(merged) == 80
    assert set(merged) <= set(regrade.queries())


def test_열쇠에_묶음이_없으면_셋째_값으로_주고_모르는_묶음은_막는다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "key.json"
    path.write_text(json.dumps({"r001": {"case": "d", "isbn": "a"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="묶음이 없다"):
        regrade._key(path, None)
    assert regrade._key(path, "나중")["r001"]["pool"] == "나중"
    with pytest.raises(ValueError, match="모르는 묶음"):
        regrade._key(path, "격자")
