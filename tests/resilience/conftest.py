"""Fixtures for the fault-injection suite.

These are black-box tests: they talk to the running app over HTTP and to
Toxiproxy's control API, exactly as an external client would.

Pass/fail thresholds are service-level objectives (SLOs) defined *here*, not
read from the app. If a developer loosens a timeout or disables a retry, the app
still has to meet these numbers, so the regression is caught. The app's settings
are only read to drive the tests (e.g. how many failures open a circuit).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest

from .toxiproxy_client import Toxiproxy

APP_URL = os.environ.get("APP_URL", "http://localhost:8000")
TOXIPROXY_URL = os.environ.get("TOXIPROXY_URL", "http://localhost:8474")
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "reports"))


def _env_number(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# -- service-level objectives (the contract under failure) ---------------------
# A read while Redis is slow or down: at most two short Redis timeouts.
SLO_DEGRADED_READ = _env_number("SLO_DEGRADED_READ_SECONDS", 1.25)
# Any answer (503 or stale data) while PostgreSQL is slow or down: no hanging.
SLO_FAILURE_RESPONSE = _env_number("SLO_FAILURE_RESPONSE_SECONDS", 2.0)
# A request rejected by an open circuit never touches the network.
SLO_FAST_FAIL = _env_number("SLO_FAST_FAIL_SECONDS", 0.25)
# Back to normal after the failed dependency returns, with no restart.
SLO_RECOVERY = _env_number("SLO_RECOVERY_SECONDS", 5.0)
# A dead dependency must be detected within this many consecutive failures.
SLO_MAX_FAILURES_BEFORE_OPEN = int(_env_number("SLO_MAX_FAILURES_BEFORE_OPEN", 5))


# -- clients ------------------------------------------------------------------


class AppClient:
    def __init__(self, base_url: str):
        self.http = httpx2.Client(base_url=base_url, timeout=15.0)

    def close(self) -> None:
        self.http.close()

    def timed(self, method: str, path: str, **kwargs: Any) -> tuple[httpx2.Response, float]:
        started = time.perf_counter()
        response = self.http.request(method, path, **kwargs)
        return response, time.perf_counter() - started

    def get_product(self, product_id: int) -> tuple[httpx2.Response, float]:
        return self.timed("GET", f"/api/products/{product_id}")

    def create_product(self) -> dict[str, Any]:
        response = self.http.post(
            "/api/products",
            json={"name": f"resilience-{uuid.uuid4().hex[:8]}", "price": 499.0, "stock": 7},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def status(self) -> dict[str, Any]:
        return self.http.get("/status").raise_for_status().json()

    def breaker_state(self, name: str) -> str:
        return self.status()["breakers"][name]["state"]

    def counter(self, name: str) -> int:
        return self.status()["counters"][name]

    def wait_until_ready(self, timeout: float = 20.0) -> None:
        """Block until both dependencies are healthy and both circuits are closed."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                response = self.http.get("/health/ready")
                last = response.json()
                checks = last["checks"]
                if (
                    response.status_code == 200
                    and last["status"] == "ready"
                    and all(check["breaker"] == "closed" for check in checks.values())
                ):
                    return
            except (httpx2.HTTPError, ValueError, KeyError) as exc:
                last = repr(exc)
            time.sleep(0.2)
        raise AssertionError(f"stack not ready after {timeout}s; last readiness: {last}")


@dataclass(frozen=True)
class StackConfig:
    db_timeout: float
    db_max_retries: int
    redis_timeout: float
    breaker_threshold: int
    breaker_reset: float
    cache_fresh: float


def source(response: httpx2.Response) -> str:
    return response.headers.get("X-Data-Source", "-")


# -- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="session")
def toxiproxy() -> Iterator[Toxiproxy]:
    client = Toxiproxy(TOXIPROXY_URL)
    try:
        client.version()
    except httpx2.HTTPError as exc:
        pytest.exit(f"Toxiproxy is not reachable at {TOXIPROXY_URL}: {exc}", returncode=2)
    missing = {"postgres", "redis"} - set(client.proxies())
    if missing:
        pytest.exit(f"Toxiproxy is missing proxies: {sorted(missing)}", returncode=2)
    yield client
    client.reset()  # never leave a fault behind
    client.close()


