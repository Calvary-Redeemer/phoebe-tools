"""The church's Planning Center conventions, read from conventions.yaml on the NAS.

The Sunday check never guesses where a piece of information lives. It reads this file:
which service type is Sunday worship, how the plan title and sermon item are written,
which team positions the service can't run without, when communion is expected. When a convention
changes, a person edits one file and no code changes.

Location: the file named by PCO_CONVENTIONS_FILE
(on the NAS: /opt/data/phoebe-state/conventions.yaml). See config/conventions.example.yaml.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

CONVENTIONS_FILE_VAR = "PCO_CONVENTIONS_FILE"


LISTING_TITLE_DEFAULT = r"^(?P<passage>.+?)\s*\|\s*(?P<title>.+)$"     # "Genesis 4:17-5:32 | Seeds of Sin & Hope"


class ConventionsError(RuntimeError):
    """conventions.yaml is missing or malformed. Message is safe to show in Slack."""


@dataclass(frozen=True)
class Conventions:
    service_type_name: str
    time_zone: str
    plan_title: re.Pattern
    sermon_title: re.Pattern
    reading_title: re.Pattern | None
    must_match_plan_title: tuple[str, ...] = ("passage", "title")
    song_item_type: str = "song"
    songs_must_be_quoted: bool = True
    required_positions: tuple[str, ...] = ()
    preacher_position: str = "Preacher"
    communion_on_first_sunday: bool = False
    communion_terms: tuple[str, ...] = ()
    preacher_aliases: dict[str, str] = field(default_factory=dict)
    everyday_names: dict[str, str] = field(default_factory=dict)
    # How Sunday sermons are listed on SermonAudio (Job D). All optional in the file.
    listing_event_type: str = "Sunday Service"
    listing_title: re.Pattern = re.compile(LISTING_TITLE_DEFAULT)
    fix_listings: bool = False

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)


def _loose(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _pattern(raw: dict, section: str, required: bool) -> re.Pattern | None:
    text = ((raw.get(section) or {}).get("title_pattern") or "").strip()
    if not text:
        if required:
            raise ConventionsError(f"conventions.yaml needs {section}.title_pattern.")
        return None
    try:
        return re.compile(text)
    except re.error as exc:
        raise ConventionsError(f"{section}.title_pattern is not a valid pattern ({exc}).") from exc


def parse_conventions(raw: dict | None) -> Conventions:
    if not isinstance(raw, dict):
        raise ConventionsError("conventions.yaml should be a mapping of settings.")
    name = str(raw.get("service_type_name") or "").strip()
    if not name:
        raise ConventionsError("conventions.yaml needs service_type_name.")
    zone = str(raw.get("time_zone") or "").strip()
    if not zone:
        raise ConventionsError("conventions.yaml needs time_zone, for example \"America/Denver\".")
    try:
        ZoneInfo(zone)
    except Exception as exc:  # unknown name, or no time zone database on this machine
        raise ConventionsError(f"time_zone \"{zone}\" is not a time zone this machine knows.") from exc

    plan = _pattern(raw, "plan", required=True)
    sermon = _pattern(raw, "sermon_item", required=True)
    for label, pattern, groups in (("plan", plan, ("passage", "title")),
                                   ("sermon_item", sermon, ("passage", "title", "preacher"))):
        missing = [g for g in groups if g not in pattern.groupindex]
        if missing:
            raise ConventionsError(f"{label}.title_pattern must capture {', '.join(missing)} "
                                   "as named groups, like (?P<passage>...).")
    reading = _pattern(raw, "scripture_reading_item", required=False)

    songs = raw.get("songs") or {}
    communion = raw.get("communion") or {}
    aliases = raw.get("preacher_aliases") or {}
    if not isinstance(aliases, dict):
        raise ConventionsError("preacher_aliases should map a schedule name to a Planning Center name.")
    if not isinstance(raw.get("required_positions") or [], list):
        raise ConventionsError("required_positions should be a list of Planning Center position names.")
    listing = raw.get("sermonaudio") or {}
    if not isinstance(listing, dict):
        raise ConventionsError("sermonaudio should be a mapping of settings.")
    listing_title = _pattern(raw, "sermonaudio", required=False) or re.compile(LISTING_TITLE_DEFAULT)
    missing = [g for g in ("passage", "title") if g not in listing_title.groupindex]
    if missing:
        raise ConventionsError(f"sermonaudio.title_pattern must capture {', '.join(missing)} "
                               "as named groups, like (?P<passage>...).")
    if not isinstance(listing.get("fix_listings", False), bool):
        raise ConventionsError("sermonaudio.fix_listings should be true or false.")
    # The other direction, for showing a name: "Matthew Example" on the record reads "Matt Example".
    everyday = {}
    for short, record in aliases.items():
        short_parts, record_parts = str(short).split(), str(record).split()
        if short_parts and len(record_parts) > 1:
            everyday[_loose(str(record))] = f"{short_parts[0]} {record_parts[-1]}"
    return Conventions(
        service_type_name=name,
        time_zone=zone,
        plan_title=plan,
        sermon_title=sermon,
        reading_title=reading,
        must_match_plan_title=tuple((raw.get("sermon_item") or {}).get("must_match_plan_title")
                                    or ("passage", "title")),
        song_item_type=str(songs.get("item_type") or "song"),
        songs_must_be_quoted=bool(songs.get("title_must_be_quoted", True)),
        required_positions=tuple(str(p) for p in raw.get("required_positions") or ()),
        preacher_position=str(raw.get("preacher_position") or "Preacher"),
        communion_on_first_sunday=communion.get("required_on") == "first_sunday",
        communion_terms=tuple(str(t) for t in communion.get("title_contains_any") or ()),
        preacher_aliases={str(k).strip().casefold(): str(v).strip() for k, v in aliases.items()},
        everyday_names=everyday,
        listing_event_type=str(listing.get("event_type") or "Sunday Service"),
        listing_title=listing_title,
        fix_listings=bool(listing.get("fix_listings", False)),
    )


def load_conventions(path: str | os.PathLike | None = None) -> Conventions:
    path = path or os.environ.get(CONVENTIONS_FILE_VAR)
    if not path:
        raise ConventionsError(f"{CONVENTIONS_FILE_VAR} is not set, so the Sunday check has no conventions to read.")
    try:
        return parse_conventions(yaml.safe_load(Path(path).read_text()))
    except OSError as exc:
        raise ConventionsError(f"Could not read conventions.yaml ({type(exc).__name__}).") from exc
    except yaml.YAMLError as exc:
        raise ConventionsError("conventions.yaml is not valid YAML.") from exc
