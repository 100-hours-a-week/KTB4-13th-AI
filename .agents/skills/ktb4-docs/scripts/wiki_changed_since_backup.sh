#!/usr/bin/env bash
# 백업 이후 원본 위키에서 바뀐 페이지 목록. 원본 위키에는 아무것도 쓰지 않는다.
# 사용: bash wiki_changed_since_backup.sh backup/wiki-original-YYYY-MM-DD
set -euo pipefail
BDIR="${1:?백업 폴더 경로를 주세요}"
COMMIT="$(sed -n 's/^커밋: //p' "$BDIR/SOURCE.txt")"
WIKI_URL="$(sed -n 's/^원본: //p' "$BDIR/SOURCE.txt")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
git clone --quiet "$WIKI_URL" "$TMP/wiki"
git -C "$TMP/wiki" remote set-url --push origin DISABLED
echo "백업 커밋 $COMMIT 이후 변경:"
git -C "$TMP/wiki" -c core.quotepath=false diff --name-status "$COMMIT" HEAD || true
echo
git -C "$TMP/wiki" log --since="$(sed -n 's/^백업일: //p' "$BDIR/SOURCE.txt")" --format='%h %ad %an %s' --date=short | head -30
