"""Turn the Preaching/Teaching Schedule sheet into dated rows.

Pure functions: no Google calls here, so they are easy to test. The sheet is laid out for
people, not programs (title rows, blank rows, a backlog of undated sermon ideas at the
bottom, the occasional typo), so the parser is deliberately forgiving about layout and
strict about one thing: a row it can't read is reported, never silently dropped.

Columns are found by their header text, not by letter, so inserting a column in the
sheet doesn't break anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from shared.passages import find_passage, passage_key

_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_NAME_SPLIT = re.compile(r"\s*(?:/|&|\band\b)\s*", re.IGNORECASE)
_NOT_A_NAME = {"", "-", "–", "—", "?", "tbd", "n/a"}
HEADER_SEARCH_ROWS = 15


@dataclass(frozen=True)
class ScheduleSettings:
    """How to read the schedule sheet (the `schedule:` block in workspace.yaml)."""
    time_zone: str
    tab_format: str = "Q{quarter}"
    date_header: str = "Date"
    sermon_header: str = "Series | Text | Title"
    preacher_header: str = "Preacher"


def _norm(cell: str) -> str:
    return re.sub(r"\s+", " ", cell or "").strip().casefold()


def quarter_tabs(settings: ScheduleSettings, start: date, end: date) -> list[str]:
    """Tab names for every quarter touched by start..end, in order, without repeats."""
    tabs: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        name = settings.tab_format.format(quarter=(month - 1) // 3 + 1, year=year)
        if name not in tabs:
            tabs.append(name)
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return tabs


def parse_date(cell: str) -> date | None:
    """Read a M/D/YYYY cell. Anything else (including a 5-digit year) is not a date."""
    match = _DATE.match((cell or "").strip())
    if not match:
        return None
    month, day, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def split_preachers(cell: str) -> list[str]:
    """ "Joe/Matt" is two names; "-", "?" and blanks are no name at all."""
    return [n.strip() for n in _NAME_SPLIT.split(cell or "") if _norm(n) not in _NOT_A_NAME]


def _find_columns(rows: list[list[str]], settings: ScheduleSettings) -> tuple[int, dict[str, int]] | None:
    wanted = {"date": _norm(settings.date_header), "sermon": _norm(settings.sermon_header),
              "preacher": _norm(settings.preacher_header)}
    for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        cells = [_norm(c) for c in row]
        if all(text in cells for text in wanted.values()):
            return index, {key: cells.index(text) for key, text in wanted.items()}
    return None


def parse_tab(tab: str, rows: list[list[str]], settings: ScheduleSettings,
              start: date, end: date) -> tuple[list[dict], list[str]]:
    """Return (schedule rows dated start..end, problems a person should fix in the sheet)."""
    found = _find_columns(rows, settings)
    if found is None:
        return [], [f"{tab}: couldn't find the header row (looking for \"{settings.date_header}\", "
                    f"\"{settings.sermon_header}\" and \"{settings.preacher_header}\")"]
    header_index, col = found

    def cell(row: list[str], key: str) -> str:
        return row[col[key]].strip() if col[key] < len(row) else ""

    out: list[dict] = []
    problems: list[str] = []
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        raw_date = cell(row, "date")
        if not raw_date:
            continue                      # undated rows are the backlog of sermon ideas
        when = parse_date(raw_date)
        if when is None:
            problems.append(f"{tab} row {offset}: date \"{raw_date}\" is not a date")
            continue
        if not (start <= when <= end):
            continue
        raw = cell(row, "sermon")
        out.append({
            "date": when.isoformat(),
            "tab": tab,
            "row": offset,
            "raw": raw,
            "passage": find_passage(raw),
            "passage_key": passage_key(raw),
            "preachers": split_preachers(cell(row, "preacher")),
        })
    return out, problems
