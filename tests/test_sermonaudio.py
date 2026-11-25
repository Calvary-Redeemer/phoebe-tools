import asyncio
import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from servers.sermonaudio import server as sa_server
from servers.sermonaudio.client import SermonAudioClient, SermonAudioError

BROADCASTER = "examplechurch"


def make_client(handler, sleeps=None):
    return SermonAudioClient("test-key", BROADCASTER, transport=httpx.MockTransport(handler),
                             sleep=(sleeps.append if sleeps is not None else lambda s: None))


def sermon(sermon_id="1115261830", broadcaster=BROADCASTER, **extra):
    return {
        "sermonID": sermon_id,
        "fullTitle": "Cain's Sin & God's Grace",
        "displayTitle": "Cain's Sin & God's Grace",
        "subtitle": "Genesis",
        "series": {"seriesID": 1, "title": "Genesis", "broadcasterID": broadcaster},
        "speaker": {"displayName": "Pat Doe", "bio": "private-ish", "portraitURL": "https://example.org/p.jpg"},
        "broadcaster": {"broadcasterID": broadcaster, "email": "office@example.org", "phone": "555-0100"},
        "preachDate": "2026-11-15",
        "bibleText": "Genesis 4:1-16",
        "eventType": "Sunday Service",
        "publishTimestamp": 1794700800,
        "updateDate": 1794704400,
        "languageCode": "en",
        "moreInfoText": "x" * 1500,
        "keywords": "genesis cain",
        "downloadCount": 42,
        "media": {"audio": [{"mediaType": "mp3", "duration": 2412, "downloadURL": "https://example.org/a.mp3"}],
                  "video": [], "text": []},
        **extra,
    }


# ---- client ----

def test_client_sends_api_key_and_only_gets():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"results": [], "totalCount": 0, "next": None})

    make_client(handler).latest_sermons(3)
    assert seen[0].method == "GET"
    assert seen[0].headers["x-api-key"] == "test-key"
    for verb in ("post", "put", "patch", "delete"):
        assert not hasattr(SermonAudioClient, verb)


def test_latest_sermons_is_pinned_to_our_broadcaster_newest_first():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"results": [sermon("1"), sermon("2"), sermon("3")]})

    rows = make_client(handler).latest_sermons(2)
    assert seen[0].path == "/v2/node/sermons"
    assert dict(seen[0].params) == {"broadcasterID": BROADCASTER, "sortBy": "newest", "page": "1", "pageSize": "2"}
    assert [r["sermonID"] for r in rows] == ["1", "2"]


def test_sermon_from_another_broadcaster_is_refused():
    c = make_client(lambda r: httpx.Response(200, json=sermon(broadcaster="someoneelse")))
    with pytest.raises(SermonAudioError, match="different broadcaster"):
        c.sermon("1115261830")


def test_sermon_id_cannot_smuggle_a_path():
    calls = []
    c = make_client(lambda r: calls.append(r) or httpx.Response(200, json=sermon()))
    for bad in ("../broadcasters/x", "12/34", "1?allowUnpublished=true", ""):
        with pytest.raises(SermonAudioError, match="sermon ID"):
            c.sermon(bad)
    assert calls == []


def test_busy_responses_are_retried_then_succeed():
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"results": []})

    make_client(handler, sleeps).latest_sermons(1)
    assert sleeps == [3.0, 2.0] and len(calls) == 3


def test_rejected_key_message_has_no_key():
    c = make_client(lambda r: httpx.Response(401))
    with pytest.raises(SermonAudioError) as err:
        c.latest_sermons(1)
    assert "test-key" not in str(err.value) and "sermonaudio.env" in str(err.value)


# ---- server ----

def test_server_exposes_only_read_tools():
    from fastmcp import Client

    async def go():
        async with Client(sa_server.mcp) as c:
            return sorted(t.name for t in await c.list_tools())

    assert asyncio.run(go()) == ["get_latest_sermons", "get_sermon"]


def test_get_latest_sermons_shapes_output(monkeypatch):
    monkeypatch.setattr(sa_server, "_client",
                        make_client(lambda r: httpx.Response(200, json={"results": [sermon()]})))
    assert sa_server.get_latest_sermons(count=500) == [{
        "sermon_id": "1115261830",
        "title": "Cain's Sin & God's Grace",
        "speaker": "Pat Doe",
        "preach_date": "2026-11-15",
        "bible_text": "Genesis 4:1-16",
        "series": "Genesis",
        "series_id": 1,
        "event_type": "Sunday Service",
        "published_at": "2026-11-15T00:00:00+00:00",
        "has_audio": True,
        "has_video": False,
        "audio_seconds": 2412,
        "url": "https://www.sermonaudio.com/sermons/1115261830",
    }]


