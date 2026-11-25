#!/usr/bin/env bash
# 기준(SEARCH_ES 없음)·후보 A·후보 B 를 평가 묶음 570건과 확인용 40건에서 판정에 쓰는 인기순 첫 화면으로 잰다(#754).
# 셋 다 이 커밋의 코드다. SEARCH_ES 가 없으면 지금 검색과 같다(app/search/es.py).
#
#   DATABASE_URL=postgresql://localhost/bookjeok_ai_eval CURSOR_SIGNING_KEY=local-eval-only \
#     evals/search/es/measure.sh <결과 폴더>
set -euo pipefail
mkdir -p "$1"
out=$(cd "$1" && pwd)
cd "$(git rev-parse --show-toplevel)"

for spec in "base|" "a|keyword" "b|keyword,vector"; do
  IFS='|' read -r name engine <<<"$spec"
  for bundle in cases confirm; do
    labels=()
    if [[ $bundle == confirm ]]; then labels=(--labels evals/search/confirm-labels.json); fi
    echo "== $name $bundle (SEARCH_ES=$engine)"
    SEARCH_ES=$engine uv run python -m evals.search.run --screen --yes \
      --bundle "evals/search/$bundle.json" ${labels[@]+"${labels[@]}"} \
      --out "$out/$name-$bundle.json" >"$out/$name-$bundle.txt" 2>"$out/$name-$bundle.log"
  done
done
