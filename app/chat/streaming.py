"""③ 스트리밍 전송(SSE, AI #236) — 말풍선(reply)을 카드보다 먼저 흘려보낸다.

명세(V2 계약)의 이벤트는 셋이다.
- `delta` {"text": …}: reply 의 글자 조각. 0회 이상
- `done`: 성공 envelope 전체. 반드시 1회
- `error`: 오류 envelope 그대로. 200 헤더가 나간 뒤라 상태 코드를 못 바꿔 이벤트로 알린다

카드 생성 LLM 은 `{"picks": […], "reply": …, "cards": […]}` 순서로 쓴다(app/routers/chat.py CARD_PROMPT). 그래서
모델이 카드 이유를 쓰는 동안 이미 다 쓴 reply 를 먼저 보낼 수 있다. 사용자가 첫 글자를 보는 시각이 "카드까지 다
쓴 뒤"에서 "고른 책과 reply 를 다 쓴 뒤"로 당겨진다. 전체 시간은 줄지 않는다.

reply 는 글자마다가 아니라 다 쓴 뒤 한 번에 보낸다(delta 1회). 까닭은 둘이다.
- 말풍선 후처리가 문장을 뺀다(요청을 되풀이한 첫 문장, 막연한 끝 질문, app/core/voice.py). 글자마다 보내면 빠질
  문장이 화면에 보였다가 done 에서 사라진다. 11/02 로컬 실측에서 "어떤 스타일이 끌리세요?"가 그랬다
- 글자마다 보내도 빨라지지 않는다. 상류(OpenRouter)가 reply 조각을 몰아 보내 조각 33개가 10ms 안에 왔다(10/20 실측,
  11/02 다시 확인). 다 쓴 뒤 보내도 첫 글자 시각이 같다

done 의 reply 가 최종이다. delta 와 다를 수 있다 — 카드에 없는 책을 짚은 문장은 카드를 다 읽어야 알 수 있어 그때
빼고, 되물을 말을 뒤에 붙이고, 모델이 쓰다 실패하면 고정 안내로 바꾼다. 받는 쪽은 done 을 받으면 그 reply 로 다시
그린다. 폴링(app/chat/jobs.py)도 같은 ReplyPreview 로 "먼저 보여 줄 말풍선"을 받는다.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

ReplySink = Callable[[str], Awaitable[None]]

_REPLY_KEY = re.compile(r'"reply"\s*:\s*"')


def wants_stream(request: Request) -> bool:
    """BE 가 `Accept: text/event-stream` 으로 부르면 흘려보낸다. 없으면 지금처럼 JSON 한 번이다."""
    return "text/event-stream" in request.headers.get("accept", "")


def closed_reply(text: str) -> str | None:
    """모델이 지금까지 쓴 글에서 reply 문자열 값을 다 썼으면 풀어 돌려준다. 아직 쓰는 중이면 None 이다."""
    match = _REPLY_KEY.search(text)
    if match is None:
        return None
    end = match.end()
    while end < len(text):
        if text[end] == "\\":
            end += 2
            continue
        if text[end] == '"':
            try:
                # strict=False: 모델이 줄바꿈을 \n 대신 그대로 쓰는 일이 있다
                return json.loads(f'"{text[match.end() : end]}"', strict=False)
            except json.JSONDecodeError:
                return None
        end += 1
    return None


class ReplyPreview:
    """모델이 쓴 글 전체를 받다가 reply 를 다 쓰면 finish 로 다듬어 sink 로 한 번 넘긴다.

    finish 는 카드 없이 할 수 있는 말풍선 후처리다. 다듬은 글이 비면 넘기지 않는다(done 의 reply 만 간다).
    """

    def __init__(self, sink: ReplySink, finish: Callable[[str], str]) -> None:
        self._sink = sink
        self._finish = finish
        self._done = False

    async def feed(self, text: str) -> None:
        if self._done:
            return
        reply = closed_reply(text)
        if reply is None:
            return
        self._done = True
        if preview := self._finish(reply):
            await self._sink(preview)


def _event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def stream_response(
    answer: Callable[[ReplySink], Awaitable[JSONResponse]],
) -> StreamingResponse:
    """answer(sink) 를 돌리며 sink 로 온 글을 delta 로 보내고, 끝나면 그 응답을 done 또는 error 로 보낸다.

    answer 는 JSON 경로와 같은 함수다. 응답을 만드는 갈래(되묻기, 쇼핑, degraded)를 SSE 용으로 따로 두지 않는다.
    """

    async def events() -> AsyncIterator[str]:
        pieces: asyncio.Queue[str | None] = asyncio.Queue()
        task = asyncio.create_task(answer(pieces.put))
        task.add_done_callback(lambda _task: pieces.put_nowait(None))
        try:
            while (piece := await pieces.get()) is not None:
                yield _event("delta", {"text": piece})
            response = task.result()
        except Exception:
            logger.exception("스트리밍 응답 처리 실패")
            yield _event("error", {"message": "internal_server_error", "data": None})
            return
        finally:
            # BE 가 중간에 끊으면 여기로 온다. 남은 LLM 호출을 멈춰 비용을 아낀다
            task.cancel()
        name = "done" if response.status_code == 200 else "error"
        yield _event(name, json.loads(response.body))

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # 프록시(nginx)가 모아 보내지 않게 한다
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
