"""The Sunday readiness check (Job A), as pure functions.

Nothing here talks to Planning Center or Google. The server gathers the plans, the
conventions, and the parsed preaching schedule, and these functions decide what is
missing or inconsistent and write the report. Phoebe relays the report; she doesn't
judge, count, or compare anything herself.

Two scopes:
- "this_sunday": the next Sunday on or after today. Every check runs.
- "coming_up": the Sundays after it. Only gaps that take time to fix are reported
  (no plan, unfilled or declined positions, missing sermon details, no communion on a
  first Sunday, disagreement with the preaching schedule). People who simply haven't
  confirmed yet, and details that get tidied the week of, are left alone.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from servers.pco.conventions import Conventions
from shared.passages import passage_key

THIS_SUNDAY = "this_sunday"
COMING_UP = "coming_up"
THIS_SUNDAY_ONLY = frozenset({
    "song_title_unquoted", "reading_missing", "sermon_passage_mismatch", "sermon_title_mismatch",
    "position_unconfirmed", "preacher_item_vs_team",
})
_QUOTE_OPEN, _QUOTE_CLOSE = "\"“", "\"”"


@dataclass(frozen=True)
class Finding:
    date: str        # ISO date of the Sunday it is about
    scope: str       # this_sunday | coming_up
    code: str        # stable name; later fixes and tags attach to it
    message: str     # one plain sentence for the report


# ---- small helpers ----

def upcoming_sundays(today: date, weeks: int) -> list[date]:
    """The next Sunday on or after `today`, then the following ones: `weeks` Sundays in all."""
    first = today + timedelta(days=(6 - today.weekday()) % 7)
    return [first + timedelta(weeks=n) for n in range(weeks)]


def plan_date(plan: dict, conv: Conventions) -> date | None:
    """A plan's local calendar date, from Planning Center's sort_date (which is in UTC)."""
    raw = plan.get("sort_date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(conv.zone).date()
    except ValueError:
        return None


def _loose(text: str | None) -> str:
    """Compare text ignoring case, punctuation, and spacing."""
    return re.sub(r"[^a-z0-9]", "", (text or "").casefold())


def _same_passage(a: str | None, b: str | None) -> bool:
    key_a, key_b = passage_key(a), passage_key(b)
    if key_a and key_b:
        return key_a == key_b
    return _loose(a) == _loose(b)       # neither parsed as a reference: fall back to the text


def _is_quoted(title: str) -> bool:
    title = title.strip()
    return len(title) >= 2 and title[0] in _QUOTE_OPEN and title[-1] in _QUOTE_CLOSE


def _short(d: date) -> str:
    return f"{d:%b} {d.day}"


def preacher_matches(sheet_name: str, full_name: str, aliases: dict[str, str]) -> bool:
    """Does a name from the schedule ("Matt", "Brian H") mean this Planning Center person?

    An alias decides it outright. Otherwise the first names must be exactly equal, and a
    last initial or last name, if the schedule gives one, must match the start of the last
    name. Exact, because "Joe" is the start of "Joel" and they are different people.
    """
    alias = aliases.get(sheet_name.strip().casefold())
    if alias is not None:
        return _loose(alias) == _loose(full_name)
    sheet_parts, full_parts = sheet_name.split(), full_name.split()
    if not sheet_parts or not full_parts:
        return False
    if sheet_parts[0].casefold() != full_parts[0].casefold():
        return False
    if len(sheet_parts) == 1:
        return True
    return full_parts[-1].casefold().startswith(sheet_parts[-1].rstrip(".").casefold())


# ---- reading one plan ----

def _sermon(detail: dict, conv: Conventions) -> tuple[re.Match | None, re.Match | None]:
    """(plan title parsed, sermon item title parsed); either may be None."""
    title = (detail.get("plan", {}).get("title") or "").strip()
    plan_match = conv.plan_title.match(title) if title else None
    item_match = None
    for item in detail.get("items", []):
        if item.get("item_type") in ("header", conv.song_item_type):
            continue
        item_match = conv.sermon_title.match((item.get("title") or "").strip())
        if item_match:
            break
    return plan_match, item_match


def _people(detail: dict, position: str) -> dict[str, list[str]]:
    """Names scheduled in a position, grouped by status (confirmed, unconfirmed, declined)."""
    groups: dict[str, list[str]] = {"confirmed": [], "unconfirmed": [], "declined": []}
    for member in detail.get("team", []):
        if _loose(member.get("position")) == _loose(position):
            groups.setdefault(member.get("status") or "unconfirmed", []).append(member.get("name") or "someone")
    return groups


def _preachers_in_plan(detail: dict, conv: Conventions, item_match: re.Match | None) -> list[str]:
    people = _people(detail, conv.preacher_position)
    scheduled = people["confirmed"] + people["unconfirmed"]
    if scheduled:
        return scheduled
    return [item_match.group("preacher").strip()] if item_match else []


def check_plan(sunday: date, detail: dict, conv: Conventions, scope: str,
               schedule_row: dict | None, compare_schedule: bool) -> list[Finding]:
    """Every finding for one plan. `schedule_row` is that Sunday's row from the preaching schedule."""
    found: list[Finding] = []

    def add(code: str, message: str) -> None:
        if scope == THIS_SUNDAY or code not in THIS_SUNDAY_ONLY:
            found.append(Finding(sunday.isoformat(), scope, code, message))

    plan_title = (detail.get("plan", {}).get("title") or "").strip()
    plan_match, item_match = _sermon(detail, conv)

    # Plan title and sermon item
    if not plan_title:
        add("plan_title_format", "The plan has no title (expected Passage | Sermon Title)")
    elif not plan_match:
        add("plan_title_format", f"Plan title \"{plan_title}\" isn't in the form Passage | Sermon Title")
    if not item_match:
        add("sermon_item_missing", "No sermon item in the form Passage | Title | Preacher")
    if plan_match and item_match:
        if "passage" in conv.must_match_plan_title and not _same_passage(
                item_match.group("passage"), plan_match.group("passage")):
            add("sermon_passage_mismatch",
                f"Sermon item has {item_match.group('passage').strip()}; "
                f"plan title has {plan_match.group('passage').strip()}")
        if "title" in conv.must_match_plan_title and _loose(item_match.group("title")) != _loose(
                plan_match.group("title")):
            add("sermon_title_mismatch",
                f"Sermon item says \"{item_match.group('title').strip()}\"; "
                f"plan title says \"{plan_match.group('title').strip()}\"")

    # Scripture reading
    if conv.reading_title is not None:
        has_reading = any(
            (m := conv.reading_title.match((i.get("title") or "").strip())) and m.groupdict().get("passage", "x").strip()
            for i in detail.get("items", []))
        if not has_reading:
            add("reading_missing", "No scripture reading item with a passage")

    # Song titles
    if conv.songs_must_be_quoted:
        for item in detail.get("items", []):
            title = (item.get("title") or "").strip()
            if item.get("item_type") == conv.song_item_type and title and not _is_quoted(title):
                add("song_title_unquoted", f"Song title isn't in quotes: {title}")

    # Required positions
    needed = {_loose(n.get("position")): n.get("quantity") or 0 for n in detail.get("unfilled_positions", [])}
    for position in conv.required_positions:
        people = _people(detail, position)
        if people["confirmed"]:
            pass
        elif people["unconfirmed"]:
            add("position_unconfirmed", f"{position}: waiting on a reply from {', '.join(people['unconfirmed'])}")
        elif people["declined"]:
            add("position_declined",
                f"{position}: {', '.join(people['declined'])} declined and nobody else is scheduled")
        else:
            add("position_unfilled", f"{position}: nobody is scheduled")
        if (people["confirmed"] or people["unconfirmed"]) and needed.get(_loose(position), 0) > 0:
            add("position_unfilled", f"{position}: Planning Center still needs {needed[_loose(position)]} more")

    # The sermon item's preacher should be the person in the Preacher position
    scheduled = _people(detail, conv.preacher_position)
    on_team = scheduled["confirmed"] + scheduled["unconfirmed"]
    if item_match and on_team:
        named = item_match.group("preacher").strip()
        if not any(_loose(named) == _loose(person) for person in on_team):
            add("preacher_item_vs_team",
                f"Sermon item names {named}; the {conv.preacher_position} position has {', '.join(on_team)}")

    # Communion on the first Sunday of the month
    if conv.communion_on_first_sunday and conv.communion_terms and sunday.day <= 7:
        titles = [(i.get("title") or "").casefold() for i in detail.get("items", [])]
        if not any(term.casefold() in title for term in conv.communion_terms for title in titles):
            add("communion_missing", f"No \"{conv.communion_terms[0]}\" item (1st Sunday of the month)")

    # Preaching schedule vs Planning Center: passage and preacher
    if compare_schedule:
        if schedule_row is None:
            add("sheet_row_missing", "Not on the preaching schedule sheet")
        else:
            pco_passage = (item_match or plan_match)
            pco_passage = pco_passage.group("passage").strip() if pco_passage else None
            if schedule_row.get("passage") and pco_passage and not _same_passage(
                    schedule_row["passage"], pco_passage):
                add("sheet_passage_mismatch",
                    f"Schedule sheet has {schedule_row['passage']}; Planning Center has {pco_passage}")
            sheet_names = schedule_row.get("preachers") or []
            pco_names = _preachers_in_plan(detail, conv, item_match)
            if sheet_names and not pco_names:
                add("sheet_preacher_unmatched",
                    f"Schedule sheet says {'/'.join(sheet_names)}; Planning Center has no preacher yet")
            elif sheet_names and not any(preacher_matches(s, p, conv.preacher_aliases)
                                         for s in sheet_names for p in pco_names):
                add("sheet_preacher_mismatch",
                    f"Schedule sheet says {'/'.join(sheet_names)}; Planning Center has {', '.join(pco_names)}")
    return found


