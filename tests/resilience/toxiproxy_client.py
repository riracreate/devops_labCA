"""A minimal client for the Toxiproxy HTTP API (https://github.com/Shopify/toxiproxy).

Toxiproxy sits between the app and each dependency. Every byte flows through it,
so the tests can inject latency, drop connections or throttle bandwidth on demand
without touching the app, PostgreSQL or Redis.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2


class Toxiproxy:
    def __init__(self, base_url: str):
        # Toxiproxy refuses requests whose User-Agent looks like a browser.
        self._http = httpx2.Client(
            base_url=base_url, timeout=5.0, headers={"User-Agent": "resilience-tests"}
        )

    def close(self) -> None:
        self._http.close()

    def version(self) -> str:
        return self._http.get("/version").raise_for_status().json()["version"]

    def proxies(self) -> dict[str, Any]:
        return self._http.get("/proxies").raise_for_status().json()

    def reset(self) -> None:
        """Remove every toxic and re-enable every proxy."""
        self._http.post("/reset").raise_for_status()

    def set_enabled(self, proxy: str, enabled: bool) -> None:
        """Disabling a proxy closes its open connections and refuses new ones."""
        self._http.patch(f"/proxies/{proxy}", json={"enabled": enabled}).raise_for_status()

    def add_toxic(
        self,
        proxy: str,
        toxic_type: str,
        stream: str = "downstream",
        toxicity: float = 1.0,
        **attributes: Any,
    ) -> str:
        name = f"{toxic_type}_{stream}_{uuid.uuid4().hex[:6]}"
        self._http.post(
            f"/proxies/{proxy}/toxics",
            json={
                "name": name,
                "type": toxic_type,
                "stream": stream,
                "toxicity": toxicity,
                "attributes": attributes,
            },
        ).raise_for_status()
        return name

    def remove_toxic(self, proxy: str, name: str) -> None:
        response = self._http.delete(f"/proxies/{proxy}/toxics/{name}")
        if response.status_code != 404:  # already gone is fine
            response.raise_for_status()

    @contextmanager
    def toxic(self, proxy: str, toxic_type: str, **kwargs: Any) -> Iterator[str]:
        name = self.add_toxic(proxy, toxic_type, **kwargs)
        try:
            yield name
        finally:
            self.remove_toxic(proxy, name)

    @contextmanager
    def disabled(self, proxy: str) -> Iterator[None]:
        self.set_enabled(proxy, False)
        try:
            yield
        finally:
            self.set_enabled(proxy, True)
