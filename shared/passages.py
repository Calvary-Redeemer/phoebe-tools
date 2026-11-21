"""Find and compare Bible passages written the way people actually type them.

The preaching schedule says "Genesis 4.17-5.32", Planning Center says "Genesis 4:17–5:32",
and both mean the same thing. `passage_key` reduces a passage to one comparable form:
"." becomes ":", every kind of dash becomes "-", spaces and capitals are dropped.
"""
from __future__ import annotations

import re

_BOOKS = [
    "Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua", "Judges", "Ruth",
    "1 Samuel", "2 Samuel", "1 Kings", "2 Kings", "1 Chronicles", "2 Chronicles", "Ezra",
    "Nehemiah", "Esther", "Job", "Psalms", "Psalm", "Proverbs", "Ecclesiastes",
    "Song of Solomon", "Song of Songs", "Isaiah", "Jeremiah", "Lamentations", "Ezekiel",
    "Daniel", "Hosea", "Joel", "Amos", "Obadiah", "Jonah", "Micah", "Nahum", "Habakkuk",
    "Zephaniah", "Haggai", "Zechariah", "Malachi",
    "Matthew", "Mark", "Luke", "John", "Acts", "Romans", "1 Corinthians", "2 Corinthians",
    "Galatians", "Ephesians", "Philippians", "Colossians", "1 Thessalonians",
    "2 Thessalonians", "1 Timothy", "2 Timothy", "Titus", "Philemon", "Hebrews", "James",
    "1 Peter", "2 Peter", "1 John", "2 John", "3 John", "Jude", "Revelation",
]
_DASHES = "-‐‑‒–—−"
# Longest names first, so "1 John" wins over "John" and "Song of Solomon" over nothing.
_BOOK_PATTERN = "|".join(
    re.escape(b).replace(r"\ ", r"\s*") for b in sorted(_BOOKS, key=len, reverse=True)
)
_REFERENCE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<book>{_BOOK_PATTERN})\s+"
    rf"(?P<ref>\d+(?:[.:]\d+)?(?:\s*[{_DASHES}]\s*\d+(?:[.:]\d+)?)?)",
    re.IGNORECASE,
)


def find_passage(text: str | None) -> str | None:
    """Return the first Bible reference in `text` as typed ("Isaiah 9.1-7"), or None.

    Works anywhere in the text, so both 'Genesis 4.17-5.32: "Seeds Of Sin & Hope"' and
    'Advent Week 1: Isaiah 9.1-7 - Rejoicing' are handled. A bare book name with no chapter
    ("One Off", "Hebrews") is not a passage.
    """
    match = _REFERENCE.search(text or "")
    return match.group(0).strip() if match else None


def passage_key(text: str | None) -> str | None:
    """Reduce the first passage in `text` to a comparable form, e.g. "genesis4:17-5:32"."""
    match = _REFERENCE.search(text or "")
    if not match:
        return None
    book = re.sub(r"\s+", "", match.group("book")).lower()
    book = "psalm" if book == "psalms" else "songofsolomon" if book == "songofsongs" else book
    ref = re.sub(r"\s+", "", match.group("ref")).replace(".", ":")
    ref = re.sub(f"[{_DASHES}]", "-", ref)
    return f"{book}{ref}"


def mixed_reference(text: str | None) -> str | None:
    """Return the chapter-and-verse part of the first passage in `text` if it is written two ways.

    "Genesis 4:17-5.32" gives "4:17-5.32"; "Genesis 4:17-5:32" and "Genesis 4.17-5.32" give
    None. `passage_key` treats all three as the same passage, which is right for comparing
    systems and wrong for a title people will read, so this is checked separately.
    """
    match = _REFERENCE.search(text or "")
    if not match:
        return None
    ref = match.group("ref").strip()
    return ref if ":" in ref and "." in ref else None


def with_colons(passage: str) -> str:
    """Write a passage the way plan titles do: "Isaiah 9.1-7" becomes "Isaiah 9:1-7"."""
    return re.sub(r"(?<=\d)\.(?=\d)", ":", passage)


def same_passage(a: str | None, b: str | None) -> bool:
    """True when both texts contain a passage and the passages are the same."""
    key_a, key_b = passage_key(a), passage_key(b)
    return key_a is not None and key_a == key_b
