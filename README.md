# pco-mcp

A local [MCP](https://modelcontextprotocol.io) server for **Planning Center Online (Services)**,
built to schedule the **tech / AV teams** onto services for corporate worship — find upcoming
plans, see staffing gaps, look people up, and add / update / remove their assignments, all from
an MCP client like Claude Code or Claude Desktop.

Built with [FastMCP](https://github.com/modelcontextprotocol/python-sdk) (the official MCP
Python SDK), `httpx`, and `uv`. Runs locally over stdio — the MCP client launches the process,
so nothing is exposed on the network.

## Setup

1. **Install [uv](https://docs.astral.sh/uv/)** (already present on this machine).

2. **Credentials.** Copy `.env.example` to `.env` and fill in a Planning Center
   [Personal Access Token](https://api.planningcenteronline.com/oauth/applications)
   (App ID + Secret). The server authenticates as the user who owns the token, so that
   user needs scheduling permissions in your PCO org.

   ```env
   PCO_CLIENT_ID=your-app-id
   PCO_CLIENT_PAT=your-secret
   ```

3. **Install dependencies:**

   ```bash
   uv sync
   ```

4. **Smoke-test connectivity** (read-only — lists your service types):

   ```bash
   uv run python -c "from pco_mcp.client import PCOClient; print(PCOClient().get_collection('/service_types')[0])"
   ```

## Run

```bash
uv run pco-mcp        # starts the MCP server on stdio
```

To inspect the tools interactively with the MCP Inspector:

```bash
uv run mcp dev src/pco_mcp/server.py
```

## Register with an MCP client

Replace `/ABS/PATH/pco-mcp` with the absolute path to this directory
(`/Users/brandonedmunds/brain/ext/Projects/pco-mcp`).

### Claude Code

```bash
claude mcp add pco-services -- uv run --directory /ABS/PATH/pco-mcp pco-mcp
```

…or add it to `.mcp.json`:

```json
{
  "mcpServers": {
    "pco-services": {
      "command": "uv",
      "args": ["run", "--directory", "/ABS/PATH/pco-mcp", "pco-mcp"]
    }
  }
}
```

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pco-services": {
      "command": "uv",
      "args": ["run", "--directory", "/ABS/PATH/pco-mcp", "pco-mcp"]
    }
  }
}
```

Then restart Claude Desktop. (The token is read from this project's `.env`, so it does not
need to live in the client config.)

## Tools

### Read / discovery

| Tool | What it does |
| --- | --- |
| `list_service_types` | All service types — start here for `service_type_id`. |
| `list_teams` | Teams, optionally scoped to a service type and filtered by name (e.g. `name_contains="tech"`). |
| `list_team_positions` | Position names defined on a team (e.g. Audio, Camera). |
| `list_team_members` | People who belong to a team — the pool you can schedule from. |
| `list_plans` | Upcoming (or past) plans for a service type, soonest first. |
| `get_plan` | One plan plus its times (the time ids needed for split teams). |
| `list_needed_positions` | Unfilled positions on a plan — the staffing gaps. |
| `list_scheduled_people` | Everyone scheduled on a plan, with team + status. Source of `team_member_id`. |
| `search_people` | Find people by partial name. |
| `get_person_schedule` | A person's upcoming/past plan assignments (check conflicts). |

### Write (mutate the live PCO org)

| Tool | What it does |
| --- | --- |
| `schedule_person` | Add a person to a plan for a team. |
| `update_schedule_status` | Set Confirmed / Unconfirmed / Declined on an assignment. |
| `unschedule_person` | Remove an assignment (the person record is untouched). |

## Notifications & safety

- **`schedule_person` does not notify volunteers by default.** Pass `notify=True` only when you
  want PCO to send the scheduling request (email/text). This prevents accidental blasts while
  you experiment.
- The server **never deletes** anything beyond the specific scheduling assignment you ask
  `unschedule_person` to remove.
- All actions run with the permissions of the PAT's owning user.

## Typical workflow

1. `list_service_types` → pick the worship service type.
2. `list_teams service_type_id=… name_contains="tech"` (and `"av"`) → get team ids.
3. `list_plans service_type_id=…` → pick the upcoming service.
4. `list_needed_positions service_type_id=… plan_id=…` → see what's unfilled.
5. `list_team_members team_id=…` or `search_people name=…` → find the volunteer.
6. `schedule_person …` → assign them (add `notify=True` to send the request).
7. `list_scheduled_people …` to confirm, `update_schedule_status` / `unschedule_person` to adjust.
