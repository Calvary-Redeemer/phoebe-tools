"""SermonAudio API client: reads, plus one narrow edit.

Reads are GET requests to SermonAudio's v2 "node" API (the public catalogue of sermons).
The one edit is `replace_listing_text`, which corrects the title, Bible text, or speaker of
one of the church's own listings (Job D). SermonAudio has no "change one field" call for
these: an edit replaces the listing's text record, so this client reads the record, sends
every field back, and changes only the ones it was asked to.

The church's API key can do far more than this (delete a sermon, upload media, create a
series). None of that is written here, and tests fail if any request other than a GET or
that one PUT can leave this class.

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
EDITABLE_FIELDS = ("fullTitle", "bibleText", "speakerName")     # the only fields an edit may change
MAX_TEXT = 200


class SermonAudioError(RuntimeError):
    """A SermonAudio request failed. Message is safe to show in Slack (no secrets)."""


def edit_body(record: dict) -> dict[str, Any]:
    """The fields SermonAudio's update call takes, filled from a listing as it stands now.

    Sending these back unchanged leaves the listing as it was. The publish time must be among
    them: SermonAudio treats a missing one as "not published".
    """
    keywords = record.get("keywords")
    if isinstance(keywords, (list, tuple)):
        keywords = " ".join(str(k) for k in keywords)
    full, display = record.get("fullTitle") or "", record.get("displayTitle")
    # SermonAudio shows a shortened title ending in ".." when no short title was typed. Sending
    # that back would pin it, and it would go stale the day the full title is corrected.
    if isinstance(display, str) and display.endswith("..") and full.startswith(display[:-2].rstrip()):
        display = None
    return {
        "acceptCopyright": True,
        "fullTitle": full,
        "speakerName": (record.get("speaker") or {}).get("displayName"),
        "preachDate": record.get("preachDate"),
        "publishTimestamp": record.get("publishTimestamp"),
        "eventType": record.get("eventType"),
        "displayTitle": display,
        "subtitle": record.get("subtitle"),
        "bibleText": record.get("bibleText"),
        "moreInfoText": record.get("moreInfoText"),
        "languageCode": record.get("languageCode"),
        "newsInFocus": "false",
        "keywords": keywords or None,
    }


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

    def replace_listing_text(self, sermon_id: str, changes: dict[str, str]) -> dict[str, Any]:
        """Correct the title, Bible text, or speaker of one of the church's listings.

        Reads the listing first (so the caller can't hand in a doctored record), sends every
        field back, and changes only the fields named in `changes`. An empty `changes` sends
        the listing back exactly as read, which is how the write is tested before it is trusted.
        Returns {"before": the fields as read, "sent": the fields as sent}. One attempt, no retry.
        """
        unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
        if unknown:
            raise SermonAudioError(f"Phoebe may not change {', '.join(unknown)} on a listing.")
        for name, value in changes.items():
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT or "\n" in value:
                raise SermonAudioError(f"The new {name} is empty, too long, or more than one line.")
        record = self.sermon(sermon_id)            # also refuses another broadcaster's sermon
        before = edit_body(record)
        stamp = before["publishTimestamp"]
        if not isinstance(stamp, (int, float)) or stamp <= 0:
            raise SermonAudioError("That listing isn't published, so Phoebe won't edit it.")
        missing = [k for k in ("fullTitle", "speakerName", "preachDate", "eventType") if not before.get(k)]
        if missing:
            raise SermonAudioError(f"The listing came back without {', '.join(missing)}, so nothing was sent.")
        sent = {**before, **changes}
        path = f"node/sermons/{str(sermon_id).strip()}"
        try:
            resp = self._http.put(path, json=sent)
        except httpx.HTTPError as exc:
            raise SermonAudioError(f"Could not reach SermonAudio: {type(exc).__name__}. "
                                   "The change may or may not have been made; check the listing.") from exc
        if resp.status_code in (401, 403):
            raise SermonAudioError(f"SermonAudio refused the change ({resp.status_code}). "
                                   "The church's API key may not be allowed to edit listings.")
        if resp.status_code >= 400:
            raise SermonAudioError(f"SermonAudio returned HTTP {resp.status_code} for the change; nothing was confirmed.")
        return {"before": before, "sent": sent}

    def close(self) -> None:
        self._http.close()
