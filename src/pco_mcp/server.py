"""FastMCP server exposing Planning Center Services scheduling tools.

The tools follow the natural scheduling funnel for corporate worship:

    service type -> upcoming plan -> team (Tech / AV) -> who's needed
    -> find the person -> schedule them onto the plan/team

Read tools are safe to call freely. Write tools mutate the live PCO org;
their docstrings say so, and scheduling notifications to volunteers are only
sent when `notify=True` is passed explicitly.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import PCOClient, build_included_index, flatten

mcp = FastMCP("pco-services")

# Single client reused across tool calls. Created lazily so the credential
# check (and any error) happens on first use rather than at import time.
_client: PCOClient | None = None


def client() -> PCOClient:
    global _client
    if _client is None:
        _client = PCOClient()
    return _client


# Normalize friendly status words to the codes PCO accepts on write.
_STATUS_MAP = {
    "confirmed": "C",
    "c": "C",
    "unconfirmed": "U",
    "u": "U",
    "declined": "D",
    "d": "D",
}


def _status_code(status: str) -> str:
    code = _STATUS_MAP.get(status.strip().lower())
    if code is None:
        raise ValueError(
            f"Invalid status {status!r}; use Confirmed/Unconfirmed/Declined (or C/U/D)."
        )
    return code


# ----------------------------------------------------------------------
# Discovery / read tools
# ----------------------------------------------------------------------


@mcp.tool()
def list_service_types() -> list[dict[str, Any]]:
    """List all service types (e.g. "Sunday Worship", "Wednesday Night").

    A service type is the container for plans. Start here to get the
    service_type_id used by most other tools.
    """
    return client().paginate("/service_types", limit=100)


@mcp.tool()
def list_teams(
    service_type_id: str | None = None,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """List teams, optionally scoped to one service type and filtered by name.

    Use name_contains (case-insensitive substring, e.g. "tech" or "av") to find
    the tech/AV teams you schedule. Returns team id, name, and scheduling mode
    (schedule_to: "plan" = whole plan, "time" = split team scheduled per time).
    """
    path = (
        f"/service_types/{service_type_id}/teams"
        if service_type_id
        else "/teams"
    )
    teams = client().paginate(path, limit=200)
    if name_contains:
        needle = name_contains.lower()
        teams = [t for t in teams if needle in (t.get("name") or "").lower()]
    return teams


@mcp.tool()
def list_team_positions(team_id: str) -> list[dict[str, Any]]:
    """List the positions defined for a team (e.g. "Audio", "Camera", "ProPresenter").

    Position names are used as team_position_name when scheduling someone.
    """
    return client().paginate(f"/teams/{team_id}/team_positions", limit=200)


@mcp.tool()
def list_team_members(
    team_id: str,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """List people who belong to a team (the pool you can schedule from).

    Optional name_contains does a case-insensitive substring filter on the name.
    Returns person id + name, which you pass to schedule_person.
    """
    people = client().paginate(
        "/people",
        {"where[team_ids][]": team_id, "include": "emails"},
        limit=500,
    )
    if name_contains:
        needle = name_contains.lower()
        people = [p for p in people if needle in (p.get("full_name") or "").lower()]
    return people


@mcp.tool()
def list_plans(
    service_type_id: str,
    when: str = "future",
    limit: int = 25,
) -> list[dict[str, Any]]:
    """List plans (services) for a service type, soonest first.

    when: "future" (default) for upcoming services, "past" for previous ones.
    Each plan includes dates, plan_people_count (people scheduled), and
    needed_positions_count (unfilled slots). Use the plan id with other tools.
    """
    if when not in ("future", "past"):
        raise ValueError('when must be "future" or "past"')
    return client().paginate(
        f"/service_types/{service_type_id}/plans",
        {"filter": when, "order": "sort_date"},
        limit=limit,
    )


@mcp.tool()
def get_plan(service_type_id: str, plan_id: str) -> dict[str, Any]:
    """Get one plan with its times (service / rehearsal / other) side-loaded.

    The returned plan_times carry the time ids needed to schedule someone onto
    a split team (one scheduled per specific time rather than the whole plan).
    """
    body = client().request(
        "GET",
        f"/service_types/{service_type_id}/plans/{plan_id}",
        params={"include": "plan_times"},
    )
    index = build_included_index(body.get("included", []))
    plan = flatten(body.get("data", {}), index)
    plan["plan_times"] = [
        flatten(item, index)
        for item in body.get("included", [])
        if item.get("type") == "PlanTime"
    ]
    return plan


@mcp.tool()
def list_needed_positions(service_type_id: str, plan_id: str) -> list[dict[str, Any]]:
    """List unfilled positions for a plan — the staffing gaps to fill.

    Each entry has the team, team_position_name, and quantity still needed.
    Side-loads team so you can see which team each gap belongs to.
    """
    return client().paginate(
        f"/service_types/{service_type_id}/plans/{plan_id}/needed_positions",
        {"include": "team"},
        limit=200,
    )


@mcp.tool()
def list_scheduled_people(service_type_id: str, plan_id: str) -> list[dict[str, Any]]:
    """List everyone currently scheduled on a plan, with their team and status.

    Each entry's `id` is the team_member_id used by update_schedule_status and
    unschedule_person. Person and team names are side-loaded via _rel.
    """
    return client().paginate(
        f"/service_types/{service_type_id}/plans/{plan_id}/team_members",
        {"include": "person,team"},
        limit=500,
    )


@mcp.tool()
def search_people(name: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search people by name (case-insensitive partial match).

    Returns id, full_name, and status. Use the id to schedule someone or to
    look up their schedule.
    """
    return client().paginate(
        "/people",
        {"where[name_like]": name, "include": "emails"},
        limit=limit,
    )


