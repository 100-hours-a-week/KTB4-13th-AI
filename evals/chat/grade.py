"""③ 챗봇 평가 채점(#322) — run.py 가 저장한 원본을 cases.json 의 기대 동작으로 채점한다.

DB·LLM 없이 돈다. 책 정보(출판사, 출간연도, 가격, 책소개 유무)는 run.py 가 원본에 같이 저장한다.
코드로 볼 수 있는 것만 채점한다 — 추천한 책이 질문에 맞는지, 이유가 책소개에 근거하는지는
여기서 보지 않는다(cases.json 의 `ungraded`). 그래서 통과율은 실제 품질보다 높게 나온다.
BE 를 거치지 않으므로 BE 에서만 나는 실패(긴 이유가 빈 카드를 저장하다 500)는 통과율에 안 들어가고
공통 지표로만 센다(#504). dev 에서는 smoke_be.py 로 본다.

사용:
    uv run python -m evals.chat.grade 원본.json
    uv run python -m evals.chat.grade 원본.json --skip-runs 4,5 --save evals/chat/results/이름.json
    uv run python -m evals.chat.grade 원본.json --only 8,43     # 일부 질문만(다른 원본과 같은 질문으로 견줄 때)
    uv run python -m evals.chat.grade --compare 전.json 후.json
"""

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from evals.chat import reason_long

CASES_PATH = Path(__file__).with_name("cases.json")

# 추천을 기대하는 행동. 이 질문에서 카드가 0장이면 "카드 0장"으로 센다.
RECOMMEND_ACTIONS = {"recommend", "recommend_and_ask"}
# 기대 동작을 아직 정하지 않았거나 코드로 볼 수 없는 질문. 돌려서 응답만 남기고 통과율에서 뺀다.
OBSERVE = "observe"

# 사용자가 말하지 않았는데 조건 문장(spec.semantic)에 붙으면 "지어낸 분위기"로 세는 낱말.
# 어림이다 — 목록에 없는 낱말은 못 잡고, 뜻이 같은 다른 말로 한 요청은 잘못 잡을 수 있다.
MOOD_WORDS = (
    "가벼운",
    "가볍게",
    "잔잔",
    "따뜻",
    "편안",
    "재미있",
    "재밌",
    "무겁지",
    "친절",
)
# 답과 이유에 나오는지 따로 세는 낱말. 3단계 프롬프트의 형식 예시에 있던 말이다.
LEAK_WORD = "잔잔"
# 이보다 빨리 온 응답은 LLM 을 부르지 않은 것으로 본다(규칙이 바로 되물은 턴).
LLM_TURN_MIN_SECONDS = 0.5

_NON_WORD = re.compile(r"[\W_]+")
# 제목 끝의 권·판본 표기("… 1", "… 제2권", "… 상", "…(양장)"). 같은 작품의 다른 권을 한 작품으로 본다(#504).
# 숫자는 띄어쓰기 뒤의 1~3자리만 뗀다 — "1984", "1Q84"는 제목이고 "트렌드 코리아 2025"는 해마다 다른 책이다.
# 어림이다 — "개정판"처럼 괄호 없이 붙은 말이나 로마 숫자는 못 뗀다.
_VOLUME_SUFFIX = re.compile(
    r"(?:\s+(?:제|vol\.?)?\s*\d{1,3}\s*(?:권|부|편|판)?"
    r"|\s*[(\[][^)\]]*[)\]]"
    r"|\s+(?:상|중|하)권?"
    r"|\s*[:\-]\s*\d{1,3})\s*$",
    re.IGNORECASE,
)


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(text: str | None) -> str:
    """제목 비교용. 띄어쓰기와 기호를 지우고 소문자로 바꾼다."""
    return _NON_WORD.sub("", (text or "").lower())


def work_key(title: str | None) -> str:
    """같은 작품의 다른 권·판본을 한 키로. 끝의 권 표기를 다 뗀 뒤 normalize 한다.

    다 떼면 남는 게 없을 때("(1)"처럼)는 원래 제목을 쓴다.
    """
    text = (title or "").strip()
    while True:
        trimmed = _VOLUME_SUFFIX.sub("", text).strip()
        if trimmed == text or not trimmed:
            break
        text = trimmed
    return normalize(text)


def _semantic(turn: dict) -> str:
    return (turn.get("spec") or {}).get("semantic") or ""


