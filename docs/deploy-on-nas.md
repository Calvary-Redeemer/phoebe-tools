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

```bash
sudo git clone https://github.com/Calvary-Redeemer/phoebe-tools /volume2/docker/hermes/tools
sudo chown -R hermes:users /volume2/docker/hermes/tools
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

```bash
sudo docker exec -u 1002 \
  -e UV_PROJECT_ENVIRONMENT=/opt/data/phoebe-state/venv \
  -e UV_CACHE_DIR=/opt/data/cache/uv \
  hermes uv --directory /opt/tools sync --frozen --no-dev
```

If `uv` isn't on the container's PATH, find it with
`sudo docker exec hermes sh -c 'command -v uv || ls /opt/data/installs/*/environments/*/venv/bin/uv'`
and use that full path here and in config.yaml.

## 4. Register the server with Hermes

In Hermes's config (`/volume2/docker/hermes/data/config.yaml`, which is
`/opt/data/config.yaml` inside the container), add under `mcp_servers:`:

```yaml
mcp_servers:
  pco:
    command: "uv"
    args: ["--directory", "/opt/tools", "run", "--frozen", "--no-dev",
           "python", "-m", "servers.pco.server"]
    env:
      PCO_ENV_FILE: "/opt/secrets/pco.env"
      UV_PROJECT_ENVIRONMENT: "/opt/data/phoebe-state/venv"
      UV_CACHE_DIR: "/opt/data/cache/uv"
    tools:
      include: [list_service_types, list_upcoming_plans, get_plan]
      prompts: false
      resources: false
```

Hermes passes stdio servers only the variables listed under `env` (plus a safe baseline
like PATH and HOME), so this server sees `pco.env` and nothing else.

Then in Slack (#phoebe-admin-tasks) send `/reload-mcp`.

## 5. Try it

Ask Phoebe in #phoebe-admin-tasks:

> What service types do we have in Planning Center, and what's in this Sunday's plan?

She should call `list_service_types`, `list_upcoming_plans`, and `get_plan`, and report
the order of service, who's scheduled and confirmed, and any unfilled positions.

## Updating

```bash
cd /volume2/docker/hermes/tools && sudo -u hermes git pull
# if uv.lock changed, rerun step 3; then /reload-mcp in Slack
```
