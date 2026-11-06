"""#556 B4·C2·C3 원리 확인(evals/searchlab/grid_checks.py) 테스트. DB 없이 돈다."""

from evals.searchlab import grid_checks


def _row(**trace) -> dict:
    return {"id": "x", "kind": "author", "trace": trace}


def test_판별_r1_은_넓히기_경로이고_제목이_똑같은_책이_없으면_서술형이다() -> None:
    assert grid_checks.is_descriptive(_row(each_word=True), "한강", "r1")
    assert not grid_checks.is_descriptive(
        _row(each_word=True, exact_ids=[1]), "한강", "r1"
    )
    assert not grid_checks.is_descriptive(_row(), "한강", "r1")


def test_판별_r2_는_낱말이_둘_이상일_때만_서술형이다() -> None:
    assert not grid_checks.is_descriptive(_row(each_word=True), "한강", "r2")
    assert grid_checks.is_descriptive(_row(each_word=True), "따뜻한 소설", "r2")


def test_같은_등수의_키워드_책과_벡터_책은_1대1_에서_동점이다() -> None:
    row = _row(keyword_ids=[1, 2], vector_ids=[3, 4], exact_ids=[])
    order, scores = grid_checks.fused(row, (1.0, 1.0))
    assert order == [1, 3, 2, 4]
    assert scores[1] == scores[3] and scores[2] == scores[4]
