"""The Sunday readiness check (Job A), as pure functions.

Nothing here talks to Planning Center or Google. The server gathers the plans, the
conventions, and the parsed preaching schedule, and these functions decide what needs
fixing and write the report. Phoebe relays the report; she doesn't judge, count, or
compare anything herself.

The report is short on purpose, because a long one goes unread:
- this Sunday's plan title and who is doing what (one line per required position);
- what needs fixing in that plan, said the way a person would say it, with the corrected
  text when the check can work it out;
- for the Sundays after it, only gaps that take time to fill: no plan, no preacher, or the
  preaching schedule disagreeing with the plan.
It never says who has or hasn't accepted, and nothing about positions outside the required
ones. Every finding is still returned in `findings` with a stable code, shown or not.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from servers.pco.conventions import Conventions
from shared.passages import find_passage, mixed_reference, passage_key, with_colons

THIS_SUNDAY = "this_sunday"
COMING_UP = "coming_up"
THIS_SUNDAY_ONLY = frozenset({
    "song_title_unquoted", "reading_missing", "sermon_passage_mismatch", "sermon_title_mismatch",
    "preacher_item_vs_team", "passage_punctuation",
})
# Findings the report shows some other way (in the who-is-doing-what lines) or not at all.
NOT_A_FIX_LINE = frozenset({
    "position_unfilled", "position_declined", "no_preacher_yet", "sheet_preacher_unmatched", "no_plan_yet",
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

    An alias pins the schedule name to one person: it matches that person's record name
    ("Matthew Example"), or the same last name written with the everyday first name
    ("Matt Example", which is how a sermon item names the preacher), and nobody else.
    Without an alias the first names must be exactly equal, and a last initial or last name,
    if the schedule gives one, must match the start of the last name. Exact, because "Joe"
    is the start of "Joel" and they are different people.
    """
    alias = aliases.get(sheet_name.strip().casefold())
    if alias is not None:
        if _loose(alias) == _loose(full_name):
            return True
        alias_parts, full_parts, sheet_parts = alias.split(), full_name.split(), sheet_name.split()
        return (len(full_parts) > 1 and len(alias_parts) > 1 and bool(sheet_parts)
                and full_parts[-1].casefold() == alias_parts[-1].casefold()
                and full_parts[0].casefold() == sheet_parts[0].casefold())
    sheet_parts, full_parts = sheet_name.split(), full_name.split()
    if not sheet_parts or not full_parts:
        return False
    if sheet_parts[0].casefold() != full_parts[0].casefold():
        return False
    if len(sheet_parts) == 1:
        return True
    return full_parts[-1].casefold().startswith(sheet_parts[-1].rstrip(".").casefold())


def same_person(written: str, full_name: str, aliases: dict[str, str]) -> bool:
    """Is the name written in a sermon item the person Planning Center has in the position?

    Yes when the two are equal apart from capitals and spacing. Otherwise the schedule-sheet
    rule decides on the first name (so an alias counts), and a last name, if one is written,
    must be the same. With the alias  Matt: "Matthew Example"  an item that says
    "Matt Example" agrees with Matthew Example on the team; without it, it does not.
    """
    if _loose(written) == _loose(full_name):
        return True
    parts, full_parts = written.split(), full_name.split()
    if not parts or not full_parts:
        return False
    if len(parts) > 1 and parts[-1].casefold() != full_parts[-1].casefold():
        return False
    return preacher_matches(parts[0], full_name, aliases)


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