def _spec_value(turn: dict, path: str) -> Any:
    value: Any = turn.get("spec") or {}
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _book(books: dict, card: dict) -> dict:
    return books.get(str(card.get("book_id"))) or {}


def grade_case(case: dict, turns: list[dict], books: dict) -> list[str]:
    """실패 사유 목록. 비어 있으면 통과다."""
    if len(turns) != len(case["turns"]) or any(t["status"] != 200 for t in turns):
        return ["요청 오류"]

    expect = case["expect"]
    last = turns[-1]
    cards = last["cards"]
    infos = [_book(books, c) for c in cards]
    titles = [normalize(i.get("title")) for i in infos]
    said = (last.get("reply") or "") + (last.get("followup") or "")
    fails: list[str] = []

    action = expect["action"]
    if action in RECOMMEND_ACTIONS and not cards:
        fails.append("카드 0장")
    if action == "clarify" and (cards or not last.get("followup")):
        fails.append("되묻지 않음")

    if expect.get("intent") and _spec_value(turns[0], "intent") != expect["intent"]:
        fails.append("의도 판정")

    want = expect.get("cards") or {}
    if want.get("any_isbn") and not any(
        i.get("isbn") in want["any_isbn"] for i in infos
    ):
        fails.append("허용 책 없음")
    # 아래 검사들은 카드가 있을 때만 본다. 카드가 없는 것은 위에서 이미 셌다.
    for needle in want.get("title_all", []):
        if cards and not all(normalize(needle) in t for t in titles):
            fails.append("제목 불일치")
    if (
        want.get("title_any")
        and cards
        and not any(normalize(n) in t for n in want["title_any"] for t in titles)
    ):
        fails.append("엉뚱한 책")
    for needle in want.get("author_all", []):
        if cards and not all(needle in (i.get("author") or "") for i in infos):
            fails.append("저자 불일치")
    for needle in want.get("publisher_all", []):
        if cards and not all(needle in (i.get("publisher") or "") for i in infos):
            fails.append("출판사 불일치")
    for needle in want.get("title_none", []):
        if any(normalize(needle) in t for t in titles):
            fails.append("기준 책이 카드에 나옴")
    for needle in want.get("title_none_exact", []):
        if any(normalize(needle) == t for t in titles):
            fails.append("기준 책이 카드에 나옴")
    for needle in want.get("author_none", []):
        if any(needle in (i.get("author") or "") for i in infos):
            fails.append("빼 달라는 저자가 나옴")
    if want.get("distinct_titles") and len(titles) != len(set(titles)):
        fails.append("같은 책 중복")
    works = [work_key(i.get("title")) for i in infos]
    if want.get("distinct_works") and len(works) != len(set(works)):
        fails.append("같은 작품 권·판본 중복")
    if "max_cards" in want and len(cards) > want["max_cards"]:
        fails.append("권수 초과")
    if "price_max" in want and any(
        (i.get("price") or 0) > want["price_max"] for i in infos
    ):
        fails.append("가격 초과")
    if "year_from" in want and any(
        (i.get("pub_year") or 0) < want["year_from"] for i in infos
    ):
        fails.append("연도 미달")
    earlier = {c["book_id"] for t in turns[:-1] for c in t["cards"]}
    if want.get("not_shown_before") and earlier & {c["book_id"] for c in cards}:
        fails.append("본 책이 다시 나옴")
    # "이 중에 하나만 골라줘"처럼 앞에서 보여 준 책 안에서 골라야 하는 질문.
    if (
        want.get("only_shown_before")
        and cards
        and not ({c["book_id"] for c in cards} <= earlier)
    ):
        fails.append("보여 준 책 밖에서 고름")
    if want.get("reason_long_needs_description") and any(
        c.get("reason_long") and not i.get("has_description")
        for c, i in zip(cards, infos, strict=True)
    ):
        fails.append("책소개 없는 책에 긴 이유")

    reply = expect.get("reply") or {}
    if reply.get("has") and reply["has"] not in said:
        fails.append("질문을 안 붙임")
    if reply.get("lacks") and reply["lacks"] in said:
        fails.append("하면 안 되는 말을 함")

    for rule in expect.get("spec", []):
        if _spec_value(turns[rule["turn"] - 1], rule["path"]) != rule["equals"]:
            fails.append(f"조건 불일치({rule['turn']}턴 {rule['path']})")

    semantic = expect.get("semantic") or {}
    at = semantic.get("changes_at")
    if at and _semantic(turns[at - 1]) == _semantic(turns[at - 2]):
        fails.append("톤 미반영")
    if any(w not in _semantic(last) for w in semantic.get("has", [])) or any(
        w in _semantic(last) for w in semantic.get("lacks", [])
    ):
        fails.append("주제 전환 무시")

    return list(dict.fromkeys(fails))


