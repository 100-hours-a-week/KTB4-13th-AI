"""⑦ LLM 에게 주는 도구 설명(#356). 4단계의 인자 모델(ARG_MODELS)에서 만든다.

- 도구 이름의 `.` 을 `_` 로 바꿔 준다. OpenAI 의 함수 이름은 영문·숫자·`_`·`-` 만 허용한다.
- JSON 스키마의 `$ref` 를 펼친다. 공급자마다 `$defs` 를 다루는 방식이 달라서 한 덩어리로 준다.
- 설명(description)은 LLM 이 인자를 올바르게 채우는 거의 유일한 단서다.
"""

from copy import deepcopy
from typing import Any

from app.agent.schemas import ToolName
from app.agent.tools import ARG_MODELS

_DESCRIPTIONS: dict[ToolName, str] = {
    ToolName.CART_VIEW: (
        "사용자의 장바구니에 담긴 책 목록과 합계를 조회한다. '장바구니 n번째'로 책을 가리키려면 "
        "먼저 이 도구를 불러야 한다. 합계는 구매할 수 있는 책만 더한 값이다."
    ),
    ToolName.CART_ADD: (
        "책을 장바구니에 담는다. 이미 담긴 책이면 담지 않고 그렇다고 알려 준다."
    ),
    ToolName.CART_UPDATE: (
        "장바구니에 이미 담긴 책의 수량을 바꾼다. 수량 0은 안 된다. 빼려면 cart_remove 를 쓴다."
    ),
    ToolName.CART_REMOVE: "장바구니에서 책을 뺀다.",
    ToolName.UI_GUIDE: (
        "사용자를 앱의 화면으로 안내하는 버튼을 붙인다. 배송이나 주문 상태를 묻는 말에는 "
        "ORDER-LIST-001(주문내역)로 안내하고, 장바구니를 보러 가게 하려면 CART-001로 안내한다."
    ),
    ToolName.BOOK_OFFER: (
        "책의 제목, 가격, 재고를 조회한다. 사용자가 제목으로 말했을 때 어느 카드인지 알아내는 "
        "데에도 쓴다."
    ),
}


def llm_name(name: ToolName) -> str:
    return name.value.replace(".", "_")


# LLM 이 부른 이름을 도구로 되돌린다. 여기에 없는 이름은 LLM 이 지어낸 것이다.
FROM_LLM_NAME: dict[str, ToolName] = {llm_name(name): name for name in ARG_MODELS}


def _flatten(node: Any, defs: dict[str, Any]) -> Any:
    """`$ref` 를 펼치고, 읽는 데 필요 없는 title 을 뺀다."""
    if isinstance(node, dict):
        if "$ref" in node:
            target = deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]])
            # `$ref` 와 같은 자리에 있는 설명이 정의 쪽 설명보다 구체적이다
            target.update({k: v for k, v in node.items() if k != "$ref"})
            return _flatten(target, defs)
        return {
            k: _flatten(v, defs)
            for k, v in node.items()
            if k != "$defs" and not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_flatten(v, defs) for v in node]
    return node


def tool_definitions(allowed: list[ToolName] | None = None) -> list[dict[str, Any]]:
    """LLM 에게 줄 도구 목록(OpenAI 도구 형식). allowed 가 있으면 그 안의 것만 준다."""
    definitions = []
    for name, args_model in ARG_MODELS.items():
        if allowed is not None and name not in allowed:
            continue
        schema = args_model.model_json_schema()
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": llm_name(name),
                    "description": _DESCRIPTIONS[name],
                    "parameters": _flatten(schema, schema.get("$defs", {})),
                },
            }
        )
    return definitions