@mcp.tool()
def get_person_schedule(person_id: str, when: str = "future") -> list[dict[str, Any]]:
    """List the plans a person is scheduled on, with plan and team side-loaded.

    when: "future" (default) or "past". Useful to check availability/conflicts
    before scheduling someone onto another service.
    """
    params: dict[str, Any] = {"include": "plan,team"}
    if when == "future":
        params["filter"] = "after"
    elif when == "past":
        params["filter"] = "before"
    elif when != "all":
        raise ValueError('when must be "future", "past", or "all"')
    return client().paginate(
        f"/people/{person_id}/plan_people", params, limit=200
    )


@mcp.tool()
def check_conflicts(
    service_type_id: str,
    plan_id: str,
    person_id: str,
    team_id: str | None = None,
) -> list[dict[str, Any]]:
    """List a person's *other* assignments on the same plan — double-booking risks.

    Use before scheduling someone to confirm they aren't already serving another
    team that service. Teams are matched by stable **team id**, never by name
    (display names can carry trailing spaces and aren't always side-loaded, which
    makes name matching silently wrong).

    - team_id: the team you intend to schedule them onto; excluded from results
      so you only see *conflicting* teams. Omit to see every assignment.

    Returns one entry per conflicting assignment with team id/name, position, and
    status; an empty list means no conflict.
    """
    rows = client().paginate(
        f"/service_types/{service_type_id}/plans/{plan_id}/team_members",
        {"include": "team"},
        limit=200,
    )
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        rel = row.get("_rel", {})
        if (rel.get("person") or {}).get("id") != person_id:
            continue
        team = rel.get("team") or {}
        if team_id and team.get("id") == team_id:
            continue
        conflicts.append(
            {
                "team_id": team.get("id"),
                "team": team.get("name"),
                "position": row.get("team_position_name"),
                "status": row.get("status"),
            }
        )
    return conflicts


# ----------------------------------------------------------------------
# Write / scheduling tools  (mutate the live PCO org)
# ----------------------------------------------------------------------


@mcp.tool()
def schedule_person(
    service_type_id: str,
    plan_id: str,
    person_id: str,
    team_id: str,
    team_position_name: str | None = None,
    status: str = "Unconfirmed",
    times_ids: list[str] | None = None,
    notify: bool = False,
) -> dict[str, Any]:
    """Schedule a person onto a plan for a team. WRITES to the live PCO org.

    Adds the person to the given team for the given plan (creates a PlanPerson).

    - status: Confirmed / Unconfirmed (default) / Declined.
    - team_position_name: position label, e.g. "Audio" or "Camera" (optional).
    - times_ids: for split teams (team.schedule_to == "time"), the plan_time
      ids the person covers; omit for whole-plan teams.
    - notify: when True, PCO sends the volunteer a scheduling notification
      (email/text). Defaults to False — no message is sent unless you opt in.

    Returns the created schedule, whose `id` is the team_member_id for later
    updates or removal.
    """
    attributes: dict[str, Any] = {
        "person_id": person_id,
        "team_id": team_id,
        "status": _status_code(status),
        "prepare_notification": bool(notify),
    }
    if team_position_name:
        attributes["team_position_name"] = team_position_name
    if times_ids:
        # PCO expects comma-joined ids for the *_ids string attributes.
        attributes["times_ids"] = ",".join(times_ids)

    body = client().request(
        "POST",
        f"/service_types/{service_type_id}/plans/{plan_id}/team_members",
        json={"data": {"type": "PlanPerson", "attributes": attributes}},
    )
    return flatten(body.get("data", {}), {})


@mcp.tool()
def update_schedule_status(
    service_type_id: str,
    plan_id: str,
    team_member_id: str,
    status: str,
    decline_reason: str | None = None,
) -> dict[str, Any]:
    """Update a scheduled person's status. WRITES to the live PCO org.

    team_member_id comes from list_scheduled_people (the entry `id`).
    status: Confirmed / Unconfirmed / Declined. Provide decline_reason when
    setting Declined.
    """
    attributes: dict[str, Any] = {"status": _status_code(status)}
    if decline_reason:
        attributes["decline_reason"] = decline_reason
    body = client().request(
        "PATCH",
        f"/service_types/{service_type_id}/plans/{plan_id}/team_members/{team_member_id}",
        json={"data": {"type": "PlanPerson", "attributes": attributes}},
    )
    return flatten(body.get("data", {}), {})


@mcp.tool()
def unschedule_person(
    service_type_id: str,
    plan_id: str,
    team_member_id: str,
) -> dict[str, str]:
    """Remove a person's assignment from a plan. WRITES to the live PCO org.

    Deletes only the specified scheduling assignment (the PlanPerson identified
    by team_member_id from list_scheduled_people). The person record itself is
    untouched. Returns a confirmation.
    """
    client().request(
        "DELETE",
        f"/service_types/{service_type_id}/plans/{plan_id}/team_members/{team_member_id}",
    )
    return {
        "status": "removed",
        "team_member_id": team_member_id,
        "plan_id": plan_id,
    }
