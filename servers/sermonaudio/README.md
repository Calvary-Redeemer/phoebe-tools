# sermonaudio server

| Tool | What it returns |
|---|---|
| `get_latest_sermons(count?)` | The church's newest published sermons (default 5, max 25), most recently preached first: title, speaker, preach date, Bible text, series, event type, whether audio and video are attached. |
| `get_sermon(sermon_id)` | One sermon's full details: the list fields plus description, keywords, and last-updated time. |

| `apply_listing_fixes()` | The last step of the Sunday listing check (Job D). Takes no arguments. Corrects the title, Bible text, or speaker of Sunday's listing exactly as the pco tool `check_sermon_listing` decided, and only while `sermonaudio.fix_listings` is true in `conventions.yaml`. Returns the finished post. |

`get_latest_sermons` also saves the newest 25 listings to
`$PHOEBE_STATE_DIR/cache/sermonaudio_latest.json` for `check_sermon_listing` to compare with
Sunday's Planning Center plan. See `docs/deploy-on-nas.md`, section 8.

## How access is limited

- **Credentials:** `/opt/secrets/sermonaudio.env` (`SA_API_KEY`, `SA_BROADCASTER_ID`), read
  through `SERMONAUDIO_ENV_FILE`. The key goes in the `X-API-Key` header.
- **One narrow edit:** besides GET requests the client can send exactly one thing, a PUT that
  corrects the title, Bible text, or speaker of one of the church's own listings
  (`replace_listing_text`). It reads the listing first and sends every other field back
  unchanged. It can't delete, upload, publish, or move a sermon, even though the broadcaster
  key could, and tests fail if any other request can leave the client.
- **Limits on the edit** (`fixes.py`): only the listing the check named, only while it still
  holds the value the check saw, each field at most once per listing, a speaker only if the
  name is already on a recent listing, the listing saved first, and a read-back afterwards.
- **Own broadcaster only:** list requests are pinned to the church's broadcaster ID, and
  `get_sermon` refuses a sermon that belongs to anyone else.
- **Trimmed output:** no download links, speaker bios, or broadcaster contact details.

Talks to SermonAudio's v2 API (`https://api.sermonaudio.com/v2/node/sermons`) directly with
`httpx` rather than the `sermonaudio` package, which keeps every other write method out of the process.
