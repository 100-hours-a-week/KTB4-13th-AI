"""⑤ 대화에서 취향 사실을 뽑는다.

순서: LLM 이 대화에서 후보를 낸다(prompts.py) → 코드가 거른다(rules.py) → 남은 것에 벡터를 붙인다.
AI 는 입력도 결과도 저장하지 않는다. 뽑은 취향은 BE 가 저장한다(명세 ⑤).
대화와 뽑은 문장은 로그에 남기지 않는다. 개수만 남긴다.
"""

import logging
from typing import Any

from app.core.config import get_settings
from app.extraction import prompts, rules
from app.extraction.schemas import ExtractionRequest
from app.gateway import embedding
from app.gateway.llm import (
    LLMUnavailableError,
    get_chat_model,
    invoke_chain,
    message_text,
    parse_json_response,
)

logger = logging.getLogger(__name__)

# 명세 ⑤ 각주: 취향 문장의 벡터는 ②의 purpose: query 와 같은 방식으로 만든다. 책 벡터와 견주는 쪽이라서다.
# 임베딩은 모르는 용도를 오류 없이 문서용으로 처리하므로, 값이 틀려도 조용히 다른 벡터가 나온다.
MEMORY_VECTOR_PURPOSE = "query"


class EmbeddingUnavailableError(Exception):
    """취향 문장을 벡터로 바꾸지 못했다. 호출부(라우터)가 503 으로 답한다(명세 ⑤).

    메시지에는 원인의 종류만 담는다. 원래 메시지에는 취향 문장이 실려 올 수 있다.
    """


async def extract(req: ExtractionRequest) -> list[dict[str, Any]]:
    """뽑은 취향 사실의 목록. 항목의 모양은 명세 ⑤의 `extractions[]` 다.

    LLM 장애는 LLMUnavailableError(시간 초과는 LLMTimeoutError), 임베딩 장애는
    EmbeddingUnavailableError 로 올라간다.
    """
    chain = (
        prompts.EXTRACTION_PROMPT
        # ⑤만 쓰는 모델. 설정이 비어 있으면 None 을 넘겨 챗봇과 같은 모델을 쓴다
        | get_chat_model(model_id=get_settings().extraction_llm_model_id or None)
        | message_text
        | parse_json_response
    )
    llm_output = await invoke_chain(chain, prompts.inputs(req))

    raw = llm_output.get("extractions")
    if not isinstance(raw, list):
        # 약속한 모양이 아닌 답을 "뽑은 것 없음"으로 넘기면 BE 는 이 대화를 처리한 것으로 보고 취향을
        # 잃는다. JSON 이 아닌 답(parse_json_response)과 같은 LLM 장애로 다뤄 나중에 다시 부르게 한다.
        raise LLMUnavailableError("LLM 답에 extractions 목록이 없음")

    extractions = await to_extractions(req, llm_output)
    logger.info(
        "취향 기억 추출(프롬프트 %s): LLM 이 낸 후보 %d, 응답 %d",
        prompts.PROMPT_VERSION,
        len(raw),
        len(extractions),
    )
    return extractions


async def to_extractions(
    req: ExtractionRequest, llm_output: Any
) -> list[dict[str, Any]]:
    """LLM 의 답을 걸러서 벡터를 붙이고, 명세 ⑤의 `extractions[]` 모양으로 만든다."""
    # 근거로 쓸 수 있는 것은 사용자가 한 말뿐이다. 챗봇의 말은 넘기지 않는다
    said = [turn.text for turn in req.used_conversation() if turn.role == "user"]
    candidates = rules.screen(llm_output, req.existing_preferences, said)
    if not candidates:
        # 뽑은 것이 없는 대화가 흔하다. 그때는 임베딩 모델을 부르지 않는다
        return []

    try:
        vectors, dim, _model = await embedding.embed(
            [candidate.value for candidate in candidates], MEMORY_VECTOR_PURPOSE
        )
    except Exception as exc:
        # 임베딩은 실패를 한 가지 예외로 모아 주지 않는다(②의 라우터도 Exception 으로 받는다).
        # 임베딩 호출 한 줄만 감싸서, 이 함수의 다른 버그가 "임베딩 장애"로 가려지지 않게 한다.
        raise EmbeddingUnavailableError(type(exc).__name__) from exc
    return [
        {
            "type": candidate.type,
            "value": candidate.value,
            "confidence": candidate.confidence,
            "vector": vector,
            # 설정값이 아니라 실제로 만든 벡터의 길이다. BE 가 자기 색인의 차원과 대조한다
            "dim": dim,
            "source_conversation_id": req.conversation_id,
        }
        for candidate, vector in zip(candidates, vectors, strict=True)
    ]
