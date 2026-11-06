"""#556 B4 격자(evals/searchlab/b4.py) 테스트. DB 없이 돈다."""

from pathlib import Path

from evals.searchlab import b4


def test_격자는_45칸에_선이_문턱_밑인_18칸을_더한다() -> None:
    cells = b4.cells()
    assert len(cells) == 45 + 18
    assert len({c.name for c in cells}) == len(cells)
    lowered = [c for c in cells if c.lowered]
    assert {c.coverage for c in lowered} == {0.5, 0.55}


def test_칸_이름과_인자() -> None:
    cell = b4.Cell(0.55, 1000, 1, 3, lowered=True)
    assert cell.name == "m055t-b1000-w13"
    args = cell.args()
    assert args[:2] == ["--set", "min_coverage=0.55"]
    assert args[-2:] == ["--trgm-threshold", "0.55"]
    assert "--trgm-threshold" not in b4.Cell(0.6, 1000, 3, 1).args()


def test_이미_있는_파일은_건너뛰고_관련도순은_단계를_남긴다(tmp_path: Path) -> None:
    cell = b4.Cell(0.6, 1000, 3, 1)
    (tmp_path / f"{cell.name}-screen.json").write_text("{}")
    todo = b4.jobs(tmp_path, [cell])
    assert [path.name for path, _ in todo] == [f"{cell.name}-rel.json"]
    assert "--trace" in todo[0][1]
