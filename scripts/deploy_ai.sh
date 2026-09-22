#!/usr/bin/env bash

set -Eeuo pipefail

if [ "$#" -ne 9 ]; then
  echo "사용법: deploy_ai.sh <deploy-dir> <compose-file> <service> <image-uri> <release-sha> <aws-region> <ecr-registry> <health-url> <candidate-compose>" >&2
  exit 2
fi

deploy_dir=$1
compose_name=$2
service_name=$3
image_uri=$4
release_sha=$5
aws_region=$6
ecr_registry=$7
health_url=$8
candidate_compose=$9

env_file="$deploy_dir/.env.production"
compose_file="$deploy_dir/$compose_name"
previous_compose_file="$compose_file.previous"
current_file="$deploy_dir/current-release.env"
previous_file="$deploy_dir/previous-release.env"
candidate_file="$deploy_dir/candidate-release.env"
lock_file="$deploy_dir/.deploy-ai.lock"

for command in aws chmod cp curl docker flock grep mv sed seq tail; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "필수 명령을 찾을 수 없습니다: $command" >&2
    exit 1
  fi
done

if ! [[ "$image_uri" =~ ^[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]]; then
  echo "Digest 기반 ECR 이미지 URI가 아닙니다: $image_uri" >&2
  exit 1
fi

if ! [[ "$release_sha" =~ ^[0-9a-f]{7}$ ]]; then
  echo "Release SHA는 7자의 16진수여야 합니다: $release_sha" >&2
  exit 1
fi

cd "$deploy_dir"

exec 9>"$lock_file"
if ! flock -n 9; then
  echo "다른 AI 배포가 실행 중입니다." >&2
  exit 1
fi

if [ ! -f "$candidate_compose" ]; then
  echo "전달된 Compose 파일을 찾을 수 없습니다: $candidate_compose" >&2
  exit 1
fi

if [ ! -f "$env_file" ]; then
  echo "운영 환경변수 파일을 찾을 수 없습니다: $env_file" >&2
  exit 1
fi

read_release_file() {
  local file=$1
  release_file_image=$(sed -n 's/^AI_IMAGE=//p' "$file" | tail -n 1)
  release_file_sha=$(sed -n 's/^RELEASE_SHA=//p' "$file" | tail -n 1)

  if ! [[ "$release_file_image" =~ @sha256:[0-9a-f]{64}$ ]] ||
    ! [[ "$release_file_sha" =~ ^[0-9a-f]{7}$ ]]; then
    echo "릴리스 파일 형식이 올바르지 않습니다: $file" >&2
    return 1
  fi
}

run_compose() {
  local target_compose=$1
  local target_image=$2
  local target_sha=$3
  shift 3

  AI_IMAGE="$target_image" RELEASE_SHA="$target_sha" \
    docker compose \
    --project-directory "$deploy_dir" \
    --env-file "$env_file" \
    -f "$target_compose" \
    "$@"
}

health_is_ready() {
  local expected_sha=$1
  local body
  body=$(curl --fail --silent --show-error --max-time 5 "$health_url") || return 1

  grep -Eq '"status"[[:space:]]*:[[:space:]]*"(ok|degraded)"' <<< "$body" &&
    grep -Eq '"database"[[:space:]]*:[[:space:]]*"ok"' <<< "$body" &&
    grep -Eq '"vector_index"[[:space:]]*:[[:space:]]*"ok"' <<< "$body" &&
    grep -Eq '"version"[[:space:]]*:[[:space:]]*"'"$expected_sha"'"' <<< "$body"
}

wait_for_health() {
  local expected_sha=$1
  local attempt

  for attempt in $(seq 1 30); do
    if health_is_ready "$expected_sha"; then
      echo "AI 서버 상태 검사가 통과했습니다: $expected_sha"
      return 0
    fi
    sleep 5
  done

  echo "AI 서버 상태 검사가 제한 시간 안에 통과하지 못했습니다: $expected_sha" >&2
  return 1
}

cleanup_failed_initial_deploy() {
  echo "이전 릴리스가 없어 실패한 최초 배포 컨테이너를 정리합니다." >&2
  run_compose "$compose_file" "$image_uri" "$release_sha" stop "$service_name" || true
  run_compose "$compose_file" "$image_uri" "$release_sha" rm -f "$service_name" || true
  rm -f "$candidate_file"
}

rollback() {
  if [ ! -f "$previous_file" ] || [ ! -f "$previous_compose_file" ]; then
    cleanup_failed_initial_deploy
    return 0
  fi

  read_release_file "$previous_file"
  cp "$previous_compose_file" "$compose_file.next"
  chmod 600 "$compose_file.next"
  mv "$compose_file.next" "$compose_file"

  echo "이전 AI 서버 이미지로 Rollback합니다: $release_file_sha"
  run_compose "$compose_file" "$release_file_image" "$release_file_sha" pull "$service_name"
  run_compose "$compose_file" "$release_file_image" "$release_file_sha" up -d --no-deps "$service_name"
  wait_for_health "$release_file_sha"
  rm -f "$candidate_file"
}

fail_after_deploy() {
  local reason=$1
  echo "$reason" >&2

  if rollback; then
    echo "AI 서버 Rollback이 완료됐습니다." >&2
    exit 1
  fi

  echo "AI 서버 Rollback도 실패했습니다. 즉시 운영 확인이 필요합니다." >&2
  exit 2
}

if [ ! -f "$current_file" ]; then
  running_container=$(docker ps -q --filter "label=com.docker.compose.service=$service_name")
  if [ -n "$running_container" ]; then
    echo "실행 중인 서비스가 있지만 current-release.env가 없습니다. 현재 Image Digest를 먼저 기록해야 합니다." >&2
    exit 1
  fi
elif [ ! -f "$compose_file" ]; then
  echo "현재 릴리스 정보는 있지만 Compose 파일이 없습니다: $compose_file" >&2
  exit 1
else
  read_release_file "$current_file"
fi

aws ecr get-login-password --region "$aws_region" |
  docker login --username AWS --password-stdin "$ecr_registry"

umask 077
printf 'AI_IMAGE=%s\nRELEASE_SHA=%s\n' "$image_uri" "$release_sha" > "$candidate_file"

run_compose "$candidate_compose" "$image_uri" "$release_sha" config --quiet
run_compose "$candidate_compose" "$image_uri" "$release_sha" pull "$service_name"

if [ -f "$current_file" ]; then
  cp "$current_file" "$previous_file"
  cp "$compose_file" "$previous_compose_file"
fi

cp "$candidate_compose" "$compose_file.next"
chmod 600 "$compose_file.next"
mv "$compose_file.next" "$compose_file"

if ! run_compose "$compose_file" "$image_uri" "$release_sha" up -d --no-deps "$service_name"; then
  fail_after_deploy "AI 서버 컨테이너 교체에 실패했습니다."
fi

if ! wait_for_health "$release_sha"; then
  fail_after_deploy "신규 AI 서버 상태 검증에 실패했습니다."
fi

mv "$candidate_file" "$current_file"
echo "AI 서버 배포가 완료됐습니다: $release_sha ($image_uri)"
