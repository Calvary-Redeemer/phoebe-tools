"""Planning Center MCP server for Phoebe (read-only, Week 2).

Run by Hermes as a stdio subprocess:

    uv --directory /opt/tools run --frozen python -m servers.pco.server

Environment (set in Hermes's config.yaml under mcp_servers.pco.env):
    PCO_ENV_FILE=/opt/secrets/pco.env    # PCO_APP_ID and PCO_SECRET

Every tool here only reads. Changes (song-title quote fixes) arrive in Week 3 as
propose_* tools behind the Slack approval gate. Tools return only what the Sunday checks
need: no emails, phone numbers, addresses, or decline reasons.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from servers.pco.client import PCOClient, PCOError

mcp = FastMCP(
    "pco",
    instructions=(
        "Read-only access to Calvary Redeemer's Planning Center Services. "
        "Use list_service_types to find the Sunday service type ID, then "
        "list_upcoming_plans and get_plan. These tools cannot change anything."
    ),
)

_client: PCOClient | None = None

STATUS_WORDS = {"C": "confirmed", "U": "unconfirmed", "D": "declined"}


def client() -> PCOClient:
    """Create the PCO client on first use, so the server starts even before credentials are tested."""
    global _client
    if _client is None:
        try:
            _client = PCOClient.from_env()
        except Exception as exc:  # MissingSecret, unreadable file
            raise ToolError(str(exc)) from exc
    return _client


def _fetch(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PCOError as exc:
        raise ToolError(str(exc)) from exc


def _attrs(record: dict, keys: list[str]) -> dict[str, Any]:
    a = record.get("attributes", {})
    return {"id": record.get("id"), **{k: a.get(k) for k in keys}}


@mcp.tool
def list_service_types() -> list[dict]:
    """List the church's Planning Center service types (e.g. Sunday Morning) with their IDs."""
    rows = _fetch(client().get_all, "/services/v2/service_types")
    return [_attrs(r, ["name", "frequency"]) for r in rows]


@mcp.tool
def list_upcoming_plans(service_type_id: str, count: int = 6) -> list[dict]:
    """List the next `count` plans (default 6, max 26) for a service type, soonest first."""
    count = max(1, min(int(count), 26))
    rows = _fetch(
        client().get_all,
        f"/services/v2/service_types/{service_type_id}/plans",
        {"filter": "future", "order": "sort_date", "per_page": count},
        limit=count,
    )
    return [
        _attrs(r, ["dates", "sort_date", "title", "series_title", "items_count",
                   "needed_positions_count", "planning_center_url"])
        for r in rows
    ]


@mcp.tool
def get_plan(service_type_id: str, plan_id: str) -> dict:
    """Everything the Sunday readiness check needs for one plan: order of service, who is
    scheduled and whether they've confirmed, positions still unfilled, and plan notes."""
    base = f"/services/v2/service_types/{service_type_id}/plans/{plan_id}"
    c = client()
    plan = _fetch(c.get, base)["data"]
    items = _fetch(c.get_all, f"{base}/items")
    team = _fetch(c.get_all, f"{base}/team_members", {"filter": "not_deleted"})
    needed = _fetch(c.get_all, f"{base}/needed_positions")
    notes = _fetch(c.get_all, f"{base}/notes")

    return {
        "plan": _attrs(plan, ["dates", "sort_date", "title", "series_title", "planning_center_url"]),
        "items": sorted(
            (_attrs(i, ["sequence", "title", "item_type", "description", "service_position",
                        "length", "key_name"]) for i in items),
            key=lambda x: (x.get("sequence") is None, x.get("sequence") or 0),
        ),
        "team": [
            {
                "name": t.get("attributes", {}).get("name"),
                "position": t.get("attributes", {}).get("team_position_name"),
                "status": STATUS_WORDS.get(t.get("attributes", {}).get("status"),
                                           t.get("attributes", {}).get("status")),
            }
            for t in team
        ],
        "unfilled_positions": [
            {"position": n.get("attributes", {}).get("team_position_name"),
             "quantity": n.get("attributes", {}).get("quantity")}
            for n in needed
        ],
        "notes": [_attrs(n, ["category_name", "content"]) for n in notes],
    }


if __name__ == "__main__":
    mcp.run()
