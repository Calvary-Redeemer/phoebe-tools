"""The words of the SermonAudio listing post (Job D).

Two servers produce this post: the pco server after it compares the listing with the plan
(nothing has been changed yet, so a change it would make reads "Would fix"), and the
sermonaudio server after it has made the changes ("Fixed"). Both call `render`, so the post
reads the same either way. Phoebe relays the text; she never writes it.

    *SermonAudio · Sun Nov 22* (Monday check)
    Genesis 4:17-5:32 | Seeds of Sin & Hope
    Pat Example · Foundations in Genesis · 51:40
    ✅ Matches Sunday's plan.
"""
from __future__ import annotations

from datetime import date


def length(seconds) -> str:
    """Audio length the way SermonAudio shows it: 51:40, or 1:02:10 past an hour."""
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    seconds = int(seconds)
    hours, minutes, secs = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def short_day(iso: str) -> str:
    day = date.fromisoformat(iso)
    return f"{day:%b} {day.day}"


def part_sentence(part: dict, done: bool) -> str:
    """One change, said plainly, with the old value. `done` picks the tense."""
    what, was, now = part["what"], part["was"], part["now"]
    says = "said" if done else "says"
    if part["kind"] == "agree":
        return f"{what} {says} \"{was}\"; {part['sources']} both say \"{now}\"."
    if part["kind"] == "punctuation":
        tail = f"so it is now \"{now}\"" if done else f"so it should be \"{now}\""
        return f"{what} {says} \"{was}\"; chapter and verse were written two ways, {tail}."
    if part["kind"] == "empty":
        return (f"{what} was empty; it now says \"{now}\"." if done
                else f"{what} is empty; it should say \"{now}\".")
    # "follow": the Bible text field held the passage the title used to have
    tail = f"it now says \"{now}\"" if done else f"it should say \"{now}\""
    return f"{what} {says} \"{was}\", the passage the title had; {tail}."


def _labelled(label_one: str, label_many: str, sentences: list[str]) -> list[str]:
    if not sentences:
        return []
    if len(sentences) == 1:
        return [f"*{label_one}:* {sentences[0]}"]
    return [f"*{label_many}:*"] + [f"• {s}" for s in sentences]


def render(result: dict, *, done: bool = False, listing: dict | None = None) -> str:
    """The post for one check.

    result:  what check_listing returned (sunday, run_day, listing, findings, fixes, …).
    done:    True once the fixes have been applied, so they read "Fixed" in the past tense.
    listing: the listing as it stands after the fixes; defaults to the one in `result`.
    """
    day = date.fromisoformat(result["sunday"])
    lines = [f"*SermonAudio · {day:%a %b} {day.day}* ({result['run_day']} check)"]
    if result.get("unreadable"):
        lines.append(f"SermonAudio couldn't be checked: {result['unreadable']}.")
        return "\n".join(lines)

    listing = listing or result.get("listing")
    findings = result.get("findings") or []
    if not listing:
        lines += [f["message"] for f in findings] or ["Sunday's sermon isn't on SermonAudio yet."]
        return "\n".join(lines)

    lines.append(listing.get("title") or "(no title)")
    has_audio = listing.get("has_audio")
    lines.append(" · ".join([
        listing.get("speaker") or "no speaker",
        listing.get("series") or "no series",
        (length(listing.get("audio_seconds")) or "audio attached") if has_audio else "no audio",
    ]))

    fixed = [part_sentence(part, done) for fix in result.get("fixes") or [] for part in fix["parts"]]
    for_a_person = [f["message"] for f in findings if not f.get("ok")]
    fine = [f["message"] for f in findings if f.get("ok")]

    lines += _labelled("Fixed" if done else "Would fix", "Fixed" if done else "Would fix", fixed)
    lines += _labelled("Needs a person", "Needs a person", for_a_person)
    lines += [f"✅ {message}" for message in fine]
    if not fixed and not for_a_person and not fine:
        lines.append("✅ Matches Sunday's plan.")
    return "\n".join(lines)
