"""Job D: does Sunday's SermonAudio listing agree with the plan, and what should change?"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from servers.pco import server as pco_server
from servers.pco.conventions import ConventionsError, parse_conventions
from servers.pco.listing import check_listing, last_sunday, plan_title_says, vote
from shared import cache
from shared.listing_report import length, render
from tests.test_pco import make_client
from tests.test_readiness import RAW, fake_pco, plan

CONV = parse_conventions({**RAW, "preacher_aliases": {"Matt": "Matthew Example"}})
NOV_22, NOV_23 = date(2026, 11, 22), date(2026, 11, 23)
TEAM = [("Matthew Example", "Preacher", "confirmed"), ("Sam Roe", "Service Leader", "confirmed")]


def listing(title="Genesis 4:17-5:32 | Seeds of Sin & Hope", speaker="Matt Example", day="2026-11-22",
            bible="Genesis 4:17-5:32", series="Foundations in Genesis", event="Sunday Service",
            secs=3100, sid="1122261914", published="2026-11-22T19:07:56+00:00"):
    return {"sermon_id": sid, "title": title, "speaker": speaker, "preach_date": day, "bible_text": bible,
            "series": series, "series_id": 7, "event_type": event, "published_at": published,
            "has_audio": bool(secs), "has_video": False, "audio_seconds": secs,
            "url": f"https://www.sermonaudio.com/sermons/{sid}"}


WEEK_BEFORE = listing(title="Genesis 4:1-16 | Cain's Sin & God's Grace", day="2026-11-15", bible="Genesis 4:1-16",
                      secs=2828, sid="1115261856", published="2026-11-15T18:57:00+00:00")


def the_plan(title="Genesis 4:17-5.32 | Seeds of Sin & Hope",
             sermon="Sermon: Genesis 4:17-5.32 | Seeds of Sin & Hope | Matt Example", team=TEAM, series=None):
    detail = plan(title=title, sermon=sermon, team=team)
    detail["plan"]["series_title"] = series
    return detail


def run(detail, *listings, today=NOV_23, sunday=NOV_22, previous_series=None, conv=CONV):
    return check_listing(today, sunday, detail, previous_series, [*listings, WEEK_BEFORE], conv)


def codes(result):
    return [f["code"] for f in result["findings"]]


# ---- which Sunday ----

@pytest.mark.parametrize("today, sunday", [
    (date(2026, 11, 22), NOV_22),          # Sunday afternoon: today
    (date(2026, 11, 23), NOV_22),          # Monday morning
    (date(2026, 11, 28), NOV_22),          # Saturday still looks back
    (date(2026, 11, 29), date(2026, 11, 29)),
])
def test_last_sunday_is_the_sunday_on_or_before_today(today, sunday):
    assert last_sunday(today) == sunday


# ---- reading the plan title ----

def test_plan_title_is_read_in_the_usual_form_and_in_the_advent_form():
    assert plan_title_says("Genesis 4:17-5.32 | Seeds of Sin & Hope", CONV) == {
        "passage": "Genesis 4:17-5.32", "title": "Seeds of Sin & Hope"}
    assert plan_title_says('Advent Week 1: Isaiah 9.1-7 "Rejoicing at the Messiah\'s Coming"', CONV) == {
        "passage": "Isaiah 9.1-7", "title": "Rejoicing at the Messiah's Coming"}
    assert plan_title_says("Advent Week 2: Isaiah 11: “The Reign of the Messiah in Righteousness”", CONV) == {
        "passage": "Isaiah 11", "title": "The Reign of the Messiah in Righteousness"}
    assert plan_title_says("Genesis Introduction | A Firm Foundation", CONV)["passage"] == "Genesis Introduction"
    assert plan_title_says("One Off Sermon", CONV) is None
    assert plan_title_says("", CONV) is None


# ---- the vote ----

@pytest.mark.parametrize("listed, first, second, outcome", [
    ("a", "a", "a", "match"),
    ("b", "a", "a", "fix"),                # the two Planning Center places agree; the listing differs
    ("a", "b", "a", "first_odd"),          # the listing agrees with the second
    ("a", "a", "b", "second_odd"),
    ("a", "b", "c", "three_way"),
    ("a", "a", None, "match"),
    ("a", None, "b", "one_source"),        # one place only, and it differs: nobody outvotes anybody
    ("a", None, None, "unknown"),
])
def test_two_of_three_decides(listed, first, second, outcome):
    assert vote(listed, first, second, lambda x, y: x == y) == outcome


# ---- the real weeks ----

def test_nov_22_as_it_really_was_matches_and_reads_as_brandon_approved():
    # Planning Center wrote "4:17-5.32" in both places; SermonAudio had it right. Nothing to fix.
    result = run(the_plan(), listing())
    assert result["findings"] == [] and result["fixes"] == [] and result["matches"] is True
    assert result["sermon_id"] == "1122261914"
    assert result["report"] == (
        "*SermonAudio · Sun Nov 22* (Monday check)\n"
        "Genesis 4:17-5:32 | Seeds of Sin & Hope\n"
        "Matt Example · Foundations in Genesis · 51:40\n"
        "✅ Matches Sunday's plan."
    )


def test_capitals_and_punctuation_in_a_sermon_title_are_not_differences():
    result = run(
        the_plan(title="Genesis 3:22-24 | The Fall (Part 4) - The Brokenness Of Sin",
                 sermon="Sermon: Genesis 3.22-24 | The Fall (Part 4) - The Brokenness Of Sin | Matt Example"),
        listing(title="Genesis 3:22-24 | The Fall (Part 4) The Brokenness of Sin", bible="Genesis 3:22-24"))
    assert result["matches"] is True and result["fixes"] == []


def test_when_the_plan_title_is_the_typo_the_listing_is_left_alone_and_the_plan_title_is_named():
    # The Nov 15 case as first seen: "Mercy" in the plan title, "Grace" in the sermon item and on SermonAudio.
    result = run(
        the_plan(title="Genesis 4:1-16 | Cain's Sin & God's Mercy",
                 sermon="Sermon: Genesis 4.1-16 | Cain's Sin & God's Grace | Matt Example"),
        listing(title="Genesis 4:1-16 | Cain's Sin & God's Grace", bible="Genesis 4:1-16"))
    assert result["fixes"] == []
    assert codes(result) == ["plan_title_odd_one_out"]
    assert result["matches"] is True                       # the listing is right
    assert result["report"].splitlines()[-1] == (
        "✅ The sermon title matches the sermon item. The plan title is the odd one out: "
        "it says \"Cain's Sin & God's Mercy\".")


def test_when_the_sermon_item_is_the_typo_it_is_the_one_named():
    result = run(the_plan(sermon="Sermon: Genesis 4:17-5.32 | Seeds of Sin & Hop | Matt Example"), listing())
    assert result["fixes"] == [] and codes(result) == ["sermon_item_odd_one_out"]
    assert "The sermon item is the odd one out" in result["report"]


# ---- when the listing should change ----

def test_a_listing_that_differs_from_both_planning_center_places_gets_fixed():
    result = run(
        the_plan(title="Isaiah 9:1-7 | Rejoicing at the Messiah's Coming",
                 sermon="Sermon: Isaiah 9.1-7 | Rejoicing at the Messiah's Coming | Matt Example"),
        listing(title="Isaiah 9:1-17 | Rejoicing at the Messiahs Coming", bible="Isaiah 9:1-17"))
    assert result["matches"] is False and result["findings"] == []
    title_fix, bible_fix = result["fixes"]
    assert (title_fix["field"], title_fix["old"], title_fix["new"]) == (
        "fullTitle", "Isaiah 9:1-17 | Rejoicing at the Messiahs Coming",
        "Isaiah 9:1-7 | Rejoicing at the Messiahs Coming")      # the title differs only by an apostrophe: kept
    assert (bible_fix["field"], bible_fix["old"], bible_fix["new"]) == ("bibleText", "Isaiah 9:1-17", "Isaiah 9:1-7")
    assert result["report"].splitlines()[3:] == [
        "*Would fix:*",
        "• the passage says \"Isaiah 9:1-17\"; the plan title and sermon item both say \"Isaiah 9:1-7\".",
        "• the Bible text says \"Isaiah 9:1-17\", the passage the title had; it should say \"Isaiah 9:1-7\".",
    ]
    done = render(result, done=True, listing={**result["listing"], "title": title_fix["new"]})
    assert done.splitlines()[1] == "Isaiah 9:1-7 | Rejoicing at the Messiahs Coming"
    assert done.splitlines()[3:5] == [
        "*Fixed:*",
        "• the passage said \"Isaiah 9:1-17\"; the plan title and sermon item both say \"Isaiah 9:1-7\".",
    ]


def test_a_wrong_sermon_title_is_fixed_to_the_plan_title_s_wording():
    result = run(the_plan(), listing(title="Genesis 4:17-5:32 | Seeds of Sin"))
    (fix,) = result["fixes"]
    assert fix["new"] == "Genesis 4:17-5:32 | Seeds of Sin & Hope"
    assert result["report"].splitlines()[-1] == (
        "*Would fix:* the sermon title says \"Seeds of Sin\"; the plan title and sermon item both say "
        "\"Seeds of Sin & Hope\".")


def test_an_advent_style_plan_title_still_counts_as_a_second_place():
    result = run(
        the_plan(title='Advent Week 1: Isaiah 9.1-7 "Rejoicing at the Messiah\'s Coming"',
                 sermon="Sermon: Isaiah 9:1-7 | Rejoicing at the Messiah's Coming | Matt Example"),
        listing(title="Isaiah 9:1-17 | Rejoicing at the Messiah's Coming", bible="Isaiah 9:1-7"))
    (fix,) = result["fixes"]
    assert fix["new"] == "Isaiah 9:1-7 | Rejoicing at the Messiah's Coming"     # written with colons


def test_with_only_one_readable_place_in_the_plan_nothing_is_changed():
    result = run(
        the_plan(title="One Off Sermon", sermon="Sermon: Luke 19:11-27 | Ready for His Return | Matt Example"),
        listing(title="Luke 19:11-17 | Ready for His Return", bible="Luke 19:11-17"))
    assert result["fixes"] == []
    assert codes(result) == ["listing_passage_mismatch"]
    assert "only one place I can read, so nothing was changed" in result["report"]


def test_three_different_versions_change_nothing():
    result = run(the_plan(title="Genesis 4:17-5:32 | Seeds of Sin & Hope",
                          sermon="Sermon: Genesis 4:17-5:30 | Seeds of Sin & Hope | Matt Example"),
                 listing(title="Genesis 4:17-5:31 | Seeds of Sin & Hope", bible="Genesis 4:17-5:31"))
    assert result["fixes"] == [] and codes(result) == ["three_way_disagreement"]


def test_a_passage_written_two_ways_on_the_listing_is_rewritten_with_colons():
    result = run(the_plan(), listing(title="Genesis 4:17-5.32 | Seeds of Sin & Hope"))
    (fix,) = result["fixes"]
    assert fix["new"] == "Genesis 4:17-5:32 | Seeds of Sin & Hope"
    assert "chapter and verse were written two ways, so it should be \"Genesis 4:17-5:32\"" in result["report"]


def test_a_title_not_in_passage_bar_title_form_is_reported_and_never_rewritten():
    result = run(the_plan(), listing(title="Seeds of Sin & Hope (Genesis 4:17-5:32)"))
    assert result["fixes"] == [] and codes(result) == ["listing_title_format"]


# ---- the Bible text field ----

def test_bible_text_may_hold_more_than_the_title_s_passage():
    assert run(the_plan(), listing(bible="Ezekiel 36:16-36; Genesis 4:17-5:32"))["matches"] is True


def test_an_empty_bible_text_is_filled_and_an_unrelated_one_is_only_reported():
    (fix,) = run(the_plan(), listing(bible=""))["fixes"]
    assert (fix["field"], fix["new"]) == ("bibleText", "Genesis 4:17-5:32")
    result = run(the_plan(), listing(bible="Genesis 6:1-8"))
    assert result["fixes"] == [] and codes(result) == ["listing_bible_text_mismatch"]


# ---- the speaker ----

def test_the_everyday_name_on_sermonaudio_is_the_person_on_the_team():
    assert run(the_plan(), listing(speaker="Matt Example"))["matches"] is True


def test_joe_is_not_joel_and_a_speaker_is_only_set_to_a_name_already_on_a_listing():
    team = [("Joel Sample", "Preacher", "confirmed")]
    detail = the_plan(sermon="Sermon: Genesis 4:17-5.32 | Seeds of Sin & Hope | Joel Sample", team=team)
    result = run(detail, listing(speaker="Joe Sample"))
    assert result["fixes"] == [] and codes(result) == ["listing_speaker_mismatch"]
    assert "isn't on a recent listing, so I left it" in result["report"]

    earlier = listing(title="Genesis 3:1-7 | First Sin", speaker="Joel Sample", day="2026-11-08",
                      bible="Genesis 3:1-7", sid="1108261900")
    (fix,) = run(detail, listing(speaker="Joe Sample"), earlier)["fixes"]
    assert (fix["field"], fix["old"], fix["new"]) == ("speakerName", "Joe Sample", "Joel Sample")


def test_when_the_listing_agrees_with_the_sermon_item_the_position_is_the_odd_one_out():
    team = [("Sam Roe", "Preacher", "confirmed")]          # a late swap nobody made on the team
    result = run(the_plan(team=team), listing(speaker="Matt Example"))
    assert result["fixes"] == [] and codes(result) == ["preacher_position_odd_one_out"]


# ---- which listing ----

def test_no_listing_for_sunday_says_so_with_the_newest_one_there():
    result = check_listing(date(2026, 11, 29), date(2026, 11, 29), the_plan(), None, [listing(), WEEK_BEFORE], CONV)
    assert codes(result) == ["listing_missing"] and result["listing"] is None and result["matches"] is False
    assert result["report"] == ("*SermonAudio · Sun Nov 29* (Sunday check)\n"
                                "Sunday's sermon isn't on SermonAudio yet. The newest one there is Nov 22.")


def test_a_listing_uploaded_sunday_under_another_date_is_pointed_out():
    wrong = listing(day="2026-11-21")
    result = run(the_plan(), wrong)
    assert codes(result) == ["listing_wrong_date"]
    assert "is dated Nov 21" in result["report"]


def test_a_sunday_school_recording_the_same_day_is_not_the_sermon():
    lesson = listing(title="E4:12 | The Story of Everything", event="Sunday School", bible="", sid="1122261700")
    assert codes(run(the_plan(), lesson)) == ["listing_missing"]
    result = run(the_plan(), lesson, listing())
    assert result["sermon_id"] == "1122261914" and result["matches"] is True


def test_the_sermon_filed_under_the_wrong_category_is_still_checked():
    result = run(the_plan(), listing(event="Sunday - PM"))
    assert codes(result) == ["listing_category"] and result["fixes"] == []


def test_two_listings_for_one_sunday_are_reported():
    assert codes(run(the_plan(), listing(), listing(sid="1122262000"))) == ["listing_duplicate"]


# ---- never changed, always said ----

def test_no_audio_and_no_series_are_for_a_person():
    result = run(the_plan(), listing(secs=None, series=None))
    assert codes(result) == ["listing_no_audio", "listing_no_series"] and result["fixes"] == []
    assert result["report"].splitlines()[2] == "Matt Example · no series · no audio"
    assert result["report"].splitlines()[3] == "*Needs a person:*"


def test_a_new_series_in_planning_center_with_the_listing_still_in_last_week_s_series():
    advent = the_plan(series="The Promised Messiah")
    result = run(advent, listing(), previous_series="Foundations: The Book of Genesis")
    assert codes(result) == ["listing_series_unchanged"]
    assert "starts a new series this Sunday (\"The Promised Messiah\")" in result["report"]
    # a new SermonAudio series, or the same Planning Center series as last week, is fine
    assert run(advent, listing(series="Advent 2026"), previous_series="Foundations: The Book of Genesis")["matches"]
    assert run(the_plan(series="Foundations: The Book of Genesis"), listing(),
               previous_series="Foundations: The Book of Genesis")["matches"]


def test_a_listing_with_no_plan_to_compare_against():
    result = run(None, listing())
    assert codes(result) == ["no_plan"] and result["listing"]["title"].startswith("Genesis")


def test_length_reads_like_sermonaudio():
    assert (length(3100), length(3725), length(None), length(0)) == ("51:40", "1:02:05", "", "")


# ---- settings ----

def test_sermonaudio_settings_are_optional_and_fixing_is_off_unless_turned_on():
    assert (CONV.listing_event_type, CONV.fix_listings) == ("Sunday Service", False)
    on = parse_conventions({**RAW, "sermonaudio": {"fix_listings": True, "event_type": "Sunday - AM"}})
    assert (on.listing_event_type, on.fix_listings) == ("Sunday - AM", True)
    assert run(the_plan(), listing(), conv=on)["fix_listings"] is True
    with pytest.raises(ConventionsError, match="true or false"):
        parse_conventions({**RAW, "sermonaudio": {"fix_listings": "yes"}})
    with pytest.raises(ConventionsError, match="must capture"):
        parse_conventions({**RAW, "sermonaudio": {"title_pattern": "^(.+)$"}})


# ---- the tool, end to end ----

SUN_4PM = datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc)      # 4:00 pm Mountain, Sunday
MON_9AM = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)      # 9:00 am Mountain, Monday


@pytest.fixture
def listing_tool(monkeypatch, tmp_path):
    import yaml

    def wire(plans, now, listings=None, saved_at=None):
        conventions = tmp_path / "conventions.yaml"
        conventions.write_text(yaml.safe_dump({**RAW, "preacher_aliases": {"Matt": "Matthew Example"}}))
        monkeypatch.setenv("PCO_CONVENTIONS_FILE", str(conventions))
        monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
        handler = fake_pco(plans)
        monkeypatch.setattr(pco_server, "_client", make_client(handler))
        monkeypatch.setattr(pco_server, "_now", lambda: now)
        if listings is not None:
            cache.write_json("sermonaudio_latest.json", {
                "generated_at": (saved_at or now - timedelta(minutes=1)).isoformat(), "sermons": listings})
        return handler
    return wire


PLANS = {"10": ("2026-11-22T17:00:00Z", the_plan()),
         "9": ("2026-11-15T17:00:00Z", the_plan(title="Genesis 4:1-16 | Cain's Sin & God's Grace",
                                                sermon="Sermon: Genesis 4.1-16 | Cain's Sin & God's Grace | Matt Example")),
         "11": ("2026-11-29T17:00:00Z", the_plan(title="Isaiah 9:1-7 | Rejoicing",
                                                 sermon="Sermon: Isaiah 9:1-7 | Rejoicing | Matt Example"))}


@pytest.mark.parametrize("now, label", [(SUN_4PM, "Sunday"), (MON_9AM, "Monday")])
def test_sunday_afternoon_and_monday_morning_check_the_same_sunday(listing_tool, tmp_path, now, label):
    handler = listing_tool(PLANS, now, [listing(), WEEK_BEFORE])
    result = pco_server.check_sermon_listing()
    assert result["sunday"] == "2026-11-22" and result["matches"] is True and result["saved"] is True
    assert result["report"].splitlines()[0] == f"*SermonAudio · Sun Nov 22* ({label} check)"
    assert {r.method for r in handler.seen} == {"GET"}
    saved = json.loads((tmp_path / "cache" / "sermon_check.json").read_text())
    assert saved["sermon_id"] == "1122261914" and saved["fixes"] == [] and "generated_at" in saved


def test_the_tool_saves_the_fix_list_for_the_sermonaudio_server(listing_tool, tmp_path):
    listing_tool(PLANS, MON_9AM, [listing(title="Genesis 4:17-5:32 | Seeds of Sin"), WEEK_BEFORE])
    result = pco_server.check_sermon_listing()
    saved = json.loads((tmp_path / "cache" / "sermon_check.json").read_text())
    assert saved["fixes"] == result["fixes"] and saved["fixes"][0]["field"] == "fullTitle"
    assert saved["fix_listings"] is False


def test_without_fresh_listings_the_tool_says_sermonaudio_was_not_checked(listing_tool):
    handler = listing_tool(PLANS, MON_9AM)
    result = pco_server.check_sermon_listing()
    assert result["report"] == ("*SermonAudio · Sun Nov 22* (Monday check)\n"
                                "SermonAudio couldn't be checked: SermonAudio's listings haven't been read yet "
                                "(run get_latest_sermons first).")
    assert handler.seen == []                               # Planning Center wasn't asked either

    listing_tool(PLANS, MON_9AM, [listing()], saved_at=MON_9AM - timedelta(minutes=45))
    assert "45 minutes old" in pco_server.check_sermon_listing()["report"]


def test_a_sunday_with_no_plan_still_shows_the_listing(listing_tool):
    listing_tool({"9": PLANS["9"]}, MON_9AM, [listing(), WEEK_BEFORE])
    result = pco_server.check_sermon_listing()
    assert [f["code"] for f in result["findings"]] == ["no_plan"]
    assert result["report"].splitlines()[1] == "Genesis 4:17-5:32 | Seeds of Sin & Hope"