def _suggested_title(item_match: re.Match | None) -> str | None:
    """What the plan title should say, worked out from the sermon item: "Passage | Title"."""
    if not item_match:
        return None
    passage, title = find_passage(item_match.group("passage")), item_match.group("title").strip()
    return f"{with_colons(passage)} | {title}" if passage and title else None


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
    if not plan_match:
        suggestion = _suggested_title(item_match)
        if suggestion:
            problem = "The plan has no title" if not plan_title else \
                "The plan title isn't in the form Passage | Sermon Title"
            add("plan_title_format", f"{problem}; it should be \"{suggestion}\"")
        elif not plan_title:
            add("plan_title_format", "The plan has no title (expected Passage | Sermon Title)")
        else:
            add("plan_title_format", "The plan title isn't in the form Passage | Sermon Title")
    if not item_match:
        add("sermon_item_missing", "No sermon item in the form Passage | Title | Preacher")
    if plan_match and item_match:
        if "passage" in conv.must_match_plan_title and not _same_passage(
                item_match.group("passage"), plan_match.group("passage")):
            add("sermon_passage_mismatch",
                f"The sermon item has {item_match.group('passage').strip()}; "
                f"the plan title has {plan_match.group('passage').strip()}")
        if "title" in conv.must_match_plan_title and _loose(item_match.group("title")) != _loose(
                plan_match.group("title")):
            add("sermon_title_mismatch",
                f"The sermon item says \"{item_match.group('title').strip()}\"; "
                f"the plan title says \"{plan_match.group('title').strip()}\"")

    # Scripture reading
    reading_passage = None
    if conv.reading_title is not None:
        has_reading = False
        for item in detail.get("items", []):
            match = conv.reading_title.match((item.get("title") or "").strip())
            if match and match.groupdict().get("passage", "x").strip():
                has_reading = True
                reading_passage = match.groupdict().get("passage")
                break
        if not has_reading:
            add("reading_missing", "No scripture reading item with a passage")

    # One reference written two ways ("4:17-5.32"). The comparisons above read past it on
    # purpose; a person reading the title would not. One finding per reference, naming
    # every place it is written that way and what it should say.
    mixed: dict[str, list[str]] = {}
    for place, text in (("plan title", plan_match.group("passage") if plan_match else None),
                        ("sermon item", item_match.group("passage") if item_match else None),
                        ("scripture reading", reading_passage)):
        ref = mixed_reference(text)
        if ref:
            mixed.setdefault(ref, []).append(place)
    for ref, places in mixed.items():
        where = places[0] if len(places) == 1 else f"{', '.join(places[:-1])} and {places[-1]}"
        add("passage_punctuation",
            f"The {where} {'says' if len(places) == 1 else 'say'} \"{ref}\"; "
            f"it should be \"{ref.replace('.', ':')}\"")

    # Song titles
    if conv.songs_must_be_quoted:
        for item in detail.get("items", []):
            title = (item.get("title") or "").strip()
            if item.get("item_type") == conv.song_item_type and title and not _is_quoted(title):
                add("song_title_unquoted", f"Song title isn't in quotes: {title}")

    # Required positions: someone is scheduled, whether or not they have replied yet
    for position in conv.required_positions:
        people = _people(detail, position)
        if people["confirmed"] or people["unconfirmed"]:
            continue
        if people["declined"]:
            add("position_declined",
                f"{position}: {', '.join(people['declined'])} declined and nobody else is scheduled")
        else:
            add("position_unfilled", f"{position}: nobody is scheduled")

    # Nobody in the Preacher position and no sermon item naming one
    if not _preachers_in_plan(detail, conv, item_match):
        add("no_preacher_yet", "No preacher yet")

    # The sermon item's preacher should be the person in the Preacher position
    scheduled = _people(detail, conv.preacher_position)
    on_team = scheduled["confirmed"] + scheduled["unconfirmed"]
    if item_match and on_team:
        named = item_match.group("preacher").strip()
        if not any(same_person(named, person, conv.preacher_aliases) for person in on_team):
            add("preacher_item_vs_team",
                f"The sermon item names {named}; the {conv.preacher_position} position has "
                f"{', '.join(everyday_name(person, conv) for person in on_team)}")

    # Communion on the first Sunday of the month
    if conv.communion_on_first_sunday and conv.communion_terms and sunday.day <= 7:
        titles = [(i.get("title") or "").casefold() for i in detail.get("items", [])]
        if not any(term.casefold() in title for term in conv.communion_terms for title in titles):
            add("communion_missing", f"No \"{conv.communion_terms[0]}\" item (1st Sunday of the month)")

    # Preaching schedule vs Planning Center: passage and preacher
    if compare_schedule:
        if schedule_row is None:
            add("sheet_row_missing", "This Sunday isn't on the preaching schedule sheet"
                if scope == THIS_SUNDAY else "Not on the preaching schedule sheet")
        else:
            pco_passage = (item_match or plan_match)
            pco_passage = pco_passage.group("passage").strip() if pco_passage else None
            if schedule_row.get("passage") and pco_passage and not _same_passage(
                    schedule_row["passage"], pco_passage):
                add("sheet_passage_mismatch",
                    f"The schedule sheet has {schedule_row['passage']}; Planning Center has {pco_passage}")
            sheet_names = schedule_row.get("preachers") or []
            pco_names = _preachers_in_plan(detail, conv, item_match)
            if sheet_names and not pco_names:
                add("sheet_preacher_unmatched",
                    f"The schedule sheet says {'/'.join(sheet_names)}; Planning Center has no preacher yet")
            elif sheet_names and not any(preacher_matches(s, p, conv.preacher_aliases)
                                         for s in sheet_names for p in pco_names):
                add("sheet_preacher_mismatch",
                    f"The schedule sheet says {'/'.join(sheet_names)}; Planning Center has "
                    f"{', '.join(everyday_name(p, conv) for p in pco_names)}")
    return found


