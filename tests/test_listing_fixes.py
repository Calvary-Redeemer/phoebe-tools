"""Job D: the one tool that changes a SermonAudio listing, and the limits around it."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastmcp.exceptions import ToolError

from servers.pco import server as pco_server
from servers.sermonaudio import client as sa_client_module
from servers.sermonaudio import fixes
from servers.sermonaudio import server as sa_server
from servers.sermonaudio.client import SermonAudioClient, SermonAudioError, edit_body
from shared import cache
from tests.test_listing import PLANS, RAW
from tests.test_pco import make_client as make_pco_client
from tests.test_readiness import fake_pco

BROADCASTER = "examplechurch"
SID = "1122261914"
MON_9AM = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)


def record(**changes):
    base = {
        "sermonID": SID,
        "fullTitle": "Genesis 4:17-5:32 | Seeds of Sin",
        "displayTitle": "Genesis 4:17-5:32 | Seeds o..",
        "subtitle": "Foundations in Genesis",
        "series": {"seriesID": 7, "title": "Foundations in Genesis"},
        "speaker": {"displayName": "Matt Example"},
        "broadcaster": {"broadcasterID": BROADCASTER},
        "preachDate": "2026-11-22",
        "publishTimestamp": 1795374476,
        "eventType": "Sunday Service",
        "bibleText": "Genesis 4:17-5:32",
        "languageCode": "en",
        "keywords": None,
        "moreInfoText": "",
        "transcript": "a very long transcript that must never be saved or sent back",
        "media": {"audio": [{"duration": 3100}], "video": []},
    }
    return {**base, **changes}


class FakeSermonAudio:
    """Holds one listing and behaves like SermonAudio's update call: a PUT replaces the text."""

    def __init__(self, start=None, status=200, drops=()):
        self.record, self.status, self.drops = start or record(), status, drops
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/v2/node/sermons":
            other = record(sermonID="1108261900", fullTitle="Genesis 3:1-7 | First Sin", preachDate="2026-11-08",
                           speaker={"displayName": "Joel Sample"}, bibleText="Genesis 3:1-7")
            return httpx.Response(200, json={"results": [self.record, other]})
        if request.method == "GET":
            return httpx.Response(200, json=self.record)
        if request.method == "PUT":
            if self.status != 200:
                return httpx.Response(self.status, json={})
            body = json.loads(request.content)
            self.record = {**self.record, "fullTitle": body["fullTitle"], "bibleText": body["bibleText"],
                           "speaker": {"displayName": body["speakerName"]},
                           "publishTimestamp": body["publishTimestamp"], "subtitle": body["subtitle"],
                           "displayTitle": body["displayTitle"] or body["fullTitle"][:27] + ".."}
            for key in self.drops:                       # a badly behaved update that loses something
                self.record[key] = None
            return httpx.Response(200, json=self.record)
        return httpx.Response(500, json={})

    @property
    def writes(self):
        return [r for r in self.requests if r.method != "GET"]


def client_for(fake):
    return SermonAudioClient("test-key", BROADCASTER, transport=httpx.MockTransport(fake), sleep=lambda s: None)


# ---- the client's one edit ----

def test_an_edit_sends_every_field_back_and_changes_only_what_was_asked():
    fake = FakeSermonAudio()
    outcome = client_for(fake).replace_listing_text(SID, {"fullTitle": "Genesis 4:17-5:32 | Seeds of Sin & Hope"})
    (put,) = fake.writes
    assert (put.method, put.url.path) == ("PUT", f"/v2/node/sermons/{SID}")
    assert put.headers["x-api-key"] == "test-key"
    body = json.loads(put.content)
    assert body == {
        "acceptCopyright": True,
        "fullTitle": "Genesis 4:17-5:32 | Seeds of Sin & Hope",
        "speakerName": "Matt Example",
        "preachDate": "2026-11-22",
        "publishTimestamp": 1795374476,          # left out, SermonAudio would unpublish the sermon
        "eventType": "Sunday Service",
        "displayTitle": None,                    # the shortened title is SermonAudio's to rebuild
        "subtitle": "Foundations in Genesis",    # the series
        "bibleText": "Genesis 4:17-5:32",
        "moreInfoText": "",
        "languageCode": "en",
        "newsInFocus": "false",
        "keywords": None,
    }
    assert "transcript" not in put.content.decode()
    assert outcome["before"]["fullTitle"] == "Genesis 4:17-5:32 | Seeds of Sin" and outcome["sent"] == body


