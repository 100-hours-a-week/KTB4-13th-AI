"""LLM 게이트웨이 단위 테스트.

실제 LLM(Ollama/상용 API)을 호출하는 테스트는 넣지 않는다 — 네트워크·모델
상태에 따라 결과가 흔들려서 CI에 안 맞는다. parse_json_response 는 순수
로직이라 여기서 검증하고, complete() 자체의 동작은 수동으로 확인한다.
"""

import pytest

from app.gateway.llm import LLMUnavailableError, parse_json_response


def test_순수_json이면_그대로_파싱한다() -> None:
    assert parse_json_response('{"mood": "잔잔함"}') == {"mood": "잔잔함"}


def test_마크다운_코드블록으로_감싸도_벗겨서_파싱한다() -> None:
    # 소형 로컬 모델이 ```json ... ``` 형태로 감싸서 주는 경우가 있다.
    raw = '```json\n{"mood": "잔잔함"}\n```'
    assert parse_json_response(raw) == {"mood": "잔잔함"}


def test_앞뒤에_설명_문장이_붙어도_json만_뽑아낸다() -> None:
    raw = '물론이죠! 요청하신 결과는 다음과 같습니다:\n{"mood": "잔잔함"}\n감사합니다.'
    assert parse_json_response(raw) == {"mood": "잔잔함"}


def test_json이_없으면_LLMUnavailableError를_던진다() -> None:
    with pytest.raises(LLMUnavailableError):
        parse_json_response("죄송하지만 답변을 드릴 수 없습니다.")


def test_중괄호는_있지만_형식이_깨지면_LLMUnavailableError를_던진다() -> None:
    with pytest.raises(LLMUnavailableError):
        parse_json_response('{"mood": 잔잔함 (따옴표 없음)}')
