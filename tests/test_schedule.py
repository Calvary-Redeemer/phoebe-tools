"""Block 1 of Job A: passages, the schedule parser, the cache file, and the preaching_schedule tool."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastmcp.exceptions import ToolError

from servers.workspace import server as ws_server
from servers.workspace.config import ConfigError, parse_config
from servers.workspace.schedule import (ScheduleSettings, parse_date, parse_tab, quarter_tabs,
                                        split_preachers)
from shared import schedule_cache
from shared.passages import find_passage, passage_key, same_passage
from tests.test_workspace import SHEET_ID, SHEET_PATH, FakeGoogle, make_client

SETTINGS = ScheduleSettings(time_zone="America/Denver")

# Shaped like the real sheet: a title row, a blank row, the header on row 3, dated rows,
# then an undated backlog of ideas. Names and titles are made up.
Q4 = [
    ["", "", "Class Schedule", "", "", "", "Q4 Preaching Schedule"],
    [],
    ["", "Date", "Topic / Section", "Teacher", "Men's Study Leader", "Youth Group Leader",
     "Series | Text | Title", "Preacher", "Please add application questions below: "],
    ["", "10/4/2026", "Class 5", "Sam", "", "", 'Genesis 2.5-17: "The Perfect World"', "Sam"],
    ["", "10/11/2026", "Class 6", "Sam", "N/A", "N/A", 'Genesis 2.18-25: "Ultimate Marriage"', "Jack"],
    ["", "10/18/2026", "Class 7", "Sam", "Joel", "Sam", 'Genesis 3.1-7: "The Fall: Part 1"', "Brian H"],
    ["", "10/25/2026", "Class 8", "Sam", "Jack", "Sam", 'Genesis 3.8-13: "The Fall: Part 2"', "Josh G"],
    ["", "11/1/2026", "Class 9", "Sam", "Matt", "Sam", 'Genesis 3.14-21: "The Fall: Part 3"', "Joel"],
    ["", "11/8/20206", "Class 10", "Sam", "N/A", "N/A", 'Genesis 3.22-24: "The Fall: Part 4"', "Sam"],
    ["", "11/15/2026", "Theology 1", "Sam", "Joel", "", 'Genesis 4.1-16: "Cain\'s Sin & God\'s Grace"', "Matt"],
    ["", "11/22/2026", "Theology 2", "Sam", "Jack", "", 'Genesis 4.17-5.32: "Seeds Of Sin & Hope"', "Matt"],
    ["", "11/29/2026", "Hebrews", "Joel", "Matt", "",
     "Advent Week 1: Isaiah 9.1-7 - Rejoicing at the Messiah's Coming", "Matt"],
    ["", "12/6/2026", "Hebrews", "Joel", "", "", "Advent Week 2: Isaiah 11 - The Reign of the Messiah", "Joel"],
    ["", "12/13/2026", "Hebrews", "Joel", "", "", "Advent Week 3: Isaiah 35 - Redemption", "Jack"],
    ["", "12/20/2026", "Hebrews", "Joel", "", "", "Advent Week 4: Isaiah 40.1-11 - Comfort", "Sam"],
    ["", "12/27/2026", "(No class?)", "-", "", "", "One Off", "Joe/Matt"],
    ["", "", "", "", "", "", "", "", "", "", "A future series idea | Mark 9.42-50"],
    ["", "", "", "", "", "", 'Genesis 5.1-6:8 "The Increase & Decrease Of Humanity"'],
]
NOV_18 = date(2026, 11, 18)


# ---- passages ----

@pytest.mark.parametrize("text, found, key", [
    ('Genesis 4.17-5.32: "Seeds Of Sin & Hope"', "Genesis 4.17-5.32", "genesis4:17-5:32"),
    ("Genesis 4:17–5:32 | Seeds of Sin & Hope", "Genesis 4:17–5:32", "genesis4:17-5:32"),
    ("Genesis 4:17-5.32 | Seeds of Sin & Hope", "Genesis 4:17-5.32", "genesis4:17-5:32"),
    ("Advent Week 1: Isaiah 9.1-7 - Rejoicing at the Messiah's Coming", "Isaiah 9.1-7", "isaiah9:1-7"),
    ("Advent Week 2: Isaiah 11 - The Reign of the Messiah", "Isaiah 11", "isaiah11"),
    ("Scripture Reading: 1 John 3:1—3", "1 John 3:1—3", "1john3:1-3"),
    ("Psalms 23", "Psalms 23", "psalm23"),
    ("genesis 2.5 - 17", "genesis 2.5 - 17", "genesis2:5-17"),
])
def test_passages_are_found_and_reduced_to_one_form(text, found, key):
    assert find_passage(text) == found
    assert passage_key(text) == key


@pytest.mark.parametrize("text", ["One Off", "Hebrews", "", None, "Advent Week 1", "Jobs 3 openings"])
def test_text_without_a_chapter_is_not_a_passage(text):
    assert find_passage(text) is None and passage_key(text) is None


def test_same_passage_needs_a_passage_on_both_sides():
    assert same_passage("Genesis 4.1-16", "Genesis 4:1–16 | Cain's Sin")
    assert not same_passage("Genesis 4.1-16", "Genesis 4:1-17")
    assert not same_passage("One Off", "One Off")


# ---- parser ----

def test_q4_rows_parse_to_the_documented_shape():
    rows, problems = parse_tab("Q4", Q4, SETTINGS, NOV_18, date(2027, 1, 13))
    assert [r["date"] for r in rows] == ["2026-11-22", "2026-11-29", "2026-12-06", "2026-12-13",
                                         "2026-12-20", "2026-12-27"]
    assert rows[0] == {
        "date": "2026-11-22", "tab": "Q4", "row": 11,
        "raw": 'Genesis 4.17-5.32: "Seeds Of Sin & Hope"',
        "passage": "Genesis 4.17-5.32", "passage_key": "genesis4:17-5:32",
        "preachers": ["Matt"],
    }
    assert rows[-1] == {
        "date": "2026-12-27", "tab": "Q4", "row": 16, "raw": "One Off",
        "passage": None, "passage_key": None, "preachers": ["Joe", "Matt"],
    }
    assert problems == ['Q4 row 9: date "11/8/20206" is not a date']


def test_undated_backlog_rows_are_skipped_not_reported():
    rows, problems = parse_tab("Q4", Q4, SETTINGS, date(2026, 1, 1), date(2027, 12, 31))
    assert len(rows) == 12                      # 13 dated rows minus the one with the typo
    assert all("5.1-6:8" not in r["raw"] and "Mark" not in r["raw"] for r in rows)
    assert len(problems) == 1


def test_columns_are_found_by_header_text_not_position():
    moved = [["Notes", "Preacher", "Series | Text | Title", "Date"],
             ["x", "Pat", "Mark 1.1-8: \"Beginning\"", "11/22/2026"]]
    rows, problems = parse_tab("Q4", moved, SETTINGS, NOV_18, date(2026, 12, 31))
    assert problems == []
    assert rows[0]["preachers"] == ["Pat"] and rows[0]["passage_key"] == "mark1:1-8" and rows[0]["row"] == 2


def test_missing_header_row_is_reported():
    rows, problems = parse_tab("Q4", [["a", "b"], ["11/22/2026", "Pat"]], SETTINGS, NOV_18, date(2026, 12, 31))
    assert rows == [] and "couldn't find the header row" in problems[0]


@pytest.mark.parametrize("cell, expected", [
    ("11/22/2026", date(2026, 11, 22)), (" 1/3/2027 ", date(2027, 1, 3)),
    ("11/8/20206", None), ("13/40/2026", None), ("Nov 22", None), ("", None),
])
def test_dates_are_read_strictly(cell, expected):
    assert parse_date(cell) == expected


@pytest.mark.parametrize("cell, names", [
    ("Joe/Matt", ["Joe", "Matt"]), ("Brian H", ["Brian H"]), ("Pat & Sam", ["Pat", "Sam"]),
    ("Pat and Sam", ["Pat", "Sam"]), ("-", []), ("", []), ("?", []), ("Brandon", ["Brandon"]),
])
def test_preacher_cells_split_into_names(cell, names):
    assert split_preachers(cell) == names


def test_quarter_tabs_cover_the_window_across_a_year_end():
    assert quarter_tabs(SETTINGS, date(2026, 11, 18), date(2026, 12, 30)) == ["Q4"]
    assert quarter_tabs(SETTINGS, date(2026, 12, 2), date(2027, 1, 27)) == ["Q4", "Q1"]
    assert quarter_tabs(SETTINGS, date(2026, 9, 20), date(2026, 10, 4)) == ["Q3", "Q4"]


# ---- config ----

def test_schedule_block_is_read_and_checked():
    cfg = parse_config({"sheets": {"preaching_schedule": {
        "id": "x", "schedule": {"time_zone": "America/Denver", "preacher_header": "Speaker"}}}})
    settings = cfg.sheets["preaching_schedule"].schedule
    assert settings.time_zone == "America/Denver" and settings.preacher_header == "Speaker"
    assert settings.tab_format == "Q{quarter}"
    assert parse_config({"sheets": {"s": {"id": "x"}}}).sheets["s"].schedule is None


@pytest.mark.parametrize("block, phrase", [
    ({}, "needs a time_zone"),
    ({"time_zone": "Mountain"}, "not a time zone"),
    ({"time_zone": "America/Denver", "tab_format": "Q{month}"}, "may only use"),
    ("Q4", "block of settings"),
])
def test_schedule_block_mistakes_are_explained(block, phrase):
    with pytest.raises(ConfigError, match=phrase):
        parse_config({"sheets": {"s": {"id": "x", "schedule": block}}})


# ---- cache ----

def test_cache_round_trip_and_staleness(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
    made = datetime(2026, 11, 18, 9, 0, 4, tzinfo=timezone(timedelta(hours=-7)))
    path = schedule_cache.write_cache({"generated_at": made.isoformat(), "rows": [{"date": "2026-11-22"}]})
    assert path == tmp_path / "cache" / "preaching_schedule.json"
    assert [p.name for p in path.parent.iterdir()] == ["preaching_schedule.json"]   # no temp file left

    data, why = schedule_cache.read_cache(now=made + timedelta(minutes=5))
    assert why is None and data["rows"][0]["date"] == "2026-11-22"

    data, why = schedule_cache.read_cache(now=made + timedelta(minutes=45))
    assert data is None and "45 minutes old" in why


def test_cache_problems_are_reasons_not_crashes(tmp_path, monkeypatch):
    monkeypatch.delenv("PHOEBE_STATE_DIR", raising=False)
    assert "PHOEBE_STATE_DIR" in schedule_cache.read_cache()[1]
    with pytest.raises(schedule_cache.CacheError, match="PHOEBE_STATE_DIR"):
        schedule_cache.write_cache({})

    monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
    assert "hasn't been read yet" in schedule_cache.read_cache()[1]
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "preaching_schedule.json").write_text("{not json")
    assert "couldn't be read" in schedule_cache.read_cache()[1]


# ---- the tool ----

CONFIG = parse_config({"sheets": {"preaching_schedule": {
    "id": SHEET_ID, "default_tab": "Q4", "columns": "A:H", "schedule": {"time_zone": "America/Denver"}}}})
META = {"properties": {"title": "Preaching Schedule"},
        "sheets": [{"properties": {"title": f"Q{n}", "index": n}} for n in (1, 2, 3, 4)]}


@pytest.fixture
def schedule_tool(monkeypatch, tmp_path):
    def wire(pages, now, state_dir=True):
        fake = FakeGoogle({SHEET_PATH: META, **pages})
        monkeypatch.setattr(ws_server, "_client", make_client(fake))
        monkeypatch.setattr(ws_server, "_config", CONFIG)
        monkeypatch.setattr(ws_server, "_now", lambda: now)
        if state_dir:
            monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
        else:
            monkeypatch.delenv("PHOEBE_STATE_DIR", raising=False)
        return fake
    return wire


def test_preaching_schedule_reads_the_quarter_tab_and_saves_the_cache(schedule_tool, tmp_path):
    # 8:38 pm Wednesday in Denver is already Thursday in UTC; the window must start Wednesday.
    fake = schedule_tool({f"{SHEET_PATH}/values/'Q4'!A:H": {"values": Q4}},
                         datetime(2026, 11, 19, 3, 38, tzinfo=timezone.utc))
    result = ws_server.preaching_schedule(weeks=4)
    assert result["from"] == "2026-11-18" and result["to"] == "2026-12-16"
    assert result["generated_at"] == "2026-11-18T20:38:00-07:00"
    assert [r["date"] for r in result["rows"]] == ["2026-11-22", "2026-11-29", "2026-12-06", "2026-12-13"]
    assert result["problems"] == ['Q4 row 9: date "11/8/20206" is not a date']
    assert result["cache"] == "saved for check_sunday_readiness"
    assert [r.url.path for r in fake.api_requests] == [SHEET_PATH, f"{SHEET_PATH}/values/'Q4'!A:H"]

    saved = json.loads((tmp_path / "cache" / "preaching_schedule.json").read_text())
    assert saved["rows"] == result["rows"] and "cache" not in saved


def test_preaching_schedule_reads_two_tabs_near_a_quarter_end(schedule_tool):
    q3 = [["", "Date", "Series | Text | Title", "Preacher"],
          ["", "9/27/2026", 'Genesis 2.1-3: "Rest"', "Pat"]]
    schedule_tool({f"{SHEET_PATH}/values/'Q3'!A:H": {"values": q3},
                   f"{SHEET_PATH}/values/'Q4'!A:H": {"values": Q4}},
                  datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc))
    rows = ws_server.preaching_schedule(weeks=3)["rows"]
    assert [(r["date"], r["tab"]) for r in rows] == [("2026-09-27", "Q3"), ("2026-10-04", "Q4"), ("2026-10-11", "Q4")]


def test_preaching_schedule_still_answers_when_the_cache_cannot_be_saved(schedule_tool):
    schedule_tool({f"{SHEET_PATH}/values/'Q4'!A:H": {"values": Q4}},
                  datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc), state_dir=False)
    result = ws_server.preaching_schedule(weeks=4)
    assert result["rows"] and result["cache"].startswith("NOT saved: PHOEBE_STATE_DIR")


def test_preaching_schedule_needs_a_schedule_block(monkeypatch):
    monkeypatch.setattr(ws_server, "_config", parse_config({"sheets": {"s": {"id": "x"}}}))
    with pytest.raises(ToolError, match="schedule:"):
        ws_server.preaching_schedule()
