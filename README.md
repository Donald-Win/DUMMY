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

- Polls Docker Hub and GHCR for newer image versions on a schedule
- Shows a live dashboard — how many containers are monitored, running, and up to date
- Lets you apply updates and watch the pull / recreate / health-check happen in real time
- Rolls back automatically if a container fails its health check after an update
- Keeps a per-container version history so you can restore any previous version at any time
- Sends push notifications via ntfy when updates are found or applied

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

That's it. DUMMY will detect the image, poll the registry, and show available updates in the UI.

### 3. Open the UI

Navigate to `http://your-server:5000`

---

## Three update strategies

DUMMY picks a strategy automatically based on which labels you add. You can use a different strategy per container.

### Strategy 1 — Docker API (default)

No extra labels needed beyond `dummy.enable=true`. DUMMY recreates the container via the Docker SDK, preserving all volumes, ports, networks, environment variables, and restart policies.

```yaml
services:
  radarr:
    image: lscr.io/linuxserver/radarr:6.0.4.10291
    labels:
      - dummy.enable=true
```

> ⚠️ If you later run `docker compose up` manually, Compose will revert the tag to whatever is pinned in your compose file. Use Strategy 2 or 3 if you want the file kept in sync.

---

### Strategy 2 — Compose file

DUMMY edits the image tag in your `docker-compose.yml` and runs `docker compose up -d <service>`. Your file always reflects what's actually running.

```yaml
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:4.0.16.2944
    labels:
      - dummy.enable=true
      - dummy.compose_file=/compose/docker-compose.yml
      # - dummy.compose_service=sonarr   # only needed if the service name differs from container name
```

Add to DUMMY's volumes:
```yaml
- /path/to/your/docker-compose.yml:/compose/docker-compose.yml
```

---

### Strategy 3 — Env file

DUMMY updates a version variable in your `.env` file and restarts the container. Best when you manage versions as variables and reference them like `image: radarr:${RADARR_VER}`.

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

Strategies 2 and 3 can be combined to keep both your compose file and `.env` in sync:

```yaml
labels:
  - dummy.enable=true
  - dummy.compose_file=/compose/docker-compose.yml
  - dummy.env_var=PROWLARR_VER
```

---

## Full example compose.yml

A realistic multi-service setup using all three strategies:

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
      - /opt/stacks/.env:/env/.env                         # needed for Strategy 3
      - /opt/stacks/docker-compose.yml:/compose/docker-compose.yml  # needed for Strategy 2
    environment:
      TZ: Pacific/Auckland
      NTFY_ENDPOINT: http://ntfy:80                        # optional push notifications
      NTFY_TOPIC: DockerUpdate
      NTFY_CLICK_URL: http://update.yourdomain.com
    networks:
      - internal

  # Strategy 1 — Docker API (simplest, no extra labels)
  adguardhome:
    image: adguard/adguardhome:v0.107.72
    container_name: adguardhome
    restart: unless-stopped
    labels:
      - dummy.enable=true
    networks:
      - internal

  # Strategy 3 — Env file (version pinned in .env as RADARR_VER=6.0.4.10291)
  radarr:
    image: lscr.io/linuxserver/radarr:${RADARR_VER}
    container_name: radarr
    restart: unless-stopped
    labels:
      - dummy.enable=true
      - dummy.env_var=RADARR_VER
    networks:
      - internal

  # Strategy 2 — Compose file (DUMMY edits this file directly)
  homepage:
    image: ghcr.io/gethomepage/homepage:V1.10.1
    container_name: homepage
    restart: unless-stopped
    labels:
      - dummy.enable=true
      - dummy.compose_file=/compose/docker-compose.yml
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

---

## Settings

Click **⚙ Settings** in the UI to configure these without editing any files:

| Setting | Default | Description |
|---|---|---|
| Check interval | Every 6h | How often DUMMY polls registries. Options: 1h / 2h / 6h / 12h / 24h. |
| History limit | 5 versions | Past versions to store per container for rollback. |
| Pre-releases | Off | Include alpha / beta / rc / nightly tags. |
| Auto-update | Off | Apply updates automatically without confirmation. |

Settings are saved to the database and persist across restarts. They override environment variables, so you can set defaults via env and tune them from the UI without redeploying.

---

## Environment variables

