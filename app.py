"""
DUMMY - Docker Update Made Manageable, Yay!

Three update strategies, auto-detected from Docker labels:

  1. docker_api  (simplest) - pull new image, recreate container via Docker SDK.
                              No files needed. Just add dummy.enable=true to a service.

  2. compose     - edit the image tag in-place in the compose YAML file, then
                   run docker compose up -d <service> to apply.
                   Add dummy.enable=true + dummy.compose_file=/path/to/compose.yaml

  3. env_file    - update a version variable in a .env file, then restart the
                   container. The original DUMMY behaviour.
                   Add dummy.enable=true + dummy.env_var=MY_VAR_NAME

All three are discovered from Docker labels. VERSION_VARS env var still works
for backward compatibility (it maps to the env_file strategy).
"""

import os
import re
import time
import logging
import sqlite3
import subprocess
import threading
from datetime import datetime

import yaml
from flask import Flask, render_template_string, request, jsonify
import docker as dockerlib
import requests as req

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger("dummy")
if LOG_LEVEL != "DEBUG":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENV_FILE         = os.environ.get("ENV_FILE_PATH", "/env/.env")
DB_PATH          = os.environ.get("DB_PATH", "/data/versions.db")
PORT             = int(os.environ.get("PORT", "5000"))
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL", "21600"))
HEALTH_TIMEOUT   = int(os.environ.get("HEALTH_CHECK_TIMEOUT", "60"))
HISTORY_LIMIT    = int(os.environ.get("HISTORY_LIMIT", "5"))
ALLOW_PRERELEASE = os.environ.get("ALLOW_PRERELEASE", "false").lower() == "true"
AUTO_UPDATE      = os.environ.get("AUTO_UPDATE", "false").lower() == "true"
WEB_TITLE        = os.environ.get("WEB_TITLE", "DUMMY")

NTFY_ENDPOINT    = os.environ.get("NTFY_ENDPOINT", "").rstrip("/")
NTFY_TOPIC       = os.environ.get("NTFY_TOPIC", "DockerUpdate")
NTFY_TOKEN       = os.environ.get("NTFY_TOKEN", "")
NTFY_CLICK_URL   = os.environ.get("NTFY_CLICK_URL", "")

GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")

# Backward-compat: VERSION_VARS => env_file strategy
VERSION_MAP: dict = {}
for pair in os.environ.get("VERSION_VARS", "").split(","):
    if "=" in pair:
        k, v = pair.strip().split("=", 1)
        VERSION_MAP[k.strip()] = v.strip()

# CHANGELOG_URLS overrides
CHANGELOGS: dict = {}
for pair in os.environ.get("CHANGELOG_URLS", "").split("|"):
    if "=" in pair:
        k, v = pair.strip().split("=", 1)
        CHANGELOGS[k.strip()] = v.strip()

# ---------------------------------------------------------------------------
# Strategy constants
# ---------------------------------------------------------------------------
STRATEGY_DOCKER_API = "docker_api"
STRATEGY_COMPOSE    = "compose"
STRATEGY_ENV_FILE   = "env_file"

STRATEGY_LABELS = {
    STRATEGY_DOCKER_API: "Docker API",
    STRATEGY_COMPOSE:    "Compose file",
    STRATEGY_ENV_FILE:   "Env file",
}

# ---------------------------------------------------------------------------
# Tag filtering
# ---------------------------------------------------------------------------
_DEV_KEYWORDS = [
    "alpha", "beta", "rc", "nightly", "edge", "dev", "test",
    "snapshot", "experimental", "-b.", ".b.",
]
_ARCH_KEYWORDS = [
    "arm64v8", "amd64", "armhf", "arm32v7", "i386", "ppc64le",
    "s390x", "linux-", "-arm64", "-armv7", "-armv6", "-aarch64",
]


def is_stable_tag(tag: str) -> bool:
    if ALLOW_PRERELEASE:
        return True
    tl = tag.lower()
    if any(kw in tl for kw in _DEV_KEYWORDS):
        return False
    if any(arch in tl for arch in _ARCH_KEYWORDS):
        return False
    if re.search(r"-ls\d+", tl):
        return False
    if re.search(r"-lt\d+-", tl):
        return False
    return True


def version_tuple(tag: str) -> tuple:
    cleaned = tag.replace("version-", "").lstrip("vV")
    numbers = re.findall(r"\d+", cleaned)
    return tuple(int(n) for n in numbers) if numbers else (0,)


def is_newer(current: str, candidate: str) -> bool:
    try:
        return version_tuple(candidate) > version_tuple(current)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Registry queries
# ---------------------------------------------------------------------------

