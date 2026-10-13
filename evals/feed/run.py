"""④⑥ 가상 사용자 묶음을 돌려 채점하는 실행기(#392).

    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.feed.run            # 몇 명을 돌릴지만 본다
    DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.feed.run --yes      # 실제로 돌린다

사용자마다 프로필 API(`POST /preferences/profile`)와 홈 추천 API(`GET /recommendations/feed`)를 같은
프로세스에서 그대로 부른다. 서버를 따로 띄우지 않고, 요청 검사·프로필 계산·후보 뽑기·채점을 실제 요청과 같은
길로 지난다. LLM 은 부르지 않는다. 취향 기억의 벡터만 로컬 임베딩 모델로 만든다.

가상 사용자의 프로필을 DB 에 쓰므로(끝나면 지운다) 이름이 `_eval` 로 끝나는 DB 에만 돌린다. 책 표를 만드는
방법은 README 에 있다.
"""

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

from evals.feed.scoring import Book, Response, Score, score, summarize

CASES_PATH = Path(__file__).with_name("cases.json")
PROFILE_URL = "/preferences/profile"
FEED_URL = "/recommendations/feed"
CALL_TIMEOUT_SECONDS = 60
# 가상 사용자의 프로필을 쓰는 DB. 개발용이나 운영 DB 에 쓰지 않게 이름으로 막는다.
EVAL_DB_SUFFIX = "_eval"

Target = Callable[[dict[str, Any]], Awaitable[Response]]
Facts = Callable[[set[str]], Awaitable[dict[str, Book]]]

# 받은 책의 정보를 한 번에 읽는다. 세부 분류·독자 대상·쪽수는 적재할 때 남은 원본 표(stg_cat_books)에만 있다.
# 그 표가 비어 있으면 세 칸이 비고, 그 칸으로 가르는 관심사는 "알 수 없음"이 된다.
_FACTS_SQL = """
SELECT b.isbn13, b.title, b.author, b.category, b.pub_year, b.price, b.description,
       s.kdc, s.reader, s.pages
FROM v_books b
LEFT JOIN (
    SELECT data->>'isbn13' AS isbn13,
           nullif(data->>'d4l_class_no', '') AS kdc,
           nullif(data->>'reader_code', '') AS reader,
           nullif(data->>'page_count', '')::int AS pages
    FROM stg_cat_books
    WHERE data->>'isbn13' = ANY($1::text[])
) s USING (isbn13)
WHERE b.isbn13 = ANY($1::text[])
"""


