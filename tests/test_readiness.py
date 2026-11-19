"""Block 2 of Job A: conventions, the readiness checks, the report text, and the tool."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastmcp.exceptions import ToolError

from servers.pco import server as pco_server
from servers.pco.conventions import ConventionsError, load_conventions, parse_conventions
from servers.pco.readiness import (check_plan, plan_date, preacher_matches, run_check,
                                   same_person, upcoming_sundays)
from shared import schedule_cache
from tests.test_pco import make_client

RAW = {
    "service_type_name": "Corporate Worship",
    "time_zone": "America/Denver",
    "plan": {"title_pattern": r"^(?P<passage>.+?)\s*\|\s*(?P<title>.+)$"},
    "sermon_item": {
        "title_pattern": r"^(?P<passage>.+?)\s*\|\s*(?P<title>.+?)\s*\|\s*(?P<preacher>.+)$",
        "must_match_plan_title": ["passage", "title"],
    },
    "scripture_reading_item": {"title_pattern": r"^Scripture Reading:\s*(?P<passage>.+)$"},
    "songs": {"item_type": "song", "title_must_be_quoted": True},
    "required_positions": ["Service Leader", "Music Leader", "Scripture Reading", "Preacher"],
    "communion": {"required_on": "first_sunday", "title_contains_any": ["Communion", "Lord's Supper"]},
    "preacher_aliases": {},
}
CONV = parse_conventions(RAW)
NOV_15, NOV_22, DEC_6 = date(2026, 11, 15), date(2026, 11, 22), date(2026, 12, 6)
TEAM = [("Sam Roe", "Service Leader", "confirmed"), ("Lee Poe", "Music Leader", "confirmed"),
        ("Pat Doe", "Scripture Reading", "confirmed"), ("Matt Example", "Preacher", "confirmed")]


def plan(title="Genesis 4:17-5.32 | Seeds of Sin & Hope",
         sermon="Genesis 4.17-5.32 | Seeds Of Sin & Hope | Matt Example",
         reading="Scripture Reading: Genesis 4:17–5:32",
         songs=('"Be Thou My Vision"', "“Holy, Holy, Holy”"),
         team=TEAM, needed=(), extra=()):
    items = [{"title": "Movement 1: Gathering", "item_type": "header"}]
    items += [{"title": s, "item_type": "song"} for s in songs]
    items += [{"title": t, "item_type": "item"} for t in (reading, sermon, *extra) if t]
    return {
        "plan": {"title": title, "sort_date": "2026-11-22T17:00:00Z"},
        "items": items,
        "team": [{"name": n, "position": p, "status": s} for n, p, s in team],
        "unfilled_positions": [{"position": p, "quantity": q} for p, q in needed],
        "notes": [],
    }


def row(day="2026-11-22", passage="Genesis 4.17-5.32", preachers=("Matt",)):
    from shared.passages import passage_key
    return {"date": day, "tab": "Q4", "row": 11, "raw": passage or "One Off", "passage": passage,
            "passage_key": passage_key(passage), "preachers": list(preachers)}


def codes(findings):
    return [f.code for f in findings]


def check(detail, sunday=NOV_22, scope="this_sunday", schedule_row=None, compare=False, conv=CONV):
    return check_plan(sunday, detail, conv, scope, schedule_row, compare)


# ---- dates ----

@pytest.mark.parametrize("today, first", [
    (date(2026, 11, 18), NOV_22),      # Wednesday
    (date(2026, 11, 21), NOV_22),      # Saturday
    (date(2026, 11, 22), NOV_22),      # Sunday itself counts as this Sunday
    (date(2026, 11, 23), date(2026, 11, 29)),
])
def test_this_sunday_is_the_next_sunday_on_or_after_today(today, first):
    sundays = upcoming_sundays(today, 6)
    assert sundays[0] == first and len(sundays) == 6
    assert all(d.weekday() == 6 for d in sundays) and sundays[5] - sundays[0] == timedelta(weeks=5)


def test_plan_date_is_the_local_day_not_the_utc_day():
    assert plan_date({"sort_date": "2026-11-22T17:00:00Z"}, CONV) == NOV_22      # 10 am Mountain
    assert plan_date({"sort_date": "2026-11-23T01:30:00Z"}, CONV) == NOV_22      # 6:30 pm Mountain, Sunday
    assert plan_date({"sort_date": None}, CONV) is None and plan_date({"sort_date": "soon"}, CONV) is None


# ---- preacher names ----

@pytest.mark.parametrize("sheet, full, expected", [
    ("Matt", "Matt Example", True),
    ("matt", "Matt Example", True),
    ("Joe", "Joel Example", False),          # the reason matching is exact
    ("Joel", "Joe Example", False),
    ("Joe", "Joe Example", True),
    ("Brian H", "Brian Hale", True),
    ("Brian H.", "Brian Hale", True),
    ("Brian H", "Brian Smith", False),
    ("Jack", "Jack Rivers", True),
    ("Matt Example", "Matt Example", True),
    ("Matt", "Matthew Example", False),      # long form needs an alias
    ("", "Matt Example", False),
])
def test_preacher_matching_is_exact_on_first_name(sheet, full, expected):
    assert preacher_matches(sheet, full, {}) is expected


def test_an_alias_decides_the_match_outright():
    aliases = parse_conventions({**RAW, "preacher_aliases": {"Matt": "Matthew Example"}}).preacher_aliases
    assert preacher_matches("Matt", "Matthew Example", aliases)
    assert not preacher_matches("Matt", "Matt Other", aliases)


# ---- one plan ----

def test_a_clean_plan_has_no_findings():
    assert check(plan(), schedule_row=row(), compare=True) == []


def test_the_real_mercy_grace_mismatch_is_found_and_nothing_else():
    detail = plan(title="Genesis 4:1–16 | Cain's Sin & God's Mercy",
                  sermon="Genesis 4.1-16 | Cain's Sin & God's Grace | Matt Example",
                  reading="Scripture Reading: Genesis 4:1–16")
    found = check(detail, sunday=NOV_15)
    assert codes(found) == ["sermon_title_mismatch"]
    assert found[0].message == ("Sermon item says \"Cain's Sin & God's Grace\"; "
                                "plan title says \"Cain's Sin & God's Mercy\"")
    assert found[0].date == "2026-11-15" and found[0].scope == "this_sunday"


def test_passage_punctuation_and_title_capitals_are_not_mismatches():
    assert check(plan(title="Genesis 4:17–5:32 | Seeds of Sin & Hope",
                      sermon="Genesis 4.17-5.32 | SEEDS OF SIN AND HOPE | Matt Example")) != []   # "and" vs "&"
    assert check(plan(title="Genesis 4:17–5:32 | Seeds of Sin & Hope",
                      sermon="Genesis 4.17-5.32 | seeds of sin & hope | Matt Example")) == []


def test_a_different_passage_is_a_mismatch():
    found = check(plan(sermon="Genesis 4.17-5.31 | Seeds Of Sin & Hope | Matt Example"))
    assert codes(found) == ["sermon_passage_mismatch"]
    assert "Genesis 4.17-5.31" in found[0].message and "Genesis 4:17-5.32" in found[0].message


def test_missing_title_sermon_and_reading_are_each_reported():
    assert codes(check(plan(title="", sermon=None, reading=None))) == [
        "plan_title_format", "sermon_item_missing", "reading_missing"]
    assert codes(check(plan(title="Seeds of Sin & Hope"))) == ["plan_title_format"]
    assert codes(check(plan(reading="Scripture Reading:"))) == ["reading_missing"]


def test_song_titles_need_quotes_straight_or_curly():
    found = check(plan(songs=('"Be Thou My Vision"', "“Holy, Holy, Holy”", "Amazing Grace", "\"Unfinished")))
    assert [f.message for f in found] == ["Song title isn't in quotes: Amazing Grace",
                                          "Song title isn't in quotes: \"Unfinished"]


def test_required_positions_unfilled_declined_and_waiting():
    team = [("Sam Roe", "Service Leader", "unconfirmed"), ("Lee Poe", "Music Leader", "declined"),
            ("Matt Example", "Preacher", "confirmed")]
    found = check(plan(team=team))
    assert [(f.code, f.message) for f in found] == [
        ("position_unconfirmed", "Service Leader: waiting on a reply from Sam Roe"),
        ("position_declined", "Music Leader: Lee Poe declined and nobody else is scheduled"),
        ("position_unfilled", "Scripture Reading: nobody is scheduled"),
    ]


def test_a_confirmed_person_covers_a_position_even_if_another_declined():
    team = TEAM + [("Ray Moe", "Scripture Reading", "declined")]
    assert check(plan(team=team)) == []
    found = check(plan(team=TEAM, needed=[("Music Leader", 1)]))
    assert [f.message for f in found] == ["Music Leader: Planning Center still needs 1 more"]


def test_sermon_item_preacher_must_be_the_person_in_the_preacher_position():
    team = TEAM[:3] + [("Joel Example", "Preacher", "confirmed")]
    found = check(plan(team=team))
    assert codes(found) == ["preacher_item_vs_team"]
    assert found[0].message == "Sermon item names Matt Example; the Preacher position has Joel Example"


def test_a_short_name_in_the_sermon_item_agrees_with_the_long_name_on_the_team_through_an_alias():
    # Planning Center's person record says "Matthew"; the sermon item and the sheet say "Matt".
    team = TEAM[:3] + [("Matthew Example", "Preacher", "confirmed")]
    aliased = parse_conventions({**RAW, "preacher_aliases": {"Matt": "Matthew Example"}})
    assert check(plan(team=team), conv=aliased) == []
    assert check(plan(team=team), schedule_row=row(), compare=True, conv=aliased) == []
    # without the alias both comparisons still speak up
    assert codes(check(plan(team=team), schedule_row=row(), compare=True)) == [
        "preacher_item_vs_team", "sheet_preacher_mismatch"]


@pytest.mark.parametrize("written, full, expected", [
    ("Matt Example", "Matthew Example", True),      # the alias, same last name
    ("matt  example", "Matthew Example", True),
    ("Matt", "Matthew Example", True),              # first name only
    ("Matt Other", "Matthew Example", False),       # alias never overrides a different last name
    ("Matthew Example", "Matthew Example", True),
    ("Joe Example", "Joel Example", False),         # Joe is not Joel
    ("Joel Example", "Joel Example", True),
    ("Joel", "Joel Example", True),
    ("", "Joel Example", False),
])
def test_same_person_rule_for_the_sermon_item(written, full, expected):
    aliases = parse_conventions({**RAW, "preacher_aliases": {"Matt": "Matthew Example"}}).preacher_aliases
    assert same_person(written, full, aliases) is expected


def test_first_sunday_needs_a_communion_item():
    assert codes(check(plan(), sunday=DEC_6)) == ["communion_missing"]
    assert check(plan(extra=("Communion Prayer",)), sunday=DEC_6) == []
    assert check(plan(extra=("The Lord's Supper",)), sunday=DEC_6) == []
    assert check(plan(), sunday=date(2026, 12, 13)) == []          # not a first Sunday


def test_later_sundays_report_gaps_but_not_week_of_details():
    team = [("Sam Roe", "Service Leader", "unconfirmed"), ("Matt Example", "Preacher", "unconfirmed")]
    detail = plan(title="", sermon=None, reading=None, songs=("Amazing Grace",), team=team)
    assert codes(check(detail, sunday=DEC_6, scope="coming_up")) == [
        "plan_title_format", "sermon_item_missing", "position_unfilled", "position_unfilled", "communion_missing"]


# ---- schedule sheet vs Planning Center ----

def test_schedule_agreement_and_each_kind_of_disagreement():
    assert check(plan(), schedule_row=row(preachers=("Joe", "Matt")), compare=True) == []

    found = check(plan(), schedule_row=row(passage="Genesis 5.1-32", preachers=("Jack",)), compare=True)
    assert [(f.code, f.message) for f in found] == [
        ("sheet_passage_mismatch", "Schedule sheet has Genesis 5.1-32; Planning Center has Genesis 4.17-5.32"),
        ("sheet_preacher_mismatch", "Schedule sheet says Jack; Planning Center has Matt Example"),
    ]

    nobody = plan(sermon=None, team=TEAM[:3])
    found = check(nobody, sunday=DEC_6, scope="coming_up", schedule_row=row("2026-12-06", "Isaiah 11", ("Joel",)),
                  compare=True)
    assert "sheet_preacher_unmatched" in codes(found)
    assert "Schedule sheet says Joel; Planning Center has no preacher yet" in [f.message for f in found]

    assert codes(check(plan(), compare=True)) == ["sheet_row_missing"]
    assert check(plan(), schedule_row=row(passage=None, preachers=()), compare=True) == []   # "One Off", no names


def test_joe_on_the_sheet_does_not_match_joel_in_planning_center():
    team = TEAM[:3] + [("Joel Example", "Preacher", "confirmed")]
    detail = plan(sermon="Genesis 4.17-5.32 | Seeds Of Sin & Hope | Joel Example", team=team)
    assert codes(check(detail, schedule_row=row(preachers=("Joe",)), compare=True)) == ["sheet_preacher_mismatch"]
    assert check(detail, schedule_row=row(preachers=("Joel",)), compare=True) == []


# ---- the whole check and its report ----

def test_all_clear_is_one_line():
    result = run_check(date(2026, 11, 18), 2, {NOV_22: plan(), date(2026, 11, 29): plan()}, CONV,
                       {"rows": [row(), row("2026-11-29")], "problems": []}, None)
    assert result["ready"] is True and result["findings"] == [] and result["schedule_compared"] is True
    assert result["report"] == "✅ Sun Nov 22 is ready. Nothing flagged through Nov 29."


def test_report_text_with_every_section():
    plans = {
        NOV_22: plan(title="Genesis 4:17-5.32 | Seeds of Sin & Mercy", songs=("Amazing Grace",)),
        DEC_6: plan(title="Isaiah 11 | The Reign of the Messiah", team=TEAM[:3], sermon=None),
    }
    schedule = {"rows": [row(), row("2026-12-06", "Isaiah 11", ("Joel",))],
                "problems": ['Q4 row 9: date "11/8/20206" is not a date']}
    result = run_check(date(2026, 11, 18), 3, plans, CONV, schedule, None)
    assert result["sunday"] == "2026-11-22" and result["ready"] is False
    assert result["report"] == "\n".join([
        "*Sunday readiness · Sun Nov 22* (Wednesday run)",
        "*Needs attention this Sunday*",
        "• Sermon item says \"Seeds Of Sin & Hope\"; plan title says \"Seeds of Sin & Mercy\"",
        "• Song title isn't in quotes: Amazing Grace",
        "*Coming up*",
        "• Nov 29: No plan yet",
        "• Dec 6: No sermon item in the form Passage | Title | Preacher",
        "• Dec 6: Preacher: nobody is scheduled",
        "• Dec 6: No \"Communion\" item (1st Sunday of the month)",
        "• Dec 6: Schedule sheet says Joel; Planning Center has no preacher yet",
        "*Preaching schedule sheet*",
        "• Q4 row 9: date \"11/8/20206\" is not a date",
        "_Report only. Nothing was changed._",
    ])
    assert {f["code"] for f in result["findings"]} >= {"sermon_title_mismatch", "no_plan_yet", "communion_missing"}


def test_without_a_usable_schedule_the_plan_checks_still_run_and_the_report_says_why():
    reason = "the saved preaching schedule is 45 minutes old (run preaching_schedule again, then this check)"
    result = run_check(date(2026, 11, 18), 1, {NOV_22: plan(songs=("Amazing Grace",))}, CONV, None, reason)
    assert result["schedule_compared"] is False
    assert [f["code"] for f in result["findings"]] == ["song_title_unquoted"]       # no sheet_* findings
    assert f"• Schedule comparison skipped: {reason}" in result["report"]


def test_this_sunday_ready_but_something_coming_up():
    result = run_check(date(2026, 11, 20), 2, {NOV_22: plan()}, CONV, {"rows": [row()], "problems": []}, None)
    assert result["ready"] is True
    assert result["report"].splitlines() == [
        "*Sunday readiness · Sun Nov 22* (Friday run)", "✅ This Sunday is ready.",
        "*Coming up*", "• Nov 29: No plan yet", "_Report only. Nothing was changed._"]


def test_no_plan_for_this_sunday_is_said_plainly():
    result = run_check(date(2026, 11, 18), 1, {}, CONV, {"rows": [], "problems": []}, None)
    assert result["ready"] is False
    assert "• No Planning Center plan exists for this Sunday" in result["report"]


# ---- conventions file ----

@pytest.mark.parametrize("change, phrase", [
    ({"time_zone": None}, "needs time_zone"),
    ({"time_zone": "Mountain"}, "not a time zone"),
    ({"service_type_name": ""}, "needs service_type_name"),
    ({"plan": {}}, "needs plan.title_pattern"),
    ({"sermon_item": {"title_pattern": "("}}, "not a valid pattern"),
    ({"sermon_item": {"title_pattern": r"^(?P<passage>.+)\|(?P<title>.+)$"}}, "must capture preacher"),
    ({"preacher_aliases": ["Matt"]}, "preacher_aliases should map"),
])
def test_conventions_mistakes_are_explained(change, phrase):
    with pytest.raises(ConventionsError, match=phrase):
        parse_conventions({**RAW, **change})


def test_conventions_file_problems_are_explained(tmp_path, monkeypatch):
    monkeypatch.delenv("PCO_CONVENTIONS_FILE", raising=False)
    with pytest.raises(ConventionsError, match="PCO_CONVENTIONS_FILE"):
        load_conventions()
    with pytest.raises(ConventionsError, match="Could not read"):
        load_conventions(tmp_path / "missing.yaml")
    bad = tmp_path / "conventions.yaml"
    bad.write_text("service_type_name: [unclosed")
    with pytest.raises(ConventionsError, match="not valid YAML"):
        load_conventions(bad)


# ---- the tool, against a fake Planning Center ----

def fake_pco(plans, service_type="Corporate Worship"):
    """plans: {plan_id: (sort_date, detail dict from plan())}"""
    base = "/services/v2/service_types"
    seen = []

    def handler(request):
        path = request.url.path
        seen.append(request)
        if path == base:
            return httpx.Response(200, json={"data": [{"id": "7", "attributes": {"name": "Wednesday Night"}},
                                                      {"id": "1", "attributes": {"name": service_type}}]})
        if path == f"{base}/1/plans":
            return httpx.Response(200, json={"data": [
                {"id": pid, "attributes": {"sort_date": when, "title": d["plan"]["title"]}}
                for pid, (when, d) in plans.items()]})
        pid, _, tail = path.removeprefix(f"{base}/1/plans/").partition("/")
        when, d = plans[pid]
        if not tail:
            return httpx.Response(200, json={"data": {"id": pid, "attributes": {**d["plan"], "sort_date": when}}})
        data = {
            "items": [{"id": str(n), "attributes": {**i, "sequence": n}} for n, i in enumerate(d["items"])],
            "team_members": [{"attributes": {"name": t["name"], "team_position_name": t["position"],
                                             "status": {"confirmed": "C", "unconfirmed": "U", "declined": "D"}[t["status"]]}}
                             for t in d["team"]],
            "needed_positions": [{"attributes": {"team_position_name": n["position"], "quantity": n["quantity"]}}
                                 for n in d["unfilled_positions"]],
            "notes": [],
        }[tail]
        return httpx.Response(200, json={"data": data})

    handler.seen = seen
    return handler


@pytest.fixture
def readiness_tool(monkeypatch, tmp_path):
    import yaml

    def wire(plans, now, schedule=None, service_type="Corporate Worship"):
        conventions = tmp_path / "conventions.yaml"
        conventions.write_text(yaml.safe_dump(RAW))
        monkeypatch.setenv("PCO_CONVENTIONS_FILE", str(conventions))
        monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
        handler = fake_pco(plans, service_type)
        monkeypatch.setattr(pco_server, "_client", make_client(handler))
        monkeypatch.setattr(pco_server, "_now", lambda: now)
        if schedule is not None:
            schedule_cache.write_cache(schedule)
        return handler
    return wire


WED_9AM = datetime(2026, 11, 18, 16, 0, tzinfo=timezone.utc)      # 9:00 am Mountain


def test_tool_runs_the_whole_check_from_planning_center_and_the_saved_schedule(readiness_tool):
    plans = {"10": ("2026-11-22T17:00:00Z", plan(songs=("Amazing Grace",))),
             "11": ("2026-11-29T17:00:00Z", plan(title="Isaiah 9:1-7 | Rejoicing",
                                                 sermon="Isaiah 9.1-7 | Rejoicing | Matt Example")),
             "12": ("2026-11-25T02:00:00Z", plan(title="A midweek plan that is not a Sunday"))}
    schedule = {"generated_at": (WED_9AM - timedelta(minutes=1)).isoformat(),
                "rows": [row(), row("2026-11-29", "Isaiah 9.1-7", ("Jack",))], "problems": []}
    handler = readiness_tool(plans, WED_9AM, schedule)
    result = pco_server.check_sunday_readiness(weeks=2)
    assert result["sunday"] == "2026-11-22" and result["schedule_compared"] is True
    assert result["report"].splitlines() == [
        "*Sunday readiness · Sun Nov 22* (Wednesday run)",
        "*Needs attention this Sunday*",
        "• Song title isn't in quotes: Amazing Grace",
        "*Coming up*",
        "• Nov 29: Schedule sheet says Jack; Planning Center has Matt Example",
        "_Report only. Nothing was changed._",
    ]
    assert {r.method for r in handler.seen} == {"GET"}
    assert not any("/plans/12" in r.url.path for r in handler.seen)        # the midweek plan is never opened


@pytest.mark.parametrize("now, label", [
    (datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc), "Saturday"),     # 8 pm Saturday in Denver, Sunday in UTC
    (datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc), "Sunday"),       # 8 pm Sunday in Denver, Monday in UTC
])
def test_this_sunday_is_worked_out_in_mountain_time(readiness_tool, now, label):
    readiness_tool({"10": ("2026-11-22T17:00:00Z", plan(songs=("Amazing Grace",)))}, now)
    result = pco_server.check_sunday_readiness(weeks=1)
    assert result["sunday"] == "2026-11-22"
    assert result["report"].startswith(f"*Sunday readiness · Sun Nov 22* ({label} run)")


def test_a_stale_schedule_is_skipped_and_the_report_says_so(readiness_tool):
    schedule = {"generated_at": (WED_9AM - timedelta(minutes=45)).isoformat(),
                "rows": [row(preachers=("Jack",))], "problems": []}
    readiness_tool({"10": ("2026-11-22T17:00:00Z", plan())}, WED_9AM, schedule)
    result = pco_server.check_sunday_readiness(weeks=1)
    assert result["schedule_compared"] is False and result["findings"] == []
    assert "Schedule comparison skipped: the saved preaching schedule is 45 minutes old" in result["report"]


def test_no_saved_schedule_at_all_is_also_just_a_note(readiness_tool):
    readiness_tool({"10": ("2026-11-22T17:00:00Z", plan())}, WED_9AM)
    assert "hasn't been read yet" in pco_server.check_sunday_readiness(weeks=1)["report"]


def test_wrong_service_type_name_lists_the_real_ones(readiness_tool):
    readiness_tool({}, WED_9AM, service_type="Sunday Morning")
    with pytest.raises(ToolError, match="Service types: Sunday Morning, Wednesday Night"):
        pco_server.check_sunday_readiness()


def test_missing_conventions_file_is_a_plain_error(monkeypatch):
    monkeypatch.delenv("PCO_CONVENTIONS_FILE", raising=False)
    with pytest.raises(ToolError, match="PCO_CONVENTIONS_FILE"):
        pco_server.check_sunday_readiness()


def test_the_tool_description_tells_phoebe_to_post_the_report_unchanged():
    from fastmcp import Client

    async def go():
        async with Client(pco_server.mcp) as c:
            return {t.name: t.description for t in await c.list_tools()}

    tools = asyncio.run(go())
    assert sorted(tools) == ["check_sunday_readiness", "get_plan", "list_service_types", "list_upcoming_plans"]
    assert "exactly as it is" in tools["check_sunday_readiness"]
