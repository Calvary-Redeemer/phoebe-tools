"""The SermonAudio listing check (Job D), as pure functions.

Nothing here talks to Planning Center or SermonAudio. The server gathers last Sunday's plan,
the conventions, and the listings the sermonaudio server saved, and these functions decide
whether the listing agrees with the plan and which of its fields, if any, should change.

Three places say what the sermon was: the plan title, the sermon item in the order of
service, and the SermonAudio listing. For the passage, the sermon title, and the speaker:

  all agree                                   -> nothing to do
  the two Planning Center places agree,
  the listing differs                         -> the listing should change to match them
  the listing agrees with one of them         -> the listing is right; the other Planning
                                                 Center place is named as the odd one out
  all three differ, or only one Planning
  Center place can be read                    -> nothing changes; a person decides

Two of three, because the typo is as often in Planning Center as on SermonAudio. This module
only decides. The sermonaudio server makes the change, and only when fix_listings is on.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timedelta

from servers.pco.conventions import Conventions
from servers.pco.readiness import _loose, _people, _same_passage, _sermon, everyday_name, same_person
from shared.listing_report import render, short_day
from shared.passages import find_passage, mixed_reference, passage_key, with_colons

_QUOTED = re.compile(r"[\"“]([^\"“”]+)[\"”]\s*$")
_SERMON_LABEL = re.compile(r"^\s*sermon\s*:\s*", re.IGNORECASE)
PLAN_TITLE, SERMON_ITEM, POSITION = "the plan title", "the sermon item", "the Preacher position"


@dataclass(frozen=True)
class Finding:
    date: str            # ISO date of the Sunday it is about
    code: str            # stable name; later fixes and tags attach to it
    message: str         # one plain sentence for the post
    ok: bool = False     # True when the listing itself is right and Planning Center is the odd one out


@dataclass
class Fix:
    field: str                       # the SermonAudio field: fullTitle, bibleText, or speakerName
    old: str                         # what the listing holds now; the change is refused if this has moved
    new: str
    parts: list[dict] = dc_field(default_factory=list)    # what changed, for the post (see shared/listing_report.py)


def last_sunday(today: date) -> date:
    """The most recent Sunday on or before `today`. On a Sunday that is today."""
    return today - timedelta(days=(today.weekday() + 1) % 7)


# ---- what Planning Center says ----

def _passage_text(text: str | None) -> str:
    """The passage in a title part: the Bible reference if there is one, else the text itself
    without a leading "Sermon:" (so "Genesis Introduction" still compares as written)."""
    return find_passage(text) or _SERMON_LABEL.sub("", text or "").strip()


def plan_title_says(title: str | None, conv: Conventions) -> dict | None:
    """Passage and sermon title from a plan title, or None when it holds neither in a readable way.

    Reads the usual form, "Genesis 4:17-5:32 | Seeds of Sin & Hope", and also a title that has a
    passage somewhere and the sermon title in quotes at the end, the way the Advent plans are
    written: 'Advent Week 1: Isaiah 9.1-7 "Rejoicing at the Messiah's Coming"'.
    """
    title = (title or "").strip()
    match = conv.plan_title.match(title) if title else None
    if match:
        return {"passage": _passage_text(match.group("passage")), "title": match.group("title").strip()}
    passage, quoted = find_passage(title), _QUOTED.search(title)
    if passage and quoted:
        return {"passage": passage, "title": quoted.group(1).strip()}
    return None


def plan_says(detail: dict, conv: Conventions) -> dict:
    """Everything the plan says about the sermon, each from its own place (None when absent)."""
    _, item = _sermon(detail, conv)
    people = _people(detail, conv.preacher_position)
    scheduled = people["confirmed"] + people["unconfirmed"]
    return {
        "plan_title": plan_title_says(detail.get("plan", {}).get("title"), conv),
        "sermon_item": ({"passage": _passage_text(item.group("passage")), "title": item.group("title").strip(),
                         "preacher": item.group("preacher").strip()} if item else None),
        "position": scheduled,
        "series": (detail.get("plan", {}).get("series_title") or "").strip() or None,
    }


# ---- the vote ----

def vote(listing: str, first: str | None, second: str | None, same) -> str:
    """How the listing's value stands against two Planning Center places.

    Returns one of: match, fix (they agree, the listing differs), first_odd (the listing agrees
    with the second; the first is the odd one out), second_odd, three_way, one_source (only one
    place could be read and it differs), unknown (neither could be read).
    """
    present = [v for v in (first, second) if v]
    if not present:
        return "unknown"
    if all(same(listing, v) for v in present):
        return "match"
    if len(present) == 1:
        return "one_source"
    if same(first, second):
        return "fix"
    if same(listing, second):
        return "first_odd"
    if same(listing, first):
        return "second_odd"
    return "three_way"


def _same_title(a: str | None, b: str | None) -> bool:
    return _loose(a) == _loose(b)


def _settle(sunday: date, what: str, noun: str, listing: str, first: tuple[str, str | None],
            second: tuple[str, str | None], same, mismatch_code: str) -> tuple[str, Finding | None]:
    """Run the vote for one field and word the finding for every outcome but match and fix."""
    (first_name, first_value), (second_name, second_value) = first, second
    outcome = vote(listing, first_value, second_value, same)
    iso = sunday.isoformat()
    if outcome in ("match", "fix", "unknown"):
        return outcome, None
    if outcome == "first_odd":
        code = "preacher_position_odd_one_out" if first_name == POSITION else "plan_title_odd_one_out"
        return outcome, Finding(iso, code,
                                f"The {noun} matches {second_name}. {first_name[0].upper()}{first_name[1:]} is the "
                                f"odd one out: it says \"{first_value}\".", ok=True)
    if outcome == "second_odd":
        return outcome, Finding(iso, "sermon_item_odd_one_out",
                                f"The {noun} matches {first_name}. {second_name[0].upper()}{second_name[1:]} is the "
                                f"odd one out: it says \"{second_value}\".", ok=True)
    if outcome == "three_way":
        return outcome, Finding(iso, "three_way_disagreement",
                                f"{what} is written three ways: SermonAudio \"{listing}\", {first_name} "
                                f"\"{first_value}\", {second_name} \"{second_value}\".")
    name, value = (first_name, first_value) if first_value else (second_name, second_value)
    return outcome, Finding(iso, mismatch_code,
                            f"{what} says \"{listing}\" on SermonAudio and \"{value}\" in {name}. Planning Center "
                            "has it in only one place I can read, so nothing was changed.")


# ---- which listing ----

def _published_day(sermon: dict, conv: Conventions) -> date | None:
    try:
        return datetime.fromisoformat(sermon.get("published_at") or "").astimezone(conv.zone).date()
    except ValueError:
        return None


def choose_listing(sermons: list[dict], sunday: date, plan_passages: list[str],
                   conv: Conventions) -> tuple[dict | None, list[Finding]]:
    """Sunday's listing, or None with the reason. A listing counts when it is dated that Sunday
    and filed under the Sunday category."""
    iso = sunday.isoformat()
    dated = [s for s in sermons if s.get("preach_date") == iso]
    wanted = _loose(conv.listing_event_type)
    main = [s for s in dated if _loose(s.get("event_type")) == wanted]
    if main:
        extra = ([Finding(iso, "listing_duplicate",
                          f"there are {len(main)} {conv.listing_event_type} listings dated {short_day(iso)}; "
                          "I checked the newest.")] if len(main) > 1 else [])
        return main[0], extra
    for sermon in dated:        # dated right, filed under another category
        texts = [sermon.get("title"), sermon.get("bible_text")]
        if any(_same_passage(t, p) for t in texts if t for p in plan_passages if passage_key(p)):
            return sermon, [Finding(iso, "listing_category",
                                    f"the listing is filed under \"{sermon.get('event_type')}\", not "
                                    f"\"{conv.listing_event_type}\".")]
    for sermon in sermons:      # uploaded since Sunday, dated some other day
        published = _published_day(sermon, conv)
        if (published and published >= sunday and sermon.get("preach_date") != iso
                and _loose(sermon.get("event_type")) == wanted):
            return None, [Finding(iso, "listing_wrong_date",
                                  f"Sunday's sermon isn't on SermonAudio under {short_day(iso)}. A listing "
                                  f"published since then, \"{sermon.get('title')}\", is dated "
                                  f"{short_day(sermon['preach_date'])}.")]
    dates = sorted(s["preach_date"] for s in sermons if s.get("preach_date"))
    newest = f" The newest one there is {short_day(dates[-1])}." if dates else ""
    return None, [Finding(iso, "listing_missing", f"Sunday's sermon isn't on SermonAudio yet.{newest}")]


# ---- the check ----

def _agree(what: str, was: str, now: str, sources: str) -> dict:
    return {"kind": "agree", "what": what, "was": was, "now": now, "sources": sources}


def check_listing(today: date, sunday: date, detail: dict | None, previous_series: str | None,
                  sermons: list[dict], conv: Conventions) -> dict:
    """Compare Sunday's listing with Sunday's plan. Returns the findings, the fixes the listing
    should get, and the post as it reads before anything is changed."""
    iso = sunday.isoformat()
    result: dict = {"sunday": iso, "run_day": f"{today:%A}", "fix_listings": conv.fix_listings,
                    "sermon_id": None, "listing": None, "findings": [], "fixes": []}

    def finish(findings: list[Finding], fixes: list[Fix] = ()) -> dict:
        result["findings"] = [asdict(f) for f in findings]
        result["fixes"] = [asdict(f) for f in fixes]
        result["matches"] = result["listing"] is not None and not fixes and all(f.ok for f in findings)
        result["report"] = render(result)
        return result

    says = plan_says(detail, conv) if detail else None
    plan_passages = [src["passage"] for src in ((says or {}).get("plan_title"), (says or {}).get("sermon_item"))
                     if src]
    listing, findings = choose_listing(sermons, sunday, plan_passages, conv)
    if listing is None:
        return finish(findings)
    result["sermon_id"] = listing.get("sermon_id")
    result["listing"] = {k: listing.get(k) for k in ("title", "speaker", "series", "has_audio", "audio_seconds")}
    if says is None:
        return finish(findings + [Finding(iso, "no_plan", "there is no Planning Center plan for that Sunday, "
                                                          "so there is nothing to compare the listing with.")])

    full_title = (listing.get("title") or "").strip()
    in_form = conv.listing_title.match(full_title)
    title_src, item_src = says["plan_title"], says["sermon_item"]
    fixes: list[Fix] = []
    title_parts: list[dict] = []

    # passage: compared even when the listing title isn't in form, changed only when it is
    listed_passage = _passage_text(in_form.group("passage")) if in_form else (find_passage(full_title) or "")
    new_passage = listed_passage
    outcome, finding = _settle(sunday, "the passage", "passage", listed_passage,
                               (PLAN_TITLE, title_src and title_src["passage"]),
                               (SERMON_ITEM, item_src and item_src["passage"]),
                               _same_passage, "listing_passage_mismatch")
    if finding:
        findings.append(finding)
    if outcome == "fix" and in_form:
        new_passage = with_colons(title_src["passage"])
        title_parts.append(_agree("the passage", listed_passage, new_passage, "the plan title and sermon item"))
    elif outcome == "fix":
        findings.append(Finding(iso, "listing_passage_mismatch",
                                f"the listing says \"{listed_passage}\"; the plan title and sermon item both "
                                f"say \"{with_colons(title_src['passage'])}\"."))
    elif in_form and mixed_reference(listed_passage):
        new_passage = with_colons(listed_passage)
        title_parts.append({"kind": "punctuation", "what": "the passage", "was": listed_passage, "now": new_passage})

    # sermon title
    new_title = None
    if not in_form:
        findings.append(Finding(iso, "listing_title_format",
                                f"the listing's title isn't in the form Passage | Sermon Title: \"{full_title}\"."))
    else:
        listed_title = in_form.group("title").strip()
        new_title = listed_title
        outcome, finding = _settle(sunday, "the sermon title", "sermon title", listed_title,
                                   (PLAN_TITLE, title_src and title_src["title"]),
                                   (SERMON_ITEM, item_src and item_src["title"]),
                                   _same_title, "listing_title_mismatch")
        if finding:
            findings.append(finding)
        if outcome == "fix":
            new_title = title_src["title"]
            title_parts.append(_agree("the sermon title", listed_title, new_title, "the plan title and sermon item"))

    if title_parts:
        fixes.append(Fix("fullTitle", full_title, f"{new_passage} | {new_title}", title_parts))

    # Bible text: changed only when empty or still holding the passage the title is losing
    bible_text = (listing.get("bible_text") or "").strip()
    wanted_key = passage_key(new_passage)
    if wanted_key:
        refs = [r.strip() for r in bible_text.split(";") if r.strip()]
        if not refs:
            fixes.append(Fix("bibleText", bible_text, new_passage,
                             [{"kind": "empty", "what": "the Bible text", "was": "", "now": new_passage}]))
        elif not any(passage_key(r) == wanted_key for r in refs):
            stale = [r for r in refs if new_passage != listed_passage and passage_key(r)
                     and passage_key(r) == passage_key(listed_passage)]
            if stale:
                changed = "; ".join(new_passage if r in stale else r for r in refs)
                fixes.append(Fix("bibleText", bible_text, changed,
                                 [{"kind": "follow", "what": "the Bible text", "was": bible_text, "now": changed}]))
            else:
                findings.append(Finding(iso, "listing_bible_text_mismatch",
                                        f"the Bible text field says \"{bible_text}\", which doesn't include "
                                        f"{new_passage}."))

    # speaker
    listed_speaker = (listing.get("speaker") or "").strip()
    position = says["position"][0] if says["position"] else None
    aliases = conv.preacher_aliases

    def same_speaker(a: str | None, b: str | None) -> bool:
        return bool(a and b) and (_loose(a) == _loose(b) or same_person(a, b, aliases) or same_person(b, a, aliases))

    outcome, finding = _settle(sunday, "the speaker", "speaker", listed_speaker,
                               (POSITION, position and everyday_name(position, conv)),
                               (SERMON_ITEM, item_src and item_src["preacher"]),
                               same_speaker, "listing_speaker_mismatch")
    if finding:
        findings.append(finding)
    if outcome == "fix":
        name = everyday_name(position, conv)
        known = {_loose(s.get("speaker")) for s in sermons if s.get("speaker")}
        if _loose(name) in known:
            fixes.append(Fix("speakerName", listed_speaker, name,
                             [_agree("the speaker", listed_speaker, name, "the Preacher position and sermon item")]))
        else:
            findings.append(Finding(iso, "listing_speaker_mismatch",
                                    f"the speaker says \"{listed_speaker}\"; the Preacher position and sermon item "
                                    f"both say {name}, who isn't on a recent listing, so I left it."))

    # never changed, always said
    if not listing.get("has_audio"):
        findings.append(Finding(iso, "listing_no_audio", "the listing has no audio attached."))
    series = (listing.get("series") or "").strip()
    if not series:
        findings.append(Finding(iso, "listing_no_series", "the listing isn't in a series."))
    elif says["series"] and previous_series and _loose(says["series"]) != _loose(previous_series):
        week_before = (sunday - timedelta(days=7)).isoformat()
        earlier = next((s for s in sermons if s.get("preach_date") == week_before
                        and _loose(s.get("event_type")) == _loose(conv.listing_event_type)), None)
        if earlier and _loose(earlier.get("series")) == _loose(series):
            findings.append(Finding(iso, "listing_series_unchanged",
                                    f"Planning Center starts a new series this Sunday (\"{says['series']}\"), but "
                                    f"the listing is still in \"{series}\"."))
    return finish(findings, fixes)
