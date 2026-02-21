# 🐳 DUMMY – Docker Update Made Manageable, Yay!

A lightweight, self-hosted web UI for monitoring Docker container versions, checking for updates, and applying or rolling back changes — no matter how your stack is configured.

---

## Setup is one label

The minimum setup for any container is a single label:

```yaml
labels:
  - dummy.enable=true
```

That's it. DUMMY will detect the image, poll the registry for newer versions, and let you apply updates from the web UI. No env files, no compose file edits, no `VERSION_VARS` to configure.

---

## How it works: three strategies

DUMMY automatically picks an update strategy based on which labels you've added. You can mix and match per-container.

### Mode 1 — Docker API (default, simplest)

Just opt in. DUMMY pulls the new image tag and recreates the container with the exact same config (volumes, ports, networks, env, restart policy, etc.) using the Docker API.

```yaml
services:
  radarr:
    image: lscr.io/linuxserver/radarr:5.2.1
    labels:
      - dummy.enable=true
```

**Volumes required in DUMMY:** just `/var/run/docker.sock`

> ⚠️ If you later run `docker compose up` manually, Compose will revert the tag to whatever is in your compose file. Use Mode 2 or 3 if you want the files kept in sync too.

---

### Mode 2 — Compose file editing

DUMMY edits the image tag directly in your `docker-compose.yml` and runs `docker compose up -d <service>` to apply it. Your file always stays in sync.

```yaml
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:4.0.16.2944
    labels:
      - dummy.enable=true
      - dummy.compose_file=/compose/docker-compose.yml
      # - dummy.compose_service=sonarr   # optional: service name if different from container name
```

**Extra volume required in DUMMY:**
```yaml
volumes:
  - /path/to/your/docker-compose.yml:/compose/docker-compose.yml
```

---

### Mode 3 — Env file editing

DUMMY updates a version variable in a `.env` file, then restarts the container. Useful if you pin versions as env vars and reference them in your compose file like `image: radarr:${RADARR_VER}`.

```yaml
services:
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:${PROWLARR_VER}
    labels:
      - dummy.enable=true
      - dummy.env_var=PROWLARR_VER
```

**Extra volume required in DUMMY:**
```yaml
volumes:
  - /path/to/your/.env:/env/.env
```

---

### Combining modes

Modes 2 and 3 can be used together to keep both your compose file and `.env` in sync at the same time:

```yaml
labels:
  - dummy.enable=true
  - dummy.compose_file=/compose/docker-compose.yml
  - dummy.env_var=PROWLARR_VER
```

---

## All supported labels

| Label | Example | Description |
|---|---|---|
| `dummy.enable` | `true` | **Required.** Opt this container in to monitoring. |
| `dummy.compose_file` | `/compose/docker-compose.yml` | Path (inside the DUMMY container) to the compose file to edit. Enables Mode 2. |
| `dummy.compose_service` | `sonarr` | Service name in the compose file. Defaults to the container name. |
| `dummy.env_var` | `SONARR_VER` | Variable name in the `.env` file to update. Enables Mode 3. |
| `dummy.changelog` | `https://github.com/.../releases` | Override the changelog link shown in the UI. Many common images are detected automatically. |

---

## Quick start

```bash
docker pull donald-win/dummy:latest
```

Minimal `compose.yaml` entry for DUMMY itself:

```yaml
dummy:
  image: donald-win/dummy:latest
  container_name: dummy
  restart: unless-stopped
  ports:
    - "5000:5000"
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - /path/to/data/dummy:/data
    # Add these only if you're using Mode 2 or 3:
    # - /path/to/docker-compose.yml:/compose/docker-compose.yml
    # - /path/to/.env:/env/.env
  networks:
    - docker-net
```

Open `http://your-server:5000`.

---

## Environment variables

### Update behaviour

| Variable | Default | Description |
|---|---|---|
| `CHECK_INTERVAL` | `21600` | Seconds between background update checks (21600 = 6 hours). |
| `ALLOW_PRERELEASE` | `false` | Include alpha/beta/rc/nightly/edge tags. |
| `AUTO_UPDATE` | `false` | Automatically apply updates without UI confirmation. Containers will be restarted. |
| `HEALTH_CHECK_TIMEOUT` | `60` | Seconds to wait for a container to become healthy before rolling back. |
| `HISTORY_LIMIT` | `5` | Past versions to remember per container. |

