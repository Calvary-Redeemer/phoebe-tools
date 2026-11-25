"""Making the changes the listing check decided on (Job D).

The pco server decides which fields of Sunday's listing should change and saves that list.
This module applies it, and it is the only code in Phoebe's tools that changes anything on
SermonAudio. It runs without a ✅ in Slack (Brandon's decision, Nov 25, 2026, for listing
text only), so the limits are here, in code, where a message can't talk Phoebe past them:

1. Only the title, Bible text, and speaker, and only on the listing the check named, which
   must still be dated the Sunday that was checked.
2. Each change is refused unless the listing still holds the old value the check saw.
3. Once per field per listing. The ledger remembers every change; if the field differs again
   later (a person corrected Phoebe), it is reported and left alone.
4. A speaker is only set to a name already on one of the church's recent listings.
5. The whole listing is read, sent back with only those fields changed, and read again. If
   anything else moved, the post says so.
6. The listing as it was is saved before the change, under sermonaudio-fixes/.

The tool that calls this takes no arguments: what gets written comes from Planning Center
through a file, never from Phoebe.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from servers.sermonaudio.client import EDITABLE_FIELDS, SermonAudioClient, edit_body

STATE_DIR_VAR = "PHOEBE_STATE_DIR"
FIX_DIR, LEDGER = "sermonaudio-fixes", "ledger.json"
LABEL = {"fullTitle": "the title", "bibleText": "the Bible text", "speakerName": "the speaker"}
_SERMON_ID = re.compile(r"^[A-Za-z0-9]{1,32}$")


class FixError(RuntimeError):
    """Nothing was changed, and why. Message is safe to show in Slack."""


def _loose(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def _dir() -> Path:
    state = os.environ.get(STATE_DIR_VAR)
    if not state:
        raise FixError(f"{STATE_DIR_VAR} is not set, so there is nowhere to record a change. Nothing was changed.")
    return Path(state) / FIX_DIR


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".fix-", suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_ledger() -> list[dict]:
    path = _dir() / LEDGER
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # An unreadable ledger could hide a change already made, so stop rather than risk a second one.
        raise FixError("The record of earlier changes couldn't be read. Nothing was changed.") from exc
    if not isinstance(entries, list):
        raise FixError("The record of earlier changes isn't a list. Nothing was changed.")
    return entries


def _current(record: dict, field: str) -> str:
    value = (record.get("speaker") or {}).get("displayName") if field == "speakerName" else record.get(field)
    return (value or "").strip()


def what_moved(before: dict, after: dict, changes: dict) -> list[str]:
    """Anything that differs after an edit beyond the fields that were meant to change.
    `before` and `after` are whole listing records as read."""
    was, now = edit_body(before), edit_body(after)
    moved = []
    for key, old in was.items():
        if key == "displayTitle" and "fullTitle" in changes:
            continue        # the shortened title is expected to follow the full one
        expected = changes.get(key, old)
        if now.get(key) != expected:
            moved.append(f"{key} is \"{now.get(key)}\" (expected \"{expected}\")")
    was_series, now_series = (before.get("series") or {}).get("seriesID"), (after.get("series") or {}).get("seriesID")
    if was_series != now_series:
        moved.append("the listing is no longer in the same series")
    had_audio = bool((before.get("media") or {}).get("audio"))
    if had_audio and not (after.get("media") or {}).get("audio"):
        moved.append("the audio is no longer attached")
    return moved


def apply_fixes(check: dict, client: SermonAudioClient, now: datetime, known_speakers: set[str]) -> dict:
    """Apply the check's fix list. Returns {"applied": [fix…], "skipped": [sentence…],
    "problems": [sentence…], "record": the listing as it stands afterwards}."""
    sermon_id = str(check.get("sermon_id") or "")
    fixes = check.get("fixes") or []
    if not _SERMON_ID.match(sermon_id) or not isinstance(fixes, list) or len(fixes) > len(EDITABLE_FIELDS):
        raise FixError("The saved check doesn't name one listing and at most three changes. Nothing was changed.")
    ledger = read_ledger()
    record = client.sermon(sermon_id)
    if record.get("preachDate") != check.get("sunday"):
        raise FixError("The listing is no longer dated the Sunday that was checked. Nothing was changed.")

    changes: dict[str, str] = {}
    applied, skipped = [], []
    for fix in fixes:
        field, new = fix.get("field"), fix.get("new")
        label = LABEL.get(field)
        if field not in EDITABLE_FIELDS or not isinstance(new, str) or field in changes:
            skipped.append(f"a change to \"{field}\" was asked for, which I don't make.")
            continue
        earlier = next((e for e in ledger if e.get("sermon_id") == sermon_id and e.get("field") == field), None)
        if earlier:
            skipped.append(f"{label} differs from Planning Center again after I changed it once "
                           f"(to \"{earlier.get('to')}\"), so I left it as it is.")
            continue
        if _current(record, field) != (fix.get("old") or "").strip():
            skipped.append(f"{label} changed on SermonAudio after the check ran, so I didn't touch it.")
            continue
        if field == "speakerName" and _loose(new) not in known_speakers:
            skipped.append(f"the speaker \"{new}\" isn't on a recent listing, so I didn't set it.")
            continue
        changes[field] = new
        applied.append(fix)
    if not changes:
        return {"applied": [], "skipped": skipped, "problems": [], "record": record}

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    snapshot = _dir() / f"{sermon_id}-{stamp}.json"
    entry = {"sermon_id": sermon_id, "sunday": check.get("sunday"), "at": now.isoformat(timespec="seconds"),
             "before": edit_body(record), "changes": changes}
    _write(snapshot, entry)                                  # the way back exists before anything changes
    client.replace_listing_text(sermon_id, changes)
    _write(_dir() / LEDGER, ledger + [
        {"sermon_id": sermon_id, "field": f, "from": _current(record, f), "to": v, "at": entry["at"]}
        for f, v in changes.items()])

    after = client.sermon(sermon_id)
    moved = what_moved(record, after, changes)
    _write(snapshot, {**entry, "after": edit_body(after), "moved": moved})
    problems = ([f"after the change, something else on the listing is different: {'; '.join(moved)}. "
                 f"The listing as it was is saved in {FIX_DIR}/{snapshot.name}."] if moved else [])
    return {"applied": applied, "skipped": skipped, "problems": problems, "record": after}
