"""홈 추천 평가 결과 둘(기준과 후보)을 사용자끼리 짝지어 판정한다(#753).

    uv run python -m evals.feed.verdict --base main.json --new pr.json

두 파일은 같은 DB 에서 `evals/feed/run.py --out` 으로 남긴 것이다. 서버도 DB 도 쓰지 않는다.

판정(#753, 2026-11-24 정함):
- 고른 관심사 가운데 위 N권에 한 권도 안 나온 것이 있는 사용자를 "빠진 사용자"로 센다
- 묶음(typical·guard)마다 따로 세어, 어느 한쪽이라도 기준보다 늘면 탈락이다. 합쳐서 세면 한쪽에서 새로
  빠진 사용자를 다른 쪽에서 다시 채워진 사용자가 가린다
- 가진 책과 제목이 같은 다른 판본이 늘면 표시만 한다
- LLM 을 쓰지 않아 같은 DB 에서 다시 돌리면 결과가 같으므로, 통계 검정 없이 센다
- 어느 쪽이든 재지 못한 사용자나 시간 예산을 넘겨 대신 채운 사용자는 견주지 않는다. 남은 사용자만으로 늘었으면
  탈락이고, 늘지 않았으면 통과 대신 "다시 재기"를 낸다(빠진 사용자가 늘었는지 알 수 없다)
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASS = "통과"
FAIL = "탈락"
REMEASURE = "다시 재기"

# 두 파일이 이 값까지 같아야 견준다. 다르면 홈 추천 코드 말고 다른 것이 바뀐 것이다
SAME = ("bundle_version", "catalog", "database")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def mismatch(base: Mapping[str, Any], new: Mapping[str, Any]) -> str | None:
    """견줄 수 없는 이유. 견줄 수 있으면 None."""
    for key in SAME:
        if base.get(key) != new.get(key):
            return f"{key} 가 다르다({base.get(key)} 대 {new.get(key)})"
    ids = {s["case_id"] for s in base["scores"]} ^ {s["case_id"] for s in new["scores"]}
    if ids:
        return f"두 파일이 잰 사용자가 다르다: {', '.join(sorted(ids)[:5])}"
    return None


def missed(score: Mapping[str, Any]) -> bool:
    """고른 관심사 가운데 한 권도 안 나온 것이 있는가. 나와야 하는 관심사가 없는 사용자는 아니다."""
    return bool(score["show"]) and score["missing"] > 0


def unusable(score: Mapping[str, Any]) -> str | None:
    """이 사용자의 목록을 견주면 안 되는 이유."""
    if not score["ok"]:
        return "재지 못함"
    if score.get("substituted"):
        return "대신 채움"
    return None


@dataclass(frozen=True)
class Group:
    name: str
    users: int
    base: tuple[str, ...]  # 기준에서 빠진 사용자
    new: tuple[str, ...]  # 후보에서 빠진 사용자

    @property
    def worse(self) -> list[str]:
        """새로 빠진 사용자."""
        return sorted(set(self.new) - set(self.base))

    @property
    def better(self) -> list[str]:
        """다시 채워진 사용자."""
        return sorted(set(self.base) - set(self.new))

    @property
    def verdict(self) -> str:
        return FAIL if len(self.new) > len(self.base) else PASS


@dataclass(frozen=True)
class Result:
    verdict: str
    groups: tuple[Group, ...]
    # 견주지 않은 사용자: (어느 쪽, 사용자 번호, 이유)
    skipped: tuple[tuple[str, str, str], ...]
    editions: tuple[int, int]  # 가진 책과 제목이 같은 다른 판본(기준, 후보)
    users: int


def compare(
    base: Mapping[str, Any],
    new: Mapping[str, Any],
    kinds: Mapping[str, Mapping[str, str]],
) -> Result:
    """두 결과를 견준다. kinds 는 묶음의 종류별 정보(어느 묶음에 드는지)다. 먼저 mismatch 로 거른다."""
    was = {s["case_id"]: s for s in base["scores"]}
    now = {s["case_id"]: s for s in new["scores"]}
    skipped = tuple(
        (side, case_id, reason)
        for side, scores in (("기준", was), ("후보", now))
        for case_id, score in sorted(scores.items())
        if (reason := unusable(score))
    )
    bad = {case_id for _, case_id, _ in skipped}
    ids = sorted(set(was) - bad)

    by_group: dict[str, list[str]] = {}
    for case_id in ids:
        by_group.setdefault(kinds[was[case_id]["kind"]]["group"], []).append(case_id)
    groups = tuple(
        Group(
            name=name,
            users=len(members),
            base=tuple(i for i in members if missed(was[i])),
            new=tuple(i for i in members if missed(now[i])),
        )
        for name, members in sorted(
            by_group.items(), key=lambda item: (item[0] != "typical", item[0])
        )
    )

    def editions(scores: Mapping[str, Mapping[str, Any]]) -> int:
        return sum(scores[i]["editions"] or 0 for i in ids)

    # 온전히 잰 사용자만으로 늘었으면 다시 재도 그대로라 탈락을 먼저 낸다
    if any(group.verdict == FAIL for group in groups):
        verdict = FAIL
    elif skipped:
        verdict = REMEASURE
    else:
        verdict = PASS
    return Result(
        verdict=verdict,
        groups=groups,
        skipped=skipped,
        editions=(editions(was), editions(now)),
        users=len(was),
    )


def _ids(ids: Sequence[str], shown: int = 10) -> str:
    """사용자 번호. 많으면 앞의 몇 명과 나머지 수만 적는다. 다 보려면 두 결과 파일을 본다."""
    if not ids:
        return "-"
    rest = f" 외 {len(ids) - shown}명" if len(ids) > shown else ""
    return ", ".join(ids[:shown]) + rest


def render(result: Result, bundle_version: int, top: int) -> str:
    """판정표. PR 본문에 그대로 붙일 수 있게 마크다운으로 낸다."""
    lines = [
        "## 홈 추천 판정",
        "",
        f"- 묶음 판 {bundle_version}, 사용자 {result.users}명, 위 {top}권",
        f"- 판정: **{result.verdict}**",
    ]
    if result.skipped:
        lines.append(
            f"- 견주지 않은 사용자 {len({i for _, i, _ in result.skipped})}명(아래 표에서 뺐습니다): "
            + ", ".join(f"{i}({side} {why})" for side, i, why in result.skipped)
        )
    if result.verdict == REMEASURE:
        lines.append(
            "- 남은 사용자로는 늘지 않았지만, 뺀 사용자가 빠졌는지 알 수 없습니다. 다시 재도 남으면 그 사용자를 봅니다"
        )
    lines += [
        "",
        "고른 관심사가 한 권도 안 나온 사용자(묶음마다 기준보다 늘면 탈락):",
        "",
        "| 묶음 | 기준 | 후보 | 새로 빠진 사용자 | 다시 채워진 사용자 | 판정 |",
        "| --- | --- | --- | --- | --- | --- |",
        *(
            f"| {g.name} | {len(g.base)}/{g.users} | {len(g.new)}/{g.users} "
            f"| {_ids(g.worse)} | {_ids(g.better)} | {g.verdict} |"
            for g in result.groups
        ),
    ]
    before, after = result.editions
    note = " (늘었습니다. 탈락은 아니고 표시만 합니다)" if after > before else ""
    lines += [
        "",
        f"- 가진 책과 제목이 같은 다른 판본: 기준 {before}권 → 후보 {after}권{note}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="홈 추천 평가 결과 둘을 사용자끼리 짝지어 판정한다"
    )
    parser.add_argument("--base", required=True, help="기준(main) 결과 파일")
    parser.add_argument("--new", required=True, help="후보(PR) 결과 파일")
    args = parser.parse_args(argv)

    from evals.feed.run import load_bundle

    bundle = load_bundle()
    base, new = load(Path(args.base)), load(Path(args.new))
    reason = mismatch(base, new)
    if reason is None and base.get("bundle_version") != bundle["version"]:
        # 사용자의 종류와 묶음은 지금 cases.json 에서 읽는다
        reason = f"결과의 묶음 판({base.get('bundle_version')})이 지금 cases.json({bundle['version']})과 다르다"
    if reason:
        print(f"견주지 않는다: {reason}", file=sys.stderr)
        return 2
    result = compare(base, new, bundle["kinds"])
    print(render(result, bundle["version"], bundle["top"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