### Paths

| Variable | Default | Description |
|---|---|---|
| `ENV_FILE_PATH` | `/env/.env` | Path inside the container to your `.env` file (Mode 3). |
| `DB_PATH` | `/data/versions.db` | Path to the SQLite database. Mount `/data` to persist across restarts. |

### Changelog overrides

| Variable | Example | Description |
|---|---|---|
| `CHANGELOG_URLS` | `myapp=https://github.com/me/myapp/releases\|other=https://other.io/changelog` | Pipe-separated `image-fragment=url` pairs. Many common images are detected automatically. |

### Notifications (ntfy)

| Variable | Default | Description |
|---|---|---|
| `NTFY_ENDPOINT` | _(disabled)_ | Base URL of your ntfy server. Leave unset to disable. |
| `NTFY_TOPIC` | `DockerUpdate` | ntfy topic name. |
| `NTFY_TOKEN` | _(none)_ | Bearer token for authenticated ntfy instances. |
| `NTFY_CLICK_URL` | _(none)_ | URL embedded in the notification — tap to open the UI. |

### GitHub API

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | _(none)_ | Personal access token. Raises GHCR rate limit from 60 to 5000 req/hour. No scopes needed for public images. |

### Backward compatibility

| Variable | Example | Description |
|---|---|---|
| `VERSION_VARS` | `radarr=RADARR_VER,sonarr=SONARR_VER` | Legacy mode: comma-separated `container=ENV_VAR` pairs. Equivalent to adding `dummy.enable=true` + `dummy.env_var=X` labels, without needing to touch your services. |

### Web UI & logging

| Variable | Default | Description |
|---|---|---|
| `WEB_TITLE` | `DUMMY` | Page title. |
| `PORT` | `5000` | Port Flask listens on inside the container. |
| `TZ` | _(system)_ | Timezone, e.g. `Pacific/Auckland`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

---

## Volumes

| Mount | Required | Description |
|---|---|---|
| `/var/run/docker.sock` | Always | Docker socket — needed to inspect and manage containers. |
| `/data` | Recommended | Persistent SQLite database for version history and update cache. |
| `/compose/docker-compose.yml` | Mode 2 only | The compose file DUMMY will edit. |
| `/env/.env` | Mode 3 only | The `.env` file DUMMY will edit. |

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/health` | Health check |
| `GET` | `/api/containers` | JSON list of all monitored containers |
| `POST` | `/api/update` | `{"container":"name","tag":"1.2.3"}` — apply an update |
| `POST` | `/api/rollback` | `{"container":"name"}` or `{"container":"name","tag":"1.0.0"}` — roll back |
| `POST` | `/api/check` | Trigger an immediate background update scan |

---

## Registry support

DUMMY auto-detects the registry from the image name:

- `ghcr.io/<org>/<repo>` → GitHub Container Registry
- `lscr.io/linuxserver/<repo>` → Docker Hub `linuxserver/<repo>`
- `<org>/<repo>` or `docker.io/<org>/<repo>` → Docker Hub
- Plain `<repo>` → Docker Hub official library

No extra config needed for any of these.

---

## Health-check gating

Every update and rollback is gated by a health check. If the container doesn't become healthy within `HEALTH_CHECK_TIMEOUT` seconds, DUMMY automatically reverts to the previous version and sends a notification. The previous image is always retained locally so rollbacks are instant.

---

## Building & publishing

```bash
docker build -t donald-win/dummy:latest .
docker tag donald-win/dummy:latest donald-win/dummy:1.0.0
docker push donald-win/dummy:latest
docker push donald-win/dummy:1.0.0
```

Multi-arch (amd64 + arm64 + armv7):
```bash
docker buildx build \
  --platform linux/amd64,linux/arm64,linux/arm/v7 \
  -t donald-win/dummy:latest \
  --push .
```

---

## License

MIT
