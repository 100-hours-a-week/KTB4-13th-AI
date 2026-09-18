#!/usr/bin/env python3
"""원본(backup) 대비 변환본(docs) 보존 검사.

구조: 원본의 제목 트리(글자·순서·계층)가 변환본에 그대로 있는지 확인한다.
내용: 원본의 모든 문장·숫자가 변환본(같은 group의 모든 파일)에 남아 있는지,
      부정어·한정어·범위 표현이 사라지거나 뒤집히지 않았는지 확인한다.
형식 차이(문체 어미, 마크업, 표/불릿, 굵게, 레이블 이름, 다이어그램 노드 텍스트 위치)는 오류가 아니다.

사용:
  python preserve_check.py --docs docs --backup-root backup            # sources가 있는 모든 group
  python preserve_check.py --docs docs --backup-root backup --group cld-3
  python preserve_check.py --source a.md --target x.md                 # 파일 직접 지정 (1:1)
  python preserve_check.py --source a.md b.md --target x.md y.md       # 여러 파일 (내용만 group 비교)

결과:
  오류  제목 트리    원본 제목이 없어졌거나 순서·계층이 바뀜 (구조 불변 위반)
  오류  누락 의심    원본 문장과 비슷한 문장이 변환본에 없음
  오류  뜻 바뀜 의심  비슷한 문장은 있으나 부정·한정·범위 표현이 빠짐
  오류  숫자 누락    원본 숫자(단위 포함)의 개수가 변환본에서 줄어듦
  경고  문장 변경    비슷한 문장이 있으나 어미 외 글자가 달라짐 → 파트 확인 (3단계 문장 손질을 켰을 때만 정상)
  정보  문체 변경    어미만 바뀜 (합니다 → 한다). 집계만 한다
  정보  레이블 통일  굵은 줄 레이블이 block-patterns 이름으로 바뀜 (Request Body → 입력)
"""
from __future__ import annotations
import argparse
import difflib
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mdlib import load_docs, latest_backup, parse_doc, as_list  # noqa: E402