@pytest.fixture(scope="session")
def app(toxiproxy: Toxiproxy) -> Iterator[AppClient]:
    client = AppClient(APP_URL)
    toxiproxy.reset()
    try:
        client.wait_until_ready(timeout=60)
    except (AssertionError, httpx2.HTTPError) as exc:
        pytest.exit(f"App is not ready at {APP_URL}: {exc}", returncode=2)
    yield client
    client.close()


@pytest.fixture(scope="session")
def cfg(app: AppClient) -> StackConfig:
    """The app's settings, used only to drive the tests, never as pass/fail budgets."""
    c = app.status()["config"]
    config = StackConfig(
        db_timeout=c["db_timeout_seconds"],
        db_max_retries=c["db_max_retries"],
        redis_timeout=c["redis_timeout_seconds"],
        breaker_threshold=c["breaker_failure_threshold"],
        breaker_reset=c["breaker_reset_timeout_seconds"],
        cache_fresh=c["cache_fresh_seconds"],
    )
    if config.cache_fresh > 5:
        pytest.exit(
            f"CACHE_FRESH_SECONDS={config.cache_fresh} is too long for the test suite; "
            "start the stack with docker-compose.ci.yml (it uses 1 s)",
            returncode=2,
        )
    return config


@pytest.fixture(autouse=True)
def clean_slate(toxiproxy: Toxiproxy, app: AppClient):
    """Every test starts with no faults injected and both circuits closed."""
    toxiproxy.reset()
    app.wait_until_ready()
    yield
    toxiproxy.reset()


# -- resilience scorecard -----------------------------------------------------
# Tests record what they injected and what they observed; at the end of the run
# the results are written as a Markdown table (shown on the GitHub Actions run
# page) and printed in the terminal.

_SCORECARD: dict[str, dict[str, str]] = {}


@pytest.fixture
def scorecard(request: pytest.FixtureRequest):
    def record(**fields: str) -> None:
        _SCORECARD.setdefault(request.node.nodeid, {}).update(fields)

    return record


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    report = yield
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        entry = _SCORECARD.setdefault(item.nodeid, {})
        doc = (getattr(item, "function", None) and item.function.__doc__) or item.name
        entry.setdefault("scenario", doc.strip().splitlines()[0])
        entry["outcome"] = report.outcome
    return report


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _SCORECARD:
        return
    icons = {"passed": "✅", "failed": "❌", "skipped": "⏭️"}
    entries = list(_SCORECARD.values())
    passed = sum(1 for e in entries if e.get("outcome") == "passed")
    lines = [
        "## Resilience scorecard",
        "",
        f"**{passed}/{len(entries)} scenarios passed.** "
        "Each row injects a real network fault with Toxiproxy and checks how the app reacts.",
        "",
        "| | Scenario | Fault injected | Expected | Observed |",
        "|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                icons.get(e.get("outcome", ""), "❔"),
                _cell(e.get("scenario", "")),
                _cell(e.get("fault", "-")),
                _cell(e.get("expected", "-")),
                _cell(e.get("observed", "-")),
            )
        )
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "resilience-scorecard.md").write_text("\n".join(lines) + "\n", "utf-8")


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if not _SCORECARD:
        return
    terminalreporter.section("resilience scorecard")
    for e in _SCORECARD.values():
        mark = "PASS" if e.get("outcome") == "passed" else e.get("outcome", "?").upper()
        terminalreporter.write_line(f"[{mark:>6}] {e.get('scenario', '')}")
        terminalreporter.write_line(f"         fault:    {e.get('fault', '-')}")
        terminalreporter.write_line(f"         expected: {e.get('expected', '-')}")
        terminalreporter.write_line(f"         observed: {e.get('observed', '-')}")