def _ghcr_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def query_dockerhub(org: str, repo: str, current_tag: str):
    url = f"https://hub.docker.com/v2/repositories/{org}/{repo}/tags?page_size=50"
    try:
        resp = req.get(url, timeout=10)
        if resp.status_code != 200:
            log.warning("DockerHub %s/%s HTTP %s", org, repo, resp.status_code)
            return None
        tags = [t["name"] for t in resp.json().get("results", []) if is_stable_tag(t["name"])]
        newest = None
        for tag in tags:
            if is_newer(current_tag, tag):
                if newest is None or is_newer(newest, tag):
                    newest = tag
        return newest
    except Exception as exc:
        log.error("query_dockerhub %s/%s: %s", org, repo, exc)
        return None


def query_ghcr(org: str, repo: str, current_tag: str):
    url = f"https://api.github.com/orgs/{org}/packages/container/{repo}/versions?per_page=30"
    try:
        resp = req.get(url, headers=_ghcr_headers(), timeout=10)
        if resp.status_code == 401:
            url = f"https://api.github.com/users/{org}/packages/container/{repo}/versions?per_page=30"
            resp = req.get(url, headers=_ghcr_headers(), timeout=10)
        if resp.status_code != 200:
            log.warning("GHCR %s/%s HTTP %s", org, repo, resp.status_code)
            return None
        newest = None
        for version in resp.json():
            tags = version.get("metadata", {}).get("container", {}).get("tags", [])
            for tag in tags:
                if tag.startswith("sha") or not is_stable_tag(tag):
                    continue
                if is_newer(current_tag, tag):
                    if newest is None or is_newer(newest, tag):
                        newest = tag
        return newest
    except Exception as exc:
        log.error("query_ghcr %s/%s: %s", org, repo, exc)
        return None


def get_latest_tag(image: str, current_tag: str):
    try:
        img = image.strip()
        if img.startswith("ghcr.io/"):
            parts = img[len("ghcr.io/"):].split("/")
            if len(parts) >= 2:
                return query_ghcr(parts[0], parts[1], current_tag)
        if img.startswith("lscr.io/linuxserver/"):
            return query_dockerhub("linuxserver", img.split("/")[-1], current_tag)
        for prefix in ("docker.io/", "index.docker.io/"):
            if img.startswith(prefix):
                img = img[len(prefix):]
                break
        parts = img.split("/")
        if len(parts) == 1:
            return query_dockerhub("library", parts[0], current_tag)
        elif len(parts) == 2:
            return query_dockerhub(parts[0], parts[1], current_tag)
        else:
            return query_dockerhub(parts[-2], parts[-1], current_tag)
    except Exception as exc:
        log.error("get_latest_tag %s: %s", image, exc)
        return None


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------
_KNOWN_CHANGELOGS = {
    "lscr.io/linuxserver":  "https://github.com/linuxserver/{repo}/releases",
    "ghcr.io/immich-app":   "https://github.com/immich-app/immich/releases",
    "ghcr.io/gethomepage":  "https://github.com/gethomepage/homepage/releases",
    "ghcr.io/flaresolverr": "https://github.com/FlareSolverr/FlareSolverr/releases",
    "ghcr.io/advplyr":      "https://github.com/advplyr/audiobookshelf/releases",
    "adguard/adguardhome":  "https://github.com/AdguardTeam/AdGuardHome/releases",
    "binwiederhier/ntfy":   "https://github.com/binwiederhier/ntfy/releases",
    "plexinc/pms-docker":   "https://forums.plex.tv/t/plex-media-server/30447",
    "qbittorrentofficial":  "https://github.com/qbittorrent/qBittorrent/releases",
    "jellyfin/jellyfin":    "https://github.com/jellyfin/jellyfin/releases",
    "portainer/portainer":  "https://github.com/portainer/portainer/releases",
}