def test_no_changes_sends_the_listing_back_exactly_as_read():
    fake = FakeSermonAudio()
    outcome = client_for(fake).replace_listing_text(SID, {})
    assert outcome["sent"] == outcome["before"] == edit_body(record())


def test_a_short_title_someone_typed_is_kept_and_keywords_become_one_string():
    body = edit_body(record(displayTitle="Seeds of Sin", keywords=["genesis", "cain"]))
    assert body["displayTitle"] == "Seeds of Sin" and body["keywords"] == "genesis cain"


@pytest.mark.parametrize("changes, message", [
    ({"eventType": "Sunday - PM"}, "may not change eventType"),
    ({"preachDate": "2026-11-21"}, "may not change preachDate"),
    ({"publishTimestamp": "0"}, "may not change publishTimestamp"),
    ({"fullTitle": ""}, "empty, too long"),
    ({"fullTitle": "a\nb"}, "more than one line"),
    ({"fullTitle": "x" * 201}, "too long"),
])
def test_the_edit_refuses_anything_but_the_three_text_fields(changes, message):
    fake = FakeSermonAudio()
    with pytest.raises(SermonAudioError, match=message):
        client_for(fake).replace_listing_text(SID, changes)
    assert fake.requests == []


def test_the_edit_refuses_another_church_s_sermon_and_an_unpublished_one():
    fake = FakeSermonAudio(record(broadcaster={"broadcasterID": "someoneelse"}))
    with pytest.raises(SermonAudioError, match="different broadcaster"):
        client_for(fake).replace_listing_text(SID, {"fullTitle": "x"})
    fake2 = FakeSermonAudio(record(publishTimestamp=None))
    with pytest.raises(SermonAudioError, match="isn't published"):
        client_for(fake2).replace_listing_text(SID, {"fullTitle": "x"})
    assert fake.writes == [] and fake2.writes == []


def test_a_key_that_cannot_write_says_so():
    with pytest.raises(SermonAudioError, match="may not be allowed to edit listings"):
        client_for(FakeSermonAudio(status=403)).replace_listing_text(SID, {})


def test_nothing_but_a_get_or_that_one_put_can_leave_the_client():
    for verb in ("post", "put", "patch", "delete"):
        assert not hasattr(SermonAudioClient, verb)
    source = Path(sa_client_module.__file__).read_text()
    assert source.count("self._http.") == 3                      # get, put, close
    assert "self._http.get(" in source and "self._http.put(path" in source
    for call in (".post(", ".patch(", ".delete(", ".request(", ".send("):
        assert call not in source
    fixes_source = Path(fixes.__file__).read_text() + Path(sa_server.__file__).read_text()
    assert "_http" not in fixes_source and "httpx" not in fixes_source


# ---- the tool ----

@pytest.fixture
def tool(monkeypatch, tmp_path):
    monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sa_server, "_now", lambda: MON_9AM)

    def wire(fake, fix_listings=True, fixes_list=None, check=None, age=timedelta(minutes=1), **extra):
        monkeypatch.setattr(sa_server, "_client", client_for(fake))
        if fixes_list is None:
            fixes_list = [{"field": "fullTitle", "old": "Genesis 4:17-5:32 | Seeds of Sin",
                           "new": "Genesis 4:17-5:32 | Seeds of Sin & Hope",
                           "parts": [{"kind": "agree", "what": "the sermon title", "was": "Seeds of Sin",
                                      "now": "Seeds of Sin & Hope", "sources": "the plan title and sermon item"}]}]
        saved = check or {
            "sunday": "2026-11-22", "run_day": "Monday", "fix_listings": fix_listings, "sermon_id": SID,
            "listing": {"title": "Genesis 4:17-5:32 | Seeds of Sin", "speaker": "Matt Example",
                        "series": "Foundations in Genesis", "has_audio": True, "audio_seconds": 3100},
            "matches": False, "findings": [], "fixes": fixes_list,
            "report": "*SermonAudio · Sun Nov 22* (Monday check)\nthe report as the check wrote it", **extra}
        cache.write_json("sermon_check.json", {"generated_at": (MON_9AM - age).isoformat(), **saved})
        cache.write_json("sermonaudio_latest.json", {
            "generated_at": (MON_9AM - age).isoformat(),
            "sermons": [{"speaker": "Matt Example"}, {"speaker": "Joel Sample"}]})
        return fake
    wire.dir = tmp_path
    return wire


