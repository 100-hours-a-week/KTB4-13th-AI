#!/usr/bin/env bash
# 원본 위키를 backup/wiki-original-YYYY-MM-DD/ 로 복사한다. 원본 위키에는 아무것도 쓰지 않는다.
# 사용: bash .agents/skills/ktb4-docs/scripts/backup_wiki.sh
set -euo pipefail
WIKI_URL="${WIKI_URL:-https://github.com/100-hours-a-week/KTB4-13th-wiki.wiki.git}"
ROOT="$(git rev-parse --show-toplevel)"
DATE="$(date +%Y-%m-%d)"
DEST="$ROOT/backup/wiki-original-$DATE"

if [ -e "$DEST" ]; then echo "이미 있음: $DEST (기존 백업은 덮어쓰지 않습니다)"; exit 1; fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git clone --quiet "$WIKI_URL" "$TMP/wiki"
git -C "$TMP/wiki" remote set-url --push origin DISABLED   # 실수로 원본에 푸시하지 않게
COMMIT="$(git -C "$TMP/wiki" rev-parse HEAD)"

mkdir -p "$DEST"
(cd "$TMP/wiki" && tar --exclude=.git -cf - .) | (cd "$DEST" && tar -xf -)

hash_cmd() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }
(cd "$DEST" && find . -type f ! -name MANIFEST.sha256 ! -name SOURCE.txt | LC_ALL=C sort | while IFS= read -r f; do hash_cmd "$f"; done) > "$DEST/MANIFEST.sha256"

COUNT="$(grep -c . "$DEST/MANIFEST.sha256")"
cat > "$DEST/SOURCE.txt" << TXT
원본: $WIKI_URL
커밋: $COMMIT
백업일: $DATE
파일 수: $COUNT
이 폴더는 수정하지 않는다. 원본이 바뀌면 새 날짜 폴더로 다시 백업한다.
TXT

echo "백업 완료: $DEST (파일 $COUNT개, 원본 커밋 $COMMIT)"
echo "확인: python .agents/skills/ktb4-docs/scripts/verify_backup.py backup"
echo "다음: git add backup/wiki-original-$DATE && git commit -m \"backup: 위키 원본 $DATE\""
