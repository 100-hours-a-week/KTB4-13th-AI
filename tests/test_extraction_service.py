"""⑤ LLM 의 답을 응답의 extractions[] 로 만드는 부분(app/extraction/service.py) 테스트(#369).

LLM 도 임베딩 모델도 쓰지 않는다. 임베딩은 문장마다 다른 벡터를 주는 가짜로 바꿔서, 벡터가 자기
문장에 붙었는지를 본다.
"""

import asyncio
import json

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.extraction import service
from app.extraction.schemas import ExtractionRequest, parse_request
from app.gateway.llm import LLMTimeoutError, LLMUnavailableError

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


def _item(type_="mood", value="잔잔한 소설을 찾음", confidence=0.8, **over) -> dict:
    """LLM 이 낸 후보 하나. 근거는 _request() 의 사용자 말에서 옮긴 것으로 채운다."""
    item = {
        "type": type_,
        "value": value,
        "confidence": confidence,
        "evidence": "잔잔한 소설 추천해줘",
        "owner": "user",
        "basis": "stated",
    }
    item.update(over)
    return item


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


def test_이미_저장된_취향과_글자가_같아도_다시_낸다(embed_calls) -> None:
    # 같은 취향을 다른 날 또 말하면 또 내서, 자주 말한 취향이 무거워지게 한다(#425)
    result = _run(
        {
            "extractions": [
                _item(value="잔잔한 소설을 찾음"),
                _item(type_="topic", value="기후 위기를 다룬 책을 찾음"),
            ]
        },
        _request(existing=[("mood", "잔잔한 소설을 찾음.")]),
    )

    assert [item["value"] for item in result] == [
        "잔잔한 소설을 찾음",
        "기후 위기를 다룬 책을 찾음",
    ]


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


# --- LLM 을 불러 뽑기 (#373). 가짜 모델로 돈다 ---


def _use_model(monkeypatch: pytest.MonkeyPatch, reply) -> list:
    """get_chat_model 을 가짜로 바꾼다. reply 가 예외면 던지고, 아니면 그 글로 답한다.

    돌려주는 목록에는 LLM 이 받은 메시지 묶음이 호출마다 하나씩 쌓인다.
    """
    seen: list = []

    def _answer(prompt) -> AIMessage:
        seen.append(prompt.to_messages())
        if isinstance(reply, Exception):
            raise reply
        return AIMessage(content=reply)

    monkeypatch.setattr(
        service, "get_chat_model", lambda **_kwargs: RunnableLambda(_answer)
    )
    return seen


def _extract(req=None) -> list[dict]:
    return asyncio.run(service.extract(req or _request()))


def _answer_json(*items) -> str:
    return json.dumps({"extractions": list(items)}, ensure_ascii=False)


def test_LLM이_뽑은_취향이_벡터와_함께_나온다(monkeypatch, embed_calls) -> None:
    _use_model(monkeypatch, _answer_json(_item()))

    assert _extract() == [
        {
            "type": "mood",
            "value": "잔잔한 소설을 찾음",
            "confidence": 0.8,
            "vector": _vector("잔잔한 소설을 찾음"),
            "dim": _DIM,
            "source_conversation_id": "cv_1",
        }
    ]


def test_LLM을_한_번만_부르고_대화만_넘긴다(monkeypatch, embed_calls) -> None:
    seen = _use_model(monkeypatch, _answer_json())

    _extract(_request(existing=[("topic", "기후 위기를 다룬 책을 찾음")]))

    assert len(seen) == 1
    human = seen[0][-1].content
    assert "잔잔한 소설 추천해줘" in human
    assert "기후 위기를 다룬 책을 찾음" not in human


def test_LLM의_답도_규칙으로_거른다(monkeypatch, embed_calls) -> None:
    _use_model(
        monkeypatch,
        _answer_json(_item(), _item(type_="genre", value="없는 종류"), _item()),
    )

    assert [item["value"] for item in _extract()] == ["잔잔한 소설을 찾음"]


def test_LLM이_뽑을_것이_없다고_하면_빈_목록이다(monkeypatch, embed_calls) -> None:
    _use_model(monkeypatch, _answer_json())

    assert _extract() == []
    assert embed_calls == []


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param('{"mock": true}', id="extractions_없음"),
        pytest.param('{"extractions": null}', id="extractions_null"),
        pytest.param('{"extractions": "없음"}', id="extractions_가_글자"),
        pytest.param('{"preferences": []}', id="칸_이름이_다름"),
        pytest.param("뽑을 취향이 없습니다.", id="JSON이_아님"),
    ],
)
def test_LLM의_답이_약속한_모양이_아니면_LLM_장애다(
    monkeypatch, embed_calls, reply
) -> None:
    # "뽑은 것 없음"으로 넘기면 BE 는 이 대화를 처리한 것으로 보고 취향을 잃는다
    _use_model(monkeypatch, reply)

    with pytest.raises(LLMUnavailableError):
        _extract()


