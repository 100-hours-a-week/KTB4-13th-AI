#!/usr/bin/env python3
"""backup/wiki-original-*/ 가 백업 당시와 같은지 MANIFEST.sha256 으로 확인."""
import hashlib
import sys
import unicodedata
from pathlib import Path

META = {"MANIFEST.sha256", "SOURCE.txt"}


def nfc(s: str) -> str:
    # macOS(APFS)와 Linux 러너가 한글 파일명을 각각 NFD/NFC로 다르게 넘겨줄 수 있어
    # 비교 전에 정규화한다. 그렇지 않으면 같은 파일이 삭제+추가로 오탐된다.
    return unicodedata.normalize("NFC", s)


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "backup")
    dirs = sorted(d for d in root.glob("wiki-original-*") if d.is_dir()) if root.exists() else []
    if not dirs:
        print("백업 없음 — 건너뜀")
        return 0
    bad = 0
    for d in dirs:
        mf = d / "MANIFEST.sha256"
        if not mf.exists():
            print(f"[오류] {d.name}: MANIFEST.sha256 없음")
            bad += 1
            continue
        expected = {}
        for ln in mf.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                h, p = ln.split(None, 1)
                expected[nfc(p.strip().removeprefix("./"))] = h
        actual_paths = {
            nfc(p.relative_to(d).as_posix()): p
            for p in d.rglob("*") if p.is_file() and p.name not in META
        }
        actual = set(actual_paths)
        for p in sorted(set(expected) - actual):
            print(f"[오류] {d.name}: 삭제됨 {p}"); bad += 1
        for p in sorted(actual - set(expected)):
            print(f"[오류] {d.name}: 추가됨 {p}"); bad += 1
        for p in sorted(actual & set(expected)):
            if hashlib.sha256(actual_paths[p].read_bytes()).hexdigest() != expected[p]:
                print(f"[오류] {d.name}: 내용 바뀜 {p}"); bad += 1
        print(f"{d.name}: 파일 {len(expected)}개 확인")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
