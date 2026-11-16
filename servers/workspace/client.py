"""Read-only Google Workspace client (Sheets and Calendar) for Phoebe.

Auth: the OAuth token file minted by mint_token.py for phoebe-bot@ (scopes
calendar.readonly, drive.file, spreadsheets.readonly). The file holds a long-lived
*refresh token*; this client trades it for a short-lived *access token* (about an hour)
and keeps that in memory only. /opt/secrets is mounted read-only, so nothing is ever
written back.

Guard rails, all enforced here rather than in Phoebe's instructions:
- The only data method is `get`. The single POST goes to Google's token endpoint.
- Requests go only to the Sheets and Calendar API hosts, so the access token can't be
  sent anywhere else.
- If Google reports that the token carries any scope beyond the three above, the
  client refuses to use it.
- Error messages name what was being read ("the preaching schedule"), never IDs or
  credentials.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
ALLOWED_API_PREFIXES = (SHEETS_API + "/", CALENDAR_API + "/")
ALLOWED_SCOPES = frozenset({
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
})
TOKEN_FILE_VAR = "WORKSPACE_TOKEN_FILE"
USER_AGENT = "phoebe-tools (Calvary Redeemer church admin agent)"
MAX_RETRIES = 3
REMINT = "Re-mint it with `python mint_token.py workspace` and copy it to the NAS."


class WorkspaceError(RuntimeError):
    """A Google Workspace request failed. Message is safe to show in Slack (no secrets, no IDs)."""


def load_token_file(path: str | os.PathLike | None = None) -> dict[str, str]:
    """Read the three fields needed to refresh, from the file named by WORKSPACE_TOKEN_FILE."""
    path = path or os.environ.get(TOKEN_FILE_VAR)
    if not path:
        raise WorkspaceError(f"{TOKEN_FILE_VAR} is not set, so there is no Google token to read.")
    try:
        data = json.loads(Path(path).read_text())
    except OSError as exc:
        raise WorkspaceError(f"Could not read the Google token file ({type(exc).__name__}).") from exc
    except ValueError as exc:
        raise WorkspaceError("The Google token file is not valid JSON. " + REMINT) from exc
    missing = [k for k in ("refresh_token", "client_id", "client_secret") if not data.get(k)]
    if missing:
        raise WorkspaceError(f"The Google token file is missing {', '.join(missing)}. " + REMINT)
    return {k: data[k] for k in ("refresh_token", "client_id", "client_secret")}


class GoogleClient:
    def __init__(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._refresh = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_env(cls) -> "GoogleClient":
        return cls(**load_token_file())

    # ---- auth ----

    def _token(self, force: bool = False) -> str:
        if self._access_token and not force and self._clock() < self._expires_at:
            return self._access_token
        try:
            resp = self._http.post(TOKEN_URL, data=self._refresh)
        except httpx.HTTPError as exc:
            raise WorkspaceError(f"Could not reach Google to sign in: {type(exc).__name__}") from exc
        if resp.status_code in (400, 401):
            raise WorkspaceError("Google rejected Phoebe's workspace token (revoked or expired). " + REMINT)
        if resp.status_code >= 400:
            raise WorkspaceError(f"Google sign-in returned HTTP {resp.status_code}.")
        body = resp.json()
        extra = set(body.get("scope", "").split()) - ALLOWED_SCOPES
        if extra:
            names = ", ".join(sorted(s.rsplit("/", 1)[-1] for s in extra))
            raise WorkspaceError(
                f"Phoebe's workspace token has permissions it should never hold ({names}). "
                "Refusing to use it. " + REMINT
            )
        self._access_token = body["access_token"]
        self._expires_at = self._clock() + max(0, int(body.get("expires_in", 3600)) - 60)
        return self._access_token

    # ---- the only data method ----

    def get(self, url: str, params: dict[str, Any] | None = None, *, what: str = "that") -> dict[str, Any]:
        """GET one JSON document from the Sheets or Calendar API. `what` names it for error messages."""
        if not url.startswith(ALLOWED_API_PREFIXES):
            raise WorkspaceError("This server only talks to the Google Sheets and Calendar APIs.")
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.get(url, params=params, headers={"Authorization": f"Bearer {self._token()}"})
            except httpx.HTTPError as exc:
                raise WorkspaceError(f"Could not reach Google: {type(exc).__name__}") from exc
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._token(force=True)
                continue
            if resp.status_code in (429, 500, 502, 503) and attempt < MAX_RETRIES:
                self._sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if resp.status_code == 401:
                raise WorkspaceError("Google rejected Phoebe's workspace token (401). " + REMINT)
            if resp.status_code == 403:
                raise WorkspaceError(
                    f"Phoebe doesn't have access to {what} (403). Share it with phoebe-bot@ as a viewer, "
                    "and check that the API is enabled in the Phoebe-bot Google Cloud project."
                )
            if resp.status_code == 404:
                raise WorkspaceError(f"Google can't find {what} (404). Check its ID in workspace.yaml.")
            if resp.status_code >= 400:
                raise WorkspaceError(f"Google returned HTTP {resp.status_code} while reading {what}.")
            return resp.json()
        raise WorkspaceError(f"Google is still busy after retries while reading {what}; try again shortly.")

    # ---- Sheets ----

    def sheet_info(self, spreadsheet_id: str, *, what: str) -> tuple[str, list[str]]:
        """Return (spreadsheet title, tab names in order)."""
        meta = self.get(
            f"{SHEETS_API}/{quote(spreadsheet_id, safe='')}",
            {"fields": "properties.title,sheets.properties(title,index)"},
            what=what,
        )
        tabs = sorted((s["properties"] for s in meta.get("sheets", [])), key=lambda p: p.get("index", 0))
        return meta.get("properties", {}).get("title", ""), [p["title"] for p in tabs]

    def sheet_values(self, spreadsheet_id: str, tab: str, columns: str | None, *, what: str) -> list[list[str]]:
        """Return the displayed text of one tab, row by row. `columns` is an A1 column span like "A:H"."""
        a1 = "'" + tab.replace("'", "''") + "'" + (f"!{columns}" if columns else "")
        body = self.get(
            f"{SHEETS_API}/{quote(spreadsheet_id, safe='')}/values/{quote(a1, safe='')}",
            {"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
            what=what,
        )
        return [[str(cell) for cell in row] for row in body.get("values", [])]

    # ---- Calendar ----

    def calendar_events(
        self, calendar_id: str, time_min: str, time_max: str, *, what: str, limit: int = 250
    ) -> tuple[dict[str, Any], list[dict]]:
        """Return (calendar name and time zone, events between two RFC 3339 times, soonest first).

        Repeating events come back as their individual occurrences (singleEvents=true).
        """
        url = f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events"
        params: dict[str, Any] = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": min(limit, 250),
            "fields": "summary,timeZone,nextPageToken,items(status,summary,description,location,"
                      "start,end,visibility,recurringEventId)",
        }
        events: list[dict] = []
        meta: dict[str, Any] = {}
        while True:
            page = self.get(url, params, what=what)
            meta = meta or {"summary": page.get("summary"), "timeZone": page.get("timeZone")}
            events.extend(page.get("items", []))
            token = page.get("nextPageToken")
            if not token or len(events) >= limit:
                return meta, events[:limit]
            params = {**params, "pageToken": token}

    def my_calendars(self) -> list[dict]:
        """Calendars on Phoebe's own calendar list (used by the check script to find IDs)."""
        page = self.get(
            f"{CALENDAR_API}/users/me/calendarList",
            {"fields": "items(id,summary,accessRole,primary)", "maxResults": 250},
            what="Phoebe's calendar list",
        )
        return page.get("items", [])

    def close(self) -> None:
        self._http.close()