def test_LLM이_제한_시간_안에_답하지_않으면_LLMTimeoutError다(
    monkeypatch, embed_calls
) -> None:
    request = httpx.Request("POST", "http://localhost/v1")
    _use_model(monkeypatch, openai.APITimeoutError(request=request))

    with pytest.raises(LLMTimeoutError):
        _extract()


def test_임베딩이_실패하면_EmbeddingUnavailableError다(monkeypatch) -> None:
    _use_model(monkeypatch, _answer_json(_item()))

    async def _fail(texts, purpose):
        raise RuntimeError("잔잔한 소설을 찾음 을 벡터로 못 바꿈")

    monkeypatch.setattr(service.embedding, "embed", _fail)

    with pytest.raises(service.EmbeddingUnavailableError) as exc_info:
        _extract()

    # 원래 메시지에는 취향 문장이 실려 올 수 있다. 종류만 남긴다
    assert "잔잔한 소설" not in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)


def test_기록에는_개수만_남고_대화와_뽑은_문장은_남지_않는다(
    monkeypatch, embed_calls, caplog
) -> None:
    _use_model(
        monkeypatch, _answer_json(_item(), _item(type_="genre", value="없는 종류"))
    )

    with caplog.at_level("DEBUG"):
        _extract(_request(existing=[("topic", "기후 위기를 다룬 책을 찾음")]))

    assert "LLM 이 낸 후보 2, 응답 1" in caplog.text
    for secret in ("잔잔한 소설", "없는 종류", "기후 위기", "추천해줘"):
        assert secret not in caplog.text


def test_챗봇이_한_말을_근거로_든_취향은_나가지_않는다(
    monkeypatch, embed_calls
) -> None:
    # 근거로 쓸 수 있는 것은 사용자가 한 말뿐이다. 챗봇의 말은 규칙에 넘기지도 않는다
    req = parse_request(
        {
            "user_id": 1,
            "consented": True,
            "conversation": [
                {"role": "user", "text": "불편한 편의점 재고 있어?"},
                {
                    "role": "assistant",
                    "text": "네, 힐링 소설을 즐겨 읽는 분들께 인기예요.",
                },
            ],
            "conversation_id": "cv_1",
        }
    )
    _use_model(
        monkeypatch,
        _answer_json(
            _item(
                value="힐링 소설을 즐겨 읽음",
                evidence="힐링 소설을 즐겨 읽는 분들께 인기예요",
            )
        ),
    )

    assert _extract(req) == []


def test_남의_취향이라고_적은_것은_나가지_않는다(monkeypatch, embed_calls) -> None:
    _use_model(
        monkeypatch,
        _answer_json(_item(owner="other"), _item(type_="topic", value="시집을 찾음")),
    )

    assert [item["value"] for item in _extract()] == ["시집을 찾음"]


def test_세_칸은_응답에_나가지_않는다(monkeypatch, embed_calls) -> None:
    # 서버 안에서 거르는 데만 쓴다. 응답은 명세 ⑤의 여섯 칸 그대로다
    _use_model(monkeypatch, _answer_json(_item()))

    assert set(_extract()[0]) == {
        "type",
        "value",
        "confidence",
        "vector",
        "dim",
        "source_conversation_id",
    }


# --- ⑤만 쓰는 모델 (#373) ---


def _model_asked(monkeypatch: pytest.MonkeyPatch) -> dict:
    """extract() 가 get_chat_model 에 넘긴 인자를 본다."""
    asked: dict = {}

    def _factory(**kwargs):
        asked.update(kwargs)
        return RunnableLambda(lambda _prompt: AIMessage(content=_answer_json()))

    monkeypatch.setattr(service, "get_chat_model", _factory)
    _extract()
    return asked


def test_EXTRACTION_LLM_MODEL_ID를_정하면_그_모델로_부른다(
    monkeypatch, embed_calls
) -> None:
    # 챗봇과 같은 모델을 쓰면 운영(gpt-4o)에서는 평가에 쓴 모델과 다른 모델로 돈다. ⑤만 따로 고른다
    monkeypatch.setenv("EXTRACTION_LLM_MODEL_ID", "openai/gpt-4o-mini")

    assert _model_asked(monkeypatch) == {"model_id": "openai/gpt-4o-mini"}


def test_EXTRACTION_LLM_MODEL_ID가_비어_있으면_공용_모델을_쓴다(
    monkeypatch, embed_calls
) -> None:
    monkeypatch.setenv("EXTRACTION_LLM_MODEL_ID", "")

    # None 이면 게이트웨이가 LLM_MODEL_ID 를 쓴다
    assert _model_asked(monkeypatch) == {"model_id": None}


def test_답을_JSON으로_고정하는_설정은_건드리지_않는다(
    monkeypatch, embed_calls
) -> None:
    # json_mode 를 끄면 모델이 JSON 이 아닌 글로 답할 수 있다. ⑤는 기본값(켬)을 쓴다
    assert "json_mode" not in _model_asked(monkeypatch)
