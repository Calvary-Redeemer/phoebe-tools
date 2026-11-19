"""Which Google Sheets and Calendars Phoebe may read.

The list lives on the NAS (/opt/data/phoebe-state/workspace.yaml, located through the
WORKSPACE_CONFIG_FILE variable), never in this public repo. Tools take the short *names*
from that file ("preaching_schedule", "church"), never raw IDs, so Phoebe can only read
what a person has listed there, even though her token could technically open any sheet
shared with her account. See config/workspace.example.yaml.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from servers.workspace.schedule import ScheduleSettings

CONFIG_FILE_VAR = "WORKSPACE_CONFIG_FILE"
_COLUMNS = re.compile(r"^[A-Z]{1,3}:[A-Z]{1,3}$")


class ConfigError(RuntimeError):
    """workspace.yaml is missing or malformed. Message is safe to show in Slack."""


@dataclass(frozen=True)
class SheetEntry:
    id: str
    default_tab: str | None = None
    columns: str | None = None
    schedule: ScheduleSettings | None = None   # set only on the preaching schedule sheet


@dataclass(frozen=True)
class WorkspaceConfig:
    sheets: dict[str, SheetEntry] = field(default_factory=dict)
    calendars: dict[str, str] = field(default_factory=dict)
    default_calendar: str | None = None

    def sheet(self, name: str | None) -> tuple[str, SheetEntry]:
        return _pick(self.sheets, name, None, "sheet", "sheets")

    def calendar(self, name: str | None) -> tuple[str, str]:
        return _pick(self.calendars, name, self.default_calendar, "calendar", "calendars")


def _pick(table: dict, name: str | None, default: str | None, kind: str, section: str):
    if not table:
        raise ConfigError(f"No {section} are listed in workspace.yaml yet, so there is nothing to read.")
    name = name or default or next(iter(table))
    if name not in table:
        raise ConfigError(f"There is no {kind} named '{name}'. Configured {section}: {', '.join(table)}.")
    return name, table[name]


def _entry_id(name: str, entry) -> str:
    value = entry.get("id") if isinstance(entry, dict) else entry
    if not isinstance(value, str) or not value.strip() or value.strip().startswith("<"):
        raise ConfigError(f"'{name}' in workspace.yaml has no ID filled in.")
    return value.strip()


def _schedule(name: str, raw) -> ScheduleSettings | None:
    """Read a sheet's optional `schedule:` block (how preaching_schedule finds dates and names)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"'{name}' schedule should be a block of settings.")
    zone = raw.get("time_zone")
    if not isinstance(zone, str) or not zone.strip():
        raise ConfigError(f"'{name}' schedule needs a time_zone, for example \"America/Denver\".")
    try:
        ZoneInfo(zone.strip())
    except Exception as exc:  # unknown name, or no time zone database on this machine
        raise ConfigError(f"'{name}' schedule time_zone \"{zone}\" is not a time zone this machine knows.") from exc
    known = {"tab_format", "date_header", "sermon_header", "preacher_header"}
    extra = {k: str(v) for k, v in raw.items() if k in known and v}
    tab_format = extra.get("tab_format", "Q{quarter}")
    try:
        tab_format.format(quarter=1, year=2000)
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"'{name}' schedule tab_format may only use {{quarter}} and {{year}}.") from exc
    return ScheduleSettings(time_zone=zone.strip(), **extra)


def parse_config(raw: dict | None) -> WorkspaceConfig:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ConfigError("workspace.yaml should be a mapping with `sheets:` and `calendars:` sections.")
    sheets: dict[str, SheetEntry] = {}
    for name, entry in (raw.get("sheets") or {}).items():
        opts = entry if isinstance(entry, dict) else {}
        columns = opts.get("columns")
        if columns is not None and not _COLUMNS.match(str(columns)):
            raise ConfigError(f"'{name}' columns should look like \"A:H\" (capital letters).")
        tab = opts.get("default_tab")
        sheets[str(name)] = SheetEntry(_entry_id(name, entry), str(tab) if tab else None, columns,
                                       _schedule(name, opts.get("schedule")))
    calendars = {str(n): _entry_id(n, e) for n, e in (raw.get("calendars") or {}).items()}
    default = raw.get("default_calendar")
    if default and default not in calendars:
        raise ConfigError(f"default_calendar '{default}' is not one of the calendars listed.")
    return WorkspaceConfig(sheets, calendars, default)


def load_config(path: str | os.PathLike | None = None) -> WorkspaceConfig:
    path = path or os.environ.get(CONFIG_FILE_VAR)
    if not path:
        raise ConfigError(f"{CONFIG_FILE_VAR} is not set, so Phoebe has no list of sheets and calendars.")
    try:
        return parse_config(yaml.safe_load(Path(path).read_text()))
    except OSError as exc:
        raise ConfigError(f"Could not read workspace.yaml ({type(exc).__name__}).") from exc
    except yaml.YAMLError as exc:
        raise ConfigError("workspace.yaml is not valid YAML.") from exc
