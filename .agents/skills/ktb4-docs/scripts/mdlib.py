"""공통 유틸: frontmatter 파싱, docs 수집, 백업 폴더 찾기. 외부 패키지 없이 동작."""
from __future__ import annotations
import re
from pathlib import Path

CODES = ["DEC", "TEAM", "PM", "REL", "SPR", "FS", "FE", "BE", "AI", "CLD"]
TYPES = {"hub", "design", "rationale", "spec", "decision", "decision-log",
         "planning", "guide", "appendix"}
STATUSES = {"미작성": "⬜", "작성중": "🚧", "완료": "✅", "보관": "🗄️"}
TITLE_RE = re.compile(r"^(%s)(-[A-Z]?\d{1,3})? \S.*$" % "|".join(CODES))
FORBIDDEN = set("+/:?#[]().,‐")


def _clean(v: str) -> str:
    v = re.sub(r"\s+#.*$", "", v).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def parse_doc(text: str):
    """(meta, body, body_start_line) 반환. body_start_line은 1부터 시작하는 줄 번호."""
    if not text.startswith("---\n"):
        return {}, text, 1
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text, 1
    fm = text[4:end]
    rest = text[end + 4:]
    if rest.startswith("\n"):
        rest = rest[1:]
    meta: dict = {}
    key = None
    for ln in fm.split("\n"):
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]+):\s*(.*)$", ln)
        if m:
            key, val = m.group(1), _clean(m.group(2))
            if val == "":
                meta[key] = []
            elif val.startswith("[") and val.endswith("]"):
                meta[key] = [_clean(x) for x in val[1:-1].split(",") if x.strip()]
            else:
                meta[key] = val
            continue
        m2 = re.match(r"^\s*-\s*(.+)$", ln)
        if m2 and key:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_clean(m2.group(1)))
    start = fm.count("\n") + 3
    return meta, rest, start


def load_docs(docs_root: Path):
    docs = []
    for p in sorted(docs_root.rglob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        text = p.read_text(encoding="utf-8")
        meta, body, start = parse_doc(text)
        docs.append({"path": p, "meta": meta, "body": body, "start": start})
    return docs


def latest_backup(backup_root: Path, name: str | None = None) -> Path | None:
    if not backup_root.exists():
        return None
    if name:
        p = backup_root / name
        return p if p.exists() else None
    dirs = sorted(d for d in backup_root.iterdir()
                  if d.is_dir() and d.name.startswith("wiki-original-"))
    return dirs[-1] if dirs else None


def as_list(v):
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def wiki_slug(title: str) -> str:
    return title.replace(" ", "-")
