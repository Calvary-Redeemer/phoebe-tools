# phoebe-tools

Least-privilege [MCP](https://modelcontextprotocol.io) tool servers for **Phoebe**, the Hermes
agent that handles church administration for Calvary Redeemer. Formerly `pco-mcp`.

Each server gives Phoebe a short, fixed list of tools for one system, runs as its own
process, and reads only its own credentials file. Phoebe reads and reports on her own;
anything that changes data goes through the Slack approval gate (`shared/approval_gate`),
which checks for a ✅ from an approved person **in code**, not just in her instructions.

| Server | Status | Tools |
|---|---|---|
| `servers/pco` | Week 2: read-only | `list_service_types`, `list_upcoming_plans`, `get_plan`, `check_sunday_readiness` (the Sunday readiness check) |
| `servers/workspace` | Week 2: read-only | `read_sheet`, `list_calendar`, `preaching_schedule` (`create_doc` planned, Week 3) |
| `servers/sermonaudio` | Week 2: read-only | `get_latest_sermons`, `get_sermon` |
| `servers/gmail` | planned (Week 3) | `list_new_messages`, `get_message` |
| `servers/youtube` | planned (Week 4) | `get_latest_stream`, `get_video`, `propose_video_update` |
| `shared/approval_gate` | planned (Week 3) | `propose_*` → `apply_change` |

There is no delete tool, no send tool that skips approval, and nothing for Giving,
permissions, or member records.

## This repo is public

Never commit: tokens or `.env` files, OAuth client JSON, the approver list, sheet or
channel IDs, `conventions.yaml`, `workspace.yaml`, exported plans, or anything with members' names or
contact details. Those live on the NAS under `/volume2/docker/hermes/` (see
`docs/deploy-on-nas.md`). `.gitignore` blocks the common cases; check `git status`
before every commit anyway.

## Develop and test

```bash
uv sync
uv run pytest
```

Tests use mocked HTTP; they never call Planning Center, Google, or SermonAudio.

## Deploy

See [`docs/deploy-on-nas.md`](docs/deploy-on-nas.md).

## Reference

`docs/` holds Planning Center's getting-started notes and the Services API (2018-11-01)
resource index and OpenAPI spec.
