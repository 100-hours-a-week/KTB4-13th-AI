#!/usr/bin/env python3
"""docs/ → GitHub Wiki 새 페이지 생성. 기본은 계획만 출력(--apply 없으면 파일을 쓰지 않음).

안전장치
  - 원본 위키(백업)에 있는 페이지 이름과 겹치면 중단
  - 이 스크립트가 만든 적 없는 기존 페이지를 덮어쓰려 하면 중단
  - _Sidebar.md 는 --sidebar 를 줬고 백업에 원본이 있을 때만 덮어씀
  - 아무것도 삭제하지 않음. 커밋·푸시는 사람이 직접

python wiki_export.py --docs docs --backup-root backup --wiki ../wiki-publish [--sidebar] [--apply]
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mdlib import CODES, STATUSES, load_docs, latest_backup, as_list, wiki_slug  # noqa: E402

MANIFEST = "_ktb4-generated.json"
TYPE_LABEL = {"design": "설계", "rationale": "선택 근거", "spec": "명세", "appendix": "부록",
              "hub": "허브", "planning": "계획", "guide": "가이드", "decision": "ADR",
              "decision-log": "결정 로그"}
MAIN_TYPES = ["hub", "decision-log", "design", "spec", "planning", "guide", "decision", "rationale"]
GROUPS = [("🧭 결정·참고", ["DEC", "TEAM"]), ("📋 기획·애자일", ["PM", "REL", "SPR"]),
          ("🧩 풀스택", ["FE", "BE", "FS"]), ("🤖 AI", ["AI"]), ("☁️ 클라우드", ["CLD"])]
LINK_RE = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s#]+\.md)(#[^)\s]*)?\)")
ORIGINAL_URL = "https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/"


def code_of(title):
    m = re.match(r"^([A-Z]+)(?:-([A-Z]?\d+))?\s", title)
    return (m.group(1), m.group(2) or "") if m else ("", "")


def strip_prefix(title):
    return re.sub(r"^[A-Z]+(?:-[A-Z]?\d+)?\s", "", title)


def doc_order(x):
    o = x["meta"].get("order")
    try:
        o = int(o) if o not in (None, "", []) else 99
    except (TypeError, ValueError):
        o = 99
    t = x["meta"].get("type")
    return (o, MAIN_TYPES.index(t) if t in MAIN_TYPES else 99, x["meta"].get("wiki", ""))


def doc_label(x, members):
    """그룹 안 링크 텍스트. type이 서로 다르면 type 이름(설계·선택 근거), 같은 type이 여럿(변환 그룹)이면 제목."""
    types = [m["meta"].get("type") for m in members]
    if types.count(x["meta"].get("type")) == 1:
        return TYPE_LABEL.get(x["meta"].get("type"), "문서")
    return strip_prefix(x["meta"]["wiki"])


def render(d, by_path, groups, repo_url, branch, docs_root, warnings):
    m = d["meta"]
    parts = [f"{STATUSES.get(m.get('status'), '')} {m.get('status', '')}".strip(),
             f"담당 {m.get('owner', '-')}", f"수정 {m.get('updated', '-')}"]
    members = sorted(groups.get(m.get("group"), []), key=doc_order)
    pair = [x for x in members if x is not d]
    types = [x["meta"].get("type") for x in members]
    if pair and len(set(types)) < len(types):
        # 변환 그룹(상위 + 하위 페이지): 하위는 상위 링크만, 상위는 하위 개수만 (목록은 사이드바)
        parent = members[0]
        if d is parent:
            parts.append(f"하위 문서 {len(pair)}개 (사이드바)")
        else:
            parts.append(f"상위 [{strip_prefix(parent['meta']['wiki'])}]({wiki_slug(parent['meta']['wiki'])})")
    elif pair:
        links = " · ".join(f"[{doc_label(x, members)}]({wiki_slug(x['meta']['wiki'])})" for x in pair)
        parts.append(f"짝 문서 {links}")
    srcs = as_list(m.get("sources")) or [s for x in members for s in as_list(x["meta"].get("sources"))]
    if srcs and m.get("type") != "appendix":
        orig = " · ".join(f"[{Path(s).stem}]({ORIGINAL_URL}{quote(Path(s).stem)})" for s in dict.fromkeys(srcs))
        parts.append(f"원본 {orig}")
    header = "> " + " · ".join(parts) + "\n\n"

    body_lines, in_code = [], False
    for ln in d["body"].split("\n"):
        if ln.strip().startswith("```"):
            in_code = not in_code
        if not in_code:
            def repl(mt):
                target = (d["path"].parent / mt.group(1)).resolve()
                t = by_path.get(target)
                if not t:
                    warnings.append(f"{d['path']}: docs 밖 링크 {mt.group(1)} (그대로 둠)")
                    return mt.group(0)
                return f"]({wiki_slug(t['meta']['wiki'])}{mt.group(2) or ''})"
            ln = LINK_RE.sub(repl, ln)
            if re.search(r"!\[[^\]]*\]\((?!https?://)", ln):
                warnings.append(f"{d['path']}: 상대 경로 이미지는 위키에서 안 보일 수 있음")
        body_lines.append(ln)
    rel = d["path"].relative_to(docs_root.parent).as_posix()
    footer = (f"\n\n---\n> ✏️ 이 페이지는 레포 [{rel}]({repo_url}/blob/{branch}/{quote(rel)})에서 수정한다. "
              "위키에서 직접 고치면 다음 반영 때 덮어써진다.\n")
    return header + "\n".join(body_lines).rstrip() + footer


def sidebar(docs, repo_url, branch, backup_name):
    buckets = {}
    for d in docs:
        t = d["meta"].get("wiki", "")
        code, num = code_of(t)
        buckets.setdefault(code, {}).setdefault((num, d["meta"].get("group")), []).append(d)
    out = [f"**[🏠 Home](Home)** · [🗄️ 변환 전 원본]({repo_url}/tree/{branch}/backup/{quote(backup_name)})", ""]
    for label, codes in GROUPS:
        lines = []
        for code in codes:
            for (num, _g), members in sorted(buckets.get(code, {}).items(),
                                             key=lambda kv: (int(re.sub(r"\D", "", kv[0][0]) or 0), kv[0][1] or "")):
                members = [x for x in members if x["meta"].get("type") != "appendix"]
                if not members:
                    continue
                members.sort(key=doc_order)
                main = members[0]["meta"]["wiki"]
                name = strip_prefix(main)
                prefix = f"{num} " if num and int(re.sub(r"\D", "", num) or 0) != 0 else ""
                types = [x["meta"].get("type") for x in members]
                if len(members) == 1:
                    lines.append(f"- [{prefix}{name}]({wiki_slug(main)})")
                elif len(set(types)) == len(types):
                    # 새 문서 그룹(설계 · 선택 근거): 한 줄
                    for w in ("설계", "명세", "선택 근거", "허브", "정의"):
                        if name.endswith(" " + w):
                            name = name[: -len(w) - 1]
                    links = " · ".join(f"[{TYPE_LABEL.get(x['meta'].get('type'), '문서')}]({wiki_slug(x['meta']['wiki'])})" for x in members)
                    lines.append(f"- {prefix}{name}: {links}")
                else:
                    # 변환 그룹(상위 페이지 + 하위 페이지): 대표 아래 중첩 목록
                    lines.append(f"- {prefix}[{name}]({wiki_slug(main)})")
                    for x in members[1:]:
                        lines.append(f"  - [{strip_prefix(x['meta']['wiki'])}]({wiki_slug(x['meta']['wiki'])})")
        if lines:
            out += [f"**{label}**", *lines, ""]
    out.append("_이 사이드바는 `wiki_export.py --sidebar` 가 생성한다. 직접 수정하지 않는다._")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default="docs")
    ap.add_argument("--backup-root", default="backup")
    ap.add_argument("--backup", help="백업 폴더 이름 (기본: 최신)")
    ap.add_argument("--wiki", required=True, help="반영 전용 위키 clone 경로")
    ap.add_argument("--repo-url", default="https://github.com/100-hours-a-week/KTB4-13th-wiki")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--sidebar", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    docs_root, wiki = Path(a.docs).resolve(), Path(a.wiki)
    bdir = latest_backup(Path(a.backup_root), a.backup)
    if not bdir:
        print("[중단] 백업이 없습니다. 먼저 scripts/backup_wiki.sh 로 원본을 백업하세요.")
        return 1
    if not (wiki / ".git").exists():
        print(f"[중단] {wiki} 는 위키 git clone 이 아닙니다.")
        return 1
    originals = {p.relative_to(bdir).as_posix() for p in bdir.rglob("*") if p.is_file()}
    mpath = wiki / MANIFEST
    generated = set(json.loads(mpath.read_text(encoding="utf-8"))) if mpath.exists() else set()

    docs = [d for d in load_docs(docs_root) if d["meta"].get("wiki")]
    by_path = {d["path"].resolve(): d for d in docs}
    groups = {}
    for d in docs:
        groups.setdefault(d["meta"].get("group"), []).append(d)

    plan, errors, warnings = [], [], []
    for d in docs:
        fname = wiki_slug(d["meta"]["wiki"]) + ".md"
        if fname in originals:
            errors.append(f"{d['path']}: `{fname}` 은 원본 위키 페이지 이름과 같음 → 제목 변경 필요")
            continue
        dest = wiki / fname
        if dest.exists() and fname not in generated:
            errors.append(f"{d['path']}: `{fname}` 이 위키에 이미 있고 이 스크립트가 만든 페이지가 아님 → 중단")
            continue
        content = render(d, by_path, groups, a.repo_url, a.branch, docs_root, warnings)
        old = dest.read_text(encoding="utf-8") if dest.exists() else None
        plan.append((fname, content, "새 페이지" if old is None else ("변경" if old != content else "같음")))

    if a.sidebar:
        if "_Sidebar.md" in originals or not (wiki / "_Sidebar.md").exists():
            content = sidebar(docs, a.repo_url, a.branch, bdir.name)
            old = (wiki / "_Sidebar.md").read_text(encoding="utf-8") if (wiki / "_Sidebar.md").exists() else None
            plan.append(("_Sidebar.md", content, "새 페이지" if old is None else ("변경" if old != content else "같음")))
        else:
            errors.append("_Sidebar.md 원본이 백업에 없음 → 백업 후 다시 실행")

    present = {p.name for p in wiki.glob("*.md")}
    orphaned = sorted(generated - {f for f, _, _ in plan} - {"_Sidebar.md"})
    for f in orphaned:
        if f in present:
            warnings.append(f"`{f}` 는 docs에서 사라졌지만 삭제하지 않음 (필요하면 사람이 판단)")

    for w in warnings:
        print(f"[경고] {w}")
    for e in errors:
        print(f"[오류] {e}")
    if errors:
        print("\n오류가 있어 아무것도 쓰지 않았습니다.")
        return 1
    for fname, _, state in plan:
        print(f"  {state:5}  {fname}")
    if not a.apply:
        print("\n계획만 출력했습니다. 반영하려면 --apply")
        return 0
    for fname, content, state in plan:
        if state != "같음":
            (wiki / fname).write_text(content, encoding="utf-8")
    mpath.write_text(json.dumps(sorted(generated | {f for f, _, _ in plan if f != "_Sidebar.md"}),
                                ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{wiki} 에 썼습니다. git diff 로 확인한 뒤 직접 커밋·푸시하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