def get_changelog(image: str, label_override: str = None):
    if label_override:
        return label_override
    image_lower = image.lower()
    for frag, url in CHANGELOGS.items():
        if frag.lower() in image_lower:
            return url
    for pattern, url_template in _KNOWN_CHANGELOGS.items():
        if pattern in image_lower:
            return url_template.format(repo=image.split("/")[-1])
    return None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS version_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        container TEXT, tag TEXT, deployed_at TEXT, status TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS available_updates (
        container TEXT PRIMARY KEY, available_tag TEXT, checked_at TEXT)""")
    conn.commit()
    conn.close()


def add_to_history(container: str, tag: str, status: str = "deployed"):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO version_history (container,tag,deployed_at,status) VALUES (?,?,?,?)",
                  (container, tag, datetime.now().isoformat(), status))
        c.execute("""DELETE FROM version_history WHERE id NOT IN (
            SELECT id FROM version_history WHERE container=? ORDER BY deployed_at DESC LIMIT ?)""",
                  (container, HISTORY_LIMIT))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("add_to_history: %s", exc)


def get_history(container: str) -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT tag,deployed_at,status FROM version_history WHERE container=? ORDER BY deployed_at DESC LIMIT ?",
                  (container, HISTORY_LIMIT))
        rows = c.fetchall()
        conn.close()
        return [{"tag": r[0], "date": r[1], "status": r[2]} for r in rows]
    except Exception:
        return []


def save_available_update(container: str, tag: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO available_updates VALUES (?,?,?)",
                  (container, tag, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("save_available_update: %s", exc)


def clear_available_update(container: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM available_updates WHERE container=?", (container,))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("clear_available_update: %s", exc)


def get_available_update(container: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT available_tag FROM available_updates WHERE container=?", (container,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def read_env() -> dict:
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except Exception as exc:
        log.error("read_env: %s", exc)
    return env


def write_env_var(var: str, value: str) -> bool:
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{var}="):
                lines[i] = f"{var}={value}\n"
                with open(ENV_FILE, "w") as f:
                    f.writelines(lines)
                return True
        log.warning("write_env_var: %s not found in %s", var, ENV_FILE)
    except Exception as exc:
        log.error("write_env_var: %s", exc)
    return False


# ---------------------------------------------------------------------------
# Compose file helpers
# ---------------------------------------------------------------------------

def read_compose(path: str):
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception as exc:
        log.error("read_compose %s: %s", path, exc)
        return None


def write_compose(path: str, data: dict) -> bool:
    try:
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception as exc:
        log.error("write_compose %s: %s", path, exc)
        return False


def get_compose_image_tag(compose_path: str, service_name: str):
    data = read_compose(compose_path)
    if not data:
        return None
    image = data.get("services", {}).get(service_name, {}).get("image", "")
    return image.rsplit(":", 1)[1] if ":" in image else None


def set_compose_image_tag(compose_path: str, service_name: str, new_tag: str):
    """Returns (success, old_tag)."""
    data = read_compose(compose_path)
    if not data:
        return False, ""
    service = data.get("services", {}).get(service_name)
    if not service:
        return False, ""
    old_image = service.get("image", "")
    base, old_tag = old_image.rsplit(":", 1) if ":" in old_image else (old_image, "latest")
    service["image"] = f"{base}:{new_tag}"
    return write_compose(compose_path, data), old_tag


def run_compose_up(compose_path: str, service_name: str):
    """Returns (success, error_string)."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d", "--no-deps", service_name],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip() or result.stdout.strip()
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Docker API recreate helpers
# ---------------------------------------------------------------------------

def get_image_parts(container_obj):
    tags = container_obj.image.tags
    if tags:
        img = tags[0]
        base, tag = img.rsplit(":", 1) if ":" in img else (img, "latest")
        return base, tag
    try:
        repo_tags = container_obj.image.attrs.get("RepoTags", [])
        if repo_tags:
            img = repo_tags[0]
            base, tag = img.rsplit(":", 1) if ":" in img else (img, "latest")
            return base, tag
    except Exception:
        pass
    return "unknown", "unknown"


