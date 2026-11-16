"""SermonAudio MCP server for Phoebe (read-only, Phase 1).

Run by Hermes as a stdio subprocess:

    uv --directory /opt/tools run --frozen --no-dev python -m servers.sermonaudio.server

Environment (set in Hermes's config.yaml under mcp_servers.sermonaudio.env):
    SERMONAUDIO_ENV_FILE=/opt/secrets/sermonaudio.env    # SA_API_KEY and SA_BROADCASTER_ID

Both tools only read the church's own listings, so Job D can compare the newest
sermon with Sunday's Planning Center plan (passage, title, speaker, series, date).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from servers.sermonaudio.client import SermonAudioClient, SermonAudioError

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
        "event_type": s.get("eventType"),
        "published_at": _timestamp(s.get("publishTimestamp")),
        "has_audio": bool(audio),
        "has_video": bool(media.get("video")),
        "audio_seconds": next((m.get("duration") for m in audio if m.get("duration")), None),
        "url": SERMON_PAGE.format(s.get("sermonID")),
    }


@mcp.tool
def get_latest_sermons(count: int = 5) -> list[dict]:
    """List the church's newest published sermons on SermonAudio (default 5, max 25),
    most recently preached first: title, speaker, preach date, Bible text, series, event
    type, and whether audio and video are attached."""
    count = max(1, min(int(count), 25))
    return [_summary(s) for s in _fetch(client().latest_sermons, count)]


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