def test_the_tool_takes_no_arguments_and_is_listed():
    async def go():
        return {t.name: t for t in await sa_server.mcp.list_tools()}
    tools = asyncio.run(go())
    assert sorted(tools) == ["apply_listing_fixes", "get_latest_sermons", "get_sermon"]
    assert tools["apply_listing_fixes"].parameters.get("properties", {}) == {}
    assert "exactly as it is" in tools["apply_listing_fixes"].description


def test_with_fixing_on_the_listing_is_corrected_and_the_post_says_what_it_was(tool):
    fake = tool(FakeSermonAudio())
    result = sa_server.apply_listing_fixes()
    assert len(fake.writes) == 1 and fake.record["fullTitle"] == "Genesis 4:17-5:32 | Seeds of Sin & Hope"
    assert result["report"] == (
        "*SermonAudio · Sun Nov 22* (Monday check)\n"
        "Genesis 4:17-5:32 | Seeds of Sin & Hope\n"
        "Matt Example · Foundations in Genesis · 51:40\n"
        "*Fixed:* the sermon title said \"Seeds of Sin\"; the plan title and sermon item both say "
        "\"Seeds of Sin & Hope\"."
    )
    ledger = json.loads((tool.dir / "sermonaudio-fixes" / "ledger.json").read_text())
    assert [(e["sermon_id"], e["field"], e["from"], e["to"]) for e in ledger] == [
        (SID, "fullTitle", "Genesis 4:17-5:32 | Seeds of Sin", "Genesis 4:17-5:32 | Seeds of Sin & Hope")]
    (snapshot,) = [p for p in (tool.dir / "sermonaudio-fixes").iterdir() if p.name != "ledger.json"]
    saved = json.loads(snapshot.read_text())
    assert saved["before"]["fullTitle"] == "Genesis 4:17-5:32 | Seeds of Sin" and saved["moved"] == []
    assert saved["after"]["fullTitle"] == "Genesis 4:17-5:32 | Seeds of Sin & Hope"
    assert "transcript" not in snapshot.read_text()


def test_with_fixing_off_nothing_is_sent_and_the_check_s_own_post_is_returned(tool):
    fake = tool(FakeSermonAudio(), fix_listings=False)
    result = sa_server.apply_listing_fixes()
    assert fake.requests == [] and result["fixed"] == []
    assert result["report"].endswith("the report as the check wrote it")
    assert not (tool.dir / "sermonaudio-fixes").exists()


def test_fixing_must_be_exactly_true_in_the_saved_check(tool):
    fake = tool(FakeSermonAudio(), fix_listings="yes")
    sa_server.apply_listing_fixes()
    assert fake.requests == []


def test_with_nothing_to_fix_nothing_is_read_or_sent(tool):
    fake = tool(FakeSermonAudio(), fixes_list=[])
    assert sa_server.apply_listing_fixes()["report"].endswith("the report as the check wrote it")
    assert fake.requests == []


def test_a_field_is_changed_once_and_a_person_s_correction_is_then_left_alone(tool):
    fake = tool(FakeSermonAudio())
    sa_server.apply_listing_fixes()
    # Sunday evening a person decides the listing was right after all and puts it back.
    fake.record["fullTitle"] = "Genesis 4:17-5:32 | Seeds of Sin"
    fake.requests.clear()
    tool(fake)                                              # Monday's check finds the same difference again
    result = sa_server.apply_listing_fixes()
    assert fake.writes == [] and fake.record["fullTitle"] == "Genesis 4:17-5:32 | Seeds of Sin"
    assert result["report"].splitlines()[1] == "Genesis 4:17-5:32 | Seeds of Sin"
    assert result["report"].splitlines()[-1] == (
        "*Needs a person:* the title differs from Planning Center again after I changed it once "
        "(to \"Genesis 4:17-5:32 | Seeds of Sin & Hope\"), so I left it as it is.")


def test_a_listing_that_changed_after_the_check_is_not_touched(tool):
    fake = tool(FakeSermonAudio(record(fullTitle="Genesis 4:17-5:32 | Seeds of Sin and Hope")))
    result = sa_server.apply_listing_fixes()
    assert fake.writes == []
    assert "changed on SermonAudio after the check ran, so I didn't touch it" in result["report"]


def test_a_speaker_is_only_set_to_a_name_already_on_a_listing(tool):
    def speaker_fix(name):
        return [{"field": "speakerName", "old": "Matt Example", "new": name,
                 "parts": [{"kind": "agree", "what": "the speaker", "was": "Matt Example", "now": name,
                            "sources": "the Preacher position and sermon item"}]}]
    fake = tool(FakeSermonAudio(), fixes_list=speaker_fix("Joe1 Sample"))
    assert "isn't on a recent listing" in sa_server.apply_listing_fixes()["report"] and fake.writes == []
    fake = tool(FakeSermonAudio(), fixes_list=speaker_fix("Joel Sample"))
    sa_server.apply_listing_fixes()
    assert fake.record["speaker"]["displayName"] == "Joel Sample"