def recreate_container(client, container_obj, new_image: str):
    """Pull new_image, stop+remove old container, recreate with same config. Returns (success, error)."""
    name     = container_obj.name
    attrs    = container_obj.attrs
    host_cfg = attrs.get("HostConfig", {})
    net_cfg  = attrs.get("NetworkSettings", {}).get("Networks", {})
    cfg      = attrs.get("Config", {})

    create_kwargs = {
        "name":           name,
        "image":          new_image,
        "environment":    cfg.get("Env") or [],
        "volumes":        host_cfg.get("Binds") or [],
        "labels":         cfg.get("Labels") or {},
        "restart_policy": host_cfg.get("RestartPolicy") or {"Name": "unless-stopped"},
        "network_mode":   host_cfg.get("NetworkMode", "bridge"),
        "hostname":       cfg.get("Hostname"),
        "entrypoint":     cfg.get("Entrypoint"),
        "command":        cfg.get("Cmd"),
        "working_dir":    cfg.get("WorkingDir"),
        "user":           cfg.get("User"),
        "tty":            cfg.get("Tty", False),
        "stdin_open":     cfg.get("OpenStdin", False),
        "cap_add":        host_cfg.get("CapAdd"),
        "cap_drop":       host_cfg.get("CapDrop"),
        "devices":        host_cfg.get("Devices"),
        "privileged":     host_cfg.get("Privileged", False),
        "shm_size":       host_cfg.get("ShmSize"),
        "pid_mode":       host_cfg.get("PidMode"),
        "ports":          host_cfg.get("PortBindings") or {},
    }
    create_kwargs = {k: v for k, v in create_kwargs.items() if v is not None and v != {} and v != []}

    try:
        log.info("Pulling %s ...", new_image)
        client.images.pull(new_image)
    except Exception as exc:
        return False, f"Pull failed: {exc}"

    try:
        log.info("Stopping and removing %s ...", name)
        container_obj.stop(timeout=30)
        container_obj.remove()
    except Exception as exc:
        return False, f"Stop/remove failed: {exc}"

    try:
        log.info("Recreating %s ...", name)
        new_container = client.containers.create(**create_kwargs)

        default_net = create_kwargs.get("network_mode", "bridge")
        for net_name, net_data in net_cfg.items():
            if net_name == default_net:
                continue
            try:
                network = client.networks.get(net_name)
                aliases = [a for a in (net_data.get("Aliases") or []) if not re.match(r"^[0-9a-f]{12}$", a)]
                network.connect(new_container, aliases=aliases or None)
            except Exception as exc:
                log.warning("Could not attach %s to %s: %s", name, net_name, exc)

        new_container.start()
        return True, ""
    except Exception as exc:
        return False, f"Recreate failed: {exc}"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_container_health(container_name: str, timeout: int = None) -> bool:
    timeout = timeout or HEALTH_TIMEOUT
    try:
        client    = dockerlib.from_env()
        container = client.containers.get(container_name)
        deadline  = time.time() + timeout
        while time.time() < deadline:
            container.reload()
            if container.status == "running":
                health = container.attrs.get("State", {}).get("Health", {})
                if not health or health.get("Status") == "healthy":
                    return True
            time.sleep(2)
        return False
    except Exception as exc:
        log.error("check_container_health %s: %s", container_name, exc)
        return False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(title: str, body: str, priority: str = "3", tags: str = "package"):
    if not NTFY_ENDPOINT:
        return
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    if NTFY_CLICK_URL:
        headers["Click"] = NTFY_CLICK_URL
    try:
        req.post(f"{NTFY_ENDPOINT}/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers, timeout=5)
    except Exception as exc:
        log.warning("notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Container discovery & strategy resolution
# ---------------------------------------------------------------------------

def _get_dummy_labels(container_obj) -> dict:
    labels = container_obj.attrs.get("Config", {}).get("Labels") or {}
    return {k[len("dummy."):]: v for k, v in labels.items() if k.startswith("dummy.")}


def _resolve_strategy(container_name: str, dummy_labels: dict) -> dict:
    compose_file = dummy_labels.get("compose_file")
    env_var      = dummy_labels.get("env_var") or VERSION_MAP.get(container_name)
    compose_svc  = dummy_labels.get("compose_service", container_name)

    if compose_file:
        return {"strategy": STRATEGY_COMPOSE, "compose_file": compose_file, "compose_svc": compose_svc}
    if env_var:
        return {"strategy": STRATEGY_ENV_FILE, "env_var": env_var}
    return {"strategy": STRATEGY_DOCKER_API}


def get_monitored_containers() -> list:
    items = []
    try:
        client   = dockerlib.from_env()
        env_vars = read_env()

        for c in client.containers.list():
            dummy_labels   = _get_dummy_labels(c)
            in_version_map = c.name in VERSION_MAP

            if dummy_labels.get("enable", "").lower() != "true" and not in_version_map:
                continue

            image, tag   = get_image_parts(c)
            strategy_cfg = _resolve_strategy(c.name, dummy_labels)
            strategy     = strategy_cfg["strategy"]

            if strategy == STRATEGY_ENV_FILE:
                current_tag = env_vars.get(strategy_cfg["env_var"], tag)
            elif strategy == STRATEGY_COMPOSE:
                current_tag = get_compose_image_tag(strategy_cfg["compose_file"], strategy_cfg["compose_svc"]) or tag
            else:
                current_tag = tag

            available_tag = get_available_update(c.name)
            history       = get_history(c.name)

            items.append({
                "container":      c.name,
                "image":          image,
                "current_tag":    current_tag,
                "available_tag":  available_tag,
                "has_update":     bool(available_tag and available_tag != current_tag),
                "status":         c.status,
                "strategy":       strategy,
                "strategy_label": STRATEGY_LABELS[strategy],
                "changelog":      get_changelog(image, dummy_labels.get("changelog")),
                "history":        history,
                "_strategy_cfg":  strategy_cfg,
            })

    except Exception as exc:
        log.error("get_monitored_containers: %s", exc)

    return sorted(items, key=lambda x: (not x["has_update"], x["container"]))


# ---------------------------------------------------------------------------
# Update / rollback
# ---------------------------------------------------------------------------

def update_service(container: str, new_tag: str) -> dict:
    try:
        client        = dockerlib.from_env()
        container_obj = client.containers.get(container)
        image, _      = get_image_parts(container_obj)
        dummy_labels  = _get_dummy_labels(container_obj)
        strategy_cfg  = _resolve_strategy(container, dummy_labels)
        strategy      = strategy_cfg["strategy"]

        if image == "unknown":
            return {"success": False, "error": "Cannot determine image name"}

        # ── ENV FILE ──────────────────────────────────────────────────────
        if strategy == STRATEGY_ENV_FILE:
            var         = strategy_cfg["env_var"]
            current_tag = read_env().get(var, "unknown")
            add_to_history(container, current_tag, status="previous")

            if not write_env_var(var, new_tag):
                return {"success": False, "error": f"Failed to write {var} to {ENV_FILE}"}
            try:
                client.images.pull(image, tag=new_tag)
            except Exception as exc:
                write_env_var(var, current_tag)
                return {"success": False, "error": f"Pull failed: {exc}"}

            container_obj.restart()
            if not check_container_health(container):
                write_env_var(var, current_tag)
                container_obj.restart()
                check_container_health(container, timeout=30)
                notify(f"Update Failed: {container}", f"Rolled back to {current_tag}", priority="4", tags="warning")
                return {"success": False, "error": "Health check failed - auto-rolled back"}

            add_to_history(container, new_tag)
            clear_available_update(container)
            notify(f"Updated: {container}", f"checkmark {container}: {current_tag} to {new_tag}", tags="white_check_mark")
            return {"success": True, "message": f"Updated to {new_tag}"}

        # ── COMPOSE FILE ──────────────────────────────────────────────────
        elif strategy == STRATEGY_COMPOSE:
            compose_file = strategy_cfg["compose_file"]
            compose_svc  = strategy_cfg["compose_svc"]
            current_tag  = get_compose_image_tag(compose_file, compose_svc) or "unknown"
            add_to_history(container, current_tag, status="previous")

            ok, old_tag = set_compose_image_tag(compose_file, compose_svc, new_tag)
            if not ok:
                return {"success": False, "error": f"Failed to edit {compose_file}"}

            ok, err = run_compose_up(compose_file, compose_svc)
            if not ok:
                set_compose_image_tag(compose_file, compose_svc, old_tag)
                return {"success": False, "error": f"docker compose up failed: {err}"}

            if not check_container_health(container):
                set_compose_image_tag(compose_file, compose_svc, old_tag)
                run_compose_up(compose_file, compose_svc)
                notify(f"Update Failed: {container}", f"Rolled back to {old_tag}", priority="4", tags="warning")
                return {"success": False, "error": "Health check failed - auto-rolled back"}

            add_to_history(container, new_tag)
            clear_available_update(container)
            notify(f"Updated: {container}", f"{container}: {current_tag} to {new_tag}", tags="white_check_mark")
            return {"success": True, "message": f"Updated to {new_tag}"}

        # ── DOCKER API ────────────────────────────────────────────────────
        else:
            _, current_tag = get_image_parts(container_obj)
            add_to_history(container, current_tag, status="previous")

            ok, err = recreate_container(client, container_obj, f"{image}:{new_tag}")
            if not ok:
                return {"success": False, "error": err}

            if not check_container_health(container):
                try:
                    failed_obj = client.containers.get(container)
                    recreate_container(client, failed_obj, f"{image}:{current_tag}")
                except Exception:
                    pass
                notify(f"Update Failed: {container}", f"Rolled back to {current_tag}", priority="4", tags="warning")
                return {"success": False, "error": "Health check failed - auto-rolled back"}

            add_to_history(container, new_tag)
            clear_available_update(container)
            notify(f"Updated: {container}", f"{container}: {current_tag} to {new_tag}", tags="white_check_mark")
            return {"success": True, "message": f"Updated to {new_tag}"}

    except Exception as exc:
        log.error("update_service %s: %s", container, exc)
        return {"success": False, "error": str(exc)}


def rollback_service(container: str, target_tag: str = None) -> dict:
    try:
        history = get_history(container)
        if not history:
            return {"success": False, "error": "No version history available"}
        if not target_tag:
            candidates = [h for h in history if h["status"] in ("previous", "deployed", "rolled_back")]
            if len(candidates) < 2:
                return {"success": False, "error": "No previous version to roll back to"}
            target_tag = candidates[1]["tag"]
        result = update_service(container, target_tag)
        if result.get("success"):
            add_to_history(container, target_tag, status="rolled_back")
        return result
    except Exception as exc:
        log.error("rollback_service %s: %s", container, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Background checker
# ---------------------------------------------------------------------------

def _check_once():
    try:
        updates_found = []
        for item in get_monitored_containers():
            container   = item["container"]
            image       = item["image"]
            current_tag = item["current_tag"]
            if image == "unknown":
                continue
            latest = get_latest_tag(image, current_tag)
            if latest and latest != current_tag:
                log.info("Update available: %s  %s -> %s", container, current_tag, latest)
                save_available_update(container, latest)
                updates_found.append((container, current_tag, latest))
                if AUTO_UPDATE:
                    log.info("AUTO_UPDATE: %s -> %s for %s", current_tag, latest, container)
                    result = update_service(container, latest)
                    if not result["success"]:
                        log.error("Auto-update failed for %s: %s", container, result["error"])
        if updates_found and not AUTO_UPDATE:
            lines = "\n".join(f"- {n}: {c} -> {l}" for n, c, l in updates_found)
            notify(f"{len(updates_found)} update(s) available", f"Updates ready:\n{lines}")
    except Exception as exc:
        log.error("_check_once: %s", exc)


def check_for_updates():
    while True:
        log.info("Running update check ...")
        _check_once()
        time.sleep(CHECK_INTERVAL)


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);min-height:100vh;padding:24px}
.wrap{max-width:1100px;margin:0 auto}
.hdr{background:rgba(255,255,255,0.05);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:28px;margin-bottom:24px}
h1{color:#e2e8f0;font-size:1.8em;margin-bottom:4px}
.tagline{color:#64748b;font-size:.9em;margin-bottom:12px;font-style:italic}
.sub{color:#94a3b8}.sub span{color:#60a5fa;font-weight:600}
.stats{display:flex;gap:14px;margin-top:18px;flex-wrap:wrap}
.stat{background:rgba(255,255,255,0.07);padding:14px 20px;border-radius:10px;min-width:110px}
.stat-num{font-size:1.7em;font-weight:700;color:#818cf8}
.stat-lbl{color:#94a3b8;font-size:.85em;margin-top:2px}
.cfg-bar{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:10px 18px;margin-bottom:20px;display:flex;gap:18px;flex-wrap:wrap;font-size:.85em;color:#64748b}
.cfg-on{color:#34d399!important}.cfg-off{color:#f87171!important}
.refresh-row{display:flex;justify-content:flex-end;margin-bottom:16px}
.btn-refresh{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.15);color:#cbd5e1;padding:8px 18px;border-radius:8px;cursor:pointer;font-size:.9em;transition:all .2s}
.btn-refresh:hover{background:rgba(255,255,255,0.14)}
.card{background:rgba(255,255,255,0.05);backdrop-filter:blur(6px);border:1px solid rgba(255,255,255,0.09);border-radius:14px;padding:22px;margin-bottom:16px;transition:transform .2s}
.card:hover{transform:translateY(-2px)}.card.has-update{border-left:4px solid #f59e0b}
.card-row{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:16px}
.cname{font-size:1.25em;font-weight:700;color:#e2e8f0}
.badges{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.badge{padding:5px 11px;border-radius:20px;font-size:.75em;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.running{background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.stopped{background:rgba(248,113,113,.15);color:#f87171;border:1px solid rgba(248,113,113,.3)}
.badge-update{background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)}
.badge-strategy{background:rgba(99,102,241,.1);color:#a5b4fc;border:1px solid rgba(99,102,241,.2);font-size:.7em}
.info{background:rgba(0,0,0,.2);padding:14px 16px;border-radius:10px;margin-bottom:14px}
.info-row{display:flex;margin:7px 0;flex-wrap:wrap;align-items:center;gap:6px}
.lbl{font-weight:600;color:#64748b;min-width:120px;font-size:.88em}
.val{font-family:monospace;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.08);padding:3px 10px;border-radius:5px;font-size:.88em;color:#cbd5e1}
.val.new{background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.3);color:#fbbf24}
.history{margin-top:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,.08)}
.history-title{font-weight:600;color:#64748b;margin-bottom:8px;font-size:.85em;text-transform:uppercase;letter-spacing:.06em}
.h-item{font-size:.82em;color:#64748b;margin:4px 0;font-family:monospace;display:flex;gap:10px;flex-wrap:wrap}
.h-tag{color:#94a3b8}.h-date{color:#475569}
.h-status{font-size:.78em;padding:1px 7px;border-radius:9px}
.h-deployed{background:rgba(52,211,153,.1);color:#34d399}
.h-rolled_back{background:rgba(245,158,11,.1);color:#f59e0b}
.h-previous{background:rgba(100,116,139,.1);color:#64748b}
.btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;align-items:center}
.btn{padding:9px 18px;border:none;border-radius:8px;font-weight:600;cursor:pointer;transition:all .2s;font-size:.9em}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 4px 15px rgba(99,102,241,.4)}
.btn-danger{background:rgba(239,68,68,.8);color:#fff}
.btn-danger:hover{background:rgba(239,68,68,1)}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none!important}
.link-btn{display:inline-flex;align-items:center;gap:5px;padding:8px 14px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:#94a3b8;text-decoration:none;border-radius:7px;font-size:.87em;transition:all .2s}
.link-btn:hover{background:rgba(255,255,255,.12);color:#e2e8f0}
.msg{padding:11px 14px;border-radius:8px;margin:10px 0;display:none;font-size:.9em}
.msg-success{background:rgba(52,211,153,.15);border:1px solid rgba(52,211,153,.3);color:#34d399}
.msg-error{background:rgba(248,113,113,.15);border:1px solid rgba(248,113,113,.3);color:#f87171}
.setup-box{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.25);border-radius:14px;padding:28px;text-align:center;color:#94a3b8}
.setup-box h2{color:#a5b4fc;margin-bottom:12px;font-size:1.2em}
.setup-box code{background:rgba(0,0,0,.3);padding:12px 16px;border-radius:8px;display:block;text-align:left;font-family:monospace;font-size:.88em;color:#cbd5e1;margin:12px 0;white-space:pre}
@media(max-width:600px){.stats{gap:8px}.stat{padding:10px 14px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>&#x1F433; {{ title }}</h1>
    <p class="tagline">Docker Update Made Manageable, Yay!</p>
    <p class="sub">Monitoring <span>{{ containers|length }}</span> container(s) &nbsp;&middot;&nbsp; checks every <span>{{ check_interval_h }}</span></p>
    <div class="stats">
      <div class="stat"><div class="stat-num">{{ containers|length }}</div><div class="stat-lbl">Monitored</div></div>
      <div class="stat"><div class="stat-num">{{ updates_count }}</div><div class="stat-lbl">Updates Ready</div></div>
      <div class="stat"><div class="stat-num">{{ running_count }}</div><div class="stat-lbl">Running</div></div>
    </div>
  </div>
  <div class="cfg-bar">
    <span>Pre-releases: <span class="{{ 'cfg-on' if allow_prerelease else 'cfg-off' }}">{{ 'ON' if allow_prerelease else 'OFF' }}</span></span>
    <span>Auto-update: <span class="{{ 'cfg-on' if auto_update else 'cfg-off' }}">{{ 'ON' if auto_update else 'OFF' }}</span></span>
    <span>Notifications: <span class="{{ 'cfg-on' if ntfy_enabled else 'cfg-off' }}">{{ 'ON' if ntfy_enabled else 'OFF' }}</span></span>
    <span>History: <span>{{ history_limit }} versions</span></span>
  </div>
  <div class="refresh-row">
    <button class="btn-refresh" onclick="location.reload()">&#8635; Refresh</button>
  </div>
  {% if containers %}
    {% for u in containers %}
    <div class="card {% if u.has_update %}has-update{% endif %}">
      <div class="card-row">
        <span class="cname">{{ u.container }}</span>
        <div class="badges">
          <span class="badge {{ u.status }}">{{ u.status }}</span>
          <span class="badge badge-strategy">{{ u.strategy_label }}</span>
          {% if u.has_update %}<span class="badge badge-update">&#x2B06; Update Available</span>{% endif %}
        </div>
      </div>
      <div class="info">
        <div class="info-row"><span class="lbl">Image</span><span class="val">{{ u.image }}</span></div>
        <div class="info-row"><span class="lbl">Current</span><span class="val">{{ u.current_tag }}</span></div>
        {% if u.available_tag %}<div class="info-row"><span class="lbl">Available</span><span class="val new">{{ u.available_tag }}</span></div>{% endif %}
        {% if u.history %}
        <div class="history">
          <div class="history-title">Version History</div>
          {% for h in u.history %}
          <div class="h-item">
            <span class="h-tag">{{ h.tag }}</span>
            <span class="h-date">{{ h.date[:10] }}</span>
            <span class="h-status h-{{ h.status }}">{{ h.status }}</span>
          </div>
          {% endfor %}
        </div>
        {% endif %}
      </div>
      <div class="msg msg-success" id="success-{{ u.container }}"></div>
      <div class="msg msg-error" id="error-{{ u.container }}"></div>
      <div class="btns">
        {% if u.has_update and not auto_update %}
        <button class="btn btn-primary" onclick="updateService('{{ u.container }}','{{ u.available_tag }}')" id="btn-update-{{ u.container }}">
          &#x2B06; Update to {{ u.available_tag }}
        </button>
        {% endif %}
        {% if u.history|length > 1 %}
        <button class="btn btn-danger" onclick="rollbackService('{{ u.container }}')" id="btn-rollback-{{ u.container }}">
          &#x21A9; Rollback to {{ u.history[1].tag }}
        </button>
        {% endif %}
        {% if u.changelog %}<a class="link-btn" href="{{ u.changelog }}" target="_blank" rel="noopener">&#x1F4DD; Changelog</a>{% endif %}
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="setup-box">
      <h2>No containers being monitored yet</h2>
      <p>Add the <strong>dummy.enable=true</strong> label to any service you want DUMMY to track. That's it.</p>
      <code>services:
  radarr:
    image: lscr.io/linuxserver/radarr:5.2.1
    labels:
      - dummy.enable=true     # minimum required

      # Optional: also update the image tag in your compose file
      - dummy.compose_file=/compose/docker-compose.yml

      # Optional: also update a variable in a .env file
      - dummy.env_var=RADARR_VER

      # Optional: override the changelog link
      - dummy.changelog=https://github.com/Radarr/Radarr/releases</code>
      <p style="margin-top:12px;font-size:.9em">See the README for full setup options.</p>
    </div>
  {% endif %}
</div>
<script>
function showMsg(c,t,x){
  const s=document.getElementById('success-'+c),e=document.getElementById('error-'+c);
  s.style.display=e.style.display='none';
  const el=t==='success'?s:e;el.textContent=x;el.style.display='block';
  setTimeout(()=>el.style.display='none',12000);
}
async function updateService(c,t){
  const b=document.getElementById('btn-update-'+c);
  b.disabled=true;b.textContent='Updating...';
  try{
    const r=await fetch('/api/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({container:c,tag:t})});
    const d=await r.json();
    if(d.success){showMsg(c,'success',d.message);setTimeout(()=>location.reload(),2500)}
    else{showMsg(c,'error',d.error);b.disabled=false;b.textContent='Update to '+t}
  }catch(err){showMsg(c,'error','Network error: '+err.message);b.disabled=false;b.textContent='Update to '+t}
}
async function rollbackService(c){
  if(!confirm('Roll back '+c+' to the previous version?'))return;
  const b=document.getElementById('btn-rollback-'+c);
  b.disabled=true;b.textContent='Rolling back...';
  try{
    const r=await fetch('/api/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({container:c})});
    const d=await r.json();
    if(d.success){showMsg(c,'success',d.message);setTimeout(()=>location.reload(),2500)}
    else{showMsg(c,'error',d.error);b.disabled=false;b.textContent='Rollback'}
  }catch(err){showMsg(c,'error','Network error: '+err.message);b.disabled=false;b.textContent='Rollback'}
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    containers    = get_monitored_containers()
    updates_count = sum(1 for c in containers if c["has_update"])
    running_count = sum(1 for c in containers if c["status"] == "running")
    interval_h    = f"{CHECK_INTERVAL // 3600}h" if CHECK_INTERVAL >= 3600 else f"{CHECK_INTERVAL // 60}m"
    for c in containers:
        c.pop("_strategy_cfg", None)
    return render_template_string(HTML, containers=containers, updates_count=updates_count,
        running_count=running_count, title=WEB_TITLE, check_interval_h=interval_h,
        allow_prerelease=ALLOW_PRERELEASE, auto_update=AUTO_UPDATE,
        ntfy_enabled=bool(NTFY_ENDPOINT), history_limit=HISTORY_LIMIT)


@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.json or {}
    container, tag = data.get("container"), data.get("tag")
    if not container or not tag:
        return jsonify({"success": False, "error": "Missing parameters"})
    return jsonify(update_service(container, tag))


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    data = request.json or {}
    container = data.get("container")
    if not container:
        return jsonify({"success": False, "error": "Missing container"})
    return jsonify(rollback_service(container, data.get("tag")))


@app.route("/api/containers")
def api_containers():
    items = get_monitored_containers()
    for c in items:
        c.pop("_strategy_cfg", None)
    return jsonify(items)


@app.route("/api/check", methods=["POST"])
def api_check():
    threading.Thread(target=_check_once, daemon=True).start()
    return jsonify({"success": True, "message": "Check triggered"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "containers_monitored": len(get_monitored_containers())})


if __name__ == "__main__":
    log.info("=== DUMMY - Docker Update Made Manageable, Yay! ===")
    log.info("Allow prerelease: %s | Auto-update: %s | Check interval: %ds",
             ALLOW_PRERELEASE, AUTO_UPDATE, CHECK_INTERVAL)
    init_db()
    threading.Thread(target=check_for_updates, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
