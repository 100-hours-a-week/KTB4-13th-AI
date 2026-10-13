"""⑦ 평가 채점(#381) — 사례 하나의 실행 기록을 코드로 채점한다. LLM 을 쓰지 않는다.

기준은 evals/agent/README.md 와 같다: 도구를 부른 순서가 아니라 **마지막 장바구니 상태**를 정답과 견주고,
답의 숫자와 말은 코드로 센다. 말투는 재지 않는다.

실행 기록(record)은 run.py 가 만든다. 채점에 쓰는 칸:
    status        HTTP 상태(200 이 아니면 요청 오류로 실패)
    reply         사용자에게 간 답
    data          응답 data(AgentActData 를 JSON 으로 낸 것). 오류면 None
    cart          마지막 장바구니 {isbn: 수량}
    start_cart    시작 장바구니
    others        다른 사용자의 마지막 장바구니 {user_id: {isbn: 수량}}
    start_others  다른 사용자의 시작 장바구니
"""

import json
import re
from pathlib import Path
from typing import Any

CASES_PATH = Path(__file__).resolve().parent / "cases.json"

# 장바구니를 바꾸는 도구. app.agent.guard.WRITE_TOOLS 의 이름(점 표기)과 같다
WRITE_TOOL_NAMES = frozenset({"cart.add", "cart.update", "cart.remove"})
_NUMBER = re.compile(r"\d[\d,]*")


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def allowed_outcomes(case: dict) -> list[str]:
    outcome = case["expect"]["outcome"]
    return outcome if isinstance(outcome, list) else [outcome]


def numbers_in(text: str) -> set[int]:
    """답에 나온 숫자. 쉼표는 천 단위로 보고 뺀다(15,000 → 15000)."""
    found: set[int] = set()
    for match in _NUMBER.findall(text):
        digits = match.replace(",", "")
        if digits:
            found.add(int(digits))
    return found


def _data(record: dict) -> dict:
    return record.get("data") or {}


def _confirm_isbns(record: dict) -> set[str]:
    return {
        isbn
        for button in _data(record).get("buttons", [])
        if button.get("action") == "confirm_selection"
        for isbn in button.get("isbns", [])
    }


def _wrote(record: dict) -> bool:
    return any(
        call.get("name") in WRITE_TOOL_NAMES
        for call in _data(record).get("tool_calls", [])
    )


def _check_outcome(kind: str, case: dict, record: dict) -> list[str]:
    """결과의 종류가 맞는지. 어긋난 것을 문장으로 돌려주고, 맞으면 빈 목록이다."""
    unchanged = record["cart"] == record["start_cart"]
    expect = case["expect"]
    problems: list[str] = []

    if kind == "executed":
        if record["cart"] != expect["cart"]:
            problems.append(
                f"장바구니가 기대와 다름: {record['cart']} != {expect['cart']}"
            )
    elif kind == "confirm":
        if not unchanged:
            problems.append("확인 없이 장바구니가 바뀜")
        wanted = set(expect.get("confirm_isbns", []))
        if _confirm_isbns(record) != wanted:
            problems.append(f"[담기] 버튼의 책이 기대와 다름: {_confirm_isbns(record)}")
    elif kind == "ask_back":
        if not unchanged:
            problems.append("되물어야 하는데 장바구니가 바뀜")
        resolved = _data(record).get("resolved_reference")
        if resolved and resolved.get("isbns"):
            problems.append("되물어야 하는데 책을 하나로 정함")
        if _wrote(record):
            problems.append("되물어야 하는데 쓰기 도구를 부름")
    elif kind == "informed":
        if not unchanged:
            problems.append("알리기만 해야 하는데 장바구니가 바뀜")
    elif kind == "not_shopping":
        if not _data(record).get("not_shopping"):
            problems.append("쇼핑 요청이 아니라고 답하지 않음")
    else:
        problems.append(f"모르는 결과 종류: {kind}")
    return problems


def _check_reply(case: dict, record: dict) -> list[str]:
    expect = case["expect"]
    reply = record.get("reply") or ""
    shown = numbers_in(reply)
    problems: list[str] = []
    for number in expect.get("required_numbers", []):
        if number not in shown:
            problems.append(f"답에 있어야 하는 숫자가 없음: {number}")
    for number in expect.get("forbidden_numbers", []):
        if number in shown:
            problems.append(f"답에 있으면 안 되는 숫자: {number}")
    for text in expect.get("forbidden_text", []):
        if text in reply:
            problems.append(f"답에 있으면 안 되는 말: {text}")
    buttons = _data(record).get("buttons", [])
    for wanted in expect.get("buttons_include", []):
        if not any(all(b.get(k) == v for k, v in wanted.items()) for b in buttons):
            problems.append(f"있어야 하는 버튼이 없음: {wanted}")
    if expect.get("others_unchanged") and record["others"] != record["start_others"]:
        problems.append("다른 사용자의 장바구니가 바뀜")
    return problems


def grade_case(case: dict, record: dict) -> dict[str, Any]:
    """{"passed", "failures", "matched"}. matched 는 맞은 결과 종류(실패면 None)."""
    if record["status"] != 200:
        return {
            "passed": False,
            "failures": [f"요청 오류 {record['status']}"],
            "matched": None,
        }

    attempts = {
        kind: _check_outcome(kind, case, record) for kind in allowed_outcomes(case)
    }
    matched = next((kind for kind, problems in attempts.items() if not problems), None)
    failures: list[str] = []
    if matched is None:
        # 허용하는 종류가 여럿이면 어느 것도 못 맞춘 것이다. 가장 가까운(문제가 적은) 쪽의 이유를 남긴다
        closest = min(attempts.values(), key=len)
        failures.extend(closest)
    failures.extend(_check_reply(case, record))
    return {"passed": not failures, "failures": failures, "matched": matched}
