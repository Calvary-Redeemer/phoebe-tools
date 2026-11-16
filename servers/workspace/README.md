# workspace server (read-only, Week 2)

| Tool | What it returns |
|---|---|
| `read_sheet(sheet?, tab?, max_rows?)` | One tab of a configured Google Sheet as rows of displayed text, plus the list of tab names. Default: the Preaching/Teaching Schedule. |
| `list_calendar(calendar?, days_ahead?)` | Events on a configured Google Calendar from now through the next 14 days (max 90): title, start, end, location, shortened description. No guest lists or email addresses. |

`create_doc` (scripture reading and weekly email drafts) is planned for Week 3 with Job B.

## How access is limited

- **Token:** `/opt/secrets/workspace.token`, the OAuth token minted for phoebe-bot@ with
  scopes `calendar.readonly`, `drive.file`, `spreadsheets.readonly`. The server reads it
  through `WORKSPACE_TOKEN_FILE`, exchanges the refresh token for a one-hour access token
  held in memory, and never writes anything back (the secrets mount is read-only).
- **Scope check:** if Google reports any scope beyond those three, the server refuses to
  use the token.
- **Names, not IDs:** Phoebe passes a name from `workspace.yaml` on the NAS
  (`WORKSPACE_CONFIG_FILE`, see `config/workspace.example.yaml`). Her token could open any
  sheet shared with her account; the server only opens the ones a person listed.
- **GET only, two hosts only:** the client has no write methods and will only send the
  access token to the Sheets and Calendar APIs.
- **Untrusted text:** cell text and event descriptions are typed by people. Long values
  are shortened, and the server's instructions tell Phoebe to treat them as data.

## Check it from the NAS

```bash
sudo docker exec -u 1002 -w /opt/tools \
  -e WORKSPACE_TOKEN_FILE=/opt/secrets/workspace.token \
  -e WORKSPACE_CONFIG_FILE=/opt/data/phoebe-state/workspace.yaml \
  hermes /opt/data/phoebe-state/venv/bin/python -m servers.workspace.check
```

Prints PASS/FAIL per sheet and calendar, then the calendars on Phoebe's list with their
IDs. It never prints tokens, cell contents, or event details.
