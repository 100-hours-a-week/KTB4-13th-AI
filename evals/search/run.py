"""① 검색 평가 묶음을 돌려 채점하는 실행기(#495).

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.run            # 몇 건을 돌릴지만 본다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.run --yes      # 실제로 돌린다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.search.run --yes --code ../v1

검색은 따로 띄운 파이썬에서 부르고(collect.py), 채점은 이 저장소의 코드로 한다. `--code` 로 다른 커밋의
worktree 를 주면 검색만 그 코드로 바뀐다. V1 운영 코드처럼 채점에 쓰는 모듈(editions 등)이 없는 커밋도 같은
묶음, 같은 규칙으로 잰다.

검색은 DB 에 쓰지 않는다. 그래도 묶음의 책을 고른 평가 DB 가 아닌 곳에서 잰 숫자와 섞이지 않게 이름이
`_eval` 로 끝나는 DB 에만 돌린다.
"""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CASES_PATH = Path(__file__).with_name("cases.json")
# 검색할 코드의 기본값. 이 저장소다
REPO = Path(__file__).resolve().parents[2]
EVAL_DB_SUFFIX = "_eval"


def load_bundle() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def select(
    cases: Sequence[dict],
    *,
    kinds: Sequence[str] | None = None,
    ids: Sequence[str] | None = None,
) -> list[dict]:
    """돌릴 사례를 고른다. 없는 이름을 주면 ValueError 다 — 오타로 0건을 돌리지 않게 한다."""
    for label, wanted, known in (
        ("종류", kinds, {c["kind"] for c in cases}),
        ("번호", ids, {c["id"] for c in cases}),
    ):
        unknown = sorted(set(wanted or ()) - known)
        if unknown:
            raise ValueError(f"묶음에 없는 {label}: {', '.join(unknown)}")
    return [
        case
        for case in cases
        if (not kinds or case["kind"] in kinds) and (not ids or case["id"] in ids)
    ]


def request(case: dict, top: int) -> dict:
    """사례의 /search 요청. 관련도순 위 top 권을 한 번에 받는다."""
    return {"query": case["query"], "filters": case.get("filters", {}), "size": top}


def database_name(url: str) -> str:
    """DB 주소에서 이름만. 주소에는 계정과 비밀번호가 들어 있을 수 있어 그대로 찍지 않는다."""
    return urlsplit(url).path.lstrip("/")


def refusal(name: str, *, allow_any: bool) -> str | None:
    """이 DB 에 돌리면 안 되는 이유. 돌려도 되면 None."""
    if allow_any or name.endswith(EVAL_DB_SUFFIX):
        return None
    return (
        f"DB '{name}' 는 평가용이 아니다. 묶음의 책은 평가 DB 에서 골랐으므로 이름이 "
        f"'{EVAL_DB_SUFFIX}' 로 끝나는 DB 에만 돌린다. 그래도 돌리려면 --any-db 를 붙인다."
    )


def code_label(code: Path) -> str:
    """잰 코드의 커밋. app 을 고친 채로 쟀으면(상수만 바꿔 본 실험) 그렇다고 붙인다."""

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(code), *args], capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        changed = git("status", "--porcelain", "--untracked-files=no", "--", "app")
    except (OSError, subprocess.CalledProcessError):
        return code.name
    return f"{commit} (app 을 고친 채)" if changed else commit