def test_a_fix_list_asking_for_another_field_or_another_sunday_is_refused(tool):
    fake = tool(FakeSermonAudio(), fixes_list=[{"field": "eventType", "old": "Sunday Service", "new": "Other",
                                                "parts": []}])
    assert "which I don't make" in sa_server.apply_listing_fixes()["report"] and fake.writes == []
    fake = tool(FakeSermonAudio(record(preachDate="2026-11-15")))
    with pytest.raises(ToolError, match="no longer dated the Sunday that was checked"):
        sa_server.apply_listing_fixes()
    assert fake.writes == []


def test_if_the_update_moves_anything_else_the_post_says_so(tool):
    fake = tool(FakeSermonAudio(drops=("publishTimestamp",)))
    report = sa_server.apply_listing_fixes()["report"]
    assert "*Fixed:*" in report and "*Needs a person:*" in report
    assert "something else on the listing is different: publishTimestamp is \"None\"" in report
    assert "The listing as it was is saved in sermonaudio-fixes/" in report


def test_a_key_that_cannot_write_surfaces_as_an_error_and_leaves_no_ledger_entry(tool):
    fake = tool(FakeSermonAudio(status=403))
    with pytest.raises(ToolError, match="may not be allowed to edit listings"):
        sa_server.apply_listing_fixes()
    assert not (tool.dir / "sermonaudio-fixes" / "ledger.json").exists()


def test_without_a_fresh_check_nothing_happens(tool, monkeypatch):
    fake = tool(FakeSermonAudio(), age=timedelta(minutes=45))
    result = sa_server.apply_listing_fixes()
    assert fake.requests == [] and "45 minutes old (run check_sermon_listing again" in result["report"]
    (tool.dir / "cache" / "sermon_check.json").unlink()
    assert "haven't been read yet (run check_sermon_listing first)" in sa_server.apply_listing_fixes()["report"]


def test_an_unreadable_ledger_stops_everything(tool):
    fake = tool(FakeSermonAudio())
    (tool.dir / "sermonaudio-fixes").mkdir()
    (tool.dir / "sermonaudio-fixes" / "ledger.json").write_text("{broken")
    with pytest.raises(ToolError, match="Nothing was changed"):
        sa_server.apply_listing_fixes()
    assert fake.requests == []


# ---- all three steps together ----

def test_the_three_steps_of_the_job_end_to_end(monkeypatch, tmp_path):
    import yaml
    conventions = tmp_path / "conventions.yaml"
    conventions.write_text(yaml.safe_dump({**RAW, "preacher_aliases": {"Matt": "Matthew Example"},
                                           "sermonaudio": {"fix_listings": True}}))
    monkeypatch.setenv("PCO_CONVENTIONS_FILE", str(conventions))
    monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(pco_server, "_client", make_pco_client(fake_pco(PLANS)))
    monkeypatch.setattr(pco_server, "_now", lambda: MON_9AM)
    monkeypatch.setattr(sa_server, "_now", lambda: MON_9AM)
    fake = FakeSermonAudio()
    monkeypatch.setattr(sa_server, "_client", client_for(fake))
    real_write = cache.write_json                       # stamp the saved listings with the test's clock
    monkeypatch.setattr(sa_server, "write_json",
                        lambda name, data: real_write(name, {**data, "generated_at": MON_9AM.isoformat()}))

    sa_server.get_latest_sermons(count=8)
    check = pco_server.check_sermon_listing()
    assert check["report"].splitlines()[-1].startswith("*Would fix:* the sermon title says \"Seeds of Sin\"")
    final = sa_server.apply_listing_fixes()
    assert final["report"].splitlines()[1] == "Genesis 4:17-5:32 | Seeds of Sin & Hope"
    assert final["report"].splitlines()[-1].startswith("*Fixed:* the sermon title said \"Seeds of Sin\"")
    assert len(fake.writes) == 1

    # Monday's second look: the listing now matches, and nothing more is sent.
    sa_server.get_latest_sermons(count=8)
    assert pco_server.check_sermon_listing()["matches"] is True
    assert sa_server.apply_listing_fixes()["report"].splitlines()[-1] == "✅ Matches Sunday's plan."
    assert len(fake.writes) == 1