def _percent(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _score_summary(values: list[int]) -> dict:
    """카드 매칭 점수(0~100)의 분포. 사분위는 정렬한 목록의 자리로 어림한다."""
    if not values:
        return {"cards": 0}
    ordered = sorted(values)

    def at(q: float) -> int:
        return ordered[min(len(ordered) - 1, int(len(ordered) * q))]

    return {
        "cards": len(ordered),
        "zero": ordered.count(0),
        "min": ordered[0],
        "p25": at(0.25),
        "median": at(0.5),
        "p75": at(0.75),
        "max": ordered[-1],
    }


def summarize(
    cases_file: dict,
    raw: dict,
    skip_runs: frozenset[int] = frozenset(),
    only: frozenset[int] = frozenset(),
) -> dict:
    """원본 하나를 채점해 요약한다. skip_runs 는 1부터 세는 회차 번호다(끊긴 회차를 뺄 때).

    only 를 주면 그 번호의 질문만 센다. 일부 질문만 돌린 원본을 전체를 돌린 원본과 같은 질문으로 견줄 때 쓴다.
    """
    cases = [c for c in cases_file["cases"] if not only or c["no"] in only]
    books = raw.get("books") or {}
    runs = [r for i, r in enumerate(raw["runs"], start=1) if i not in skip_runs]
    ran = [c for c in cases if all(str(c["no"]) in r for r in runs)]
    covered = [c for c in ran if c["expect"]["action"] != OBSERVE]
    observed = [c["no"] for c in ran if c["expect"]["action"] == OBSERVE]

    per_case: dict[str, list[list[str]]] = {
        str(c["no"]): [grade_case(c, r[str(c["no"])], books) for r in runs]
        for c in covered
    }
    passed_per_run = [
        sum(not per_case[str(c["no"])][i] for c in covered) for i in range(len(runs))
    ]
    total = len(covered) * len(runs)
    passed = sum(passed_per_run)

    recommend = [c for c in covered if c["expect"]["action"] in RECOMMEND_ACTIONS]
    zero_cards = Counter(
        str(c["no"])
        for c in recommend
        for r in runs
        if r[str(c["no"])][-1]["status"] == 200 and not r[str(c["no"])][-1]["cards"]
    )

    def tally(field: str, names) -> dict:
        out = {}
        for name in names:
            group = [c for c in covered if c.get(field) == name]
            ok = sum(not f for c in group for f in per_case[str(c["no"])])
            # 회차마다 따로 낸 통과율 중 가장 낮은 것. 평균이 괜찮아도 한 회차에서 크게 떨어지는 종류를 본다.
            per_run = [
                _percent(sum(not per_case[str(c["no"])][i] for c in group), len(group))
                for i in range(len(runs))
            ]
            out[name] = {
                "passed": ok,
                "total": len(group) * len(runs),
                "rate": _percent(ok, len(group) * len(runs)),
                "min_rate": min(per_run) if group and per_run else 0.0,
            }
        return out

    by_kind = tally("kind", cases_file["kinds"])
    # 출처별로 따로 본다. 버그를 다시 일으키는 질문의 결과가 실사용 품질처럼 읽히지 않게.
    by_source = tally("source", cases_file.get("sources", {}))
    # 전체 통과율이 가리는 약한 종류(#504). 문항이 있는 종류 가운데 통과율이 가장 낮은 것.
    graded_kinds = {k: v for k, v in by_kind.items() if v["total"]}
    lowest = min(graded_kinds, key=lambda k: graded_kinds[k]["rate"], default=None)
    lowest_kind = {"kind": lowest, **graded_kinds[lowest]} if lowest else None

    # 공통 지표. 질문과 상관없이 모든 턴에서 본다.
    llm_turns = unsaid_mood = leaked = no_description = same_title = same_work = 0
    null_reason_long: Counter[str] = Counter()
    scores: dict[str, list[int]] = {}
    llm_turn_said: list[tuple[dict, str]] = []  # 긴 이유 지표(#503)에 넘긴다
    degraded_per_run = []
    seconds: list[float] = []
    appearances: Counter[str] = Counter()
    for run in runs:
        degraded = 0
        for case in covered:
            user_said = ""
            shown: set[str] = set()
            for turn in run[str(case["no"])]:
                user_said += " " + turn["msg"]
                degraded += bool(turn.get("degraded"))
                infos = [_book(books, c) for c in turn["cards"]]
                shown |= {i["title"] for i in infos if i.get("title")}
                no_description += any(not i.get("has_description") for i in infos)
                titles = [normalize(i.get("title")) for i in infos]
                same_title += len(titles) != len(set(titles))
                works = [work_key(i.get("title")) for i in infos]
                same_work += len(works) != len(set(works))
                # 명세는 책소개 없는 책과 LLM 장애 턴의 긴 이유를 null 로 두지만, BE 의
                # recommendation_cards.reason_long 은 NOT NULL 이라 BE 를 거치면 이 턴은 500 이다(#504).
                if any(not c.get("reason_long") for c in turn["cards"]):
                    null_reason_long[str(case["no"])] += 1
                if turn.get("degraded") or turn["sec"] <= LLM_TURN_MIN_SECONDS:
                    continue
                llm_turns += 1
                llm_turn_said.append((turn, user_said))
                seconds.append(turn["sec"])
                card_scores = [
                    c["match_score"]
                    for c in turn["cards"]
                    if c.get("match_score") is not None
                ]
                if card_scores:
                    intent = _spec_value(turn, "intent") or "?"
                    scores.setdefault(intent, []).extend(card_scores)
                semantic = _semantic(turn)
                unsaid_mood += any(
                    w in semantic and w not in user_said for w in MOOD_WORDS
                )
                texts = [turn.get("reply") or ""]
                for card in turn["cards"]:
                    texts += [
                        card.get("reason_short") or "",
                        card.get("reason_long") or "",
                    ]
                leaked += LEAK_WORD not in user_said and any(
                    LEAK_WORD in t for t in texts
                )
            appearances.update(shown)
        degraded_per_run.append(degraded)
    seconds.sort()
    pairs = len(covered) * len(runs)

    return {
        "meta": raw.get("meta") or {},
        "cases_version": cases_file["version"],
        "runs": len(runs),
        "skipped_runs": sorted(skip_runs),
        "only": sorted(only),
        "cases": len(covered),
        "passed": passed,
        "total": total,
        "pass_rate": _percent(passed, total),
        "passed_per_run": passed_per_run,
        "zero_cards": sum(zero_cards.values()),
        "zero_cards_total": len(recommend) * len(runs),
        "zero_cards_rate": _percent(
            sum(zero_cards.values()), len(recommend) * len(runs)
        ),
        "zero_cards_by_case": dict(sorted(zero_cards.items(), key=lambda x: int(x[0]))),
        "by_kind": by_kind,
        "lowest_kind": lowest_kind,
        "by_source": by_source,
        "observed": observed,
        "fail_reasons": dict(
            Counter(r for fs in per_case.values() for f in fs for r in f).most_common()
        ),
        "passed_by_case": {k: sum(not f for f in v) for k, v in per_case.items()},
        "common": {
            "llm_turns": llm_turns,
            "unsaid_mood_turns": unsaid_mood,
            "unsaid_mood_rate": _percent(unsaid_mood, llm_turns),
            "leak_word": LEAK_WORD,
            "leak_word_turns": leaked,
            "leak_word_rate": _percent(leaked, llm_turns),
            "turns_with_card_without_description": no_description,
            "turns_with_same_title_cards": same_title,
            "turns_with_same_work_cards": same_work,
            "turns_with_null_reason_long": sum(null_reason_long.values()),
            "null_reason_long_by_case": dict(
                sorted(null_reason_long.items(), key=lambda x: int(x[0]))
            ),
            # LLM 을 부른 턴의 카드만. 제목 찾기(exact) 카드가 0점으로 나온 적이 있어(10/11 시험 실행)
            # 의도별로도 나눠 본다.
            "match_score": {
                "all": _score_summary([s for v in scores.values() for s in v]),
                "by_intent": {k: _score_summary(v) for k, v in sorted(scores.items())},
            },
            "degraded_turns_per_run": degraded_per_run,
            # 쏠림: 한 책이 (질문, 회차) 쌍 가운데 몇 %에 나왔나. 상위 5권.
            "most_shown_books": [
                {"title": title, "rate": _percent(n, pairs)}
                for title, n in appearances.most_common(5)
            ],
            "seconds_median": round(statistics.median(seconds), 1) if seconds else None,
            "seconds_p90": round(seconds[int(len(seconds) * 0.9)], 1)
            if seconds
            else None,
            **reason_long.metrics(llm_turn_said, books, MOOD_WORDS),
        },
    }


def _render_scores(part: dict) -> str:
    if not part["cards"]:
        return "카드 없음"
    return (
        f"{part['cards']}장 · 0점 {part['zero']}장 · 최소 {part['min']} · 25% {part['p25']}"
        f" · 중앙 {part['median']} · 75% {part['p75']} · 최대 {part['max']}"
    )


def render(summary: dict, cases_file: dict) -> str:
    """사람이 읽는 요약."""
    s, c = summary, summary["common"]
    meta = s["meta"]
    low = s["lowest_kind"]
    lines = [
        f"질문 묶음 v{s['cases_version']} · {s['cases']}문항 × {s['runs']}회"
        + (f" (뺀 회차: {s['skipped_runs']})" if s["skipped_runs"] else "")
        + (f" (질문 {len(s['only'])}개만)" if s.get("only") else ""),
        f"서버 {meta.get('server_version', '?')} · 모델 {meta.get('model', '?')} · {meta.get('started_at', '?')}",
        "",
        f"통과 {s['passed']}/{s['total']} ({s['pass_rate']}%) · 회차별 {s['passed_per_run']}"
        + (f" · 가장 낮은 종류 {low['kind']} {low['rate']}%" if low else ""),
        f"카드 0장 {s['zero_cards']}/{s['zero_cards_total']} ({s['zero_cards_rate']}%) · 질문별 {s['zero_cards_by_case']}",
        "",
        "종류별 통과율 (괄호 안 = 회차별로 냈을 때 가장 낮은 회차)",
    ]

    def group_line(name: str, k: dict, about: str) -> str:
        return (
            f"  {name:<10} {k['passed']}/{k['total']} ({k['rate']}%, 최저 {k['min_rate']}%)"
            f"  {about}"
        )

    for kind, about in cases_file["kinds"].items():
        k = s["by_kind"][kind]
        if k["total"]:
            lines.append(group_line(kind, k, about["about"]))
    if s.get("by_source"):
        lines += ["", "출처별 통과율"]
        for source, about in cases_file.get("sources", {}).items():
            k = s["by_source"][source]
            if k["total"]:
                lines.append(group_line(source, k, about["about"]))
    if s.get("observed"):
        lines.append(
            "기록만 한 질문(통과율에서 뺌): "
            + ", ".join(f"#{n}" for n in s["observed"])
        )
    lines += [
        "",
        "실패 사유: "
        + (", ".join(f"{k} {v}" for k, v in s["fail_reasons"].items()) or "없음"),
        "",
        f"LLM 을 부른 턴 {c['llm_turns']}",
        f"  말하지 않은 분위기가 조건 문장에 붙음 {c['unsaid_mood_turns']} ({c['unsaid_mood_rate']}%)",
        f"  답·이유에 '{c['leak_word']}' {c['leak_word_turns']} ({c['leak_word_rate']}%)",
        f"책소개 없는 카드가 낀 턴 {c['turns_with_card_without_description']} · 제목이 같은 카드가 낀 턴 {c['turns_with_same_title_cards']}",
        f"같은 작품의 다른 권·판본이 낀 턴 {c['turns_with_same_work_cards']}",
        f"긴 이유가 빈 카드가 낀 턴 {c['turns_with_null_reason_long']} · 질문별 {c['null_reason_long_by_case']}",
    ]
    if c["turns_with_null_reason_long"]:
        lines.append(
            "  ! BE 의 recommendation_cards.reason_long 이 NOT NULL 이라 BE 를 거치면 이 턴은 500 이다(#504)."
            " AI 단독으로는 200 이라 통과율에 안 보인다. smoke_be.py 로 dev 에서 확인한다."
        )
    score = c["match_score"]
    lines.append(f"매칭 점수 (LLM 을 부른 턴의 카드) {_render_scores(score['all'])}")
    lines += [
        f"  {intent:<9} {_render_scores(part)}"
        for intent, part in score["by_intent"].items()
    ]
    lines += [
        *reason_long.render(c),
        "가장 많이 나온 책: "
        + ", ".join(f"{b['title']} {b['rate']}%" for b in c["most_shown_books"]),
        f"응답 시간 중앙 {c['seconds_median']}초 · 90% {c['seconds_p90']}초 (측정 환경에 따라 다름)",
        f"회차별 degraded 턴 {c['degraded_turns_per_run']}",
    ]
    if any(c["degraded_turns_per_run"]):
        lines.append(
            "  ! degraded 턴은 LLM 이 끊긴 응답이다. 많은 회차는 --skip-runs 로 빼고 다시 채점한다."
        )
    lines += ["", "질문별 통과 수"]
    for case in cases_file["cases"]:
        no = str(case["no"])
        if no not in s["passed_by_case"]:
            continue
        ungraded = " (채점 안 한 기준 있음)" if case["expect"].get("ungraded") else ""
        lines.append(
            f"  #{no:>2} {s['passed_by_case'][no]}/{s['runs']}  {case['turns'][-1][:30]}{ungraded}"
        )
    return "\n".join(lines)


def compare(before: dict, after: dict) -> str:
    """요약 두 개를 나란히. 버전이나 문항 수가 다르면 숫자를 견줄 수 없다고 알린다.

    v4(#504) 전에 저장한 요약에는 가장 낮은 종류와 긴 이유가 빈 턴이 없어 "?"로 둔다.
    """

    def lowest(s: dict) -> str:
        low = s.get("lowest_kind")
        return f"{low['kind']} {low['rate']}%" if low else "?"

    rows = [
        ("통과율", f"{before['pass_rate']}%", f"{after['pass_rate']}%"),
        ("가장 낮은 종류", lowest(before), lowest(after)),
        ("회차별 통과 수", before["passed_per_run"], after["passed_per_run"]),
        (
            "카드 0장",
            f"{before['zero_cards']}/{before['zero_cards_total']} ({before['zero_cards_rate']}%)",
            f"{after['zero_cards']}/{after['zero_cards_total']} ({after['zero_cards_rate']}%)",
        ),
        (
            "지어낸 분위기",
            f"{before['common']['unsaid_mood_rate']}%",
            f"{after['common']['unsaid_mood_rate']}%",
        ),
        (
            f"'{LEAK_WORD}' 누출",
            f"{before['common']['leak_word_rate']}%",
            f"{after['common']['leak_word_rate']}%",
        ),
        (
            "긴 이유가 빈 카드가 낀 턴(BE 500)",
            before["common"].get("turns_with_null_reason_long", "?"),
            after["common"].get("turns_with_null_reason_long", "?"),
        ),
        *reason_long.compare_rows(before["common"], after["common"]),
    ]
    lines = []
    if (before["cases_version"], before["cases"]) != (
        after["cases_version"],
        after["cases"],
    ):
        lines.append("! 질문 묶음 버전이나 문항 수가 다르다. 숫자를 견줄 수 없다.")
    lines += [f"{name}: {a}  →  {b}" for name, a, b in rows]
    lines.append("통과 수가 달라진 질문 (전 → 후, 회차 수가 다르면 비율로 볼 것)")
    for no, a in before["passed_by_case"].items():
        b = after["passed_by_case"].get(no)
        if b is not None and (a / before["runs"]) != (b / after["runs"]):
            lines.append(f"  #{no:>2} {a}/{before['runs']} → {b}/{after['runs']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="③ 챗봇 평가 채점")
    parser.add_argument("raw", nargs="?", help="run.py 가 저장한 원본")
    parser.add_argument("--skip-runs", default="", help="뺄 회차 번호(1부터). 예: 4,5")
    parser.add_argument("--only", default="", help="이 번호의 질문만 센다. 예: 8,43")
    parser.add_argument("--save", help="요약을 저장할 JSON 경로")
    parser.add_argument(
        "--compare", nargs=2, metavar=("전", "후"), help="저장한 요약 둘을 견준다"
    )
    args = parser.parse_args()

    if args.compare:
        before, after = (
            json.loads(Path(p).read_text(encoding="utf-8")) for p in args.compare
        )
        print(compare(before, after))
        return
    if not args.raw:
        parser.error("원본 경로가 필요하다")

    cases_file = load_cases()
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    skip = frozenset(int(n) for n in args.skip_runs.split(",") if n.strip())
    only = frozenset(int(n) for n in args.only.split(",") if n.strip())
    summary = summarize(cases_file, raw, skip, only)
    print(render(summary, cases_file))
    if args.save:
        Path(args.save).write_text(
            json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"\n요약 저장: {args.save}")


if __name__ == "__main__":
    main()