# ---- who is doing what ----

def everyday_name(name: str, conv: Conventions) -> str:
    """The name people use. A record name that has an alias is shown the everyday way."""
    return conv.everyday_names.get(_loose(name), name)


def overview(detail: dict, conv: Conventions, schedule_row: dict | None) -> tuple[list[str], bool]:
    """One line per required position, in the order conventions.yaml lists them, and whether
    any of them is empty. Someone who has declined is not doing it; whether the others have
    replied is not shown."""
    _, item_match = _sermon(detail, conv)
    lines, any_empty = [], False
    for position in conv.required_positions:
        people = _people(detail, position)
        names = [everyday_name(n, conv) for n in people["confirmed"] + people["unconfirmed"]]
        if names:
            lines.append(f"• {position}: {', '.join(names)}")
            continue
        any_empty = True
        who = "nobody yet"
        if _loose(position) == _loose(conv.preacher_position):
            if item_match:           # the sermon item names someone the team list doesn't have yet
                who = f"{item_match.group('preacher').strip()} (not scheduled yet)"
            elif schedule_row and schedule_row.get("preachers"):
                who = f"nobody yet (the schedule sheet says {'/'.join(schedule_row['preachers'])})"
        lines.append(f"• {position}: {who}")
    return lines, any_empty


def fix_lines(this_sunday: list[Finding]) -> list[str]:
    """This Sunday's findings as the short list a person would act on."""
    songs = [f for f in this_sunday if f.code == "song_title_unquoted"]
    lines: list[str] = []
    for finding in this_sunday:
        if finding.code in NOT_A_FIX_LINE:
            continue
        if finding.code == "song_title_unquoted":
            if finding is songs[0]:
                lines.append(finding.message if len(songs) == 1
                             else f"{len(songs)} song titles aren't in quotes")
            continue
        lines.append(finding.message)
    return lines


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

    first = sundays[0]
    detail = plans.get(first)
    title = ((detail or {}).get("plan", {}).get("title") or "").strip()
    roles, any_empty = overview(detail, conv, rows.get(first.isoformat())) if detail else ([], False)
    fixes = fix_lines([f for f in findings if f.scope == THIS_SUNDAY])
    return {
        "sunday": first.isoformat(),
        "ready": detail is not None and not fixes and not any_empty,
        "findings": [asdict(f) for f in findings],
        "sheet_notes": sheet_notes,
        "schedule_compared": schedule is not None,
        "report": render_report(today, first, detail is not None, title, roles, any_empty, fixes,
                                [f for f in findings if f.scope == COMING_UP], sheet_notes),
    }


_COMING_UP_GROUPS = (("no_plan_yet", "No plan yet"), ("no_preacher_yet", "No preacher yet"),
                     ("sheet_row_missing", "Not on the schedule sheet"))
_COMING_UP_LINES = ("sheet_passage_mismatch", "sheet_preacher_mismatch")


def _inline(message: str) -> str:
    """A finding as the end of a sentence: lower-case start, full stop."""
    text = message[:1].lower() + message[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def render_report(today: date, sunday: date, has_plan: bool, title: str, roles: list[str],
                  any_empty: bool, fixes: list[str], coming_up: list[Finding],
                  sheet_notes: list[str]) -> str:
    """The finished Slack message. Phoebe posts this text unchanged."""
    lines = [f"*{sunday:%a} {_short(sunday)}* ({today:%A} check)"]
    if not has_plan:
        lines.append("No Planning Center plan exists for this Sunday.")
    else:
        lines.append(title or "(no plan title)")
        lines += roles
        if len(fixes) == 1:
            lines.append(f"*One fix:* {_inline(fixes[0])}")
        elif fixes:
            lines.append("*To fix:*")
            lines += [f"• {fix}" for fix in fixes]
        elif not any_empty:
            lines.append("✅ Nothing to fix.")

    later: list[str] = []
    for code, label in _COMING_UP_GROUPS:
        dates = [_short(date.fromisoformat(f.date)) for f in coming_up if f.code == code]
        if dates:
            later.append(f"• {label}: {', '.join(dates)}")
    later += [f"• {_short(date.fromisoformat(f.date))}: {f.message}"
              for f in coming_up if f.code in _COMING_UP_LINES]
    if later:
        lines.append("*Coming up*")
        lines += later
    if sheet_notes:
        lines.append("*Preaching schedule sheet*")
        lines += [f"• {note}" for note in sheet_notes]
    return "\n".join(lines)