def load_bundle() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def load_cases(bundle: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """사용자 목록. 사용자마다 자기 종류의 묶음(typical·guard)을 `group` 으로 붙여 준다."""
    bundle = bundle or load_bundle()
    return [
        {**case, "group": bundle["kinds"][case["kind"]]["group"]}
        for case in bundle["cases"]
    ]


def select(
    cases: Sequence[dict[str, Any]],
    *,
    kinds: Sequence[str] | None = None,
    ids: Sequence[str] | None = None,
    group: str | None = None,
) -> list[dict[str, Any]]:
    """돌릴 사용자를 고른다. 없는 이름을 주면 ValueError 다 — 오타로 0명을 돌리지 않게 한다."""
    for label, wanted, known in (
        ("종류", kinds, {c["kind"] for c in cases}),
        ("번호", ids, {c["id"] for c in cases}),
        ("묶음", [group] if group else None, {c["group"] for c in cases}),
    ):
        unknown = sorted(set(wanted or ()) - known)
        if unknown:
            raise ValueError(f"묶음에 없는 {label}: {', '.join(unknown)}")

    picked = [
        case
        for case in cases
        if (not kinds or case["kind"] in kinds)
        and (not ids or case["id"] in ids)
        and (not group or case["group"] == group)
    ]
    if ids:
        picked.sort(key=lambda case: list(ids).index(case["id"]))
    return picked


def twins_of(
    picked: Sequence[dict[str, Any]], by_id: Mapping[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """고른 사용자의 짝 가운데 고른 목록에 없는 사용자. 부르기는 하되 채점하지는 않는다."""
    have = {case["id"] for case in picked}
    extra: dict[str, dict[str, Any]] = {}
    for case in picked:
        twin = case["expect"].get("compare_to")
        if twin and twin not in have:
            extra[twin] = by_id[twin]
    return list(extra.values())


async def run(
    picked: Sequence[dict[str, Any]],
    by_id: Mapping[str, dict[str, Any]],
    target: Target,
    facts: Facts,
    interests: Mapping[str, Any],
) -> list[Score]:
    """고른 사용자를 차례로 부르고 채점한다. 한 번에 한 명씩 부른다(시간을 재기 때문이다)."""
    responses: dict[str, Response] = {}
    for case in [*picked, *twins_of(picked, by_id)]:
        responses[case["id"]] = await target(case)

    # 책 정보는 받은 책을 모아 한 번에 묻는다. 가진 책은 목록에 나오지 않으니 따로 넣는다.
    isbns = {isbn for response in responses.values() for isbn in response.isbns}
    for case in picked:
        history = case["user"]["history"]
        isbns.update(history["purchased_isbns"], history["library_isbns"])
    shelf = await facts(isbns)

    return [
        score(
            case,
            responses[case["id"]],
            shelf,
            interests,
            twin=responses.get(case["expect"].get("compare_to")),
        )
        for case in picked
    ]


def database_name(url: str) -> str:
    """DB 주소에서 이름만. 주소에는 계정과 비밀번호가 들어 있을 수 있어 그대로 찍지 않는다."""
    return urlsplit(url).path.lstrip("/")


def refusal(name: str, *, allow_any: bool) -> str | None:
    """이 DB 에 돌리면 안 되는 이유. 돌려도 되면 None."""
    if allow_any or name.endswith(EVAL_DB_SUFFIX):
        return None
    return (
        f"DB '{name}' 는 평가용이 아니다. 실행기는 가상 사용자의 프로필을 DB 에 쓰므로 이름이 "
        f"'{EVAL_DB_SUFFIX}' 로 끝나는 DB 에만 돌린다. 그래도 돌리려면 --any-db 를 붙인다."
    )


def _num(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _arrow(before: float | None, after: float | None) -> str:
    return "-" if before is None or after is None else f"{before:.1f} → {after:.1f}"


def render(summary: dict[str, Any], top: int) -> str:
    """사람이 읽는 결과 표. PR 본문에 그대로 붙일 수 있게 마크다운으로 낸다."""

    def row(name: str, item: dict[str, Any]) -> str:
        measured = item["measured"]
        return (
            f"| {name} | {item['users']} | {_num(item['least_mean'])} "
            f"| {item['missing_users']}/{measured} | {item['low_users']}/{measured} "
            f"| {_num(item['none_mean'])} | {_num(item['unsure_mean'])} "
            f"| {_num(item.get('seconds_median'), 3)} |"
        )

    kinds = summary["kinds"]
    lines = [
        f"위 {top}권 기준. † 는 낱말로 가르는 거친 기준이 낀 종류다.",
        "",
        (
            "| 종류 | 사용자 | 가장 적게 나온 관심사(평균 권수) | 한 권도 안 나온 관심사가 있는 사용자 "
            "| 2권 이하인 관심사가 있는 사용자 | 어느 관심사도 아닌 책(평균) | 알 수 없는 책(평균) "
            "| 시간(초, 중앙값) |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        *(
            row(f"{kind}{' †' if item['rough'] else ''}", item)
            for kind, item in kinds.items()
        ),
        *(row(f"**{group} 합계**", item) for group, item in summary["groups"].items()),
    ]

    failed = sum(item["users"] - item["measured"] for item in kinds.values())
    if failed:
        lines += [
            "",
            f"- 재지 못한 사용자 {failed}명(API 오류, 벡터 없이 답함, 개인화 여부가 기대와 다름)",
        ]

    paired = {k: v for k, v in kinds.items() if "twin_show_mean" in v}
    if paired:
        lines += [
            "",
            "**짝과 견줌** (마지막 취향 기억 하나를 더하기 전 → 더한 뒤)",
            "",
            f"| 종류 | 나와야 하는 관심사의 책 | 늘면 안 되는 관심사의 책 | 기울어야 하는 쪽의 책 | 짝과 같은 책({top}권 중) |",
            "| --- | --- | --- | --- | --- |",
            *(
                f"| {kind} | {_arrow(item['twin_show_mean'], item['show_mean'])} "
                f"| {_arrow(item.get('twin_avoid_mean'), item.get('avoid_mean'))} "
                f"| {_arrow(item.get('twin_toward_mean'), item.get('toward_mean'))} "
                f"| {_num(item['same_as_twin_mean'])} |"
                for kind, item in paired.items()
            ),
        ]

    alone = {k: v for k, v in kinds.items() if "avoid_mean" in v and k not in paired}
    for kind, item in alone.items():
        lines += [
            "",
            f"- {kind}: 나오면 안 되는 관심사의 책 평균 {_num(item['avoid_mean'])}권",
        ]

    for kind, item in kinds.items():
        if "owned_users" in item:
            lines += [
                "",
                (
                    f"- {kind}: 가진 책이 나온 사용자 {item['owned_users']}/{item['measured']}, "
                    f"가진 책과 제목이 같은 다른 판본 평균 {_num(item['editions_mean'])}권"
                ),
            ]

    for kind, item in kinds.items():
        if "piles" in item:
            lines += [
                "",
                f"**책이 쌓인 사용자** ({kind}: 좋아한 책의 권수가 같은 사용자끼리 견줌)",
                "",
                f"| 좋아한 책 | 사용자 | 두 사용자의 위 {top}권이 겹치는 권수 | 사용자들의 위 {top}권에 나온 서로 다른 책 |",
                "| --- | --- | --- | --- |",
                *(
                    f"| {size}권 | {pile['users']} | {_num(pile['overlap_mean'])} "
                    f"| {pile['distinct']}/{pile['users'] * top} |"
                    for size, pile in item["piles"].items()
                ),
            ]
    return "\n".join(lines)


async def measure(
    picked: Sequence[dict[str, Any]],
    by_id: Mapping[str, dict[str, Any]],
    bundle: Mapping[str, Any],
) -> tuple[list[Score], dict[str, Any]]:
    """실제 서버 코드에 붙어 돌린다. (채점 결과, 돌린 책 표의 정보)를 돌려준다."""
    import asyncpg
    import httpx

    from app.core.config import get_settings
    from app.extraction.service import MEMORY_VECTOR_PURPOSE
    from app.gateway import embedding
    from app.main import app

    settings = get_settings()
    headers = (
        {"Authorization": f"Bearer {settings.ai_service_token}"}
        if settings.ai_service_token
        else {}
    )
    conn = await asyncpg.connect(settings.database_url)
    users = [case["user_id"] for case in [*picked, *twins_of(picked, by_id)]]
    try:
        info = {
            "name": database_name(settings.database_url),
            "books": await conn.fetchval("SELECT count(*) FROM v_books"),
            "with_vectors": await conn.fetchval("SELECT count(*) FROM book_embeddings"),
        }
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://eval",
                timeout=CALL_TIMEOUT_SECONDS,
                headers=headers,
            ) as client,
        ):

            async def target(case: dict[str, Any]) -> Response:
                user = case["user"]
                memories = []
                if user["memories"]:
                    # 취향 기억의 벡터는 ⑤가 붙이는 것과 같은 방법으로 만든다.
                    vectors, dim, _model = await embedding.embed(
                        [m["value"] for m in user["memories"]], MEMORY_VECTOR_PURPOSE
                    )
                    memories = [
                        {**memory, "vector": vector, "dim": dim}
                        for memory, vector in zip(
                            user["memories"], vectors, strict=True
                        )
                    ]
                profile = await client.post(
                    PROFILE_URL,
                    json={
                        "user_id": case["user_id"],
                        "idempotency_key": f"eval_{case['id']}",
                        "onboarding": user["onboarding"],
                        "memories": memories,
                        "history": user["history"],
                    },
                )
                history = user["history"]
                started = perf_counter()
                feed = await client.get(
                    FEED_URL,
                    params={
                        "user_id": case["user_id"],
                        "surface": "home",
                        "size": bundle["top"],
                        # 가진 책은 BE 가 요청마다 실어 보낸다(#330). 빈 값은 "가진 책이 없다"는 뜻이다.
                        "purchased_isbns": ",".join(history["purchased_isbns"]),
                        "library_isbns": ",".join(history["library_isbns"]),
                    },
                )
                seconds = perf_counter() - started
                data = feed.json().get("data") if feed.status_code == 200 else None
                data = data or {}
                return Response(
                    profile_status=profile.status_code,
                    feed_status=feed.status_code,
                    cold_start=data.get("cold_start"),
                    degraded=feed.headers.get("X-Degraded"),
                    isbns=tuple(item["isbn"] for item in data.get("items", [])),
                    seconds=seconds,
                )

            async def facts(isbns: set[str]) -> dict[str, Book]:
                rows = await conn.fetch(_FACTS_SQL, sorted(isbns))
                return {
                    r["isbn13"]: Book(
                        isbn=r["isbn13"],
                        title=r["title"],
                        author=r["author"],
                        category=r["category"],
                        kdc=r["kdc"],
                        reader=r["reader"],
                        pages=r["pages"],
                        pub_year=r["pub_year"],
                        price=r["price"],
                        text=f"{r['title']} {r['description'] or ''}",
                    )
                    for r in rows
                }

            # 첫 요청은 DB 연결을 여느라 느리다. 재는 시간에 섞이지 않게 한 번 불러 둔다.
            await client.get(
                FEED_URL,
                params={"user_id": 2_000_000_000, "surface": "home", "size": 1},
            )
            scores = await run(picked, by_id, target, facts, bundle["interests"])
        return scores, info
    finally:
        # 가상 사용자의 프로필을 남기지 않는다. 남으면 다음에 다른 묶음 버전으로 잴 때 섞인다.
        await conn.execute(
            "DELETE FROM taste_profile WHERE user_id = ANY($1::int[])", users
        )
        await conn.close()


def _split(text: str | None) -> list[str] | None:
    return [part.strip() for part in text.split(",") if part.strip()] if text else None


def main(
    argv: Sequence[str] | None = None,
    *,
    target: Target | None = None,
    facts: Facts | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="④⑥ 가상 사용자 묶음을 돌려 홈 추천을 채점한다"
    )
    parser.add_argument("--kinds", help="돌릴 종류(쉼표로 여럿). 예: dislike,author")
    parser.add_argument("--ids", help="돌릴 사용자 번호(쉼표로 여럿). 예: dislike-01")
    parser.add_argument("--group", help="돌릴 묶음: typical 또는 guard")
    parser.add_argument("--yes", action="store_true", help="실제로 돌린다")
    parser.add_argument("--out", help="사용자마다의 결과를 남길 JSON 파일")
    parser.add_argument(
        "--any-db",
        action="store_true",
        help="이름이 _eval 로 끝나지 않는 DB 에도 돌린다",
    )
    args = parser.parse_args(argv)

    bundle = load_bundle()
    cases = load_cases(bundle)
    by_id = {case["id"]: case for case in cases}
    try:
        picked = select(
            cases, kinds=_split(args.kinds), ids=_split(args.ids), group=args.group
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    extra = twins_of(picked, by_id)
    print(
        f"사용자 {len(picked)}명(견줄 짝 {len(extra)}명을 더 부른다). "
        f"프로필과 홈 추천을 한 번씩, API 호출 {(len(picked) + len(extra)) * 2}번. LLM 은 부르지 않는다."
    )

    injected = target is not None and facts is not None
    if not injected:
        from app.core.config import get_settings

        name = database_name(get_settings().database_url)
        print(f"DB: {name}")
        reason = refusal(name, allow_any=args.any_db)
        if reason:
            print(reason, file=sys.stderr)
            return 2
    if not args.yes:
        print(
            "실제로 돌리려면 --yes 를 붙인다. 가상 사용자의 프로필을 DB 에 썼다가 끝나면 지운다."
        )
        return 0

    # 서버는 요청마다 로그를 남긴다. 수백 번 부르면 결과 표가 묻히므로 도는 동안은 경고부터만 보인다.
    logging.disable(logging.INFO)
    try:
        if injected:
            scores = asyncio.run(run(picked, by_id, target, facts, bundle["interests"]))
            database = None
        else:
            scores, database = asyncio.run(measure(picked, by_id, bundle))
    finally:
        logging.disable(logging.NOTSET)

    summary = summarize(scores, bundle["kinds"], bundle["interests"])
    print()
    if database and (
        database["books"] != bundle["catalog"]["books"]
        or database["with_vectors"] != bundle["catalog"]["with_vectors"]
    ):
        # 책 표가 다르면 같은 묶음이어도 숫자를 견줄 수 없다.
        print(
            f"주의: 이 DB 의 책 표(책 {database['books']:,}권, 벡터 {database['with_vectors']:,}권)가 "
            f"묶음을 만든 책 표(책 {bundle['catalog']['books']:,}권, 벡터 "
            f"{bundle['catalog']['with_vectors']:,}권)와 다르다.\n"
        )
    print(render(summary, bundle["top"]))

    if args.out:
        saved = {
            "bundle_version": bundle["version"],
            "catalog": bundle["catalog"],
            "database": database,
            "summary": summary,
            "scores": [asdict(s) for s in scores],
        }
        Path(args.out).write_text(
            json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n사용자마다의 결과: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
