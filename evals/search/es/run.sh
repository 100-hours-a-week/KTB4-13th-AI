#!/usr/bin/env bash
# Elasticsearch 판정(#754)용 로컬 한 대를 띄운다. 이 기계의 127.0.0.1 에서만 받고, 보안 기능은 끈다(평가 전용).
# 메모리·샤드 같은 설정은 공식 이미지 기본값 그대로 둔다.
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t bookjeok-es-eval:9.5.7 .
docker run -d --name bookjeok-es-eval \
  -p 127.0.0.1:9200:9200 \
  -e discovery.type=single-node \
  -e xpack.security.enabled=false \
  -v bookjeok-es-eval-data:/usr/share/elasticsearch/data \
  bookjeok-es-eval:9.5.7