# ---- the whole check ----

def run_check(today: date, weeks: int, plans: dict[date, dict], conv: Conventions,
              schedule: dict | None, schedule_reason: str | None) -> dict:
    """Check `weeks` Sundays starting with the next one. `plans` maps a Sunday to its plan detail."""
    sundays = upcoming_sundays(today, weeks)
    rows = {r["date"]: r for r in (schedule or {}).get("rows", [])}
    findings: list[Finding] = []
    for index, sunday in enumerate(sundays):
        scope = THIS_SUNDAY if index == 0 else COMING_UP
        detail = plans.get(sunday)
        if detail is None:
            message = ("No Planning Center plan exists for this Sunday" if scope == THIS_SUNDAY
                       else "No plan yet")
            findings.append(Finding(sunday.isoformat(), scope, "no_plan_yet", message))
            continue
        findings.extend(check_plan(sunday, detail, conv, scope, rows.get(sunday.isoformat()),
                                   compare_schedule=schedule is not None))

    sheet_notes = list((schedule or {}).get("problems", []))
    if schedule is None:
        sheet_notes.append(f"Schedule comparison skipped: {schedule_reason or 'no schedule available'}")
    this_sunday = [f for f in findings if f.scope == THIS_SUNDAY]
    return {
        "sunday": sundays[0].isoformat(),
        "ready": not this_sunday,
        "findings": [asdict(f) for f in findings],
        "sheet_notes": sheet_notes,
        "schedule_compared": schedule is not None,
        "report": render_report(today, sundays, findings, sheet_notes),
    }


def render_report(today: date, sundays: list[date], findings: list[Finding], sheet_notes: list[str]) -> str:
    """The finished Slack message. Phoebe posts this text unchanged."""
    first, last = sundays[0], sundays[-1]
    this_sunday = [f for f in findings if f.scope == THIS_SUNDAY]
    coming_up = [f for f in findings if f.scope == COMING_UP]
    if not findings and not sheet_notes:
        through = f" Nothing flagged through {_short(last)}." if len(sundays) > 1 else ""
        return f"✅ {first:%a} {_short(first)} is ready.{through}"

    lines = [f"*Sunday readiness · {first:%a} {_short(first)}* ({today:%A} run)"]
    if this_sunday:
        lines.append("*Needs attention this Sunday*")
        lines += [f"• {f.message}" for f in this_sunday]
    else:
        lines.append("✅ This Sunday is ready.")
    if coming_up:
        lines.append("*Coming up*")
        lines += [f"• {_short(date.fromisoformat(f.date))}: {f.message}" for f in coming_up]
    if sheet_notes:
        lines.append("*Preaching schedule sheet*")
        lines += [f"• {note}" for note in sheet_notes]
    lines.append("_Report only. Nothing was changed._")
    return "\n".join(lines)
