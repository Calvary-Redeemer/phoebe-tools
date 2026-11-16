# sermonaudio server (read-only, Phase 1)

| Tool | What it returns |
|---|---|
| `get_latest_sermons(count?)` | The church's newest published sermons (default 5, max 25), most recently preached first: title, speaker, preach date, Bible text, series, event type, whether audio and video are attached. |
| `get_sermon(sermon_id)` | One sermon's full details: the list fields plus description, keywords, and last-updated time. |

Job D uses these to compare the newest listing with Sunday's Planning Center plan.

## How access is limited

- **Credentials:** `/opt/secrets/sermonaudio.env` (`SA_API_KEY`, `SA_BROADCASTER_ID`), read
  through `SERMONAUDIO_ENV_FILE`. The key goes in the `X-API-Key` header.
- **GET only:** the client has no write methods, so it can't edit or upload a sermon even
  though the broadcaster key could.
- **Own broadcaster only:** list requests are pinned to the church's broadcaster ID, and
  `get_sermon` refuses a sermon that belongs to anyone else.
- **Trimmed output:** no download links, speaker bios, or broadcaster contact details.

Talks to SermonAudio's v2 API (`https://api.sermonaudio.com/v2/node/sermons`) directly with
`httpx` rather than the `sermonaudio` package, which keeps write methods out of the process.
