#!/usr/bin/env bash
# Live chaos walkthrough for demos. It injects real network faults with
# Toxiproxy and shows how the Catalog API answers each request. Keep the
# dashboard (http://localhost:8000) open next to the terminal while it runs.
#
#   docker compose up -d --build        # start the stack first
#   bash scripts/chaos-demo.sh          # pauses between scenarios (press Enter)
#   AUTO=1 bash scripts/chaos-demo.sh   # runs straight through
set -euo pipefail

APP_URL="${APP_URL:-http://localhost:8000}"
TOXIPROXY_URL="${TOXIPROXY_URL:-http://localhost:8474}"
AUTO="${AUTO:-0}"

if [ -t 1 ]; then
  BOLD=$'\e[1m' DIM=$'\e[2m' RED=$'\e[31m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RESET=$'\e[0m'
else
  BOLD='' DIM='' RED='' GREEN='' YELLOW='' RESET=''
fi

# -- Toxiproxy control ----------------------------------------------------------

toxi() { # method path [json-body]
  local args=(-fsS -X "$1" -H 'Content-Type: application/json')
  if [ $# -ge 3 ]; then args+=(--data "$3"); fi
  curl "${args[@]}" "${TOXIPROXY_URL}$2" >/dev/null
}
add_latency() { # proxy milliseconds
  toxi POST "/proxies/$1/toxics" \
    "{\"name\":\"demo_latency\",\"type\":\"latency\",\"stream\":\"downstream\",\"attributes\":{\"latency\":$2}}"
}
remove_latency() { toxi DELETE "/proxies/$1/toxics/demo_latency"; }
set_proxy() { toxi PATCH "/proxies/$1" "{\"enabled\":$2}"; }
reset_faults() { toxi POST /reset; }

# -- App helpers ----------------------------------------------------------------

create_product() { # name -> prints id
  curl -fsS -X POST "${APP_URL}/api/products" -H 'Content-Type: application/json' \
    --data "{\"name\":\"$1\",\"price\":999,\"stock\":5}" | sed -n 's/.*"id":\([0-9]*\).*/\1/p'
}

request() { # label path
  local out code seconds ms source color
  out=$(curl -s -o /dev/null -D - --max-time 10 -w 'STATUS=%{http_code} TIME=%{time_total}' \
    "${APP_URL}$2" | tr -d '\r') || true
  code=$(sed -n 's/.*STATUS=\([0-9]*\).*/\1/p' <<<"$out")
  seconds=$(sed -n 's/.*TIME=\([0-9.]*\).*/\1/p' <<<"$out")
  ms=$(awk -v s="${seconds:-0}" 'BEGIN { printf "%d", s * 1000 }')
  source=$(sed -n 's/^[Xx]-[Dd]ata-[Ss]ource: *//p' <<<"$out")
  case "$code" in
    200) color=$GREEN ;;
    503) color=$YELLOW ;;
    *) color=$RED ;;
  esac
  printf '  %-36s %s%3s%s %6s ms  %s\n' "$1" "$color" "${code:-000}" "$RESET" "$ms" \
    "${source:+served from $source}"
}

readiness() {
  local body
  body=$(curl -s --max-time 5 "${APP_URL}/health/ready" || true)
  printf '  %-36s %s\n' "readiness" "$(sed -n 's/^{"status":"\([a-z]*\)".*/\1/p' <<<"$body")"
}

wait_until_ready() { # "ready" means both probes passed, so both circuits are closed
  for _ in $(seq 1 60); do
    if curl -fs --max-time 2 "${APP_URL}/health/ready" | grep -q '"status":"ready"'; then
      return 0
    fi
    sleep 0.5
  done
  echo "${RED}The stack did not recover; check: docker compose logs app${RESET}" >&2
  exit 1
}

scenario() { printf '\n%s%s%s\n%s%s%s\n' "$BOLD" "$1" "$RESET" "$DIM" "$2" "$RESET"; }
pause() {
  if [ "$AUTO" = "1" ]; then
    sleep 2
  else
    read -r -p "${DIM}Press Enter for the next scenario...${RESET}" _
  fi
}

trap 'reset_faults 2>/dev/null || true' EXIT

# -- Walkthrough ----------------------------------------------------------------

if ! curl -fs --max-time 3 "${APP_URL}/health/live" >/dev/null; then
  echo "${RED}Cannot reach the app at ${APP_URL}. Start it with: docker compose up -d --build${RESET}" >&2
  exit 1
fi
reset_faults
wait_until_ready
warm=$(create_product "demo-warm-$RANDOM")

scenario "0. Baseline" "No faults. The first read goes to PostgreSQL, the next one is served by Redis."
request "first read" "/api/products/${warm}"
request "second read" "/api/products/${warm}"
pause

scenario "1. Redis answers 2 s late" \
  "Each Redis call is cut off after 250 ms, then the Redis circuit opens and the cache is skipped."
add_latency redis 2000
for i in 1 2 3 4; do request "read ${i}" "/api/products/${warm}"; done
remove_latency redis
wait_until_ready
pause

scenario "2. Redis goes down" "Reads and writes keep working without the cache; readiness says 'degraded'."
set_proxy redis false
request "read" "/api/products/${warm}"
readiness
set_proxy redis true
wait_until_ready
request "read after Redis returns" "/api/products/${warm}"
pause

scenario "3. PostgreSQL answers 3 s late" \
  "Uncached reads fail after the 1 s timeout, then the circuit opens and they fail instantly."
request "warm product, before the fault" "/api/products/${warm}"
cold=$(create_product "demo-cold-$RANDOM")
add_latency postgres 3000
for i in 1 2 3 4; do request "uncached read ${i}" "/api/products/${cold}"; done
request "warm product (cached copy)" "/api/products/${warm}"
remove_latency postgres
wait_until_ready
pause

scenario "4. PostgreSQL goes down, then comes back" \
  "Readiness turns 503, cached data is still served, and the service heals itself on recovery."
cold=$(create_product "demo-cold-$RANDOM")
set_proxy postgres false
readiness
request "warm product" "/api/products/${warm}"
request "uncached product" "/api/products/${cold}"
set_proxy postgres true
start=$(date +%s.%N)
printf '  %s\n' "PostgreSQL is back; polling the uncached product until it is served..."
for _ in $(seq 1 60); do
  if curl -fs --max-time 2 -o /dev/null "${APP_URL}/api/products/${cold}"; then break; fi
  sleep 0.25
done
request "uncached product" "/api/products/${cold}"
printf '  %s%s%s\n' "$GREEN" "Recovered on its own in $(awk -v a="$start" -v b="$(date +%s.%N)" 'BEGIN { printf "%.1f", b - a }') s, no restart." "$RESET"

printf '\n%sDone. All faults removed.%s\n' "$BOLD" "$RESET"
