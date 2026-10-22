#!/usr/bin/env bash
# Deploy an image with Docker Compose, smoke test it, and roll back on failure.
# Used by pipeline stage 5 (GitHub Actions self-hosted runner, or Jenkins).
#
#   APP_IMAGE=ghcr.io/<owner>/<repo>:sha-abc1234 bash scripts/deploy.sh
#
# Optional environment:
#   EXPECTED_VERSION  git commit the new container must report on /status
#   APP_PORT          host port for the app (default 8080)
#   SMOKE_URL         where the smoke test reaches the app (default http://localhost:$APP_PORT);
#                     set it when the deploying process runs inside a container
#   DB_PASSWORD       PostgreSQL password (only applied when the volume is first created)
set -euo pipefail
cd "$(dirname "$0")/.."

: "${APP_IMAGE:?set APP_IMAGE to the image to deploy}"
export APP_PORT="${APP_PORT:-8080}"
SMOKE_URL="${SMOKE_URL:-http://localhost:${APP_PORT}}"
PROJECT=catalog-prod
COMPOSE=(docker compose --project-name "$PROJECT" --file deploy/docker-compose.prod.yml)

running_image() {
  docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=app" --format '{{.Image}}' | head -n 1
}

release() { # image [expected-version]
  APP_IMAGE="$1" "${COMPOSE[@]}" up -d --wait --wait-timeout 120 \
    && bash scripts/smoke-test.sh "$SMOKE_URL" "${2:-}"
}

PREVIOUS_IMAGE="$(running_image || true)"
echo "Currently running: ${PREVIOUS_IMAGE:-nothing}"
echo "Deploying:         ${APP_IMAGE}"

if ! APP_IMAGE="$APP_IMAGE" "${COMPOSE[@]}" pull --quiet app; then
  echo "Could not pull ${APP_IMAGE}; trying a locally built image with that name" >&2
fi

if release "$APP_IMAGE" "${EXPECTED_VERSION:-}"; then
  echo "Deployment healthy: ${APP_IMAGE}"
  exit 0
fi

echo "Deployment of ${APP_IMAGE} failed its checks" >&2
if [ -n "$PREVIOUS_IMAGE" ] && [ "$PREVIOUS_IMAGE" != "$APP_IMAGE" ]; then
  echo "Rolling back to ${PREVIOUS_IMAGE}" >&2
  if release "$PREVIOUS_IMAGE"; then
    echo "Rollback complete: ${PREVIOUS_IMAGE} is serving again" >&2
  else
    echo "Rollback failed too; inspect with: docker compose -p ${PROJECT} logs app" >&2
  fi
else
  echo "No previous version to roll back to" >&2
fi
exit 1
