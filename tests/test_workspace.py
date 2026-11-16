import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from fastmcp.exceptions import ToolError

from servers.workspace import check as ws_check
from servers.workspace import server as ws_server
from servers.workspace.client import (ALLOWED_SCOPES, TOKEN_URL, GoogleClient, WorkspaceError,
                                      load_token_file)
from servers.workspace.config import ConfigError, load_config, parse_config

SHEET_ID = "sheet-id-for-tests"
CAL_ID = "calendar-for-tests@group.calendar.google.com"
CONFIG = parse_config({
    "sheets": {"preaching_schedule": {"id": SHEET_ID, "default_tab": "Q4", "columns": "A:H"}},
    "calendars": {"church": CAL_ID, "phoebe": {"id": "primary"}},
    "default_calendar": "church",
})
GOOD_SCOPE = " ".join(sorted(ALLOWED_SCOPES))


class FakeGoogle:
    """Stands in for Google's token endpoint and the Sheets/Calendar APIs."""

    def __init__(self, pages=None, scope=GOOD_SCOPE, token_status=200):
        self.pages = pages or {}
        self.scope = scope
        self.token_status = token_status
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": f"access-{self.token_calls}",
                                             "expires_in": 3600, "scope": self.scope})
        page = self.pages[request.url.path]
        if callable(page):
            page = page(request)
        return page if isinstance(page, httpx.Response) else httpx.Response(200, json=page)

    @property
    def api_requests(self):
        return [r for r in self.requests if str(r.url) != TOKEN_URL]


def make_client(fake, sleeps=None, clock=lambda: 0.0):
    return GoogleClient("refresh-secret", "client-id", "client-secret",
                        transport=httpx.MockTransport(fake),
                        sleep=(sleeps.append if sleeps is not None else lambda s: None), clock=clock)


SHEET_PATH = f"/v4/spreadsheets/{SHEET_ID}"
SHEET_META = {"properties": {"title": "Preaching Schedule"},
              "sheets": [{"properties": {"title": "Q4", "index": 1}},
                         {"properties": {"title": "Q3", "index": 0}}]}
EVENTS_PATH = "/calendar/v3/calendars/calendar-for-tests@group.calendar.google.com/events"


@pytest.fixture
def wired(monkeypatch):
    """Point the server at a fake Google and the test config; returns a function taking pages."""
    def wire(pages, **kwargs):
        fake = FakeGoogle(pages, **kwargs)
        monkeypatch.setattr(ws_server, "_client", make_client(fake))
        monkeypatch.setattr(ws_server, "_config", CONFIG)
        return fake
    return wire


# ---- token file ----

def test_token_file_yields_only_refresh_fields(tmp_path):
    f = tmp_path / "workspace.token"
    f.write_text(json.dumps({"token": "stale-access", "refresh_token": "r", "client_id": "c",
                             "client_secret": "s", "scopes": [], "expiry": "2026-01-01T00:00:00Z"}))
    assert load_token_file(f) == {"refresh_token": "r", "client_id": "c", "client_secret": "s"}


def test_token_file_problems_name_fields_not_values(tmp_path, monkeypatch):
    f = tmp_path / "workspace.token"
    f.write_text(json.dumps({"client_id": "c", "client_secret": "hunter2"}))
    with pytest.raises(WorkspaceError) as err:
        load_token_file(f)
    assert "refresh_token" in str(err.value) and "hunter2" not in str(err.value)
    monkeypatch.delenv("WORKSPACE_TOKEN_FILE", raising=False)
    with pytest.raises(WorkspaceError, match="WORKSPACE_TOKEN_FILE"):
        load_token_file()


# ---- client ----

