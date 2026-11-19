"""Planning Center MCP server for Phoebe (read-only, Week 2).

Run by Hermes as a stdio subprocess:

    uv --directory /opt/tools run --frozen python -m servers.pco.server

Environment (set in Hermes's config.yaml under mcp_servers.pco.env):
    PCO_ENV_FILE=/opt/secrets/pco.env                               # PCO_APP_ID and PCO_SECRET
    PCO_CONVENTIONS_FILE=/opt/data/phoebe-state/conventions.yaml    # how this church writes its plans
    PHOEBE_STATE_DIR=/opt/data/phoebe-state                         # where the parsed preaching schedule is kept

Every tool here only reads. Changes (song-title quote fixes) arrive in Week 3 as
propose_* tools behind the Slack approval gate. Tools return only what the Sunday checks
need: no emails, phone numbers, addresses, or decline reasons.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from servers.pco.client import PCOClient, PCOError
from servers.pco.conventions import ConventionsError, load_conventions
from servers.pco.readiness import plan_date, run_check, upcoming_sundays
from shared.schedule_cache import read_cache

mcp = FastMCP(
    "pco",
    instructions=(
        "Read-only access to Calvary Redeemer's Planning Center Services. "
        "For the Sunday readiness check, first run the workspace tool preaching_schedule, then "
        "check_sunday_readiness, and post its `report` text exactly as returned. For other questions "
        "use list_service_types to find the Sunday service type ID, then list_upcoming_plans and "
        "get_plan. These tools cannot change anything."
    ),
)

_client: PCOClient | None = None

STATUS_WORDS = {"C": "confirmed", "U": "unconfirmed", "D": "declined"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    return _plan_detail(service_type_id, plan_id)


def _plan_detail(service_type_id: str, plan_id: str) -> dict:
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


@mcp.tool
def check_sunday_readiness(weeks: int = 6) -> dict:
    """Run the Sunday readiness check: this Sunday's plan in full, and the following Sundays
    for gaps, `weeks` Sundays in all (default 6, max 12). Every check is decided here from
    conventions.yaml, with dates worked out in the church's own time zone. Run the workspace
    tool preaching_schedule first so the schedule sheet can be compared. Post the returned
    `report` text exactly as it is: do not reword, shorten, or add to it. Changes nothing."""
    weeks = max(1, min(int(weeks), 12))
    try:
        conv = load_conventions()
    except ConventionsError as exc:
        raise ToolError(str(exc)) from exc
    now = _now()
    today = now.astimezone(conv.zone).date()
    c = client()

    types = _fetch(c.get_all, "/services/v2/service_types")
    wanted = conv.service_type_name.strip().casefold()
    match = [t for t in types if (t.get("attributes", {}).get("name") or "").strip().casefold() == wanted]
    if not match:
        names = ", ".join(sorted(t.get("attributes", {}).get("name") or "?" for t in types))
        raise ToolError(f"No Planning Center service type is named \"{conv.service_type_name}\". "
                        f"Service types: {names}. Fix service_type_name in conventions.yaml.")
    service_type_id = match[0]["id"]

    rows = _fetch(
        c.get_all,
        f"/services/v2/service_types/{service_type_id}/plans",
        {"filter": "future", "order": "sort_date", "per_page": 25},
        limit=25,
    )
    sundays = set(upcoming_sundays(today, weeks))
    plans: dict = {}
    for row in rows:
        when = plan_date(row.get("attributes", {}), conv)
        if when in sundays and when not in plans:
            plans[when] = _plan_detail(service_type_id, row["id"])

    schedule, reason = read_cache(now=now)
    return run_check(today, weeks, plans, conv, schedule, reason)


if __name__ == "__main__":
    mcp.run()