### Required paths

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/data/versions.db` | SQLite database. Always mount `/data` to a host directory. |
| `ENV_FILE_PATH` | `/env/.env` | Your `.env` file inside the container (Strategy 3). |
| `PORT` | `5000` | Port Flask listens on. |

### Notifications (ntfy)

| Variable | Description |
|---|---|
| `NTFY_ENDPOINT` | Base URL of your ntfy server, e.g. `http://ntfy:80`. Leave unset to disable. |
| `NTFY_TOPIC` | Topic name. Default: `DockerUpdate`. |
| `NTFY_TOKEN` | Bearer token for authenticated ntfy instances. |
| `NTFY_CLICK_URL` | URL opened when tapping the notification. |

### Advanced

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | Personal access token. Raises GHCR rate limit from 60 → 5000 req/hour. No scopes needed for public images. |
| `HEALTH_CHECK_TIMEOUT` | `60` | Seconds to wait for a healthy container before rolling back. |
| `CHANGELOG_URLS` | — | Pipe-separated `image-fragment=url` pairs to add or override changelog links. Example: `myapp=https://github.com/me/myapp/releases` |
| `WEB_TITLE` | `DUMMY` | Page title shown in the browser tab. |
| `TZ` | system | Container timezone, e.g. `Pacific/Auckland`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

### Initial defaults (overrideable in the UI)

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `21600` | Seconds between checks (21600 = 6h). |
| `ALLOW_PRERELEASE` | `false` | Include pre-release tags. |
| `AUTO_UPDATE` | `false` | Apply updates automatically. |
| `HISTORY_LIMIT` | `5` | Versions stored per container. |

---

## Volumes

| Mount | When needed | Description |
|---|---|---|
| `/var/run/docker.sock` | Always | Docker socket. Required for all container inspection and management. |
| `/data` | Always | **Bind-mount this to a host path.** Stores version history, update cache, and settings. Data is lost on restart without this. |
| `/env/.env` | Strategy 3 | The `.env` file DUMMY will read and edit. |
| `/compose/docker-compose.yml` | Strategy 2 | The compose file DUMMY will read and edit. |

---

## How updates work

When you click **Update** (or DUMMY applies one automatically):

1. Pulls the new image
2. Stops and removes the old container
3. Recreates it with identical config — same volumes, ports, networks, env vars, restart policy, and capabilities
4. Waits up to `HEALTH_CHECK_TIMEOUT` seconds for the container to become healthy
5. If the health check fails → automatically reverts to the previous image, restarts, and sends a failure notification
6. If it passes → records the new version in history and sends a success notification

---

## Version history and rollback

Every container card shows its version history. Click **↩ Restore** next to any past entry to roll back to that exact version. The rollback goes through the same pull → recreate → health-check flow as a forward update.

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

Re-importing is safe — duplicates are skipped automatically.

---

## Supported registries

| Image format | Source |
|---|---|
| `ghcr.io/<org>/<repo>` | GitHub Container Registry |
| `lscr.io/linuxserver/<repo>` | Docker Hub (LinuxServer) |
| `<org>/<repo>` | Docker Hub |
| `<repo>` | Docker Hub official library |

GHCR images are queried via the GitHub Packages API first, with an automatic fallback to the Docker Registry v2 API for public images that don't require a token.

---

## Auto-detected changelogs

Changelog links are shown automatically for:

`linuxserver/*` · `immich-app/immich` · `gethomepage/homepage` · `FlareSolverr/FlareSolverr` · `advplyr/audiobookshelf` · `AdguardTeam/AdGuardHome` · `binwiederhier/ntfy` · `Plex Media Server` · `qBittorrent` · `jellyfin/jellyfin` · `portainer/portainer`

Add your own via the `CHANGELOG_URLS` environment variable or `dummy.changelog` label.

---

## API

All update, rollback, and check operations return a `job_id` immediately and run in the background. Poll `/api/jobs/<id>` to stream log lines and check completion.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check |
| `GET` | `/api/status` | Last/next check times and current interval |
| `GET` | `/api/containers` | All monitored containers and their current state |
| `POST` | `/api/check` | Trigger an immediate update scan |
| `POST` | `/api/update` | `{"container":"name","tag":"1.2.3"}` |
| `POST` | `/api/rollback` | `{"container":"name"}` or `{"container":"name","tag":"1.0.0"}` |
| `GET` | `/api/jobs/<id>` | Poll a running job for live log output |
| `GET` | `/api/settings` | Current settings |
| `POST` | `/api/settings` | Update settings |
| `GET` | `/api/history/export` | Download full history as JSON |
| `POST` | `/api/history/import` | Restore history from JSON |
| `GET` | `/api/history/<container>` | History for one container |

---

## License

MIT
