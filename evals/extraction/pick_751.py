"""#751 취향 뽑기 사람 채점 시트를 만든다. 한 번 쓰는 코드라 이 브랜치에만 둔다.

저장된 답 두 파일(운영 모델 gpt-4o-mini, 미세조정 Gemma)에서 코드 통과 10개·실패 10개씩, 모두 40개.
대화가 겹치지 않게 고르고(실패가 적은 쪽부터), 대화마다 그 결과의 회차 하나를 무작위로 고른다. 섞어서 번호만 붙이고
시트에는 모델·회차·코드 채점 결과를 적지 않는다. 열쇠는 채점이 끝날 때까지 열지 않는다.

    uv run python -m evals.extraction.pick_751 --answers mini=api-gpt-4o-mini.json \\
        --answers e5=gpu-gemma-4-e4b-e5-fp8.json --sheet sheet.md --key key.json
"""

import argparse
import json
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SEED = "751"
EACH = 10
CASES_PATH = Path(__file__).with_name("cases.json")
ROLE = {"user": "사용자", "assistant": "챗봇"}


def _sign(strength: Any) -> str:
    if isinstance(strength, (int, float)) and strength > 0:
        return "좋아함"
    if isinstance(strength, (int, float)) and strength < 0:
        return "싫어함"
    return "세기 없음"


def choose(
    answers: Mapping[str, Sequence[Mapping[str, Any]]], rng: random.Random
) -> list[dict[str, Any]]:
    """(모델, 통과 여부)마다 EACH 개. 대화는 40개 모두 다르다. 고를 대화가 적은 칸부터 채운다."""
    slots = [(model, passed) for model in sorted(answers) for passed in (False, True)]
    pools = {
        slot: sorted({r["case_id"] for r in answers[slot[0]] if r["passed"] is slot[1]})
        for slot in slots
    }
    used: set[str] = set()
    picked = []
    for slot in sorted(slots, key=lambda s: (len(pools[s]), s)):
        free = [case for case in pools[slot] if case not in used]
        if len(free) < EACH:
            raise SystemExit(f"{slot}: 겹치지 않는 대화가 {len(free)}개뿐이다")
        for case in rng.sample(free, EACH):
            used.add(case)
            runs = [
                i
                for i, r in enumerate(answers[slot[0]])
                if r["case_id"] == case and r["passed"] is slot[1]
            ]
            index = rng.choice(runs)
            picked.append({"model": slot[0], "index": index, "case": case})
    rng.shuffle(picked)
    return picked


def _items(record: Mapping[str, Any]) -> list[str]:
    lines = []
    for value, got in zip(record["values"], record["got"], strict=True):
        lines.append(f"- {got.get('type')} · {_sign(got.get('strength'))} · {value}")
    return lines or ["- (뽑은 것 없음)"]


def sheet(
    picked: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Sequence[Mapping[str, Any]]],
    cases: Mapping[str, Mapping[str, Any]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    lines = [
        "# 취향 뽑기 사람 채점",
        "",
        "대화를 읽고, 뽑은 취향이 맞으면 통과, 하나라도 틀리면 실패를 고릅니다. 실패면 이유를 한 줄 적습니다.",
        "",
        "실패로 보는 것(11/6 E4 뜻 채점 기준에 놓친 것을 더함):",
        "",
        "- **놓침**: 기대한 취향을 뜻으로 뽑지 않았다. 말을 바꿔 적은 것은 뽑은 것이다",
        "- **뽑으면 안 되는 것**: 「뽑으면 안 됨」과 뜻이 같은 취향을 뽑았다(좋아함·싫어함까지 같을 때)",
        "- **근거 없이 더 뽑음**: 사용자가 한 말에 없는 것을 뽑았다. 짐작해 보탠 장르·저자·분위기, 챗봇 말에만 있는 것. 뽑을 것이 없는 대화에서 뽑은 것도 실패다",
        "- **종류가 틀림**: 뽑은 취향의 종류가 기대 또는 종류 설명과 다르다",
        "",
        "사용자가 말은 했지만 찾을 때만 붙인 조건, 남에게 줄 책처럼 원래 안 뽑는 취향을 더 뽑은 것은 통과로 봅니다(E4와 같음). 세기 숫자·어디에·핵심어는 보지 않습니다.",
        "",
    ]
    key = {}
    for n, item in enumerate(picked, 1):
        number = f"e{n:03d}"
        record = answers[item["model"]][item["index"]]
        case = cases[item["case"]]
        expect = case["expect"]
        key[number] = {**item, "code_passed": record["passed"]}
        talk = [
            f"> **{ROLE[turn['role']]}**: {' '.join(turn['text'].split())}"
            for turn in case["request"]["conversation"]
        ]
        wanted = [
            f"- {want['type']} · {_sign(want.get('strength'))} · {want['value']}"
            for want in expect["extractions"]
        ] or ["- (뽑을 것 없음)"]
        banned = [f"- {text}" for text in expect["must_not_extract"]] or ["- (없음)"]
        lines += [
            f"## {number}",
            "",
            *talk,
            "",
            "**기대**",
            "",
            *wanted,
            "",
            "**뽑으면 안 됨**",
            "",
            *banned,
            "",
            "**뽑은 취향**",
            "",
            *_items(record),
            "",
            "- [ ] 통과",
            "- [ ] 실패",
            "",
            "이유: ",
            "",
        ]
    return "\n".join(lines), key


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--answers", action="append", required=True, help="이름=파일")
    parser.add_argument("--sheet", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    args = parser.parse_args(argv)

    answers = {}
    for item in args.answers:
        name, path = item.split("=", 1)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data["bundle_version"] != 7 or data["prompt_version"] != "v9":
            raise SystemExit(f"{path}: 판 7·v9 가 아니다")
        answers[name] = data["runs"]
    bundle = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if bundle["version"] != 7:
        raise SystemExit("cases.json 판이 7이 아니다")
    cases = {case["id"]: case for case in bundle["cases"]}

    picked = choose(answers, random.Random(SEED))
    text, key = sheet(picked, answers, cases)
    args.sheet.write_text(text, encoding="utf-8")
    args.key.write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(picked)}개: {args.sheet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
