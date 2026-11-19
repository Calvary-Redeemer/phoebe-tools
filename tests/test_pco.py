import asyncio
import json

import httpx
import pytest

from servers.pco import server as pco_server
from servers.pco.client import PCOClient, PCOError
from shared.secrets import MissingSecret, load_env_file, require


def make_client(handler, sleeps=None):
    return PCOClient("app", "secret", transport=httpx.MockTransport(handler),
                     sleep=(sleeps.append if sleeps is not None else lambda s: None))


# ---- secrets ----

def test_load_env_file_strips_quotes_and_comments(tmp_path):
    f = tmp_path / "pco.env"
    f.write_text('# comment\nPCO_APP_ID="abc"\n\nPCO_SECRET=xyz\n')
    assert load_env_file(f) == {"PCO_APP_ID": "abc", "PCO_SECRET": "xyz"}


def test_require_names_missing_keys_not_values(tmp_path, monkeypatch):
    f = tmp_path / "pco.env"
    f.write_text("PCO_APP_ID=abc\n")
    monkeypatch.setenv("PCO_ENV_FILE", str(f))
    monkeypatch.delenv("PCO_SECRET", raising=False)
    with pytest.raises(MissingSecret) as err:
        require(["PCO_APP_ID", "PCO_SECRET"], "PCO_ENV_FILE")
    assert "PCO_SECRET" in str(err.value) and "abc" not in str(err.value)


# ---- client ----

def test_client_sends_basic_auth_and_only_gets():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    make_client(handler).get("/services/v2/service_types")
    assert seen[0].method == "GET"
    assert seen[0].headers["authorization"].startswith("Basic ")
    assert not hasattr(PCOClient, "post") and not hasattr(PCOClient, "patch")


def test_get_all_follows_next_links():
    def handler(request):
        if "offset" in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "2"}], "links": {}})
        return httpx.Response(200, json={
            "data": [{"id": "1"}],
            "links": {"next": "https://api.planningcenteronline.com/services/v2/service_types?offset=1"},
        })

    assert [r["id"] for r in make_client(handler).get_all("/services/v2/service_types")] == ["1", "2"]


def test_429_waits_for_retry_after_then_succeeds():
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"data": []})

    make_client(handler, sleeps).get("/x")
    assert sleeps == [7.0] and len(calls) == 2


def test_401_message_has_no_secret():
    c = make_client(lambda r: httpx.Response(401))
    with pytest.raises(PCOError) as err:
        c.get("/x")
    assert "secret" not in str(err.value).lower().replace("pco.env", "")


# ---- server ----

def list_tool_names():
    from fastmcp import Client

    async def go():
        async with Client(pco_server.mcp) as c:
            return sorted(t.name for t in await c.list_tools())

    return asyncio.run(go())


def test_server_exposes_only_read_tools():
    names = list_tool_names()
    assert names == ["check_sunday_readiness", "get_plan", "list_service_types", "list_upcoming_plans"]
    assert not any(n.startswith(("create", "update", "delete", "apply", "propose")) for n in names)


def test_get_plan_shapes_output_and_drops_personal_fields(monkeypatch):
    base = "/services/v2/service_types/1/plans/9"
    pages = {
        base: {"data": {"id": "9", "attributes": {"dates": "November 16, 2026", "title": "Genesis 3"}}},
        f"{base}/items": {"data": [
            {"id": "b", "attributes": {"sequence": 2, "title": "Sermon", "item_type": "item"}},
            {"id": "a", "attributes": {"sequence": 1, "title": "\"Be Thou My Vision\"", "item_type": "song"}},
        ]},
        f"{base}/team_members": {"data": [
            {"id": "t", "attributes": {"name": "Pat Doe", "team_position_name": "Service Leader",
                                       "status": "D", "decline_reason": "private", "photo_thumbnail": "x"}},
        ]},
        f"{base}/needed_positions": {"data": [
            {"id": "n", "attributes": {"team_position_name": "Scripture Reader", "quantity": 1}},
        ]},
        f"{base}/notes": {"data": []},
    }

    def handler(request):
        return httpx.Response(200, json=pages[request.url.path])

    monkeypatch.setattr(pco_server, "_client", make_client(handler))
    result = pco_server.get_plan("1", "9")
    assert [i["title"] for i in result["items"]] == ["\"Be Thou My Vision\"", "Sermon"]
    assert result["team"] == [{"name": "Pat Doe", "position": "Service Leader", "status": "declined"}]
    assert result["unfilled_positions"] == [{"position": "Scripture Reader", "quantity": 1}]
    assert "decline_reason" not in json.dumps(result) and "photo" not in json.dumps(result)
