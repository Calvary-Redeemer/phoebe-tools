"""Small JSON files that carry data from one tool server to another.

Each server holds one system's credentials and nothing else. When a check needs data from
two systems, the server that can read the first one saves what it read here, and the server
that can read the second one picks it up. Phoebe never copies data between tools.

Location: $PHOEBE_STATE_DIR/cache/<name>   (on the NAS: /opt/data/phoebe-state/cache/).
A file older than 30 minutes is refused, so a check never runs on last week's data.

The preaching schedule has its own, older helper (shared/schedule_cache.py) with the same rules.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR_VAR = "PHOEBE_STATE_DIR"
MAX_AGE = timedelta(minutes=30)
_NAME = re.compile(r"^[a-z][a-z0-9_]*\.json$")


class CacheError(RuntimeError):
    """The file could not be written. Message is safe to show in Slack."""


def cache_path(name: str) -> Path:
    if not _NAME.match(name):
        raise CacheError(f"\"{name}\" is not a cache file name.")
    state = os.environ.get(STATE_DIR_VAR)
    if not state:
        raise CacheError(f"{STATE_DIR_VAR} is not set, so there is nowhere to keep {name}.")
    return Path(state) / "cache" / name


def write_json(name: str, data: dict) -> Path:
    """Write atomically: a reader sees the old file or the new one, never half of one."""
    path = cache_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        raise CacheError(f"Could not save {name} ({type(exc).__name__}).") from exc
    return path


def read_json(name: str, *, what: str, rerun: str, now: datetime | None = None,
              max_age: timedelta = MAX_AGE) -> tuple[dict | None, str | None]:
    """Return (data, None), or (None, why it can't be used) when missing, unreadable, or stale.

    `what` names the data in a sentence ("SermonAudio's listings") and `rerun` names the tool
    that refreshes it ("get_latest_sermons"), so the reason reads plainly in a report.
    """
    try:
        path = cache_path(name)
    except CacheError as exc:
        return None, str(exc)
    if not path.exists():
        return None, f"{what} haven't been read yet (run {rerun} first)"
    try:
        data = json.loads(path.read_text())
        generated = datetime.fromisoformat(data["generated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, f"the saved copy of {what} couldn't be read (run {rerun} again)"
    now = now or datetime.now(timezone.utc)
    age = now - generated
    if age > max_age or age < -max_age:
        minutes = int(abs(age).total_seconds() // 60)
        return None, f"the saved copy of {what} is {minutes} minutes old (run {rerun} again, then this check)"
    return data, None
