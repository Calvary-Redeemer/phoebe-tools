"""SermonAudio MCP server for Phoebe (read-only, Phase 1).

Run by Hermes as a stdio subprocess:

    uv --directory /opt/tools run --frozen --no-dev python -m servers.sermonaudio.server

Environment (set in Hermes's config.yaml under mcp_servers.sermonaudio.env):
    SERMONAUDIO_ENV_FILE=/opt/secrets/sermonaudio.env    # SA_API_KEY and SA_BROADCASTER_ID
    PHOEBE_STATE_DIR=/opt/data/phoebe-state              # where get_latest_sermons saves what it read

Both tools only read the church's own listings. get_latest_sermons also saves what it read to
$PHOEBE_STATE_DIR/cache/sermonaudio_latest.json, which the pco server's check_sermon_listing
compares with Sunday's Planning Center plan (Job D). The saved file holds public listing data only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from servers.sermonaudio.client import SermonAudioClient, SermonAudioError
from shared.cache import CacheError, write_json

mcp = FastMCP(
    "sermonaudio",
    instructions=(
        "Read-only access to Calvary Redeemer's sermon listings on SermonAudio. "
        "Use get_latest_sermons to see the newest uploads and get_sermon for one sermon's "
        "full details. These tools cannot change anything."
    ),
)

SERMON_PAGE = "https://www.sermonaudio.com/sermons/{}"
MAX_TEXT_CHARS = 1000
LISTINGS_FILE = "sermonaudio_latest.json"
SAVED_COUNT = 25      # how many listings the saved file holds, whatever count Phoebe asked for

_client: SermonAudioClient | None = None


def client() -> SermonAudioClient:
    """Create the client on first use, so the server starts even before credentials are tested."""
    global _client
    if _client is None:
        try:
            _client = SermonAudioClient.from_env()
        except Exception as exc:  # MissingSecret, unreadable file
            raise ToolError(str(exc)) from exc
    return _client


def _fetch(fn, *args):
    try:
        return fn(*args)
    except SermonAudioError as exc:
        raise ToolError(str(exc)) from exc


def _timestamp(value: Any) -> str | None:
    """SermonAudio reports publish/update times as Unix seconds; show them as UTC ISO text."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")


def _summary(s: dict) -> dict[str, Any]:
    """The fields Job D compares against the Planning Center plan."""
    media = s.get("media") or {}
    audio = media.get("audio") or []
    return {
        "sermon_id": s.get("sermonID"),
        "title": s.get("fullTitle") or s.get("displayTitle"),
        "speaker": (s.get("speaker") or {}).get("displayName"),
        "preach_date": s.get("preachDate"),
        "bible_text": s.get("bibleText"),
        "series": (s.get("series") or {}).get("title") or s.get("subtitle"),
        "series_id": (s.get("series") or {}).get("seriesID"),
        "event_type": s.get("eventType"),
        "published_at": _timestamp(s.get("publishTimestamp")),
        "has_audio": bool(audio),
        "has_video": bool(media.get("video")),
        "audio_seconds": next((m.get("duration") for m in audio if m.get("duration")), None),
        "url": SERMON_PAGE.format(s.get("sermonID")),
    }


def _save(sermons: list[dict]) -> None:
    """Keep what was just read where the pco server can find it. Never fails the read itself:
    if there is nowhere to save, check_sermon_listing says the listings haven't been read."""
    try:
        write_json(LISTINGS_FILE, {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sermons": sermons,
        })
    except CacheError:
        pass


@mcp.tool
def get_latest_sermons(count: int = 5) -> list[dict]:
    """List the church's newest published sermons on SermonAudio (default 5, max 25),
    most recently preached first: title, speaker, preach date, Bible text, series, event
    type, and whether audio and video are attached. Also saves the newest 25 for the pco
    tool check_sermon_listing, so run this first when checking Sunday's listing."""
    count = max(1, min(int(count), SAVED_COUNT))
    sermons = [_summary(s) for s in _fetch(client().latest_sermons, SAVED_COUNT)]
    _save(sermons)
    return sermons[:count]


@mcp.tool
def get_sermon(sermon_id: str) -> dict:
    """Full details for one of the church's sermons by its SermonAudio ID (from
    get_latest_sermons): everything in the list view plus the description, keywords,
    and when the listing was last updated."""
    s = _fetch(client().sermon, sermon_id)
    more = (s.get("moreInfoText") or "").strip()
    return {
        **_summary(s),
        "display_title": s.get("displayTitle"),
        "subtitle": s.get("subtitle"),
        "description": more[:MAX_TEXT_CHARS] + ("…" if len(more) > MAX_TEXT_CHARS else ""),
        "keywords": s.get("keywords"),
        "language": s.get("languageCode"),
        "updated_at": _timestamp(s.get("updateDate")),
    }


if __name__ == "__main__":
    mcp.run()
