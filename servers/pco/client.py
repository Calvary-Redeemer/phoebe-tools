"""Read-only Planning Center API client.

This class can only send GET requests. Any change to Planning Center goes through the
approval gate (shared/approval_gate, Week 3), never through this client directly.

Auth: Personal Access Token created under Phoebe's own PCO login, sent as HTTP Basic
(application ID as username, secret as password).
Rate limit: PCO allows roughly 100 requests per 20 seconds per user. On HTTP 429 we wait
for the Retry-After header and try again, up to three times.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from shared.secrets import require

BASE_URL = "https://api.planningcenteronline.com"
USER_AGENT = "phoebe-tools (Calvary Redeemer church admin agent)"
MAX_RETRIES = 3


class PCOError(RuntimeError):
    """A Planning Center request failed. Message is safe to show in Slack (no secrets)."""


class PCOClient:
    def __init__(
        self,
        app_id: str,
        secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(
            base_url=BASE_URL,
            auth=(app_id, secret),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self._sleep = sleep

    @classmethod
    def from_env(cls) -> "PCOClient":
        creds = require(["PCO_APP_ID", "PCO_SECRET"], file_var="PCO_ENV_FILE")
        return cls(creds["PCO_APP_ID"], creds["PCO_SECRET"])

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one page. `path` is relative to the API root, e.g. /services/v2/service_types."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                raise PCOError(f"Could not reach Planning Center: {type(exc).__name__}") from exc
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                self._sleep(float(resp.headers.get("Retry-After", "20")))
                continue
            if resp.status_code == 401:
                raise PCOError("Planning Center rejected Phoebe's token (401). Check pco.env.")
            if resp.status_code == 403:
                raise PCOError(f"Phoebe's PCO permissions don't allow {path} (403).")
            if resp.status_code == 404:
                raise PCOError(f"Not found in Planning Center: {path}")
            if resp.status_code >= 400:
                raise PCOError(f"Planning Center returned HTTP {resp.status_code} for {path}")
            return resp.json()
        raise PCOError("Planning Center rate limit: still busy after retries, try again shortly.")

    def get_all(self, path: str, params: dict[str, Any] | None = None, limit: int | None = None) -> list[dict]:
        """GET every page of a collection (following links.next), up to `limit` records."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        records: list[dict] = []
        next_url: str | None = path
        first = True
        while next_url:
            page = self.get(next_url, params if first else None)
            first = False
            records.extend(page.get("data", []))
            if limit is not None and len(records) >= limit:
                return records[:limit]
            next_url = (page.get("links") or {}).get("next")
        return records

    def close(self) -> None:
        self._http.close()