def test_it_always_reads_25_and_returns_the_count_asked_for(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.params["pageSize"])
        return httpx.Response(200, json={"results": [sermon(sermon_id=str(n)) for n in range(25)]})

    monkeypatch.setattr(sa_server, "_client", make_client(handler))
    assert len(sa_server.get_latest_sermons(count=500)) == 25
    assert len(sa_server.get_latest_sermons(count=-3)) == 1
    assert len(sa_server.get_latest_sermons()) == 5
    assert seen == ["25", "25", "25"]


def test_get_latest_sermons_saves_what_it_read_for_the_pco_server(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from shared import cache
    monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sa_server, "_client", make_client(
        lambda r: httpx.Response(200, json={"results": [sermon(sermon_id=str(n)) for n in range(25)]})))
    returned = sa_server.get_latest_sermons(count=2)
    saved = json.loads((tmp_path / "cache" / "sermonaudio_latest.json").read_text())
    assert len(returned) == 2 and len(saved["sermons"]) == 25
    assert saved["sermons"][0] == returned[0]
    assert set(saved) == {"generated_at", "sermons"}
    # no contact details, descriptions, or download links reach the file
    text = json.dumps(saved)
    assert "office@example.org" not in text and "a.mp3" not in text and "xxxx" not in text

    data, why = cache.read_json("sermonaudio_latest.json", what="SermonAudio's listings",
                                rerun="get_latest_sermons")
    assert why is None and len(data["sermons"]) == 25
    later = datetime.now(timezone.utc) + timedelta(minutes=45)
    data, why = cache.read_json("sermonaudio_latest.json", what="SermonAudio's listings",
                                rerun="get_latest_sermons", now=later)
    assert data is None and "minutes old" in why and "get_latest_sermons" in why


def test_with_nowhere_to_save_the_listings_still_come_back(monkeypatch):
    from shared import cache
    monkeypatch.delenv("PHOEBE_STATE_DIR", raising=False)
    monkeypatch.setattr(sa_server, "_client",
                        make_client(lambda r: httpx.Response(200, json={"results": [sermon()]})))
    assert len(sa_server.get_latest_sermons()) == 1
    data, why = cache.read_json("sermonaudio_latest.json", what="SermonAudio's listings",
                                rerun="get_latest_sermons")
    assert data is None and "PHOEBE_STATE_DIR" in why
    with pytest.raises(cache.CacheError, match="not a cache file name"):
        cache.write_json("../secrets/x.json", {})


def test_a_missing_or_broken_saved_file_says_what_to_run(monkeypatch, tmp_path):
    from shared import cache
    monkeypatch.setenv("PHOEBE_STATE_DIR", str(tmp_path))
    kw = dict(what="SermonAudio's listings", rerun="get_latest_sermons")
    assert "haven't been read yet (run get_latest_sermons first)" in cache.read_json("sermonaudio_latest.json", **kw)[1]
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "sermonaudio_latest.json").write_text("{not json")
    assert "couldn't be read" in cache.read_json("sermonaudio_latest.json", **kw)[1]


def test_get_sermon_adds_detail_and_drops_contact_fields(monkeypatch):
    monkeypatch.setattr(sa_server, "_client", make_client(lambda r: httpx.Response(200, json=sermon())))
    result = sa_server.get_sermon("1115261830")
    assert result["updated_at"] == "2026-11-15T01:00:00+00:00"
    assert result["keywords"] == "genesis cain"
    assert len(result["description"]) == 1001 and result["description"].endswith("…")
    dumped = json.dumps(result)
    for leaked in ("office@example.org", "555-0100", "portraitURL", "downloadURL", "bio"):
        assert leaked not in dumped


def test_tool_errors_are_plain_messages(monkeypatch):
    monkeypatch.setattr(sa_server, "_client", make_client(lambda r: httpx.Response(404)))
    with pytest.raises(ToolError, match="Not found on SermonAudio"):
        sa_server.get_sermon("999")


def test_missing_credentials_name_the_keys_not_values(monkeypatch, tmp_path):
    env = tmp_path / "sermonaudio.env"
    env.write_text("SA_API_KEY=abc123\n")
    monkeypatch.setenv("SERMONAUDIO_ENV_FILE", str(env))
    monkeypatch.delenv("SA_BROADCASTER_ID", raising=False)
    monkeypatch.setattr(sa_server, "_client", None)
    with pytest.raises(ToolError) as err:
        sa_server.get_latest_sermons()
    assert "SA_BROADCASTER_ID" in str(err.value) and "abc123" not in str(err.value)
