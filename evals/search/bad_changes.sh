#!/usr/bin/env bash
# 일부러 나쁜 변경 셋을 넣어 검색 판정(verdict.py)이 잡는지 본다(#752, V4 최종안 4절 5번).
# 한 번 쓰는 측정 코드라 브랜치에만 둔다(PR 아님).
#
# 변경마다 이 커밋을 임시 worktree로 꺼내 app 의 한 줄만 바꾸고, 판정에 쓰는 첫 화면(인기순 위 12권)으로
# 묶음 전부를 잰 뒤, 기준선 결과 파일과 판정한다. 결과 파일의 code 칸은 "<커밋> (app 을 고친 채)"가 된다.
#
#   DATABASE_URL=postgresql://localhost/bookjeok_ai_eval CURSOR_SIGNING_KEY=local-eval-only \
#     evals/search/bad_changes.sh <결과 폴더> <기준선 첫 화면 결과 파일>
set -euo pipefail

out=$(cd "$1" && pwd)
base=$2
repo=$(git rev-parse --show-toplevel)
cd "$repo"

# 이름 | 파일 | 바꿀 줄 | 바꾼 줄
CHANGES=(
  "coverage-0.3|app/search/keyword.py|MIN_COVERAGE = 0.6|MIN_COVERAGE = 0.3"
  "ef-search-10|app/search/vector.py|SEARCH_EF_SEARCH = 400|SEARCH_EF_SEARCH = 10"
  "no-title-first|app/search/service.py|exact = await keyword.exact_title_ids(conn, keyword_ids, req.query)|exact: set[int] = set()"
)

for change in "${CHANGES[@]}"; do
  IFS='|' read -r name file from to <<<"$change"
  code="$out/code-$name"
  if [[ ! -d $code ]]; then
    git worktree add -q --detach "$code" HEAD
  fi
  # 바꿀 줄이 정확히 한 번 있어야 한다. 아니면 다른 코드를 잰 것이 되니 멈춘다.
  python3 - "$code/$file" "$from" "$to" <<'EOF'
import sys
from pathlib import Path

path, old, new = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8")
if text.count(new) == 1 and text.count(old) == 0:
    sys.exit(0)  # 이미 바꿈
if text.count(old) != 1:
    sys.exit(f"{path}: '{old}' 가 {text.count(old)}번 있다")
path.write_text(text.replace(old, new), encoding="utf-8")
EOF
  git -C "$code" diff --stat
  echo "== $name: 첫 화면으로 잰다"
  uv run python -m evals.search.run --screen --yes --code "$code" \
    --out "$out/$name-screen.json" >"$out/$name-screen.txt" 2>"$out/$name-screen.log"
  echo "== $name: 판정"
  uv run python -m evals.search.verdict compare --base "$base" --new "$out/$name-screen.json" \
    | tee "$out/$name-verdict.txt"
done
