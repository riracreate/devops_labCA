#!/usr/bin/env bash
# Runs the pipeline stages with nothing but Docker. Jenkins calls this script;
# you can run it locally before pushing to see what CI will say.
#
#   bash scripts/pipeline.sh              all stages: unit, build, resilience
#   bash scripts/pipeline.sh unit         lint + unit tests (coverage gate 90%)
#   bash scripts/pipeline.sh build        build the production image
#   bash scripts/pipeline.sh resilience   fault-injection suite against the stack
#
# Reports (JUnit XML, coverage, scorecard, stack logs) are written to ./reports.
set -euo pipefail
cd "$(dirname "$0")/.."

RUN_ID="${RUN_ID:-$$}"
export APP_IMAGE="${APP_IMAGE:-catalog-api:pipeline-${RUN_ID}}"
export TEST_IMAGE="${TEST_IMAGE:-catalog-api-tests:pipeline-${RUN_ID}}"
export APP_VERSION="${APP_VERSION:-$(git rev-parse HEAD 2>/dev/null || echo local)}"
# An independent Compose project and ports preserve any running demo stack and
# its database when the resilience stage tears down its temporary test stack.
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-resilience-ci-${RUN_ID}}"
export APP_HOST_PORT="${APP_HOST_PORT:-18000}"
export TOXIPROXY_HOST_PORT="${TOXIPROXY_HOST_PORT:-18474}"
COMPOSE=(docker compose -p "$COMPOSE_PROJECT_NAME" -f docker-compose.yml -f docker-compose.ci.yml)
mkdir -p reports

stage() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

unit() {
  stage "Stage 1: lint and unit tests (inside the test image)"
  docker build --target test -t "$TEST_IMAGE" .
  local name="unit-tests-${RUN_ID}" status=0
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run --name "$name" "$TEST_IMAGE" sh -c \
    "ruff check . && ruff format --check . && pytest -p no:cacheprovider \
     --cov=catalog --cov-report=term-missing --cov-report=xml:reports/coverage.xml \
     --cov-fail-under=90 --junitxml=reports/unit-junit.xml" || status=$?
  docker cp "${name}:/app/reports/." reports/ 2>/dev/null || true
  docker rm -f "$name" >/dev/null 2>&1 || true
  return "$status"
}

build() {
  stage "Stage 2: build the production image ${APP_IMAGE} (version ${APP_VERSION})"
  docker build --target runtime --build-arg "APP_VERSION=${APP_VERSION}" -t "$APP_IMAGE" .
  docker build --target test -t "$TEST_IMAGE" . # cached; the resilience stage needs it
  echo "Runs as user id: $(docker run --rm "$APP_IMAGE" id -u)"
}

resilience() {
  stage "Stage 3: resilience tests (Toxiproxy fault injection)"
  local tester="resilience-tester-${RUN_ID}" status=0
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  if "${COMPOSE[@]}" up -d --no-build --wait; then
    "${COMPOSE[@]}" run --name "$tester" tester || status=$?
    docker cp "${tester}:/app/reports/." reports/ 2>/dev/null || true
    docker rm -f "$tester" >/dev/null 2>&1 || true
  else
    status=1
    echo "The stack failed to start; see reports/stack-logs.txt" >&2
  fi
  "${COMPOSE[@]}" logs --no-color --timestamps >reports/stack-logs.txt 2>&1 || true
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  return "$status"
}

case "${1:-all}" in
  unit) unit ;;
  build) build ;;
  resilience) resilience ;;
  all) unit && build && resilience ;;
  *)
    echo "usage: $0 [unit|build|resilience|all]" >&2
    exit 2
    ;;
esac
