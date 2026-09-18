#!/usr/bin/env python3
"""docs/ 문서 규칙 검사. 오류가 있으면 종료 코드 1.

python check_docs.py docs
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mdlib import TYPES, STATUSES, TITLE_RE, FORBIDDEN, load_docs, as_list  # noqa: E402

LIMITS = {  # (최대 줄, 최대 ## 섹션)
    "hub": (60, 4), "design": (150, 5), "rationale": (100, 5), "spec": (250, 5),
    "decision": (40, 4), "decision-log": (None, None), "planning": (80, 4),
    "guide": (100, 4), "appendix": (400, None),
}
REQUIRED = ["wiki", "type", "group", "owner", "status", "updated"]
PLACEHOLDERS = [r"your-link", r"여기에_", r"URL_삽입", r"\bTODO\b", r"<실행 링크>",
                r"@담당자", r"YYYY-MM-DD"]
BAD_LINK_TEXT = {"여기", "링크", "이것", "클릭", "here", "link", "click"}
ORIGINAL_WIKI = "github.com/100-hours-a-week/KTB4-13th-wiki/wiki/"
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
TOP_LABEL_RE = re.compile(r"^(?:\d+\.|-)\s+.*\*\*")
NESTED_ORDERED_RE = re.compile(r"^(\s+)\d+\.\s")


def check(docs_root: Path):
    issues = []  # (level, path, line, msg)
    docs = load_docs(docs_root)
    titles = {}
    converted_groups = {d["meta"].get("group") for d in docs if as_list(d["meta"].get("sources"))}

    def add(level, d, line, msg):
        issues.append((level, d["path"], line, msg))

    for d in docs:
        m, body, start = d["meta"], d["body"], d["start"]
        converted = bool(as_list(m.get("sources"))) or m.get("group") in converted_groups

        if not m:
            add("오류", d, 1, "frontmatter 없음")
            continue
        for k in REQUIRED:
            if not m.get(k):
                add("오류", d, 1, f"frontmatter `{k}` 없음")
        t = m.get("type", "")
        if t and t not in TYPES:
            add("오류", d, 1, f"알 수 없는 type `{t}`")
        if m.get("status") and m["status"] not in STATUSES:
            add("오류", d, 1, f"status는 {'/'.join(STATUSES)} 중 하나")
        if m.get("updated") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(m["updated"])):
            add("오류", d, 1, "updated 형식은 YYYY-MM-DD")

        title = m.get("wiki", "")
        if isinstance(title, str) and title:
            if not TITLE_RE.match(title):
                add("오류", d, 1, f"제목 규칙 위반 `{title}` (코드[-번호] 주제 종류)")
            bad = sorted(c for c in title if c in FORBIDDEN)
            if bad:
                add("오류", d, 1, f"제목 금지 문자 {''.join(bad)}")
            if len(title) > 30:
                add("오류", d, 1, f"제목 30자 초과 ({len(title)}자)")
            if t == "appendix" and not title.endswith("부록"):
                add("오류", d, 1, "부록 제목은 `부록`으로 끝나야 함")
            if title in titles:
                add("오류", d, 1, f"제목 중복: {titles[title]}")
            titles[title] = d["path"]

        lines = body.split("\n")
        in_code = False
        nonempty = 0
        sections = 0
        in_comment = False
        for i, ln in enumerate(lines):
            no = start + i
            s = ln.strip()
            if "<!--" in s and "-->" not in s:
                in_comment = True
                continue
            if in_comment:
                if "-->" in s:
                    in_comment = False
                continue
            if s.startswith("<!--") and s.endswith("-->"):
                continue
            if s.startswith("```"):
                if not in_code and s == "```":
                    add("오류", d, no, "코드 블록 언어 지정 없음")
                in_code = not in_code
                if s:
                    nonempty += 1
                continue
            if s:
                nonempty += 1
            if in_code:
                continue
            if re.match(r"^# ", ln):
                add("오류", d, no, "`#` 제목 사용 금지 (`##`부터)")
            if re.match(r"^#### ", ln):
                if converted:
                    add("경고", d, no, "`####` 이하 (변환 문서는 원본 계층 유지 — 나누지 않는다)")
                else:
                    add("오류", d, no, "`####` 이하 금지 (문서 분리 신호)")
            if re.match(r"^## ", ln):
                sections += 1
            if TOP_LABEL_RE.match(ln):
                j = i + 1
                if j < len(lines) and NESTED_ORDERED_RE.match(lines[j]):
                    indent = NESTED_ORDERED_RE.match(lines[j]).group(1)
                    k = j + 1
                    while k < len(lines) and lines[k].strip() == "":
                        k += 1
                    has_sibling = k < len(lines) and lines[k].startswith(indent) and NESTED_ORDERED_RE.match(lines[k])
                    if not has_sibling:
                        add("경고", d, no,
                            "가짜 중첩 목록 (자식 1개짜리 번호 목록) → block-patterns#5 `- **레이블**: 설명` 모양으로")
            for pat in PLACEHOLDERS:
                if re.search(pat, ln):
                    add("경고", d, no, f"자리표시자 의심: `{re.search(pat, ln).group(0)}`")
                    break
            for text, target in LINK_RE.findall(ln):
                if text.strip().lower() in BAD_LINK_TEXT:
                    add("경고", d, no, f"링크 텍스트가 목적지를 설명하지 않음: `{text}`")
                if target.startswith(("http://", "https://")):
                    if ORIGINAL_WIKI in target:
                        add("경고", d, no, "원본 위키 직접 링크 (변환 문서가 생기면 docs 상대 링크로)")
                    continue
                if target.startswith(("#", "mailto:")):
                    continue
                rel = target.split("#")[0]
                if rel and not (d["path"].parent / rel).resolve().exists():
                    add("오류", d, no, f"깨진 링크: {target}")
            if s.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", s):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if len(cells) > 5 and not converted:
                    add("경고", d, no, f"표 열 {len(cells)}개 (5개 이하)")
                if any(len(c) > 60 and len(re.findall(r"[.!?](?:\s|$)", c)) >= 2 for c in cells):
                    add("경고", d, no, "긴 셀에 문장이 두 개 이상 (첫 문장 + 각주로)")
                elif not converted and any(len(c) > 80 for c in cells):
                    add("경고", d, no, "표 셀이 너무 김 (한 문장 이내)")
            if re.search(r"(습니다|합니다)[.\s]*$", s) and not s.startswith("|"):
                add("경고", d, no, "\"~한다\"체로 (변환 문서도 어미 통일, 결정 #16)")
            if s.startswith("~~~"):
                add("경고", d, no, "코드 펜스는 ``` 로")
            if s in ("---", "***") and no > start + 1:
                add("경고", d, no, "구분선 제거 (제목이 구분한다)")
            if "<br" in s:
                add("경고", d, no, "셀 안 <br> 금지 (각주로)")

        if in_code:
            add("오류", d, start + len(lines), "코드 블록이 닫히지 않음")
        max_lines, max_sec = LIMITS.get(t, (None, None))
        exempt = m.get("limit_exempt") or converted  # 변환 문서는 원본 분량 그대로라 경고만
        if max_lines and nonempty > max_lines:
            add("경고" if exempt else "오류", d, 1,
                f"{nonempty}줄 > 상한 {max_lines}줄 → " + ("긴 블록을 제자리에서 접기" if converted else "부록으로 옮기기")
                + (f" (예외: {exempt})" if exempt and not converted else ""))
        if max_sec and sections > max_sec:
            add("경고" if exempt else "오류", d, 1, f"## 섹션 {sections}개 > 상한 {max_sec}개")
        if not re.search(r"^\*\*요약\*\*", body, re.M):
            add("경고", d, 1, "`**요약**` 줄 없음")

    return docs, issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("docs", nargs="?", default="docs")
    a = ap.parse_args()
    root = Path(a.docs)
    if not root.exists():
        print(f"{root} 없음")
        return 1
    docs, issues = check(root)
    errors = [x for x in issues if x[0] == "오류"]
    for level, path, line, msg in sorted(issues, key=lambda x: (str(x[1]), x[2])):
        print(f"[{level}] {path}:{line} {msg}")
    print(f"\n문서 {len(docs)}개 · 오류 {len(errors)} · 경고 {len(issues) - len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
