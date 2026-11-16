# Deploying phoebe-tools on the church NAS

Paths below are on the UGREEN DXP6800 Pro. The Hermes container sees:

| On the NAS | In the container | Access |
|---|---|---|
| `/volume2/docker/hermes/tools` (this repo) | `/opt/tools` | read-only |
| `/volume2/docker/hermes/secrets` | `/opt/secrets` | read-only |
| `/volume2/docker/hermes/data` | `/opt/data` | read-write |

Writable state the tools need (the Python environment, later the approval database)
lives in `/opt/data/phoebe-state`.

## 1. Clone the repo

UGOS has no `git` package, and system packages added by hand can be wiped by UGOS
updates, so git runs in a throwaway `alpine/git` container instead, **as the hermes
user** (uid 1002). Running it as hermes means the files come out owned by hermes, and
git's "dubious ownership" check passes on later pulls. `HOME=/tmp` gives git a writable
home inside that container.

```bash
sudo docker run --rm --user 1002:100 -e HOME=/tmp -v /volume2/docker/hermes:/work -w /work \
  alpine/git clone https://github.com/Calvary-Redeemer/phoebe-tools tools
sudo mkdir -p /volume2/docker/hermes/data/phoebe-state
sudo chown hermes:users /volume2/docker/hermes/data/phoebe-state
```

## 2. Mount it read-only

In `/volume2/docker/hermes/docker-compose.yaml`, under `volumes:` add:

```yaml
      - /volume2/docker/hermes/tools:/opt/tools:ro
```

Then `cd /volume2/docker/hermes && sudo docker compose up -d` (a plain `restart` does
not pick up compose changes).

## 3. Build the Python environment once

`uv` isn't on the container's PATH. Hermes ships it at a versioned path
(`/opt/hermes/tools/uv-<version>-linux-x64/uv`) that changes when Hermes updates, so copy
it into Phoebe's own state folder once and always use that copy:

```bash
sudo docker exec hermes sh -c 'ls -d /opt/hermes/tools/uv-*/uv'      # find the current path
sudo docker exec -u 1002 hermes sh -c 'mkdir -p /opt/data/phoebe-state/bin && \
  cp /opt/hermes/tools/uv-0.13.5-linux-x64/uv /opt/data/phoebe-state/bin/uv && \
  /opt/data/phoebe-state/bin/uv --version'

sudo docker exec -u 1002 \
  -e UV_PROJECT_ENVIRONMENT=/opt/data/phoebe-state/venv \
  -e UV_CACHE_DIR=/opt/data/cache/uv \
  -e UV_PYTHON_INSTALL_DIR=/opt/data/phoebe-state/python \
  hermes /opt/data/phoebe-state/bin/uv --directory /opt/tools sync --frozen --no-dev
```

## 4. List the sheets and calendars Phoebe may read

The `workspace` server takes short names ("preaching_schedule", "church"), never raw
IDs, and looks them up in a file on the NAS. Copy `config/workspace.example.yaml` to your
laptop as `workspace.yaml`, fill in the IDs, then put it in Phoebe's state folder:

```bash
# on your laptop
ssh rskadmin@<nas-tailscale-ip> 'cat > /tmp/workspace.yaml' < workspace.yaml
# on the NAS
sudo install -o hermes -g users -m 600 /tmp/workspace.yaml \
  /volume2/docker/hermes/data/phoebe-state/workspace.yaml && rm /tmp/workspace.yaml
```

Check the token and the file together before involving Phoebe:

```bash
sudo docker exec -u 1002 -w /opt/tools \
  -e WORKSPACE_TOKEN_FILE=/opt/secrets/workspace.token \
  -e WORKSPACE_CONFIG_FILE=/opt/data/phoebe-state/workspace.yaml \
  hermes /opt/data/phoebe-state/venv/bin/python -m servers.workspace.check
```

