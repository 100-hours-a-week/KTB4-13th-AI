"""⑤ LLM 의 답을 응답의 extractions[] 로 만드는 부분(app/extraction/service.py) 테스트(#369).

LLM 도 임베딩 모델도 쓰지 않는다. 임베딩은 문장마다 다른 벡터를 주는 가짜로 바꿔서, 벡터가 자기
문장에 붙었는지를 본다.
"""

import asyncio

import pytest

from app.extraction import service
from app.extraction.schemas import ExtractionRequest, parse_request

_DIM = 3


def _vector(text: str) -> list[float]:
    # 문장마다 다른 값. 벡터가 다른 문장에 붙으면 테스트가 알아챈다
    return [float(len(text)), float(ord(text[0])), float(ord(text[-1]))]


@pytest.fixture
def embed_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str]]:
    calls: list[tuple[list[str], str]] = []

    async def _fake(texts: list[str], purpose: str):
        calls.append((list(texts), purpose))
        return [_vector(text) for text in texts], _DIM, "fake-model"

    monkeypatch.setattr(service.embedding, "embed", _fake)
    return calls


def _request(existing=()) -> ExtractionRequest:
    req = parse_request(
        {
            "user_id": 1,
            "consented": True,
            "conversation": [{"role": "user", "text": "잔잔한 소설 추천해줘"}],
            "conversation_id": "cv_1",
            "existing_preferences": [
                {"type": type_, "value": value} for type_, value in existing
            ],
        }
    )
    assert req is not None
    return req


def _item(type_="mood", value="잔잔한 소설을 찾음", confidence=0.8) -> dict:
    return {"type": type_, "value": value, "confidence": confidence}


def _run(llm_output, req=None) -> list[dict]:
    return asyncio.run(service.to_extractions(req or _request(), llm_output))


def test_후보마다_명세의_여섯_칸으로_나온다(embed_calls) -> None:
    result = _run({"extractions": [_item()]})

    assert result == [
        {
            "type": "mood",
            "value": "잔잔한 소설을 찾음",
            "confidence": 0.8,
            "vector": _vector("잔잔한 소설을 찾음"),
            "dim": _DIM,
            "source_conversation_id": "cv_1",
        }
    ]
    # 응답에 나가는 칸의 순서도 명세의 예시와 같게 둔다
    assert list(result[0]) == [
        "type",
        "value",
        "confidence",
        "vector",
        "dim",
        "source_conversation_id",
    ]


def test_벡터는_책_벡터와_견주는_쪽의_용도로_한_번에_만든다(embed_calls) -> None:
    _run(
        {
            "extractions": [
                _item(value="잔잔한 소설을 찾음", confidence=0.6),
                _item(
                    type_="topic", value="기후 위기를 다룬 책을 찾음", confidence=0.9
                ),
            ]
        }
    )

    # 용도가 틀리면 임베딩 쪽은 오류 없이 문서용 벡터를 만든다. 그래서 여기서 값을 고정한다
    assert embed_calls == [
        (["기후 위기를 다룬 책을 찾음", "잔잔한 소설을 찾음"], "query")
    ]


def test_벡터는_자기_문장에_붙는다(embed_calls) -> None:
    result = _run(
        {
            "extractions": [
                _item(value="잔잔한 소설을 찾음", confidence=0.6),
                _item(
                    type_="topic", value="기후 위기를 다룬 책을 찾음", confidence=0.9
                ),
                _item(type_="author", value="김초엽 작가를 좋아함", confidence=0.7),
            ]
        }
    )

    assert [item["value"] for item in result] == [
        "기후 위기를 다룬 책을 찾음",
        "김초엽 작가를 좋아함",
        "잔잔한 소설을 찾음",
    ]
    assert all(item["vector"] == _vector(item["value"]) for item in result)


def test_벡터_길이는_임베딩이_돌려준_값을_쓴다(embed_calls) -> None:
    # 설정값을 복창하면 모델이 바뀌어도 BE 가 알아채지 못한다(명세 ②와 같은 이유)
    assert [item["dim"] for item in _run({"extractions": [_item()]})] == [_DIM]


def test_규칙에_걸린_후보는_벡터를_만들지_않는다(embed_calls) -> None:
    result = _run({"extractions": [_item(), _item(type_="genre", value="버려질 것")]})

    assert [item["value"] for item in result] == ["잔잔한 소설을 찾음"]
    assert embed_calls == [(["잔잔한 소설을 찾음"], "query")]


def test_이미_저장된_취향과_글자가_같은_후보는_나가지_않는다(embed_calls) -> None:
    result = _run(
        {
            "extractions": [
                _item(value="잔잔한 소설을 찾음"),
                _item(type_="topic", value="기후 위기를 다룬 책을 찾음"),
            ]
        },
        _request(existing=[("mood", "잔잔한 소설을 찾음.")]),
    )

    assert [item["value"] for item in result] == ["기후 위기를 다룬 책을 찾음"]


@pytest.mark.parametrize(
    "llm_output",
    [
        pytest.param({"extractions": []}, id="뽑은_것_없음"),
        pytest.param(None, id="답이_깨짐"),
        pytest.param({"extractions": [_item(confidence=0.2)]}, id="전부_규칙에_걸림"),
    ],
)
def test_남은_후보가_없으면_임베딩을_부르지_않는다(embed_calls, llm_output) -> None:
    assert _run(llm_output) == []
    assert embed_calls == []


def test_남은_후보가_전부_이미_있는_취향이어도_임베딩을_부르지_않는다(
    embed_calls,
) -> None:
    result = _run(
        {"extractions": [_item()]}, _request(existing=[("mood", "잔잔한 소설을 찾음")])
    )

    assert result == []
    assert embed_calls == []
