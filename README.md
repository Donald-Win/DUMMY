# 🐳 DUMMY – Docker Update Made Manageable, Yay!

A lightweight, self-hosted web UI for monitoring Docker container versions, checking for updates, and applying or rolling back changes — with live progress feedback and persistent version history.

![Docker Hub](https://img.shields.io/docker/v/donaldwin/dummy?label=Docker%20Hub&logo=docker)
![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64%20%7C%20armv7-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Dashboard overview** — stat cards, configuration status, live countdown to next check
- **Three update strategies** — auto-detected from labels, no manual mapping required
- **Live progress modal** — real-time log output for every update, rollback, and check
- **Health-check gating** — automatic rollback if a container fails to become healthy
- **Full version history** — roll back to any previous version, not just the last one
- **History export / import** — download a JSON backup before migrations, restore after
- **In-UI settings** — adjust check interval, history limit, pre-releases, and auto-update without editing compose files
- **Dark / light mode** — persisted per browser
- **ntfy notifications** — updates available, success, and failure alerts
- **Persistence check** — warns on startup and in the UI if `/data` is not bind-mounted

---

## Quick start

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
      - /path/to/data/dummy:/data          # required for persistent history
      # - /path/to/.env:/env/.env          # add if using Mode 3 (env file)
    environment:
      - TZ=Pacific/Auckland
      - NTFY_ENDPOINT=http://ntfy:80       # optional
      - NTFY_TOPIC=DockerUpdate
```

Open `http://your-server:5000`. Then add `dummy.enable=true` to any container you want monitored.

---

## Setup is one label

The minimum to monitor any container:

```yaml
labels:
  - dummy.enable=true
```

DUMMY detects the image, polls the registry, and shows updates in the UI. No env files, no compose edits, no `VERSION_VARS` to configure.

---

## Three update strategies

DUMMY automatically selects a strategy based on which labels are present. You can mix strategies across containers.

### Mode 1 — Docker API (default)

DUMMY pulls the new image and recreates the container via the Docker SDK, preserving all config — volumes, ports, networks, env vars, restart policy, capabilities, and network aliases.

```yaml
services:
  radarr:
    image: lscr.io/linuxserver/radarr:6.0.4.10291
    labels:
      - dummy.enable=true
```

> ⚠️ Running `docker compose up` manually after an API-mode update will revert the tag to whatever is in your compose file. Use Mode 2 or 3 if you need files kept in sync.

---

### Mode 2 — Compose file editing

DUMMY edits the image tag directly in your `docker-compose.yml` and runs `docker compose up -d <service>`. Your compose file always reflects the running version.

```yaml
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:${SONARR_VER}
    labels:
      - dummy.enable=true
      - dummy.compose_file=/compose/docker-compose.yml
      # - dummy.compose_service=sonarr   # optional if name differs from container
```

Add to DUMMY's volumes:
```yaml
- /path/to/your/docker-compose.yml:/compose/docker-compose.yml
```

---

### Mode 3 — Env file editing

DUMMY updates a version variable in your `.env` file and restarts the container. Ideal when you pin versions as variables and reference them in compose like `image: radarr:${RADARR_VER}`.

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

### Combining modes

Modes 2 and 3 work together — DUMMY updates both files in one operation:

```yaml
labels:
  - dummy.enable=true
  - dummy.compose_file=/compose/docker-compose.yml
  - dummy.env_var=PROWLARR_VER
```

---

## All supported labels

| Label | Example value | Description |
|---|---|---|
| `dummy.enable` | `true` | **Required.** Opt this container in to monitoring. |
| `dummy.compose_file` | `/compose/docker-compose.yml` | Path inside the DUMMY container to the compose file to edit. Enables Mode 2. |
| `dummy.compose_service` | `sonarr` | Service name in the compose file. Defaults to the container name. |
| `dummy.env_var` | `SONARR_VER` | Variable name in the `.env` file to update. Enables Mode 3. |
| `dummy.changelog` | `https://github.com/.../releases` | Override the changelog link shown in the UI. Many registries are auto-detected. |

---

## In-UI settings

Click the **⚙ Settings** button to adjust these without touching your compose file:

| Setting | Default | Description |
|---|---|---|
| Check interval | 6 hours | How often DUMMY polls registries for updates. Options: 1h / 2h / 6h / 12h / 24h. |
| History limit | 5 versions | How many past versions to store per container. |
| Pre-releases | Off | Include alpha / beta / rc / nightly / edge tags when checking for newer versions. |
| Auto-update | Off | Apply updates automatically without UI confirmation. |

Settings are persisted in the SQLite database and survive container restarts. They take precedence over environment variables, so you can set a sensible default via env and override it from the UI at any time.

---

## Environment variables

Settings that cannot be changed from the UI at runtime:

### Paths and ports

| Variable | Default | Description |
|---|---|---|
| `ENV_FILE_PATH` | `/env/.env` | Path inside the container to your `.env` file (Mode 3). |
| `DB_PATH` | `/data/versions.db` | SQLite database path. Mount `/data` to a host directory to persist. |
| `PORT` | `5000` | Port Flask listens on inside the container. |

### Notifications (ntfy)

| Variable | Default | Description |
|---|---|---|
| `NTFY_ENDPOINT` | _(disabled)_ | Base URL of your ntfy server, e.g. `http://ntfy:80`. Leave unset to disable. |
| `NTFY_TOPIC` | `DockerUpdate` | ntfy topic name. |
| `NTFY_TOKEN` | _(none)_ | Bearer token for authenticated ntfy instances. |
| `NTFY_CLICK_URL` | _(none)_ | URL embedded in the notification to open the DUMMY UI. |

### GitHub API

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | _(none)_ | Personal access token. Raises GHCR rate limit from 60 to 5000 req/hour. No scopes needed for public images. |

### Update behaviour (overrideable in UI)

These set the initial defaults. The UI settings panel takes precedence once saved.

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `21600` | Seconds between background update checks. |
| `ALLOW_PRERELEASE` | `false` | Include pre-release tags. |
| `AUTO_UPDATE` | `false` | Apply updates automatically. |
| `HEALTH_CHECK_TIMEOUT` | `60` | Seconds to wait for a container to become healthy before rolling back. |
| `HISTORY_LIMIT` | `5` | Past versions to store per container. |

### Misc

| Variable | Default | Description |
|---|---|---|
| `CHANGELOG_URLS` | _(none)_ | Pipe-separated `image-fragment=url` pairs to override auto-detected changelog links, e.g. `myapp=https://github.com/me/myapp/releases\|other=https://other.io`. |
| `WEB_TITLE` | `DUMMY` | Page title shown in the browser tab and header. |
| `TZ` | _(system)_ | Container timezone, e.g. `Pacific/Auckland`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

### Backward compatibility

| Variable | Example | Description |
|---|---|---|
| `VERSION_VARS` | `radarr=RADARR_VER,sonarr=SONARR_VER` | Legacy: comma-separated `container=ENV_VAR` pairs. Equivalent to `dummy.enable=true` + `dummy.env_var=X` on each service, without modifying the service definitions. |

---

## Volumes

| Mount | Required | Description |
|---|---|---|
| `/var/run/docker.sock` | Always | Docker socket for container inspection and management. |
| `/data` | **Strongly recommended** | Persistent SQLite database. Without this, version history and rollback data is lost on every container restart. DUMMY will show a warning in the UI if this is not a proper bind mount. |
| `/compose/docker-compose.yml` | Mode 2 only | The compose file DUMMY will read and edit. |
| `/env/.env` | Mode 3 only | The `.env` file DUMMY will read and edit. |

---

## Health-check gating

Every update and rollback waits for the container to become healthy before marking it as complete. If the container fails to reach a healthy state within `HEALTH_CHECK_TIMEOUT` seconds, DUMMY:

1. Automatically reverts to the previous version
2. Restarts the container on the old version
3. Sends a failure notification via ntfy (if configured)

The previous image is always retained locally so rollbacks are instant — no re-pull needed.

---

## Version history and rollback

DUMMY stores the last N versions per container (configurable). In the UI each container shows its history with a **↩ Restore** button on every past entry. You can roll back to any recorded version, not just the most recent.

### Exporting and importing history

Before migrating your stack or rebuilding your Pi, export a backup:

```bash
curl http://your-server:5000/api/history/export -o dummy-history.json
```

Or use the **↓ Export History** button in the UI. To restore after:

```bash
curl -X POST http://your-server:5000/api/history/import \
  -H "Content-Type: application/json" \
  -d @dummy-history.json
```

Re-importing is safe — duplicate entries are skipped.

---

## Registry support

DUMMY auto-detects the registry from the image name:

| Image prefix | Registry queried |
|---|---|
| `ghcr.io/<org>/<repo>` | GitHub Container Registry |
| `lscr.io/linuxserver/<repo>` | Docker Hub `linuxserver/<repo>` |
| `<org>/<repo>` or `docker.io/<org>/<repo>` | Docker Hub |
| Plain `<repo>` | Docker Hub official library |

---

## Auto-detected changelogs

DUMMY automatically links the changelog for common images. The following are recognised without any configuration:

`linuxserver/*` · `immich-app/immich` · `gethomepage/homepage` · `FlareSolverr/FlareSolverr` · `advplyr/audiobookshelf` · `AdguardTeam/AdGuardHome` · `binwiederhier/ntfy` · `Plex Media Server` · `qBittorrent` · `jellyfin/jellyfin` · `portainer/portainer`

Override or add extras using the `CHANGELOG_URLS` environment variable or the `dummy.changelog` label.

---

## API reference

| Method | Path | Body / Notes |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | `{"status":"ok","containers_monitored":N}` |
| `GET` | `/api/status` | Last check time, next check time, current interval |
| `GET` | `/api/containers` | JSON array of all monitored containers with current state |
| `POST` | `/api/check` | Trigger an immediate background update scan. Returns `{"job_id":"..."}` |
| `POST` | `/api/update` | `{"container":"name","tag":"1.2.3"}` — apply an update. Returns `{"job_id":"..."}` |
| `POST` | `/api/rollback` | `{"container":"name"}` (previous) or `{"container":"name","tag":"1.0.0"}` (specific). Returns `{"job_id":"..."}` |
| `GET` | `/api/jobs/<id>` | Poll a running job for live log output and completion status |
| `GET` | `/api/settings` | Current effective settings (DB overrides or env defaults) |
| `POST` | `/api/settings` | `{"check_interval":3600,"allow_prerelease":false,...}` — update settings |
| `GET` | `/api/history/export` | Download full version history as `dummy-history.json` |
| `POST` | `/api/history/import` | Restore history from a previously exported JSON file |
| `GET` | `/api/history/<container>` | Version history for a single container |

---
# 🐳 DUMMY – Docker Update Made Manageable, Yay!

A lightweight, self-hosted web UI for monitoring Docker container versions, checking for updates, and applying or rolling back changes — with live progress feedback and persistent version history.

![Docker Hub](https://img.shields.io/docker/v/donaldwin/dummy?label=Docker%20Hub&logo=docker)
![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64%20%7C%20armv7-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Dashboard overview** — stat cards, configuration status, live countdown to next check
- **Three update strategies** — auto-detected from labels, no manual mapping required
- **Live progress modal** — real-time log output for every update, rollback, and check
- **Health-check gating** — automatic rollback if a container fails to become healthy
- **Full version history** — roll back to any previous version, not just the last one
- **History export / import** — download a JSON backup before migrations, restore after
- **In-UI settings** — adjust check interval, history limit, pre-releases, and auto-update without editing compose files
- **Dark / light mode** — persisted per browser
- **ntfy notifications** — updates available, success, and failure alerts
- **Persistence check** — warns on startup and in the UI if `/data` is not bind-mounted

---

## Quick start

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
      - /path/to/data/dummy:/data          # required for persistent history
      # - /path/to/.env:/env/.env          # add if using Mode 3 (env file)
    environment:
      - TZ=Pacific/Auckland
      - NTFY_ENDPOINT=http://ntfy:80       # optional
      - NTFY_TOPIC=DockerUpdate
```

Open `http://your-server:5000`. Then add `dummy.enable=true` to any container you want monitored.

---

## Setup is one label

The minimum to monitor any container:

```yaml
labels:
  - dummy.enable=true
```

DUMMY detects the image, polls the registry, and shows updates in the UI. No env files, no compose edits, no `VERSION_VARS` to configure.

---

## Three update strategies

DUMMY automatically selects a strategy based on which labels are present. You can mix strategies across containers.

### Mode 1 — Docker API (default)

DUMMY pulls the new image and recreates the container via the Docker SDK, preserving all config — volumes, ports, networks, env vars, restart policy, capabilities, and network aliases.

```yaml
services:
  radarr:
    image: lscr.io/linuxserver/radarr:6.0.4.10291
    labels:
      - dummy.enable=true
```

> ⚠️ Running `docker compose up` manually after an API-mode update will revert the tag to whatever is in your compose file. Use Mode 2 or 3 if you need files kept in sync.

---

### Mode 2 — Compose file editing

DUMMY edits the image tag directly in your `docker-compose.yml` and runs `docker compose up -d <service>`. Your compose file always reflects the running version.

```yaml
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:${SONARR_VER}
    labels:
      - dummy.enable=true
      - dummy.compose_file=/compose/docker-compose.yml
      # - dummy.compose_service=sonarr   # optional if name differs from container
```

Add to DUMMY's volumes:
```yaml
- /path/to/your/docker-compose.yml:/compose/docker-compose.yml
```

---

### Mode 3 — Env file editing

DUMMY updates a version variable in your `.env` file and restarts the container. Ideal when you pin versions as variables and reference them in compose like `image: radarr:${RADARR_VER}`.

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

### Combining modes

Modes 2 and 3 work together — DUMMY updates both files in one operation:

```yaml
labels:
  - dummy.enable=true
  - dummy.compose_file=/compose/docker-compose.yml
  - dummy.env_var=PROWLARR_VER
```

---

## All supported labels

| Label | Example value | Description |
|---|---|---|
| `dummy.enable` | `true` | **Required.** Opt this container in to monitoring. |
| `dummy.compose_file` | `/compose/docker-compose.yml` | Path inside the DUMMY container to the compose file to edit. Enables Mode 2. |
| `dummy.compose_service` | `sonarr` | Service name in the compose file. Defaults to the container name. |
| `dummy.env_var` | `SONARR_VER` | Variable name in the `.env` file to update. Enables Mode 3. |
| `dummy.changelog` | `https://github.com/.../releases` | Override the changelog link shown in the UI. Many registries are auto-detected. |

---

## In-UI settings

Click the **⚙ Settings** button to adjust these without touching your compose file:

| Setting | Default | Description |
|---|---|---|
| Check interval | 6 hours | How often DUMMY polls registries for updates. Options: 1h / 2h / 6h / 12h / 24h. |
| History limit | 5 versions | How many past versions to store per container. |
| Pre-releases | Off | Include alpha / beta / rc / nightly / edge tags when checking for newer versions. |
| Auto-update | Off | Apply updates automatically without UI confirmation. |

Settings are persisted in the SQLite database and survive container restarts. They take precedence over environment variables, so you can set a sensible default via env and override it from the UI at any time.

---

## Environment variables

Settings that cannot be changed from the UI at runtime:

### Paths and ports

| Variable | Default | Description |
|---|---|---|
| `ENV_FILE_PATH` | `/env/.env` | Path inside the container to your `.env` file (Mode 3). |
| `DB_PATH` | `/data/versions.db` | SQLite database path. Mount `/data` to a host directory to persist. |
| `PORT` | `5000` | Port Flask listens on inside the container. |

### Notifications (ntfy)

| Variable | Default | Description |
|---|---|---|
| `NTFY_ENDPOINT` | _(disabled)_ | Base URL of your ntfy server, e.g. `http://ntfy:80`. Leave unset to disable. |
| `NTFY_TOPIC` | `DockerUpdate` | ntfy topic name. |
| `NTFY_TOKEN` | _(none)_ | Bearer token for authenticated ntfy instances. |
| `NTFY_CLICK_URL` | _(none)_ | URL embedded in the notification to open the DUMMY UI. |

### GitHub API

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | _(none)_ | Personal access token. Raises GHCR rate limit from 60 to 5000 req/hour. No scopes needed for public images. |

### Update behaviour (overrideable in UI)

These set the initial defaults. The UI settings panel takes precedence once saved.

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `21600` | Seconds between background update checks. |
| `ALLOW_PRERELEASE` | `false` | Include pre-release tags. |
| `AUTO_UPDATE` | `false` | Apply updates automatically. |
| `HEALTH_CHECK_TIMEOUT` | `60` | Seconds to wait for a container to become healthy before rolling back. |
| `HISTORY_LIMIT` | `5` | Past versions to store per container. |

### Misc

| Variable | Default | Description |
|---|---|---|
| `CHANGELOG_URLS` | _(none)_ | Pipe-separated `image-fragment=url` pairs to override auto-detected changelog links, e.g. `myapp=https://github.com/me/myapp/releases\|other=https://other.io`. |
| `WEB_TITLE` | `DUMMY` | Page title shown in the browser tab and header. |
| `TZ` | _(system)_ | Container timezone, e.g. `Pacific/Auckland`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

### Backward compatibility

| Variable | Example | Description |
|---|---|---|
| `VERSION_VARS` | `radarr=RADARR_VER,sonarr=SONARR_VER` | Legacy: comma-separated `container=ENV_VAR` pairs. Equivalent to `dummy.enable=true` + `dummy.env_var=X` on each service, without modifying the service definitions. |

---

## Volumes

| Mount | Required | Description |
|---|---|---|
| `/var/run/docker.sock` | Always | Docker socket for container inspection and management. |
| `/data` | **Strongly recommended** | Persistent SQLite database. Without this, version history and rollback data is lost on every container restart. DUMMY will show a warning in the UI if this is not a proper bind mount. |
| `/compose/docker-compose.yml` | Mode 2 only | The compose file DUMMY will read and edit. |
| `/env/.env` | Mode 3 only | The `.env` file DUMMY will read and edit. |

---

## Health-check gating

Every update and rollback waits for the container to become healthy before marking it as complete. If the container fails to reach a healthy state within `HEALTH_CHECK_TIMEOUT` seconds, DUMMY:

1. Automatically reverts to the previous version
2. Restarts the container on the old version
3. Sends a failure notification via ntfy (if configured)

The previous image is always retained locally so rollbacks are instant — no re-pull needed.

---

## Version history and rollback

DUMMY stores the last N versions per container (configurable). In the UI each container shows its history with a **↩ Restore** button on every past entry. You can roll back to any recorded version, not just the most recent.

### Exporting and importing history

Before migrating your stack or rebuilding your Pi, export a backup:

```bash
curl http://your-server:5000/api/history/export -o dummy-history.json
```

Or use the **↓ Export History** button in the UI. To restore after:

```bash
curl -X POST http://your-server:5000/api/history/import \
  -H "Content-Type: application/json" \
  -d @dummy-history.json
```

Re-importing is safe — duplicate entries are skipped.

---

## Registry support

DUMMY auto-detects the registry from the image name:

| Image prefix | Registry queried |
|---|---|
| `ghcr.io/<org>/<repo>` | GitHub Container Registry |
| `lscr.io/linuxserver/<repo>` | Docker Hub `linuxserver/<repo>` |
| `<org>/<repo>` or `docker.io/<org>/<repo>` | Docker Hub |
| Plain `<repo>` | Docker Hub official library |

---

## Auto-detected changelogs

DUMMY automatically links the changelog for common images. The following are recognised without any configuration:

`linuxserver/*` · `immich-app/immich` · `gethomepage/homepage` · `FlareSolverr/FlareSolverr` · `advplyr/audiobookshelf` · `AdguardTeam/AdGuardHome` · `binwiederhier/ntfy` · `Plex Media Server` · `qBittorrent` · `jellyfin/jellyfin` · `portainer/portainer`

Override or add extras using the `CHANGELOG_URLS` environment variable or the `dummy.changelog` label.

---

## API reference

| Method | Path | Body / Notes |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | `{"status":"ok","containers_monitored":N}` |
| `GET` | `/api/status` | Last check time, next check time, current interval |
| `GET` | `/api/containers` | JSON array of all monitored containers with current state |
| `POST` | `/api/check` | Trigger an immediate background update scan. Returns `{"job_id":"..."}` |
| `POST` | `/api/update` | `{"container":"name","tag":"1.2.3"}` — apply an update. Returns `{"job_id":"..."}` |
| `POST` | `/api/rollback` | `{"container":"name"}` (previous) or `{"container":"name","tag":"1.0.0"}` (specific). Returns `{"job_id":"..."}` |
| `GET` | `/api/jobs/<id>` | Poll a running job for live log output and completion status |
| `GET` | `/api/settings` | Current effective settings (DB overrides or env defaults) |
| `POST` | `/api/settings` | `{"check_interval":3600,"allow_prerelease":false,...}` — update settings |
| `GET` | `/api/history/export` | Download full version history as `dummy-history.json` |
| `POST` | `/api/history/import` | Restore history from a previously exported JSON file |
| `GET` | `/api/history/<container>` | Version history for a single container |

---

## License

MIT

