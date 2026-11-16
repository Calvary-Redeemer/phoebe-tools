"""Check the workspace server's token and workspace.yaml from the NAS shell.

    sudo docker exec -u 1002 -w /opt/tools \
      -e WORKSPACE_TOKEN_FILE=/opt/secrets/workspace.token \
      -e WORKSPACE_CONFIG_FILE=/opt/data/phoebe-state/workspace.yaml \
      hermes /opt/data/phoebe-state/venv/bin/python -m servers.workspace.check

Prints PASS/FAIL for each configured sheet and calendar, then the calendars on Phoebe's
own list with their IDs, which is the quickest way to find an ID for workspace.yaml.
Never prints tokens, cell contents, or event details. Output goes to your terminal
only; Phoebe has no tool that runs this.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from servers.workspace.client import GoogleClient, WorkspaceError
from servers.workspace.config import ConfigError, WorkspaceConfig, load_config


def run(client: GoogleClient, config: WorkspaceConfig | None, out=print) -> bool:
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            out(f"PASS  {label}: {fn()}")
        except WorkspaceError as exc:
            ok = False
            out(f"FAIL  {label}: {exc}")

    for name, entry in (config.sheets if config else {}).items():
        def sheet(entry=entry):
            title, tabs = client.sheet_info(entry.id, what=f"the '{name}' sheet")
            if entry.default_tab and entry.default_tab not in tabs:
                raise WorkspaceError(f"default_tab '{entry.default_tab}' is not a tab. Tabs: {', '.join(tabs)}")
            return f"'{title}', tabs: {', '.join(tabs)}"
        check(f"sheet {name}", sheet)

    now = datetime.now(timezone.utc)
    for name, calendar_id in (config.calendars if config else {}).items():
        def calendar(calendar_id=calendar_id):
            meta, events = client.calendar_events(
                calendar_id, now.isoformat(timespec="seconds"),
                (now + timedelta(days=14)).isoformat(timespec="seconds"), what=f"the '{name}' calendar")
            return f"'{meta.get('summary')}', {len(events)} events in the next 14 days"
        check(f"calendar {name}", calendar)

    def listing():
        rows = client.my_calendars()
        for row in rows:
            out(f"        {row.get('summary')}  [{row.get('accessRole')}]  id: {row.get('id')}")
        return f"{len(rows)} on Phoebe's list (IDs above go in workspace.yaml under calendars:)"
    check("calendar list", listing)
    return ok


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"FAIL  workspace.yaml: {exc}")
        config = None
    try:
        client = GoogleClient.from_env()
    except WorkspaceError as exc:
        print(f"FAIL  token: {exc}")
        return 1
    return 0 if run(client, config) and config is not None else 1


if __name__ == "__main__":
    sys.exit(main())
