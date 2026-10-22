#!/usr/bin/env bash
# Post-deployment smoke test: is the new version up, ready and serving data?
#
#   bash scripts/smoke-test.sh [base-url] [expected-version]
#   bash scripts/smoke-test.sh http://localhost:8080 "$(git rev-parse HEAD)"
set -euo pipefail

BASE_URL="${1:-http://localhost:8080}"
EXPECTED_VERSION="${2:-}"
ATTEMPTS="${SMOKE_ATTEMPTS:-30}"

echo "Smoke testing ${BASE_URL}"

for attempt in $(seq 1 "$ATTEMPTS"); do
  if body=$(curl -fsS --max-time 5 "${BASE_URL}/health/ready" 2>/dev/null); then
    echo "  ready:   $(sed -n 's/^{"status":"\([a-z]*\)".*/\1/p' <<<"$body")"
    break
  fi
  if [ "$attempt" -eq "$ATTEMPTS" ]; then
    echo "  FAIL: /health/ready did not return 200 after ${ATTEMPTS} attempts" >&2
    exit 1
  fi
  sleep 2
done

status=$(curl -fsS --max-time 5 "${BASE_URL}/status")
version=$(sed -n 's/.*"version":"\([^"]*\)".*/\1/p' <<<"$status")
echo "  version: ${version}"
if [ -n "$EXPECTED_VERSION" ] && [ "$version" != "$EXPECTED_VERSION" ]; then
  echo "  FAIL: expected version ${EXPECTED_VERSION}, the deployed app reports ${version}" >&2
  exit 1
fi

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${BASE_URL}/api/products")
if [ "$code" != "200" ]; then
  echo "  FAIL: GET /api/products returned ${code}" >&2
  exit 1
fi
echo "  catalog: GET /api/products returned 200"
echo "Smoke test passed"
