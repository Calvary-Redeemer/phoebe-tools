"""The small file that carries the parsed preaching schedule from one server to another.

The workspace server (which holds the Google token) writes it; the pco server (which
holds the Planning Center token) reads it. That is how the Sunday check compares the
sheet with Planning Center without either server holding the other's credentials, and
without asking Phoebe to copy rows between tools.

Location: $PHOEBE_STATE_DIR/cache/preaching_schedule.json
(on the NAS: /opt/data/phoebe-state/cache/preaching_schedule.json).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR_VAR = "PHOEBE_STATE_DIR"
FILE_NAME = "preaching_schedule.json"
MAX_AGE = timedelta(minutes=30)


class CacheError(RuntimeError):
    """The cache could not be written. Message is safe to show in Slack."""


def cache_path() -> Path:
    state = os.environ.get(STATE_DIR_VAR)
    if not state:
        raise CacheError(f"{STATE_DIR_VAR} is not set, so there is nowhere to keep the schedule.")
    return Path(state) / "cache" / FILE_NAME


def write_cache(data: dict) -> Path:
    """Write the schedule atomically: a reader sees the old file or the new one, never half."""
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".schedule-", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        raise CacheError(f"Could not save the schedule ({type(exc).__name__}).") from exc
    return path


def read_cache(now: datetime | None = None, max_age: timedelta = MAX_AGE) -> tuple[dict | None, str | None]:
    """Return (schedule, None), or (None, why it can't be used) when missing, unreadable, or stale."""
    try:
        path = cache_path()
    except CacheError as exc:
        return None, str(exc)
    if not path.exists():
        return None, "the preaching schedule hasn't been read yet (run preaching_schedule first)"
    try:
        data = json.loads(path.read_text())
        generated = datetime.fromisoformat(data["generated_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, "the saved preaching schedule couldn't be read (run preaching_schedule again)"
    now = now or datetime.now(timezone.utc)
    age = now - generated
    if age > max_age or age < -max_age:
        minutes = int(abs(age).total_seconds() // 60)
        return None, (f"the saved preaching schedule is {minutes} minutes old "
                      "(run preaching_schedule again, then this check)")
    return data, None