def test_refreshes_once_then_reuses_access_token_and_only_gets():
    fake = FakeGoogle({SHEET_PATH: SHEET_META})
    c = make_client(fake)
    c.sheet_info(SHEET_ID, what="x")
    c.sheet_info(SHEET_ID, what="x")
    assert fake.token_calls == 1
    form = parse_qs(fake.requests[0].content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["refresh-secret"]
    assert {r.method for r in fake.api_requests} == {"GET"}
    assert all(r.headers["authorization"] == "Bearer access-1" for r in fake.api_requests)
    for verb in ("post", "put", "patch", "delete"):
        assert not hasattr(GoogleClient, verb)


def test_access_token_is_refreshed_after_it_expires():
    now = [0.0]
    fake = FakeGoogle({SHEET_PATH: SHEET_META})
    c = make_client(fake, clock=lambda: now[0])
    c.sheet_info(SHEET_ID, what="x")
    now[0] = 3600.0
    c.sheet_info(SHEET_ID, what="x")
    assert fake.token_calls == 2


def test_token_with_extra_scopes_is_refused_before_any_api_call():
    fake = FakeGoogle({SHEET_PATH: SHEET_META},
                      scope=GOOD_SCOPE + " https://www.googleapis.com/auth/drive")
    with pytest.raises(WorkspaceError, match="should never hold") as err:
        make_client(fake).sheet_info(SHEET_ID, what="x")
    assert "drive" in str(err.value) and fake.api_requests == []


def test_revoked_token_says_to_remint_without_leaking():
    fake = FakeGoogle(token_status=400)
    with pytest.raises(WorkspaceError, match="mint_token.py workspace") as err:
        make_client(fake).sheet_info(SHEET_ID, what="x")
    assert "refresh-secret" not in str(err.value) and "client-secret" not in str(err.value)


def test_get_refuses_hosts_other_than_sheets_and_calendar():
    fake = FakeGoogle()
    for url in ("https://www.googleapis.com/drive/v3/files",
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                "https://example.org/v4/spreadsheets/x",
                "https://sheets.googleapis.com.example.org/v4/spreadsheets/x"):
        with pytest.raises(WorkspaceError, match="only talks to"):
            make_client(fake).get(url)
    assert fake.requests == []


def test_a_401_triggers_one_fresh_token_then_succeeds():
    calls = []

    def page(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(401) if len(calls) == 1 else SHEET_META

    fake = FakeGoogle({SHEET_PATH: page})
    make_client(fake).sheet_info(SHEET_ID, what="x")
    assert calls == ["Bearer access-1", "Bearer access-2"]


def test_busy_responses_wait_and_retry():
    calls, sleeps = [], []

    def page(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "4"}) if len(calls) == 1 else SHEET_META

    make_client(FakeGoogle({SHEET_PATH: page}), sleeps).sheet_info(SHEET_ID, what="x")
    assert sleeps == [4.0] and len(calls) == 2


def test_errors_name_the_thing_not_its_id():
    for status, phrase in ((403, "doesn't have access to the 'preaching_schedule' sheet"),
                           (404, "can't find the 'preaching_schedule' sheet")):
        fake = FakeGoogle({SHEET_PATH: httpx.Response(status)})
        with pytest.raises(WorkspaceError, match=phrase) as err:
            make_client(fake).sheet_info(SHEET_ID, what="the 'preaching_schedule' sheet")
        assert SHEET_ID not in str(err.value)


# ---- config ----

def test_config_accepts_plain_ids_and_mappings_and_picks_defaults():
    assert CONFIG.sheet(None)[0] == "preaching_schedule"
    assert CONFIG.calendar(None) == ("church", CAL_ID)
    assert CONFIG.calendar("phoebe") == ("phoebe", "primary")


def test_unknown_name_lists_names_never_ids():
    with pytest.raises(ConfigError) as err:
        CONFIG.calendar("youth")
    assert "church, phoebe" in str(err.value) and CAL_ID not in str(err.value)


@pytest.mark.parametrize("raw, phrase", [
    ({"sheets": {"s": {"id": "<spreadsheet ID>"}}}, "no ID filled in"),
    ({"sheets": {"s": {"id": "x", "columns": "A1:H9"}}}, "columns should look like"),
    ({"calendars": {"church": "x"}, "default_calendar": "other"}, "not one of the calendars"),
    (["not", "a", "mapping"], "should be a mapping"),
])
def test_config_mistakes_are_explained(raw, phrase):
    with pytest.raises(ConfigError, match=phrase):
        parse_config(raw)


def test_empty_or_missing_config_is_explained(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="No calendars are listed"):
        parse_config({}).calendar(None)
    monkeypatch.delenv("WORKSPACE_CONFIG_FILE", raising=False)
    with pytest.raises(ConfigError, match="WORKSPACE_CONFIG_FILE"):
        load_config()
    with pytest.raises(ConfigError, match="Could not read"):
        load_config(tmp_path / "missing.yaml")


# ---- server ----

def test_server_exposes_only_read_tools():
    from fastmcp import Client

    async def go():
        async with Client(ws_server.mcp) as c:
            return {t.name: t.input_schema["properties"] for t in await c.list_tools()}

    tools = asyncio.run(go())
    assert sorted(tools) == ["list_calendar", "read_sheet"]
    # Phoebe passes configured names, never raw spreadsheet or calendar IDs.
    assert not any("id" in arg.lower() for props in tools.values() for arg in props)


def test_read_sheet_reads_default_tab_and_configured_columns(wired):
    long_cell = "application question " * 40
    fake = wired({
        SHEET_PATH: SHEET_META,
        f"{SHEET_PATH}/values/'Q4'!A:H": {"values": [
            ["", "Date", "Series | Text | Title", "Preacher"],
            [],
            ["", "11/22/2026", 'Genesis 4.17-5.32: "Seeds Of Sin & Hope"', "Pat", long_cell],
        ]},
    })
    result = ws_server.read_sheet()
    assert result["sheet"] == "preaching_schedule" and result["title"] == "Preaching Schedule"
    assert result["tabs"] == ["Q3", "Q4"] and result["tab"] == "Q4" and result["columns"] == "A:H"
    assert result["rows"][1] == [] and result["rows"][2][3] == "Pat"
    assert result["rows"][2][4].endswith("more characters]") and len(result["rows"][2][4]) < 340
    assert result["truncated"] is False
    assert fake.api_requests[1].url.params["valueRenderOption"] == "FORMATTED_VALUE"


def test_read_sheet_other_tab_and_row_limit(wired):
    wired({SHEET_PATH: SHEET_META,
           f"{SHEET_PATH}/values/'Q3'!A:H": {"values": [[str(n)] for n in range(10)]}})
    result = ws_server.read_sheet(tab="Q3", max_rows=4)
    assert result["rows"] == [["0"], ["1"], ["2"], ["3"]] and result["truncated"] is True


def test_read_sheet_rejects_unknown_tab_and_unknown_sheet(wired):
    fake = wired({SHEET_PATH: SHEET_META})
    with pytest.raises(ToolError, match="no tab named 'Q4'!A1:Z'. Tabs: Q3, Q4"):
        ws_server.read_sheet(tab="Q4'!A1:Z")
    with pytest.raises(ToolError, match="no sheet named 'budget'"):
        ws_server.read_sheet(sheet="budget")
    assert len(fake.api_requests) == 1   # only the tab listing; no values were requested


def test_tab_names_with_quotes_are_escaped():
    fake = FakeGoogle({f"{SHEET_PATH}/values/'Men''s Study'": {"values": [["ok"]]}})
    assert make_client(fake).sheet_values(SHEET_ID, "Men's Study", None, what="x") == [["ok"]]


def test_list_calendar_shapes_events_and_drops_guests(wired, monkeypatch):
    monkeypatch.setattr(ws_server, "_now", lambda: datetime(2026, 11, 16, 21, 0, tzinfo=timezone.utc))
    fake = wired({EVENTS_PATH: {
        "summary": "Church Calendar", "timeZone": "America/Denver",
        "items": [
            {"status": "confirmed", "summary": "Men's Bible Study", "location": "Fellowship Hall",
             "description": "Bring a friend. " * 60, "recurringEventId": "abc",
             "start": {"dateTime": "2026-11-17T06:30:00-07:00"}, "end": {"dateTime": "2026-11-17T07:30:00-07:00"},
             "attendees": [{"email": "member@example.org"}], "creator": {"email": "staff@example.org"}},
            {"status": "confirmed", "summary": "Thanksgiving", "start": {"date": "2026-11-26"},
             "end": {"date": "2026-11-27"}},
            {"status": "confirmed", "summary": "Elders", "visibility": "private", "description": "agenda",
             "start": {"dateTime": "2026-11-18T19:00:00-07:00"}, "end": {"dateTime": "2026-11-18T20:30:00-07:00"}},
            {"status": "cancelled", "summary": "Gone"},
        ],
    }})
    result = ws_server.list_calendar(days_ahead=30)
    assert result["calendar"] == "church" and result["title"] == "Church Calendar"
    assert result["from"] == "2026-11-16T21:00:00+00:00" and result["to"] == "2026-12-16T21:00:00+00:00"
    assert [e["title"] for e in result["events"]] == ["Men's Bible Study", "Thanksgiving", "Elders"]
    study, holiday, elders = result["events"]
    assert study["repeats"] and study["location"] == "Fellowship Hall" and not study["all_day"]
    assert study["description"].endswith("more characters]")
    assert holiday == {"title": "Thanksgiving", "start": "2026-11-26", "end": "2026-11-27",
                       "all_day": True, "repeats": False}
    assert "description" not in elders
    assert "example.org" not in json.dumps(result)

    params = fake.api_requests[0].url.params
    assert params["singleEvents"] == "true" and params["orderBy"] == "startTime"
    assert params["timeMin"] == "2026-11-16T21:00:00+00:00" and "attendees" not in params["fields"]


def test_list_calendar_caps_the_window_and_follows_pages(wired, monkeypatch):
    monkeypatch.setattr(ws_server, "_now", lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    def page(request):
        if "pageToken" in request.url.params:
            return {"items": [{"summary": "second", "start": {"date": "2026-01-03"}, "end": {"date": "2026-01-04"}}]}
        return {"summary": "Church", "nextPageToken": "p2",
                "items": [{"summary": "first", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"}}]}

    wired({EVENTS_PATH: page})
    result = ws_server.list_calendar(days_ahead=5000)
    assert result["to"] == "2026-04-01T00:00:00+00:00"          # 90 days
    assert [e["title"] for e in result["events"]] == ["first", "second"]


def test_tools_report_setup_problems_as_plain_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_server, "_client", None)
    monkeypatch.setattr(ws_server, "_config", CONFIG)
    monkeypatch.delenv("WORKSPACE_TOKEN_FILE", raising=False)
    with pytest.raises(ToolError, match="WORKSPACE_TOKEN_FILE"):
        ws_server.read_sheet()

    monkeypatch.setattr(ws_server, "_config", None)
    monkeypatch.setenv("WORKSPACE_CONFIG_FILE", str(tmp_path / "missing.yaml"))
    with pytest.raises(ToolError, match="Could not read workspace.yaml"):
        ws_server.list_calendar()


# ---- check script ----

def test_check_script_reports_pass_fail_and_lists_calendar_ids():
    fake = FakeGoogle({
        SHEET_PATH: SHEET_META,
        EVENTS_PATH: {"summary": "Church Calendar", "items": [{"summary": "secret title"}]},
        "/calendar/v3/calendars/primary/events": httpx.Response(404),
        "/calendar/v3/users/me/calendarList": {"items": [
            {"id": CAL_ID, "summary": "Church Calendar", "accessRole": "reader"}]},
    })
    lines: list[str] = []
    assert ws_check.run(make_client(fake), CONFIG, out=lines.append) is False   # 'phoebe' calendar 404s
    text = "\n".join(lines)
    assert "PASS  sheet preaching_schedule: 'Preaching Schedule', tabs: Q3, Q4" in text
    assert "PASS  calendar church: 'Church Calendar', 1 events" in text
    assert "FAIL  calendar phoebe" in text
    assert f"id: {CAL_ID}" in text and "secret title" not in text and "access-1" not in text
