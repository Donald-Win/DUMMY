# 🐳 DUMMY
### Docker Update Made Manageable, Yay!

DUMMY is a self-hosted web UI that monitors your Docker containers, alerts you when new versions are available, and lets you update or roll back with one click — all with live progress feedback.

[![Docker Hub](https://img.shields.io/docker/v/donaldwin/dummy?label=Docker%20Hub&logo=docker&logoColor=white)](https://hub.docker.com/r/donaldwin/dummy)
[![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64%20%7C%20armv7-blue)](#)
[![License](https://img.shields.io/badge/license-MIT-green)](#)

---

![DUMMY screenshot showing dashboard with stat cards and container list](.github/screenshot.png)

---

## What it does

- Polls Docker Hub and GHCR for newer image versions on a schedule, with retry and rate-limit handling
- Shows a live dashboard — monitored, running, updates ready, up to date
- Applies updates with a single click and shows the pull / recreate / health-check in real time
- Rolls back automatically if a container fails its health check after an update
- Keeps a per-container version history — roll back to any recorded version at any time
- Dismisses updates you don't want to act on right now
- Pins containers you want to monitor but never auto-update
- Sends push notifications via ntfy and outbound webhooks on every event
- Supports basic auth to protect the UI from other users on your network

---

## Quick setup

### 1. Add DUMMY to your stack

```yaml
services:
  dummy:
    image: donaldwin/dummy:latest
    container_name: dummy
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock   # required — Docker access
      - /opt/stacks/dummy:/data                      # required — persistent history
    environment:
      TZ: Pacific/Auckland
    networks:
      - your_network
```

### 2. Opt containers in with a single label

Add `dummy.enable=true` to any service you want monitored:

```yaml
services:
  radarr:
    image: lscr.io/linuxserver/radarr:6.0.4.10291
    labels:
      - dummy.enable=true
```

That's it. DUMMY detects the image, polls the registry, and shows available updates in the UI.

### 3. Open the UI

Navigate to `http://your-server:5000`

---

## Three update strategies

DUMMY picks a strategy automatically based on which labels are present. You can use a different strategy per container.

### Strategy 1 — Docker API (default)

No extra labels needed. DUMMY recreates the container via the Docker SDK, preserving all volumes, ports, networks, environment variables, restart policies, and capabilities.

```yaml
services:
  radarr:
    image: lscr.io/linuxserver/radarr:6.0.4.10291
    labels:
      - dummy.enable=true
```

> ⚠️ Running `docker compose up` manually after a Docker API update will revert the tag to whatever is in your compose file. Use Strategy 2 or 3 if you need files kept in sync.

---

### Strategy 2 — Compose file

DUMMY edits the image tag in your `docker-compose.yml` and runs `docker compose up -d <service>`. Your file always reflects what's running.

```yaml
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:4.0.16.2944
    labels:
      - dummy.enable=true
      - dummy.compose_file=/compose/docker-compose.yml
      # - dummy.compose_service=sonarr   # only needed if service name differs from container name
```

Add to DUMMY's volumes:
```yaml
- /path/to/your/docker-compose.yml:/compose/docker-compose.yml
```

---

### Strategy 3 — Env file

DUMMY updates a version variable in your `.env` file and restarts the container. Best when you pin versions as variables and reference them like `image: radarr:${RADARR_VER}`.

```yaml
services:
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:${PROWLARR_VER}
    labels:
      - dummy.enable=true
      - dummy.env_var=PROWLARR_VER
```

Add to DUMMY's volumes:
```yaml
- /path/to/your/.env:/env/.env
```

---

### Combining strategies

Strategies 2 and 3 work together — DUMMY updates both files in one operation:

```yaml
labels:
  - dummy.enable=true
  - dummy.compose_file=/compose/docker-compose.yml
  - dummy.env_var=PROWLARR_VER
```

---

## Full example compose.yml

```yaml
services:

  dummy:
    image: donaldwin/dummy:latest
    container_name: dummy
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /opt/stacks/dummy:/data
      - /opt/stacks/.env:/env/.env                                    # Strategy 3
      - /opt/stacks/docker-compose.yml:/compose/docker-compose.yml    # Strategy 2
    environment:
      TZ: Pacific/Auckland
      NTFY_ENDPOINT: http://ntfy:80
      NTFY_TOPIC: DockerUpdate
      NTFY_CLICK_URL: http://update.yourdomain.com
      WEBHOOK_URL: http://homeassistant:8123/api/webhook/dummy         # optional
      BASIC_AUTH_USER: admin                                           # optional
      BASIC_AUTH_PASS: changeme                                        # optional
    networks:
      - internal

  # Strategy 1 — Docker API (simplest)
  adguardhome:
    image: adguard/adguardhome:v0.107.72
    container_name: adguardhome
    restart: unless-stopped
    labels:
      - dummy.enable=true
    networks:
      - internal

  # Strategy 3 — Env file
  radarr:
    image: lscr.io/linuxserver/radarr:${RADARR_VER}
    container_name: radarr
    restart: unless-stopped
    labels:
      - dummy.enable=true
      - dummy.env_var=RADARR_VER
    networks:
      - internal

  # Strategy 2 — Compose file
  homepage:
    image: ghcr.io/gethomepage/homepage:V1.10.1
    container_name: homepage
    restart: unless-stopped
    labels:
      - dummy.enable=true
      - dummy.compose_file=/compose/docker-compose.yml
    networks:
      - internal

  # Pinned — monitored but never updated automatically or prompted
  plex:
    image: plexinc/pms-docker:1.40.0.7998
    container_name: plex
    restart: unless-stopped
    labels:
      - dummy.enable=true
      - dummy.pin=true
    networks:
      - internal

networks:
  internal:
    external: true
```

---

## All labels

| Label | Example | Description |
|---|---|---|
| `dummy.enable` | `true` | **Required.** Opt this container in to monitoring. |
| `dummy.compose_file` | `/compose/docker-compose.yml` | Path to the compose file inside the DUMMY container. Enables Strategy 2. |
| `dummy.compose_service` | `sonarr` | Service name in the compose file if it differs from the container name. |
| `dummy.env_var` | `RADARR_VER` | Variable name in the `.env` file to update. Enables Strategy 3. |
| `dummy.changelog` | `https://github.com/.../releases` | Override the changelog URL shown in the UI. |
| `dummy.pin` | `true` | Monitor but never show or apply updates. Useful for containers you've deliberately held back. |

---

## Settings

Click **⚙ Settings** in the UI to adjust these at runtime without redeploying:

| Setting | Default | Description |
|---|---|---|
| Check interval | Every 6h | How often DUMMY polls registries. Options: 1h / 2h / 6h / 12h / 24h. |
| History limit | 5 versions | Past versions stored per container. |
| Pre-releases | Off | Include alpha / beta / rc / nightly tags. |
| Auto-update | Off | Apply updates automatically without confirmation. |

Settings are saved to the database and survive restarts. They take precedence over environment variables, so you can set a sensible default via env and override it from the UI at any time.

---

## Dismissing updates

If an update is available but you don't want to act on it right now, click **✕ Dismiss** next to the Update button. The version will be silently skipped until a newer one is found. Dismissed updates can be un-dismissed via the API:

```bash
curl -X POST http://your-server:5000/api/undismiss \
  -H "Content-Type: application/json" \
  -d '{"container":"radarr","tag":"7.0.0"}'
```

---

## Environment variables

### Security

| Variable | Default | Description |
|---|---|---|
| `BASIC_AUTH_USER` | — | Username for basic auth. Leave unset to disable authentication. |
| `BASIC_AUTH_PASS` | — | Password for basic auth. Set both user and pass to enable. The `/health` endpoint is always unauthenticated for Docker's own health check. |

### Paths and ports

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/versions.db` | SQLite database path. Always mount `/data` to a host directory. |
| `ENV_FILE_PATH` | `/env/.env` | Your `.env` file inside the container (Strategy 3). |
| `PORT` | `5000` | Port Flask listens on. |

### Notifications (ntfy)

| Variable | Default | Description |
|---|---|---|
| `NTFY_ENDPOINT` | — | Base URL of your ntfy server, e.g. `http://ntfy:80`. Leave unset to disable. |
| `NTFY_TOPIC` | `DockerUpdate` | ntfy topic name. |
| `NTFY_TOKEN` | — | Bearer token for authenticated ntfy instances. |
| `NTFY_CLICK_URL` | — | URL embedded in the notification to open the DUMMY UI. |

### Webhooks

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_URL` | — | URL to POST JSON event payloads to. Leave unset to disable. |

DUMMY sends a POST to this URL for three event types:

- `update_success` — a container was successfully updated: `{"event","timestamp","container","from_tag","to_tag","status"}`
- `update_failed` — update failed and was rolled back: `{"event","timestamp","container","attempted_tag","rolled_back_to"}`
- `updates_found` — new versions detected during a check: `{"event","timestamp","count","updates":[{"container","from","to"}]}`

This lets DUMMY trigger Home Assistant automations, Pushover via n8n, or any other webhook-capable system without bespoke integrations.

### GitHub API

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | Personal access token. Raises GHCR rate limit from 60 → 5000 req/hour. No scopes needed for public images. |

### Update behaviour (overrideable in UI)

These set the initial defaults. The Settings panel takes precedence once saved.

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `21600` | Seconds between background checks (21600 = 6h). |
| `ALLOW_PRERELEASE` | `false` | Include pre-release tags. |
| `AUTO_UPDATE` | `false` | Apply updates automatically. |
| `HEALTH_CHECK_TIMEOUT` | `60` | Seconds to wait for a healthy container before rolling back. |
| `HISTORY_LIMIT` | `5` | Past versions stored per container. |

### Misc

| Variable | Default | Description |
|---|---|---|
| `CHANGELOG_URLS` | — | Pipe-separated `image-fragment=url` pairs to add or override changelog links, e.g. `myapp=https://github.com/me/myapp/releases\|other=https://other.io`. |
| `WEB_TITLE` | `DUMMY` | Page title shown in the browser tab. |
| `TZ` | system | Container timezone, e.g. `Pacific/Auckland`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

### Backward compatibility

| Variable | Example | Description |
|---|---|---|
| `VERSION_VARS` | `radarr=RADARR_VER,sonarr=SONARR_VER` | Legacy: comma-separated `container=ENV_VAR` pairs. Equivalent to `dummy.enable=true` + `dummy.env_var=X` on each service without modifying them. |

---

## Volumes

| Mount | When needed | Description |
|---|---|---|
| `/var/run/docker.sock` | Always | Docker socket. Required for container inspection and management. |
| `/data` | **Always** | Persistent SQLite database for version history, update cache, dismissed updates, and settings. Without this, all state is lost on restart. DUMMY warns in the UI if this is not a bind mount. |
| `/env/.env` | Strategy 3 | The `.env` file DUMMY will read and edit. |
| `/compose/docker-compose.yml` | Strategy 2 | The compose file DUMMY will read and edit. |

---

## How updates work

When you click **Update** (or DUMMY applies one automatically):

1. Checks no other update is already running on that container
2. Pulls the new image
3. Stops and removes the old container
4. Recreates it with identical config — volumes, ports, networks, env vars, restart policy, capabilities
5. Waits up to `HEALTH_CHECK_TIMEOUT` seconds for the container to become healthy
6. If the health check fails → reverts to the previous image, restarts, notifies, and fires the `update_failed` webhook
7. If it passes → records the new version in history, notifies, and fires the `update_success` webhook

File writes (`.env`, compose YAML) are atomic — DUMMY writes to a `.tmp` file then renames, so a power failure mid-write never corrupts your files.

---

## Version history and rollback

Every container card shows its full version history. Click **↩ Restore** next to any past entry to roll back to that exact version. The rollback goes through the same pull → recreate → health-check flow as a forward update, and is recorded in history as `rolled_back`.

### Exporting and importing history

Before migrating your host or rebuilding your server:

```bash
# Export
curl http://your-server:5000/api/history/export -o dummy-history.json

# Import after migration
curl -X POST http://your-server:5000/api/history/import \
  -H "Content-Type: application/json" \
  -d @dummy-history.json
```

Re-importing is safe — duplicate entries are skipped automatically.

---

## Supported registries

| Image format | Registry |
|---|---|
| `ghcr.io/<org>/<repo>` | GitHub Container Registry |
| `lscr.io/linuxserver/<repo>` | Docker Hub (LinuxServer) |
| `<org>/<repo>` or `docker.io/<org>/<repo>` | Docker Hub |
| Plain `<repo>` | Docker Hub official library |

GHCR images are tried via the GitHub Packages API first, then fall back to the Docker Registry v2 API anonymously. Both endpoints paginate fully so no updates are missed regardless of how many tags an image has. All registry requests use exponential backoff retry and respect `Retry-After` headers on rate-limit responses.

---

## Auto-detected changelogs

Changelog links appear automatically for:

`linuxserver/*` · `immich-app/immich` · `gethomepage/homepage` · `FlareSolverr/FlareSolverr` · `advplyr/audiobookshelf` · `AdguardTeam/AdGuardHome` · `binwiederhier/ntfy` · `Plex Media Server` · `qBittorrent` · `jellyfin/jellyfin` · `portainer/portainer`

Add or override with the `CHANGELOG_URLS` env var or the `dummy.changelog` label.

---

## API reference

All update, rollback, and check operations return a `job_id` immediately and run in the background. Poll `/api/jobs/<id>` for live log lines and completion status.

| Method | Endpoint | Body / notes |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | `{"status":"ok","containers_monitored":N}` — always unauthenticated |
| `GET` | `/api/status` | Last/next check times, check_running flag, active_updates list |
| `GET` | `/api/containers` | All monitored containers with current state |
| `POST` | `/api/check` | Trigger a scan. Optional: `{"container":"name"}` to check one container only |
| `POST` | `/api/update` | `{"container":"name","tag":"1.2.3"}` |
| `POST` | `/api/rollback` | `{"container":"name"}` (previous) or `{"container":"name","tag":"1.0.0"}` (specific) |
| `GET` | `/api/jobs/<id>` | Poll a running job for live log output and done/success status |
| `GET` | `/api/settings` | Current effective settings |
| `POST` | `/api/settings` | `{"check_interval":3600,"allow_prerelease":false,...}` |
| `POST` | `/api/dismiss` | `{"container":"name","tag":"1.2.3"}` — hide this update |
| `POST` | `/api/undismiss` | `{"container":"name"}` (all) or add `"tag"` for a specific version |
| `GET` | `/api/history/export` | Download full version history as `dummy-history.json` |
| `POST` | `/api/history/import` | Restore from a previously exported file |
| `GET` | `/api/history/<container>` | Version history for one container |

---

## Running the tests

```bash
python3 -m unittest test_dummy -v
```

No extra dependencies needed — the test suite stubs out Docker, Flask, and requests and runs with the standard library only. Tests cover tag filtering, version comparison, registry routing, changelog URL generation, retry logic, webhook behaviour, and the dismiss system.

---

## License

MIT

