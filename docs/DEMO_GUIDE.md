# Demo guide

A 10-minute live demonstration, with a checklist, a script and answers to common questions.

## Before the demo (at least an hour ahead)

1. **Start the stack once** so every image is downloaded (about 500 MB the first time):
   `docker compose up -d --build`
2. **Open two tabs:** the store at <http://localhost:8000/shop> and the resilience monitor at <http://localhost:8000>.
3. **Push to GitHub** and let one full pipeline run finish (about 5 minutes). Keep its run page open: it shows the five stages, the coverage table and the resilience scorecard.
4. **Prepare the regression run** (see "Proving the pipeline catches regressions" below). Push it in advance and keep the failed run open in a tab; waiting 5 minutes for CI during a demo is risky.
5. Test the terminal demo once: `bash scripts/chaos-demo.sh`.

## The demonstration

A suggested split between three presenters; change it to match who built what.

### Part 1: the problem and the application (about 3 minutes)

- **Say:** most outages come from a slow or failing dependency, and normal tests never simulate one. Our pipeline injects real network faults on every push and blocks releases that don't handle them.
- **Show:** the architecture diagram in the README. Every connection to PostgreSQL and Redis passes through Toxiproxy, which can break the network on command.
- **Show:** the dashboard. Press **Send test traffic**: requests are served from the cache (green) or PostgreSQL (dark), and both circuit breakers are closed.
- **Show:** the store at `/shop`. Browse the products and add one with the form; it appears in the list straight away. Say: "this is what customers see; the monitor is what engineers see."

### Part 2: live fault injection (about 4 minutes)

Put the terminal next to the dashboard and run `bash scripts/chaos-demo.sh`. Press Enter between scenarios and narrate:

| Scenario | Point out in the terminal | Point out on the dashboard |
|---|---|---|
| 1. Redis answers 2 s late | ~500 ms, then ~250 ms, then 2 ms: the timeout cuts Redis off, then the circuit opens | The Redis switch flips open and turns red |
| 2. Redis goes down | Reads still work; readiness says `degraded` | Headline: "Running without the cache" |
| 3. PostgreSQL answers 3 s late | Three 503s at ~1 s each (the deadline), then 1 ms (circuit open); the cached product is still served | PostgreSQL switch opens; amber "rejected fast" bars |
| 4. PostgreSQL down, then back | Stale data still served; "Recovered on its own in ~5 s, no restart" | Switch closes again by itself. Switch to the store tab: it shows "you're seeing the last saved copy", and adding a product says "Not saved, try again in a few seconds" instead of crashing |

**Key sentence:** "Nobody restarted anything. The service protected itself during the outage and healed itself afterwards."

### Part 3: the pipeline (about 4 minutes)

- **Show:** the green run. Walk through the five stages: lint and unit tests, build once, resilience tests, publish to GHCR, deploy with smoke test and rollback.
- **Show:** the run summary. The resilience scorecard lists all 18 scenarios with what was expected and what was measured.
- **Show:** the failed regression run (next section). This is the strongest moment of the demo.
- **Close with** the "Testing the tests" table in the README: all 8 planted regressions were caught, and the 4 configuration regressions were caught *only* by the resilience stage.

## Proving the pipeline catches regressions

Simulate a well-meaning change: someone raises the database timeout to 10 s to "fix" slow queries.

```bash
git checkout -b demo/raise-db-timeout
```

In `docker-compose.yml`, under `app:` > `environment:`, add one line:

```yaml
      DB_TIMEOUT_SECONDS: "10"
```

```bash
git commit -am "Raise database timeout to 10 s"
git push -u origin demo/raise-db-timeout
```

Open a pull request. Expected result:

- **Stage 1 passes:** the unit tests are green, because they do not see deployment configuration.
- **Stage 3 fails:** the scorecard shows "PostgreSQL answers 3 s late" returning **200 after about 9 s** instead of a 503 within 2 s. Each query makes three network round trips, and each one paid the 3-second delay.
- With branch protection on, the **Merge** button is blocked.

Close the pull request afterwards without merging.

**Without GitHub** (on a laptop), after making the same edit:

```bash
bash scripts/pipeline.sh build && bash scripts/pipeline.sh resilience
```

Undo the edit afterwards with `git checkout docker-compose.yml`.

## If something goes wrong during the demo

| Problem | Fix |
|---|---|
| Dashboard says "Can't reach the Catalog API" | `docker compose ps`, then `docker compose up -d` |
| A fault seems stuck (for example the chaos script was interrupted) | `curl -X POST http://localhost:8474/reset` |
| The API docs page (`/docs`) is blank | It loads its interface from the internet. Without Wi-Fi, show the API with `curl -i http://localhost:8000/api/products/1` instead; the dashboard itself works offline. |
| Anything else looks wrong | Start clean: `docker compose down -v && docker compose up -d --build` |

## Questions evaluators often ask

**Why Toxiproxy instead of just stopping a container?** Stopping a container only simulates "completely down". Real incidents are usually slow, flaky or half-broken networks. Toxiproxy reproduces those against the real PostgreSQL and Redis protocols, and every fault can be added and removed through an API.

**What is a circuit breaker?** It counts consecutive failures to a dependency. After 3 it "opens" and rejects calls instantly instead of waiting on a dead service. After a cool-down it lets one trial call through; if that succeeds it closes again.

**Why not retry everything?** Retrying a timeout makes a slow database even slower. We retry reads only when a connection was dropped or refused. Clients can safely retry a write with the same idempotency key; the server does not retry it automatically.

**Why are the test limits fixed numbers instead of the app's settings?** If tests read the limits from the app, loosening a timeout would loosen the test too. Our first draft had this flaw, and mutation testing exposed it.

**What did your tests find?** Two things. First, redis-py's defaults (10 retries, 5 s timeout) could stall requests for close to a minute. Second, our own design had a gap: the timeout applied to each network round trip, but a query makes three, so delays added up. A new test proved it (200 after 2.11 s), and the fix brought it to 503 in 1.00 s.

**Why build the image once?** The image that is published and deployed is exactly the one that passed the fault tests, not a rebuild that might differ.

**How does rollback work?** `scripts/deploy.sh` records the running image, deploys the new one and runs a smoke test that also checks the deployed version matches the commit. If anything fails, it redeploys the previous image and marks the pipeline failed.

**What would you add next?** Coordinated circuit breakers across multiple replicas and continuous fault injection in a staging environment.
