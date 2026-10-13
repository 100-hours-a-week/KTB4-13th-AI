"""⑤ 대화에서 취향 사실을 뽑는다.

지금은 뽑는 로직이 없어 항상 빈 목록이다. LLM 으로 뽑기와 벡터 붙이기는 #299 의 뒤 단계에서 채운다.
AI 는 입력도 결과도 저장하지 않는다. 뽑은 취향은 BE 가 저장한다(명세 ⑤).
"""

from typing import Any

from app.extraction.schemas import ExtractionRequest


async def extract(req: ExtractionRequest) -> list[dict[str, Any]]:
    """뽑은 취향 사실의 목록. 항목의 모양은 명세 ⑤의 `extractions[]` 다."""
    return []
