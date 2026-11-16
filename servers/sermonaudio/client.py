"""Read-only SermonAudio API client.

This class can only send GET requests to SermonAudio's v2 "node" API (the public
catalogue of sermons). Phase 1 never changes a listing; if Phoebe later corrects
listings, that goes through the approval gate, not through this client.

Auth: the church's broadcaster API key, sent in the X-API-Key header.
Scope: every request is pinned to the church's own broadcaster ID. A sermon that
belongs to a different broadcaster is refused, so this isn't a general SermonAudio reader.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

import httpx

from shared.secrets import require

BASE_URL = "https://api.sermonaudio.com/v2/"
USER_AGENT = "phoebe-tools (Calvary Redeemer church admin agent)"
MAX_RETRIES = 3
_SERMON_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")


class SermonAudioError(RuntimeError):
    """A SermonAudio request failed. Message is safe to show in Slack (no secrets)."""


class SermonAudioClient:
    def __init__(
        self,
        api_key: str,
        broadcaster_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.broadcaster_id = broadcaster_id
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"X-API-Key": api_key, "User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self._sleep = sleep

    @classmethod
    def from_env(cls) -> "SermonAudioClient":
        creds = require(["SA_API_KEY", "SA_BROADCASTER_ID"], file_var="SERMONAUDIO_ENV_FILE")
        return cls(creds["SA_API_KEY"], creds["SA_BROADCASTER_ID"])

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET one JSON document. `path` is relative to the v2 root, e.g. node/sermons."""
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                raise SermonAudioError(f"Could not reach SermonAudio: {type(exc).__name__}") from exc
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                self._sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if resp.status_code in (401, 403):
                raise SermonAudioError(
                    f"SermonAudio rejected the church's API key ({resp.status_code}). Check sermonaudio.env."
                )
            if resp.status_code == 404:
                raise SermonAudioError(f"Not found on SermonAudio: {path}")
            if resp.status_code >= 400:
                raise SermonAudioError(f"SermonAudio returned HTTP {resp.status_code} for {path}")
            return resp.json()
        raise SermonAudioError("SermonAudio is still busy after retries; try again shortly.")

    def latest_sermons(self, count: int) -> list[dict]:
        """The church's newest published sermons, most recently preached first."""
        page = self.get("node/sermons", {
            "broadcasterID": self.broadcaster_id,
            "sortBy": "newest",
            "page": 1,
            "pageSize": count,
        })
        return list(page.get("results") or [])[:count]

    def sermon(self, sermon_id: str) -> dict:
        """One sermon by ID. Refuses IDs that aren't plain letters/digits and other broadcasters' sermons."""
        sermon_id = str(sermon_id).strip()
        if not _SERMON_ID.match(sermon_id):
            raise SermonAudioError("That doesn't look like a SermonAudio sermon ID (letters and digits only).")
        record = self.get(f"node/sermons/{sermon_id}")
        owner = (record.get("broadcaster") or {}).get("broadcasterID") or ""
        if owner.lower() != self.broadcaster_id.lower():
            raise SermonAudioError("That sermon belongs to a different broadcaster, so Phoebe won't read it.")
        return record

    def close(self) -> None:
        self._http.close()