It prints PASS or FAIL for each sheet and calendar, then every calendar on Phoebe's own
list with its ID. If you don't know a calendar's ID yet, leave `calendars:` with just
`phoebe: "primary"`, run the check, and copy the ID from that list. A calendar shared
directly with phoebe-bot@ normally appears there. One shared with the whole organization
may not, but it can still be read by its ID (Google Calendar > Settings > the calendar >
Integrate calendar > Calendar ID).

## 5. Register the servers with Hermes

In Hermes's config (`/volume2/docker/hermes/data/config.yaml`, which is
`/opt/data/config.yaml` inside the container), add under `mcp_servers:`:

```yaml
mcp_servers:
  pco:
    command: "/opt/data/phoebe-state/bin/uv"
    args: ["--directory", "/opt/tools", "run", "--frozen", "--no-dev",
           "python", "-m", "servers.pco.server"]
    env:
      PCO_ENV_FILE: "/opt/secrets/pco.env"
      UV_PROJECT_ENVIRONMENT: "/opt/data/phoebe-state/venv"
      UV_CACHE_DIR: "/opt/data/cache/uv"
      UV_PYTHON_INSTALL_DIR: "/opt/data/phoebe-state/python"
    tools:
      include: [list_service_types, list_upcoming_plans, get_plan]
      prompts: false
      resources: false
  workspace:
    command: "/opt/data/phoebe-state/bin/uv"
    args: ["--directory", "/opt/tools", "run", "--frozen", "--no-dev",
           "python", "-m", "servers.workspace.server"]
    env:
      WORKSPACE_TOKEN_FILE: "/opt/secrets/workspace.token"
      WORKSPACE_CONFIG_FILE: "/opt/data/phoebe-state/workspace.yaml"
      UV_PROJECT_ENVIRONMENT: "/opt/data/phoebe-state/venv"
      UV_CACHE_DIR: "/opt/data/cache/uv"
      UV_PYTHON_INSTALL_DIR: "/opt/data/phoebe-state/python"
    tools:
      include: [read_sheet, list_calendar]
      prompts: false
      resources: false
  sermonaudio:
    command: "/opt/data/phoebe-state/bin/uv"
    args: ["--directory", "/opt/tools", "run", "--frozen", "--no-dev",
           "python", "-m", "servers.sermonaudio.server"]
    env:
      SERMONAUDIO_ENV_FILE: "/opt/secrets/sermonaudio.env"
      UV_PROJECT_ENVIRONMENT: "/opt/data/phoebe-state/venv"
      UV_CACHE_DIR: "/opt/data/cache/uv"
      UV_PYTHON_INSTALL_DIR: "/opt/data/phoebe-state/python"
    tools:
      include: [get_latest_sermons, get_sermon]
      prompts: false
      resources: false
```

Hermes passes stdio servers only the variables listed under `env` (plus a safe baseline
like PATH and HOME), so each server sees its own secrets file and nothing else. The
`include` list is an allowlist: a tool added to a server later stays hidden from Phoebe
until its name is added here.

Then in Slack (#phoebe-admin-tasks) send `/reload-mcp`.

## 6. Try it

Ask Phoebe in #phoebe-admin-tasks, one fresh thread per server:

> What service types do we have in Planning Center, and what's in this Sunday's plan?

She should call `list_service_types`, `list_upcoming_plans`, and `get_plan`, and report
the order of service, who's scheduled and confirmed, and any unfilled positions.

> Read the preaching schedule and tell me who is preaching the next three Sundays, then
> list what's on the church calendar for the next two weeks.

She should call `read_sheet` and `list_calendar`.

> What are the three newest sermons on SermonAudio? Give me the full details of the latest.

She should call `get_latest_sermons`, then `get_sermon` with the newest sermon's ID.

## Updating

```bash
sudo docker run --rm --user 1002:100 -e HOME=/tmp \
  -v /volume2/docker/hermes/tools:/git -w /git alpine/git pull
# if uv.lock changed, rerun step 3; then /reload-mcp in Slack
```
