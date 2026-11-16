"""태스크마다 운영 응답에서 확인할 수 있는 규칙을 채점한다."""

import json
from functools import wraps

from fastapi.responses import JSONResponse

from app.core import langsmith


def _ranked(rows: list[dict], degraded: bool) -> tuple[dict, int]:
    isbns = [row.get("isbn") for row in rows]
    duplicates = len(isbns) - len(set(isbns))
    return (
        {"returned": len(rows), "duplicates": duplicates, "degraded": degraded},
        100 if all(isbns) and duplicates == 0 and not degraded else 0,
    )


def _score(
    task: str, response: JSONResponse, observed: langsmith.Observation
) -> tuple[dict, int | float | None] | None:
    status = response.status_code
    if task == "chat" and observed.facts.get("delegated_to_agent"):
        return None
    # 요청 검사나 커서 만료는 생성·검색 품질의 실패로 세지 않는다.
    if 400 <= status < 500:
        return None
    metrics = {"status": status}
    if status != 200:
        return metrics, 0
    if task == "embedding":
        return metrics, 100 if observed.facts.get("shape_ok") else 0
    data = json.loads(response.body)["data"]
    degraded = bool(response.headers.get("X-Degraded"))
    if task == "search":
        rules, score = _ranked(data["results"], degraded)
        return {**metrics, **rules}, score
    if task == "feed":
        rules, score = _ranked(data["items"], degraded)
        return {**metrics, **rules, "cold_start": data["cold_start"]}, score
    if task == "chat":
        if data.get("route") == "shop":
            # 쇼핑 요청은 실제 생성이 일어난 shop_turn 에서 채점한다.
            return None
        rules, score = _ranked(data["cards"], data["degraded"])
        empty = not any((data["cards"], data.get("reply"), data.get("followup")))
        issues = empty or observed.facts.get("filtered_text_fields", 0)
        return {**metrics, **rules, "empty": empty}, 0 if issues else score
    if task == "extraction":
        if not observed.facts.get("evaluated"):
            return None
        items = data["extractions"]
        filtered = max(0, observed.facts.get("candidates", len(items)) - len(items))
        valid = data["nothing_found"] is (not items) and all(
            item["dim"] > 0 and len(item["vector"]) == item["dim"] for item in items
        )
        return {**metrics, "returned": len(items), "filtered": filtered}, (
            100 if valid and filtered == 0 else 0
        )
    if task == "photo":
        items = data["items"]
        found = sum(item["state"] == "FOUND" for item in items)
        return {**metrics, "detected": len(items), "matched": found}, (
            100 * found / len(items) if items else None
        )
    raise ValueError(f"채점기가 없는 태스크 {task}")


def _body(response: JSONResponse) -> dict:
    try:
        body = json.loads(response.body)
    except (ValueError, TypeError, AttributeError):
        return {"status": getattr(response, "status_code", None)}
    return {"status": response.status_code, **body} if isinstance(body, dict) else {}


def evaluate_response(task: str):
    """본문을 읽는 평가는 선택된 응답에만 실행한다.

    점수와 숫자는 늘 보낸다. 원문 트레이스(LANGSMITH_TRACE)를 켠 표본 요청이면 응답 본문도 트레이스에 싣는다.
    """

    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            with langsmith.observe(task) as observed:
                try:
                    response = await fn(*args, **kwargs)
                except Exception:
                    if observed is not None:
                        observed.record({"status": 500}, 0)
                    raise
                if observed is None:
                    return response
                if observed.run is not None:
                    observed.trace_outputs(_body(response))
                if observed.eval_mode:
                    if observed.versions:
                        response.headers[langsmith.EVAL_PROMPTS_HEADER] = json.dumps(
                            observed.versions, sort_keys=True
                        )
                    return response
                if not observed.sampled:
                    return response
                try:
                    result = _score(task, response, observed)
                except (KeyError, ValueError, TypeError, AttributeError):
                    result = {"status": response.status_code, "format_error": True}, 0
                if result is not None:
                    observed.record(*result)
                return response

        return wrapped

    return decorate
