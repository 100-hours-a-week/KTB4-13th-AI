"""⑤ 대화에서 취향 사실을 뽑는다.

지금은 LLM 을 부르지 않아 extract() 는 항상 빈 목록이다. LLM 의 답을 응답 모양으로 만드는 뒷부분
(to_extractions)만 있고, 프롬프트와 LLM 호출은 #299 의 뒤 단계에서 잇는다.
AI 는 입력도 결과도 저장하지 않는다. 뽑은 취향은 BE 가 저장한다(명세 ⑤).
"""

from typing import Any

from app.extraction import rules
from app.extraction.schemas import ExtractionRequest
from app.gateway import embedding

# 명세 ⑤ 각주: 취향 문장의 벡터는 ②의 purpose: query 와 같은 방식으로 만든다. 책 벡터와 견주는 쪽이라서다.
# 임베딩은 모르는 용도를 오류 없이 문서용으로 처리하므로, 값이 틀려도 조용히 다른 벡터가 나온다.
MEMORY_VECTOR_PURPOSE = "query"


async def extract(req: ExtractionRequest) -> list[dict[str, Any]]:
    """뽑은 취향 사실의 목록. 항목의 모양은 명세 ⑤의 `extractions[]` 다."""
    return []


async def to_extractions(
    req: ExtractionRequest, llm_output: Any
) -> list[dict[str, Any]]:
    """LLM 의 답을 걸러서 벡터를 붙이고, 명세 ⑤의 `extractions[]` 모양으로 만든다."""
    candidates = rules.screen(llm_output, req.existing_preferences)
    if not candidates:
        # 뽑은 것이 없는 대화가 흔하다. 그때는 임베딩 모델을 부르지 않는다
        return []

    vectors, dim, _model = await embedding.embed(
        [candidate.value for candidate in candidates], MEMORY_VECTOR_PURPOSE
    )
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
