"""③ 평가 서버를 실험 하나를 건 채로 띄운다. 103문항 평가(evals/chat)를 실험 쪽으로 돌릴 때 쓴다.

    CHAT_VARIANT=rerank LLM_PROVIDER=eval-no-zdr DATABASE_URL=postgresql://…/bookjeok_ai_eval \\
      uv run uvicorn evals.chat_stages.serve:app --port 8001

CHAT_VARIANT 는 base·rerank·vector 중 하나다(variants.py). base 는 `app.main:app` 과 같다.
"""

import os

from app.main import app
from evals.chat_stages import variants

variants.install(os.environ.get("CHAT_VARIANT", "base"))

__all__ = ["app"]