RISK = {
    "부정": r"않|없[다는이음었어고지]|없$|아니|금지|불가|못\s?[하한할]",
    "한정": r"(?<=[가-힣A-Za-z0-9)\]`])만(?=[\s,.)]|$)|뿐|오직|단,",
    "범위": r"최대|최소|이내|이상|이하|초과|미만",
    "의무": r"필수|반드시|해야|필요",
    "제외": r"제외|빼고",
    "주체": r"다른\s|같은\s",
    "버전": r"\bV\d\b",
}
UNIT = r"(?:%|초|분|시간|일|주|개월|년|명|회|건|자|원|배|장|권|대|개|줄|MB|GB|KB|TB|RPS|ms|KST|달러)"
NUM_RE = re.compile(r"(?<![A-Za-z%])\$?\d+(?:[.,]\d+)*(?:\s?~\s?\d+(?:[.,]\d+)*)?\s?" + UNIT + "?")
URL_RE = re.compile(r"\]\([^)]*\)|https?://\S+")
BOLD_ONLY_RE = re.compile(r"^\*\*[^*]+\*\*[:：]?$")
BOLD_CODE_RE = re.compile(r"^\*\*([^*]+)\*\*\s*`[^`]*`\s*$")       # **출력** `200 OK`
STATUS_LINE_RE = re.compile(r"^HTTP\s+\d{3}\b.*$", re.I)              # HTTP <code>200 OK</code>
FENCE_RE = re.compile(r"^(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
SIM_CHANGED = 0.6

# block-patterns.md 의 레이블과 그 원본 표기. 굵은 줄이 이 목록에 있으면 제목이 아니라 레이블로 본다.
LABELS = {
    "입력", "출력", "에러", "예시", "설명", "컬럼설명", "컬럼정의", "개념요약", "특징", "서비스에적용할점", "참고", "주의점",
    "request", "requestheader", "header", "headers", "pathparameter", "pathparameters", "queryparameter",
    "queryparameters", "requestbody", "body", "response", "response성공", "response실패", "성공", "실패",
    "응답", "응답코드", "error", "errors", "example", "examples", "출력200",
}
MERMAID_QUOTED = re.compile(r'"([^"]+)"')
MERMAID_BRACKET = re.compile(r"\[([^\]\"]+)\]|\(([^)\"]+)\)|\{([^}\"]+)\}|\|([^|\"]+)\|")
MERMAID_MSG = re.compile(r"(?:-+>>?|-+x|-+\)|Note\s+(?:over|left of|right of)\s+[^:]+):\s*(.+)$")


CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_PROTECT = {c: chr(0xE000 + i) for i, c in enumerate(CIRCLED)}
_RESTORE = {v: k for k, v in _PROTECT.items()}


def norm(s: str) -> str:
    # ①②③ 는 참조 번호(내용)라 NFKC로 숫자가 되지 않게 보호한다. 숫자로 세지도, 목록 번호로 지우지도 않는다.
    s = "".join(_PROTECT.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(_RESTORE.get(ch, ch) for ch in s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = URL_RE.sub(" ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"^\s*#+\s*", "", s)
    s = re.sub(r"^\s*(?:[-*+]|\d+(?:[.-]\d+)*\.?)\s+", "", s)
    s = re.sub(r"^\s*(?:[-*+]\s+)?\[[ xX]\]\s+", "", s)
    s = re.sub(r"[*_`>|]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def nospace(s: str) -> str:
    return re.sub(r"[\s.,:;·\-–—]", "", s)


def _drop_jong(ch: str, jong: int):
    code = ord(ch) - 0xAC00
    if not 0 <= code < 11172:
        return None
    cho, rest = divmod(code, 588)
    jung, j = divmod(rest, 28)
    return chr(0xAC00 + (cho * 21 + jung) * 28) if j == jong else None


def stem_ending(s: str) -> str:
    """문장 끝 종결 어미를 어간으로 줄인다. 합니다→하, 한다→하, 있습니다→있, 있다→있, 입니다→이, 이다→이.
    문체(합니다/한다) 차이만 있는 문장을 같은 문장으로 보기 위한 것이다."""
    s = re.sub(r"[\s.!?]+$", "", s.rstrip())
    if s.endswith("습니다"):
        return s[:-3]
    if s.endswith("니다") and len(s) >= 3:
        base = _drop_jong(s[-3], 17)  # ㅂ
        if base:
            return s[:-3] + base
    if s.endswith("는다"):
        return s[:-2]
    if s.endswith("다") and len(s) >= 2:
        base = _drop_jong(s[-2], 4)  # ㄴ
        if base:
            return s[:-2] + base
        return s[:-1]
    return s


def strip_numbering(s: str) -> str:
    """제목·굵은 줄 앞의 번호(1., 3-1, 2.2.)를 뗀다. 번호는 순서 검사가 따로 본다."""
    return re.sub(r"^\d+(?:[.-]\d+)*(?:\.\s*|\s+)", "", s)


def label_key(s: str) -> str:
    """굵은 줄 레이블 비교용 키: 소문자, 기호·숫자·공백 제거."""
    return re.sub(r"[\s\W\d_]+", "", norm(s).lower())


def split_sentences(s: str):
    return [p for p in re.split(r"(?<=[.!?])\s+", s) if p.strip()]


def mermaid_labels(line: str):
    """mermaid 한 줄에서 사람이 읽는 텍스트(노드 레이블, 간선 레이블, 메시지)만 뽑는다."""
    found = MERMAID_QUOTED.findall(line)
    rest = MERMAID_QUOTED.sub(" ", line)
    for m in MERMAID_BRACKET.finditer(rest):
        found.append(next(g for g in m.groups() if g))
    m = MERMAID_MSG.search(line)
    if m:
        found.append(m.group(1))
    out = []
    for f in found:
        f = re.sub(r'^[\[\]\(\)\{\}"/\s]+|[\[\]\(\)\{\}"/\s]+$', "", f)
        if f and not re.fullmatch(r"[A-Za-z0-9_]+", f):
            out.append(f)
    return out


def units(text: str):
    """비교 단위 목록: (원문, 정규화, 종류). 종류는 text / heading / bold / label.
    표 셀은 문장 단위로 나누고, mermaid 블록은 줄이 아니라 레이블 단위로 본다."""
    _, body, _ = parse_doc(text)
    out = []
    in_code, lang = False, ""
    lines = body.split("\n")
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if FENCE_RE.match(s):
            in_code = not in_code
            lang = s[3:].strip().lower() if in_code else ""
            continue
        if not s or s.startswith("<!--") or re.match(r"^\|[\s:|-]+\|$", s) or s in ("---", "***"):
            continue
        if (not in_code and s.startswith("|") and idx + 1 < len(lines)
                and re.match(r"^\|[\s:|-]+\|$", lines[idx + 1].strip())):
            continue  # 표 머리행(열 이름)은 형식이라 비교하지 않는다
        if in_code:
            if lang == "mermaid":
                out.extend((lb, "text") for lb in mermaid_labels(s))
            else:
                out.append((s, "text"))
            continue
        if s.startswith("|"):
            for c in s.strip("|").split("|"):
                out.extend((p, "text") for p in split_sentences(c.strip()))
            continue
        if HEADING_RE.match(s):
            out.append((s, "heading"))
            continue
        if BOLD_ONLY_RE.match(s):
            out.append((s, "label" if label_key(s) in LABELS else "bold"))
            continue
        m = BOLD_CODE_RE.match(s)
        if (m and label_key(m.group(1)) in LABELS) or STATUS_LINE_RE.match(norm(s)):
            out.append((s, "label"))  # **출력** `200 OK` / HTTP 200 OK 는 상태 레이블
            continue
        out.extend((p, "text") for p in split_sentences(s))
    res = []
    for u, kind in out:
        n = norm(u)
        if kind != "text":
            n = strip_numbering(n)
        if len(nospace(n)) >= 4:
            res.append((u, n, kind))
    return res


def numbers(unit_list):
    """본문 숫자 개수. 제목·굵은 줄의 숫자는 제목 트리 검사가 보므로 세지 않는다."""
    c = Counter()
    for _, n, k in unit_list:
        if k != "text":
            continue
        for m in NUM_RE.findall(n):
            key = re.sub(r"\s", "", m)
            key = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", key)  # 13,500 == 13500
            c[key] += 1
    return c


def risks(s: str):
    return {k for k, p in RISK.items() if re.search(p, s)}


def load_overrides(docs_root: Path | None, group: str):
    phrases = []
    if not docs_root:
        return phrases
    f = docs_root / "_preserve-overrides.tsv"
    if not f.exists():
        return phrases
    for ln in f.read_text(encoding="utf-8").splitlines():
        if not ln.strip() or ln.startswith("#") or ln.startswith("group\t"):
            continue
        parts = ln.split("\t")
        if len(parts) >= 2 and parts[0] in (group, "*"):
            phrases.append((nospace(norm(parts[1])), parts[2] if len(parts) > 2 else ""))
    return phrases


# ---------- 구조: 제목 트리 ----------

def heading_tree(text: str):
    """(level, 정규화 제목, 종류) 목록. 진짜 제목과 제목처럼 쓴 굵은 줄(레이블 제외)."""
    _, body, _ = parse_doc(text)
    out, in_code = [], False
    for ln in body.split("\n"):
        s = ln.strip()
        if FENCE_RE.match(s):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = HEADING_RE.match(s)
        if m:
            out.append((len(m.group(1)), strip_numbering(norm(m.group(2))), "heading"))
        elif BOLD_ONLY_RE.match(s) and label_key(s) not in LABELS:
            out.append((None, strip_numbering(norm(s)), "bold"))
    return out


def compare_tree(src_text: str, tgt_text: str, sibling_texts=()):
    """원본 제목이 변환본에 같은 순서로 있는지, 진짜 제목끼리의 계층 방향이 같은지 확인한다.
    변환본에 제목이 더 있는 것(굵은 줄·summary 승격)은 허용. 원본 첫 제목이 페이지 제목이면 빠져도 된다.
    같은 group의 다른 파일(부록)에 있는 제목은 오류 대신 경고 — 파트 결정으로 부록을 나눈 경우만 허용된다."""
    src, tgt = heading_tree(src_text), heading_tree(tgt_text)
    errors, warns, info = [], [], []
    if not src:
        return errors, warns, info
    sibling_keys = {nospace(h[1]) for st in sibling_texts for h in heading_tree(st)}
    levels = [h[0] for h in src if h[2] == "heading"]
    first = src[0]
    title_like = (first[2] == "heading" and levels and first[0] == min(levels) and levels.count(first[0]) == 1)
    tkeys = [nospace(h[1]) for h in tgt]
    pos = 0
    matched = []  # (src_idx, tgt_idx)
    for i, (lv, txt, kind) in enumerate(src):
        key = nospace(txt)
        j = next((k for k in range(pos, len(tkeys)) if tkeys[k] == key), None)
        if j is None:
            if i == 0 and title_like and key not in tkeys:
                info.append(f"원본 첫 제목 `{txt}` 은 페이지 제목으로 보고 본문에서 뺀 것으로 처리")
                continue
            if key in tkeys:
                errors.append(("순서 변경", txt))
            elif key in sibling_keys:
                warns.append(("다른 파일로 이동 (파트 결정으로 부록을 나눈 경우만 허용)", txt))
            else:
                errors.append(("제목 누락·글자 변경", txt))
            continue
        matched.append((i, j))
        pos = j + 1
    # 계층 방향 검사 (진짜 제목끼리만)
    prev = None
    for i, j in matched:
        if src[i][2] != "heading" or tgt[j][2] != "heading":
            continue
        if prev is not None:
            pi, pj = prev
            ds = (src[i][0] > src[pi][0]) - (src[i][0] < src[pi][0])
            dt = (tgt[j][0] > tgt[pj][0]) - (tgt[j][0] < tgt[pj][0])
            if ds != dt:
                errors.append(("계층 변경", f"`{src[pi][1]}` → `{src[i][1]}` (원본 {src[pi][0]}→{src[i][0]}, 변환본 {tgt[pj][0]}→{tgt[j][0]})"))
        prev = (i, j)
    src_heading_keys = {nospace(h[1]) for h in src if h[2] == "heading"}
    for h in tgt:
        if h[2] == "heading" and nospace(h[1]) not in src_heading_keys and any(nospace(h[1]) == nospace(s[1]) for s in src):
            info.append(f"승격된 제목: `{h[1]}` (원본은 굵은 줄)")
    return errors, warns, info


# ---------- 내용 ----------

def compare(src_texts, tgt_texts, overrides=()):
    src = [u for t in src_texts for u in units(t)]
    tgt = [u for t in tgt_texts for u in units(t)]
    tgt_joined = nospace(" ".join(n for _, n, _k in tgt))
    tgt_joined_style = nospace(" ".join(stem_ending(n) for _, n, _k in tgt))
    tgt_style = [nospace(stem_ending(n)) for _, n, _k in tgt]
    result = {"누락 의심": [], "뜻 바뀜 의심": [], "숫자 누락": [], "문장 변경": [],
              "제목 변경": [], "승인된 변경": [], "문체 변경": [], "레이블 통일": []}

    seen = set()
    for raw, n, kind in src:
        key = nospace(n)
        if key in seen:
            continue
        seen.add(key)
        ov = next((r for p, r in overrides if p and p in key), None)
        if ov is not None:
            result["승인된 변경"].append((raw, ov))
            continue
        if key in tgt_joined:
            continue
        if kind == "heading":
            continue  # 진짜 제목은 제목 트리 검사가 담당
        if kind == "label":
            result["레이블 통일"].append(raw)  # 레이블 이름은 block-patterns 로 통일하므로 글자 변경이 정상
            continue
        key_style = nospace(stem_ending(n))
        if key_style and key_style in tgt_joined_style:
            result["문체 변경"].append(raw)
            continue
        best, best_i = 0.0, -1
        for i, t in enumerate(tgt_style):
            sm = difflib.SequenceMatcher(None, key_style, t, autojunk=False)
            if sm.real_quick_ratio() < best or sm.quick_ratio() < best:
                continue
            r = sm.ratio()
            if r > best:
                best, best_i = r, i
        if kind == "bold":
            result["제목 변경"].append((raw, tgt[best_i][0] if best_i >= 0 else "", best))
            continue
        if best < SIM_CHANGED:
            result["누락 의심"].append((raw, tgt[best_i][0] if best_i >= 0 else "", best))
            continue
        lost = risks(n) - risks(tgt[best_i][1])
        if lost:
            result["뜻 바뀜 의심"].append((raw, tgt[best_i][0], best, sorted(lost)))
        else:
            result["문장 변경"].append((raw, tgt[best_i][0], best))

    sc, tc = numbers(src), numbers(tgt)
    for k, v in sc.items():
        if tc[k] < v:
            result["숫자 누락"].append((k, v, tc[k]))
    return result, len(src)


def report(name, result, n_src, tree_errors=(), tree_warns=(), tree_info=()):
    errs = sum(len(result[k]) for k in ("누락 의심", "뜻 바뀜 의심", "숫자 누락")) + len(tree_errors)
    print(f"\n## {name} — 원본 단위 {n_src}개 · 오류 {errs} (제목 트리 {len(tree_errors)}) · 문장 변경 {len(result['문장 변경'])}"
          f" · 문체 변경 {len(result['문체 변경'])} · 레이블 통일 {len(result['레이블 통일'])} · 제목 변경 {len(result['제목 변경'])}")
    for kind, txt in tree_errors:
        print(f"\n[오류] 제목 트리 — {kind}: {txt}")
    for kind, txt in tree_warns:
        print(f"\n[경고] 제목 트리 — {kind}: {txt}")
    for k in ("누락 의심", "뜻 바뀜 의심"):
        for item in result[k]:
            print(f"\n[오류] {k} (유사도 {item[2]:.2f})" + (f" 사라진 표현: {', '.join(item[3])}" if k == "뜻 바뀜 의심" else ""))
            print(f"  원본  : {item[0]}")
            print(f"  변환본: {item[1] or '(없음)'}")
    for k, v, t in result["숫자 누락"]:
        print(f"\n[오류] 숫자 누락 `{k}` 원본 {v}회 → 변환본 {t}회")
    for raw, tg, sim in result["문장 변경"]:
        print(f"\n[경고] 문장 변경 (유사도 {sim:.2f}) → 파트 확인")
        print(f"  원본  : {raw}")
        print(f"  변환본: {tg}")
    for raw, tg, sim in result["제목 변경"]:
        print(f"\n[경고] 제목처럼 쓴 굵은 줄이 변환본에 없음 → 승격했으면 글자 그대로인지 확인")
        print(f"  원본  : {raw}")
        print(f"  가장 비슷한 변환본: {tg or '(없음)'}")
    for raw, why in result["승인된 변경"]:
        print(f"\n[정보] 승인된 변경 ({why}): {raw}")
    for msg in tree_info:
        print(f"[정보] {msg}")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs")
    ap.add_argument("--backup-root", default="backup")
    ap.add_argument("--group")
    ap.add_argument("--source", nargs="*")
    ap.add_argument("--target", nargs="*")
    a = ap.parse_args()

    total = 0
    if a.source and a.target:
        src_texts = [Path(p).read_text(encoding="utf-8") for p in a.source]
        tgt_texts = [Path(p).read_text(encoding="utf-8") for p in a.target]
        res, n = compare(src_texts, tgt_texts)
        te, tw, ti = ([], [], [])
        if len(src_texts) == 1 and tgt_texts:
            te, tw, ti = compare_tree(src_texts[0], tgt_texts[0], tgt_texts[1:])
        total += report("직접 비교", res, n, te, tw, ti)
        return 1 if total else 0

    if not a.docs:
        ap.error("--docs 또는 --source/--target 필요")
    docs_root = Path(a.docs)
    if not docs_root.exists():
        print("docs 없음 — 건너뜀")
        return 0
    groups = {}
    for d in load_docs(docs_root):
        g = d["meta"].get("group")
        if g:
            groups.setdefault(g, []).append(d)
    checked = 0
    for g, members in sorted(groups.items()):
        if a.group and g != a.group:
            continue
        sources = [s for d in members for s in as_list(d["meta"].get("sources"))]
        if not sources:
            continue
        bname = next((d["meta"].get("backup") for d in members if d["meta"].get("backup")), None)
        bdir = latest_backup(Path(a.backup_root), bname)
        if not bdir:
            print(f"[오류] {g}: 백업 폴더를 찾을 수 없음 ({a.backup_root})")
            total += 1
            continue
        src_texts, tree_errors, tree_warns, tree_info = [], [], [], []
        tgt_by_doc = {d["path"]: d["path"].read_text(encoding="utf-8") for d in members}
        for d in members:
            own = as_list(d["meta"].get("sources"))
            for s in own:
                p = bdir / s
                if not p.exists():
                    print(f"[오류] {g}: 원본 파일 없음 {p}")
                    total += 1
                    continue
                st = p.read_text(encoding="utf-8")
                src_texts.append(st)
                if len(own) == 1:  # 1 원본 = 1 문서일 때만 제목 트리 대조
                    siblings = [v for k, v in tgt_by_doc.items() if k != d["path"]]
                    te, tw, ti = compare_tree(st, tgt_by_doc[d["path"]], siblings)
                    tree_errors += [(k, f"{d['path'].name}: {t}") for k, t in te]
                    tree_warns += [(k, f"{d['path'].name}: {t}") for k, t in tw]
                    tree_info += [f"{d['path'].name}: {m}" for m in ti]
        tgt_texts = list(tgt_by_doc.values())
        res, n = compare(src_texts, tgt_texts, load_overrides(docs_root, g))
        total += report(f"{g} ({bdir.name})", res, n, tree_errors, tree_warns, tree_info)
        checked += 1
    print(f"\n검사한 group {checked}개 · 오류 합계 {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
