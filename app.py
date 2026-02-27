"""
DUMMY - Docker Update Made Manageable, Yay!

Three update strategies, auto-detected from Docker labels:

  1. docker_api  - pull + recreate via Docker SDK. Just add dummy.enable=true.
  2. compose     - edit the compose YAML + docker compose up -d.
                   Add dummy.enable=true + dummy.compose_file=/path/to/compose.yaml
  3. env_file    - update a .env variable + restart.
                   Add dummy.enable=true + dummy.env_var=MY_VAR_NAME
"""

import os
import re
import time
import uuid
import json
import logging
import sqlite3
import subprocess
import threading
from datetime import datetime

import yaml
from flask import Flask, render_template_string, request, jsonify, Response
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
# Job tracking (in-memory, for live progress reporting)
# ---------------------------------------------------------------------------
_jobs: dict = {}         # {job_id: {log, done, success, error, message, started}}
_jobs_lock = threading.Lock()

# Check timing
_last_check_time: float = 0.0
_next_check_time: float = 0.0


def _new_job() -> str:
    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {
            "log":     [],
            "done":    False,
            "success": False,
            "error":   "",
            "message": "",
            "started": time.time(),
        }
    # Prune jobs older than 10 minutes
    cutoff = time.time() - 600
    with _jobs_lock:
        stale = [k for k, v in _jobs.items() if v["started"] < cutoff and v["done"]]
        for k in stale:
            del _jobs[k]
    return job_id


def _jlog(job_id: str, msg: str, level: str = "info"):
    log.info("[job:%s] %s", job_id, msg)
    if job_id:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["log"].append({
                    "t":   datetime.now().strftime("%H:%M:%S"),
                    "msg": msg,
                    "lvl": level,
                })


def _job_done(job_id: str, success: bool, message: str = "", error: str = ""):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["done"]    = True
            _jobs[job_id]["success"] = success
            _jobs[job_id]["message"] = message
            _jobs[job_id]["error"]   = error


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
        if resp.status_code in (401, 404):
            # org endpoint failed — try user endpoint (e.g. FlareSolverr, advplyr)
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
    "lscr.io/linuxserver":  "https://github.com/linuxserver/docker-{repo}/releases",
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
    db_dir = os.path.dirname(DB_PATH)
    os.makedirs(db_dir, exist_ok=True)
    try:
        data_dev   = os.stat(db_dir).st_dev
        parent_dev = os.stat(os.path.dirname(db_dir.rstrip("/"))).st_dev
        if data_dev == parent_dev:
            log.warning("=" * 60)
            log.warning("PERSISTENCE WARNING: %s is not a bind-mounted host", db_dir)
            log.warning("directory. History and rollback data will be LOST on restart.")
            log.warning("Add to compose.yaml:  - /stacks/data/dummy:/data")
            log.warning("=" * 60)
        else:
            log.info("Persistence OK — %s is a bind-mounted volume.", db_dir)
    except Exception as exc:
        log.warning("Could not verify persistence of %s: %s", db_dir, exc)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS version_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        container TEXT, tag TEXT, deployed_at TEXT, status TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS available_updates (
        container TEXT PRIMARY KEY, available_tag TEXT, checked_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""")
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


# ---------------------------------------------------------------------------
# Settings (runtime config overrides stored in SQLite)
# ---------------------------------------------------------------------------

_SETTING_DEFAULTS = {
    "check_interval":  None,   # falls back to CHECK_INTERVAL env var
    "allow_prerelease": None,  # falls back to ALLOW_PRERELEASE env var
    "auto_update":     None,   # falls back to AUTO_UPDATE env var
    "history_limit":   None,   # falls back to HISTORY_LIMIT env var
}


def get_setting(key: str):
    """Return the DB-stored setting value, or None if not set."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def save_setting(key: str, value: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        log.error("save_setting %s: %s", key, exc)
        return False


def get_all_settings() -> dict:
    """Return all current effective settings (DB override or env default)."""
    def _bool(key, env_default):
        v = get_setting(key)
        if v is None: return env_default
        return v == "true"

    def _int(key, env_default):
        v = get_setting(key)
        return int(v) if v else env_default

    return {
        "check_interval":   _int("check_interval", CHECK_INTERVAL),
        "allow_prerelease": _bool("allow_prerelease", ALLOW_PRERELEASE),
        "auto_update":      _bool("auto_update", AUTO_UPDATE),
        "history_limit":    _int("history_limit", HISTORY_LIMIT),
    }


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
# Docker API recreate
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


def recreate_container(client, container_obj, new_image: str, jlog=None):
    """Pull new_image, stop+remove old container, recreate with same config."""
    def _log(msg):
        log.info(msg)
        if jlog:
            jlog(msg)

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

    _log(f"Pulling {new_image}...")
    try:
        client.images.pull(new_image)
    except Exception as exc:
        return False, f"Pull failed: {exc}"

    _log(f"Stopping {name}...")
    try:
        container_obj.stop(timeout=30)
        _log(f"Removing {name}...")
        container_obj.remove()
    except Exception as exc:
        return False, f"Stop/remove failed: {exc}"

    _log(f"Recreating {name}...")
    try:
        new_container = client.containers.create(**create_kwargs)
        default_net = create_kwargs.get("network_mode", "bridge")
        for net_name, net_data in net_cfg.items():
            if net_name == default_net:
                continue
            try:
                network = client.networks.get(net_name)
                aliases = [a for a in (net_data.get("Aliases") or []) if not re.match(r"^[0-9a-f]{12}$", a)]
                network.connect(new_container, aliases=aliases or None)
                _log(f"Attached to network: {net_name}")
            except Exception as exc:
                log.warning("Could not attach %s to %s: %s", name, net_name, exc)
        _log(f"Starting {name}...")
        new_container.start()
        return True, ""
    except Exception as exc:
        return False, f"Recreate failed: {exc}"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_container_health(container_name: str, timeout: int = None, jlog=None) -> bool:
    timeout = timeout or HEALTH_TIMEOUT
    if jlog:
        jlog(f"Health check — waiting up to {timeout}s...")
    try:
        client    = dockerlib.from_env()
        container = client.containers.get(container_name)
        deadline  = time.time() + timeout
        while time.time() < deadline:
            container.reload()
            if container.status == "running":
                health = container.attrs.get("State", {}).get("Health", {})
                if not health or health.get("Status") == "healthy":
                    if jlog:
                        jlog("Health check passed ✓")
                    return True
            time.sleep(2)
        if jlog:
            jlog("Health check timed out ✗", "error")
        return False
    except Exception as exc:
        log.error("check_container_health %s: %s", container_name, exc)
        if jlog:
            jlog(f"Health check error: {exc}", "error")
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

def update_service(container: str, new_tag: str, job_id: str = None) -> dict:
    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    try:
        client        = dockerlib.from_env()
        container_obj = client.containers.get(container)
        image, _      = get_image_parts(container_obj)
        dummy_labels  = _get_dummy_labels(container_obj)
        strategy_cfg  = _resolve_strategy(container, dummy_labels)
        strategy      = strategy_cfg["strategy"]

        if image == "unknown":
            return {"success": False, "error": "Cannot determine image name"}

        jl(f"Container:  {container}")
        jl(f"Strategy:   {STRATEGY_LABELS[strategy]}")
        jl(f"Target tag: {new_tag}")

        # ── ENV FILE ──────────────────────────────────────────────────────
        if strategy == STRATEGY_ENV_FILE:
            var         = strategy_cfg["env_var"]
            current_tag = read_env().get(var, "unknown")
            jl(f"Current:    {current_tag}  ({var})")
            add_to_history(container, current_tag, status="previous")

            jl(f"Writing {var}={new_tag} to .env...")
            if not write_env_var(var, new_tag):
                return {"success": False, "error": f"Failed to write {var} to {ENV_FILE}"}

            jl(f"Pulling {image}:{new_tag}...")
            try:
                client.images.pull(image, tag=new_tag)
            except Exception as exc:
                write_env_var(var, current_tag)
                return {"success": False, "error": f"Pull failed: {exc}"}

            jl("Restarting container...")
            container_obj.restart()

            if not check_container_health(container, jlog=jl):
                jl(f"Rolling back — restoring {var}={current_tag}", "warn")
                write_env_var(var, current_tag)
                container_obj.restart()
                check_container_health(container, timeout=30)
                notify(f"Update Failed: {container}", f"Rolled back to {current_tag}", priority="4", tags="warning")
                return {"success": False, "error": "Health check failed — auto-rolled back"}

            add_to_history(container, new_tag)
            clear_available_update(container)
            jl(f"Done! {current_tag} → {new_tag}")
            notify(f"Updated: {container}", f"{container}: {current_tag} → {new_tag}", tags="white_check_mark")
            return {"success": True, "message": f"Updated to {new_tag}"}

        # ── COMPOSE FILE ──────────────────────────────────────────────────
        elif strategy == STRATEGY_COMPOSE:
            compose_file = strategy_cfg["compose_file"]
            compose_svc  = strategy_cfg["compose_svc"]
            current_tag  = get_compose_image_tag(compose_file, compose_svc) or "unknown"
            jl(f"Current:    {current_tag}")
            add_to_history(container, current_tag, status="previous")

            jl(f"Editing image tag in {compose_file}...")
            ok, old_tag = set_compose_image_tag(compose_file, compose_svc, new_tag)
            if not ok:
                return {"success": False, "error": f"Failed to edit {compose_file}"}

            jl(f"Running: docker compose up -d {compose_svc}")
            ok, err = run_compose_up(compose_file, compose_svc)
            if not ok:
                set_compose_image_tag(compose_file, compose_svc, old_tag)
                return {"success": False, "error": f"docker compose up failed: {err}"}

            if not check_container_health(container, jlog=jl):
                jl("Rolling back compose file...", "warn")
                set_compose_image_tag(compose_file, compose_svc, old_tag)
                run_compose_up(compose_file, compose_svc)
                notify(f"Update Failed: {container}", f"Rolled back to {old_tag}", priority="4", tags="warning")
                return {"success": False, "error": "Health check failed — auto-rolled back"}

            add_to_history(container, new_tag)
            clear_available_update(container)
            jl(f"Done! {current_tag} → {new_tag}")
            notify(f"Updated: {container}", f"{container}: {current_tag} → {new_tag}", tags="white_check_mark")
            return {"success": True, "message": f"Updated to {new_tag}"}

        # ── DOCKER API ────────────────────────────────────────────────────
        else:
            _, current_tag = get_image_parts(container_obj)
            jl(f"Current:    {current_tag}")
            add_to_history(container, current_tag, status="previous")

            ok, err = recreate_container(client, container_obj, f"{image}:{new_tag}",
                                         jlog=lambda m: jl(m))
            if not ok:
                return {"success": False, "error": err}

            if not check_container_health(container, jlog=jl):
                jl("Rolling back to previous image...", "warn")
                try:
                    failed_obj = client.containers.get(container)
                    recreate_container(client, failed_obj, f"{image}:{current_tag}",
                                       jlog=lambda m: jl(m))
                except Exception:
                    pass
                notify(f"Update Failed: {container}", f"Rolled back to {current_tag}", priority="4", tags="warning")
                return {"success": False, "error": "Health check failed — auto-rolled back"}

            add_to_history(container, new_tag)
            clear_available_update(container)
            jl(f"Done! {current_tag} → {new_tag}")
            notify(f"Updated: {container}", f"{container}: {current_tag} → {new_tag}", tags="white_check_mark")
            return {"success": True, "message": f"Updated to {new_tag}"}

    except Exception as exc:
        log.error("update_service %s: %s", container, exc)
        return {"success": False, "error": str(exc)}


def rollback_service(container: str, target_tag: str = None, job_id: str = None) -> dict:
    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    try:
        history = get_history(container)
        if not history:
            return {"success": False, "error": "No version history available"}

        if not target_tag:
            candidates = [h for h in history if h["status"] in ("previous", "deployed", "rolled_back")]
            if len(candidates) < 2:
                return {"success": False, "error": "No previous version to roll back to"}
            target_tag = candidates[1]["tag"]

        jl(f"Rolling back {container} to {target_tag}")
        result = update_service(container, target_tag, job_id=job_id)
        if result.get("success"):
            add_to_history(container, target_tag, status="rolled_back")
        return result

    except Exception as exc:
        log.error("rollback_service %s: %s", container, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Background checker
# ---------------------------------------------------------------------------

def _check_once(job_id: str = None):
    global _last_check_time
    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    cfg = get_all_settings()
    try:
        containers  = get_monitored_containers()
        updates_found = []
        jl(f"Checking {len(containers)} container(s)...")

        for item in containers:
            container   = item["container"]
            image       = item["image"]
            current_tag = item["current_tag"]
            if image == "unknown":
                continue
            jl(f"Checking {container} ({current_tag})...")
            latest = get_latest_tag(image, current_tag)
            if latest and latest != current_tag:
                jl(f"  → Update available: {latest}")
                save_available_update(container, latest)
                updates_found.append((container, current_tag, latest))
                if cfg["auto_update"]:
                    jl(f"  → AUTO_UPDATE: applying {latest}...")
                    result = update_service(container, latest, job_id=job_id)
                    if not result["success"]:
                        jl(f"  → Auto-update failed: {result['error']}", "error")
            else:
                jl(f"  → Up to date")

        _last_check_time = time.time()

        if updates_found and not cfg["auto_update"]:
            lines = "\n".join(f"- {n}: {c} → {l}" for n, c, l in updates_found)
            notify(f"{len(updates_found)} update(s) available", f"Updates ready:\n{lines}")
            jl(f"Found {len(updates_found)} update(s). Notifications sent.")
        elif not updates_found:
            jl("All containers are up to date.")

    except Exception as exc:
        log.error("_check_once: %s", exc)
        jl(f"Check failed: {exc}", "error")


def check_for_updates():
    global _next_check_time
    while True:
        log.info("Running update check...")
        _check_once()
        interval = get_all_settings()["check_interval"]
        _next_check_time = time.time() + interval
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Persistence check
# ---------------------------------------------------------------------------

def _check_persistence() -> bool:
    try:
        db_dir     = os.path.dirname(DB_PATH)
        data_dev   = os.stat(db_dir).st_dev
        parent_dev = os.stat(os.path.dirname(db_dir.rstrip("/"))).st_dev
        return data_dev != parent_dev
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Flask app + HTML template
# ---------------------------------------------------------------------------
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
/* ── Theme variables ─────────────────────────────────────────────────────── */
:root {
  --bg:          linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);
  --surface:     rgba(255,255,255,0.05);
  --surface-2:   rgba(255,255,255,0.08);
  --border:      rgba(255,255,255,0.09);
  --border-2:    rgba(255,255,255,0.15);
  --text-hi:     #e2e8f0;
  --text-md:     #94a3b8;
  --text-lo:     #64748b;
  --accent:      #818cf8;
  --accent-2:    #6366f1;
  --val-bg:      rgba(255,255,255,0.06);
  --val-border:  rgba(255,255,255,0.08);
  --val-text:    #cbd5e1;
  --code-bg:     rgba(0,0,0,0.3);
  --shadow:      rgba(0,0,0,0.4);
}
[data-theme="light"] {
  --bg:          linear-gradient(135deg,#f0f4ff 0%,#e8f0fe 50%,#eef7ff 100%);
  --surface:     rgba(255,255,255,0.85);
  --surface-2:   rgba(255,255,255,0.95);
  --border:      rgba(0,0,0,0.08);
  --border-2:    rgba(0,0,0,0.15);
  --text-hi:     #1e293b;
  --text-md:     #475569;
  --text-lo:     #94a3b8;
  --accent:      #4f46e5;
  --accent-2:    #4338ca;
  --val-bg:      rgba(0,0,0,0.04);
  --val-border:  rgba(0,0,0,0.08);
  --val-text:    #334155;
  --code-bg:     rgba(0,0,0,0.06);
  --shadow:      rgba(0,0,0,0.12);
}

/* ── Reset + base ────────────────────────────────────────────────────────── */
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:var(--bg);min-height:100vh;padding:20px;
     color:var(--text-hi);transition:background .3s,color .3s}
.wrap{max-width:1100px;margin:0 auto}

/* ── Dashboard header ────────────────────────────────────────────────────── */
.dash{background:var(--surface);backdrop-filter:blur(12px);
      border:1px solid var(--border);border-radius:18px;
      padding:24px;margin-bottom:20px}
.dash-title-row{display:flex;justify-content:space-between;align-items:flex-start;
                margin-bottom:20px;flex-wrap:wrap;gap:10px}
.dash-title h1{color:var(--text-hi);font-size:1.7em;margin-bottom:2px}
.dash-title p{color:var(--text-lo);font-size:.88em;font-style:italic}
.dash-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

/* ── Stat grid ───────────────────────────────────────────────────────────── */
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat-card{background:var(--surface-2);border:1px solid var(--border);
           border-radius:12px;padding:16px 18px;text-align:center}
.stat-num{font-size:2em;font-weight:700;color:var(--accent);line-height:1}
.stat-lbl{color:var(--text-md);font-size:.82em;margin-top:5px}
.stat-card.has-updates .stat-num{color:#f59e0b}
.stat-card.all-good .stat-num{color:#34d399}

/* ── Dash info row ───────────────────────────────────────────────────────── */
.dash-info-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.dash-panel{background:var(--surface-2);border:1px solid var(--border);
            border-radius:12px;padding:16px}
.dash-panel-lbl{font-size:.75em;font-weight:700;text-transform:uppercase;
                letter-spacing:.07em;color:var(--text-lo);margin-bottom:10px}

/* ── Config flags ────────────────────────────────────────────────────────── */
.flags{display:flex;flex-wrap:wrap;gap:7px}
.flag{font-size:.78em;font-weight:600;padding:4px 10px;border-radius:20px}
.flag-on {background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.flag-off{background:rgba(100,116,139,.1);color:var(--text-md);border:1px solid rgba(100,116,139,.2)}
.flag-warn{background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)}
.flag-ok {background:rgba(52,211,153,.1);color:#34d399;border:1px solid rgba(52,211,153,.2)}

/* ── Timing panel ────────────────────────────────────────────────────────── */
.timing{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
.timing-row{display:flex;gap:6px;align-items:center;font-size:.85em;color:var(--text-md)}
.timing-row .t-icon{font-size:1em}
.timing-row .t-val{color:var(--text-hi);font-weight:600}
.action-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}

/* ── Warn bar ────────────────────────────────────────────────────────────── */
.warn-bar{background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);
          border-radius:10px;padding:10px 16px;margin-bottom:16px;
          color:#f59e0b;font-size:.87em}
.warn-bar code{background:var(--code-bg);padding:1px 6px;border-radius:4px;font-family:monospace}

/* ── Generic buttons ─────────────────────────────────────────────────────── */
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border:none;
     border-radius:8px;font-weight:600;cursor:pointer;font-size:.87em;
     transition:all .18s;text-decoration:none;white-space:nowrap}
.btn:disabled{opacity:.4;cursor:not-allowed;pointer-events:none}
.btn-ghost{background:var(--surface-2);border:1px solid var(--border-2);color:var(--text-md)}
.btn-ghost:hover{background:var(--surface);color:var(--text-hi)}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-primary:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 4px 14px rgba(99,102,241,.4)}
.btn-danger{background:rgba(239,68,68,.85);color:#fff}
.btn-danger:hover:not(:disabled){background:rgba(239,68,68,1)}
.btn-success{background:rgba(52,211,153,.2);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.btn-warn{background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)}
.btn-warn:hover:not(:disabled){background:rgba(245,158,11,.25)}
.btn-sm{padding:4px 10px;font-size:.78em;border-radius:6px}

/* ── Cards ───────────────────────────────────────────────────────────────── */
.card{background:var(--surface);border:1px solid var(--border);
      border-radius:14px;margin-bottom:10px;overflow:hidden;
      transition:border-color .2s,box-shadow .2s}
.card:hover{box-shadow:0 4px 20px var(--shadow)}
.card.has-update{border-left:3px solid #f59e0b}
.card.stopped-card{opacity:.7}

/* card header — always visible, no collapse */
.card-header{display:flex;justify-content:space-between;align-items:flex-start;
             padding:14px 18px;flex-wrap:nowrap;gap:12px}
.card-left{flex:1;min-width:0}
.cname{font-size:1.1em;font-weight:700;color:var(--text-hi);
       word-break:break-word;padding-top:1px}

/* badges stack vertically on the right, never wrap below the name */
.badges{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0}
.badge{padding:3px 9px;border-radius:20px;font-size:.72em;font-weight:700;
       text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.b-running {background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.b-stopped {background:rgba(248,113,113,.15);color:#f87171;border:1px solid rgba(248,113,113,.3)}
.b-update  {background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)}
.b-strategy{background:rgba(99,102,241,.1);color:#a5b4fc;border:1px solid rgba(99,102,241,.2);font-size:.68em}

/* card body — always visible */
.card-body{display:block}
.card-inner{padding:0 18px 18px}

/* info grid */
.info-block{background:rgba(0,0,0,.15);border-radius:10px;padding:12px 14px;margin-bottom:12px}
[data-theme="light"] .info-block{background:rgba(0,0,0,.04)}
.info-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:6px 0}
.lbl{font-weight:600;color:var(--text-lo);min-width:80px;font-size:.83em}
.val{font-family:monospace;background:var(--val-bg);border:1px solid var(--val-border);
     padding:2px 9px;border-radius:5px;font-size:.84em;color:var(--val-text)}
.val.available{background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.3);color:#fbbf24}

/* version history */
.history-block{margin-bottom:12px}
.history-lbl{font-size:.75em;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
             color:var(--text-lo);margin-bottom:8px}
.h-item{display:flex;align-items:center;gap:8px;padding:5px 0;
        border-bottom:1px solid var(--border);flex-wrap:wrap}
.h-item:last-child{border-bottom:none}
.h-tag{font-family:monospace;font-size:.82em;color:var(--text-md);flex:1;min-width:120px}
.h-date{font-size:.78em;color:var(--text-lo)}
.h-pill{font-size:.7em;padding:2px 8px;border-radius:10px;font-weight:600}
.h-deployed  {background:rgba(52,211,153,.1);color:#34d399}
.h-rolled_back{background:rgba(245,158,11,.1);color:#f59e0b}
.h-previous  {background:rgba(100,116,139,.1);color:var(--text-md)}

/* action buttons row */
.btns{display:flex;gap:8px;flex-wrap:wrap;align-items:center}

/* feedback message */
.msg{padding:10px 14px;border-radius:8px;margin-bottom:10px;display:none;font-size:.87em}
.msg-ok {background:rgba(52,211,153,.15);border:1px solid rgba(52,211,153,.3);color:#34d399}
.msg-err{background:rgba(248,113,113,.15);border:1px solid rgba(248,113,113,.3);color:#f87171}

/* ── Progress modal ───────────────────────────────────────────────────────── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);
         backdrop-filter:blur(4px);z-index:1000;
         display:flex;align-items:center;justify-content:center;
         opacity:0;pointer-events:none;transition:opacity .2s}
.overlay.visible{opacity:1;pointer-events:all}
.modal{background:var(--surface-2);border:1px solid var(--border-2);
       border-radius:16px;width:min(560px,95vw);
       display:flex;flex-direction:column;max-height:80vh;
       box-shadow:0 20px 60px var(--shadow)}
.modal-head{display:flex;align-items:center;gap:12px;padding:18px 20px;
            border-bottom:1px solid var(--border)}
.modal-icon{font-size:1.3em;flex-shrink:0}
.modal-title-text{font-weight:700;color:var(--text-hi);font-size:1.05em}
.modal-log{flex:1;overflow-y:auto;padding:14px 18px;font-family:monospace;
           font-size:.82em;line-height:1.6;min-height:200px;max-height:50vh}
.log-line{display:flex;gap:10px;margin:1px 0}
.log-t{color:var(--text-lo);flex-shrink:0}
.log-msg{color:var(--text-md)}
.log-msg.warn{color:#f59e0b}
.log-msg.error{color:#f87171}
.modal-foot{padding:14px 20px;border-top:1px solid var(--border);
            display:flex;justify-content:flex-end;gap:8px}

/* ── Spinner ─────────────────────────────────────────────────────────────── */
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{display:inline-block;width:18px;height:18px;
         border:2px solid var(--border-2);border-top-color:var(--accent);
         border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}

/* ── Setup box (empty state) ─────────────────────────────────────────────── */
.setup-box{background:rgba(99,102,241,.07);border:1px solid rgba(99,102,241,.2);
           border-radius:14px;padding:28px;text-align:center;color:var(--text-md)}
.setup-box h2{color:#a5b4fc;margin-bottom:10px}
.setup-box pre{background:var(--code-bg);padding:12px 16px;border-radius:8px;
               text-align:left;font-size:.85em;color:var(--val-text);
               margin:12px 0;overflow-x:auto}

/* ── Theme button — fixed so it never jumps ──────────────────────────────── */
#btn-theme{position:fixed;top:14px;right:14px;z-index:500;
           padding:6px 14px;font-size:.82em;
           box-shadow:0 2px 10px var(--shadow)}

/* ── Settings modal ──────────────────────────────────────────────────────── */
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:18px 20px}
.setting-row{display:flex;flex-direction:column;gap:6px}
.setting-lbl{font-size:.82em;font-weight:700;color:var(--text-md);text-transform:uppercase;letter-spacing:.05em}
.setting-desc{font-size:.76em;color:var(--text-lo);margin-top:-2px}
.setting-val select,.setting-val input[type=number]{
  background:var(--val-bg);border:1px solid var(--val-border);
  color:var(--text-hi);padding:7px 10px;border-radius:8px;font-size:.88em;width:100%;
  outline:none;transition:border-color .2s}
.setting-val select:focus,.setting-val input[type=number]:focus{border-color:var(--accent)}
.toggle-wrap{display:flex;align-items:center;gap:10px;margin-top:4px}
.toggle{position:relative;width:42px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:rgba(100,116,139,.3);
               border-radius:24px;cursor:pointer;transition:background .2s}
.toggle-slider:before{content:"";position:absolute;height:18px;width:18px;
                       left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.toggle input:checked + .toggle-slider{background:#6366f1}
.toggle input:checked + .toggle-slider:before{transform:translateX(18px)}
.toggle-label{font-size:.88em;color:var(--text-md)}
.settings-footer{padding:14px 20px;border-top:1px solid var(--border);
                 display:flex;justify-content:space-between;align-items:center;gap:8px}
.settings-saved{font-size:.85em;color:#34d399;opacity:0;transition:opacity .4s}
.settings-saved.show{opacity:1}

/* ── Responsive ──────────────────────────────────────────────────────────── */
@media(max-width:680px){
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .dash-info-row{grid-template-columns:1fr}
  .settings-grid{grid-template-columns:1fr}
}
@media(max-width:420px){
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .card-header{padding:12px 14px}
  .card-inner{padding:0 14px 14px}
}
</style>
</head>
<body>
<div class="wrap">

  <!-- ── Dashboard ────────────────────────────────────────────────────────── -->
  <div class="dash">
    <div class="dash-title-row">
      <div class="dash-title">
        <h1>&#x1F433; {{ title }}</h1>
        <p>Docker Update Made Manageable, Yay!</p>
      </div>
      <div class="dash-controls">
        <button class="btn btn-ghost" onclick="openSettings()">&#9881; Settings</button>
      </div>
      <!-- theme button rendered fixed via CSS, outside flow -->
      <button class="btn btn-ghost" id="btn-theme" onclick="toggleTheme()">&#9788; Light</button>
    </div>

    <!-- Stat cards -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-num">{{ containers|length }}</div>
        <div class="stat-lbl">Monitored</div>
      </div>
      <div class="stat-card {% if updates_count > 0 %}has-updates{% endif %}">
        <div class="stat-num">{{ updates_count }}</div>
        <div class="stat-lbl">Updates Ready</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{{ running_count }}</div>
        <div class="stat-lbl">Running</div>
      </div>
      <div class="stat-card {% if updates_count == 0 and containers|length > 0 %}all-good{% endif %}">
        <div class="stat-num">{{ containers|length - updates_count }}</div>
        <div class="stat-lbl">Up to Date</div>
      </div>
    </div>

    <!-- Config + timing row -->
    <div class="dash-info-row">
      <!-- Config flags -->
      <div class="dash-panel">
        <div class="dash-panel-lbl">Configuration</div>
        <div class="flags">
          <span class="flag {{ 'flag-on' if allow_prerelease else 'flag-off' }}">
            Pre-releases {{ 'ON' if allow_prerelease else 'OFF' }}
          </span>
          <span class="flag {{ 'flag-on' if auto_update else 'flag-off' }}">
            Auto-update {{ 'ON' if auto_update else 'OFF' }}
          </span>
          <span class="flag {{ 'flag-on' if ntfy_enabled else 'flag-off' }}">
            Notifications {{ 'ON' if ntfy_enabled else 'OFF' }}
          </span>
          <span class="flag {{ 'flag-ok' if persistent else 'flag-warn' }}">
            Persistence {{ 'OK' if persistent else 'WARNING' }}
          </span>
          <span class="flag flag-off">
            History {{ history_limit }} versions
          </span>
        </div>
      </div>

      <!-- Timing + actions -->
      <div class="dash-panel">
        <div class="dash-panel-lbl">Update Checks (every {{ check_interval_h }})</div>
        <div class="timing">
          <div class="timing-row">
            <span class="t-icon">&#x23F0;</span>
            <span>Last checked:</span>
            <span class="t-val" id="last-checked">Loading...</span>
          </div>
          <div class="timing-row">
            <span class="t-icon">&#x23F3;</span>
            <span>Next check in:</span>
            <span class="t-val" id="next-check">Loading...</span>
          </div>
        </div>
        <div class="action-row">
          <button class="btn btn-primary" id="btn-check" onclick="checkNow()">
            <span id="check-icon">&#8635;</span> Check Now
          </button>
          <a class="btn btn-ghost" href="/api/history/export" download="dummy-history.json">
            &#x2193; Export History
          </a>
        </div>
      </div>
    </div>
  </div>

  {% if not persistent %}
  <div class="warn-bar">
    &#9888; <strong>Persistence warning:</strong> <code>/data</code> is not bind-mounted.
    History and rollback data will be lost on restart.
    Add <code>- /stacks/data/dummy:/data</code> to your compose volumes.
  </div>
  {% endif %}

  <!-- ── Container cards ──────────────────────────────────────────────────── -->
  {% if containers %}
    {% for u in containers %}
    <div class="card {% if u.has_update %}has-update{% endif %} {% if u.status != 'running' %}stopped-card{% endif %}"
         id="card-{{ u.container }}">

      <!-- Header -->
      <div class="card-header">
        <div class="card-left">
          <span class="cname">{{ u.container }}</span>
        </div>
        <div class="badges">
          <span class="badge {{ 'b-running' if u.status == 'running' else 'b-stopped' }}">
            {{ u.status }}
          </span>
          <span class="badge b-strategy">{{ u.strategy_label }}</span>
          {% if u.has_update %}
          <span class="badge b-update">&#x2B06; {{ u.available_tag }}</span>
          {% endif %}
        </div>
      </div>

      <div class="card-body">
        <div class="card-inner">

          <!-- Info -->
          <div class="info-block">
            <div class="info-row">
              <span class="lbl">Image</span>
              <span class="val">{{ u.image }}</span>
            </div>
            <div class="info-row">
              <span class="lbl">Current</span>
              <span class="val">{{ u.current_tag }}</span>
            </div>
            {% if u.available_tag %}
            <div class="info-row">
              <span class="lbl">Available</span>
              <span class="val available">{{ u.available_tag }}</span>
            </div>
            {% endif %}
          </div>

          <!-- Version history with per-entry rollback -->
          {% if u.history %}
          <div class="history-block">
            <div class="history-lbl">Version History</div>
            {% for h in u.history %}
            <div class="h-item">
              <span class="h-tag">{{ h.tag }}</span>
              <span class="h-date">{{ h.date[:10] }}</span>
              <span class="h-pill h-{{ h.status }}">{{ h.status }}</span>
              {% if not loop.first %}
              <button class="btn btn-warn btn-sm"
                      onclick="rollbackTo('{{ u.container }}','{{ h.tag }}')">
                &#x21A9; Restore
              </button>
              {% endif %}
            </div>
            {% endfor %}
          </div>
          {% endif %}

          <!-- Messages -->
          <div class="msg msg-ok"  id="msg-ok-{{ u.container }}"></div>
          <div class="msg msg-err" id="msg-err-{{ u.container }}"></div>

          <!-- Action buttons -->
          <div class="btns">
            {% if u.has_update and not auto_update %}
            <button class="btn btn-primary"
                    id="btn-upd-{{ u.container }}"
                    onclick="doUpdate('{{ u.container }}','{{ u.available_tag }}')">
              &#x2B06; Update to {{ u.available_tag }}
            </button>
            {% endif %}
            {% if u.changelog %}
            <a class="btn btn-ghost"
               href="{{ u.changelog }}" target="_blank" rel="noopener">
              &#x1F4DD; Changelog
            </a>
            {% endif %}
          </div>

        </div>
      </div>
    </div>
    {% endfor %}

  {% else %}
    <div class="setup-box">
      <h2>No containers monitored yet</h2>
      <p>Add <strong>dummy.enable=true</strong> to any service. That's all that's required.</p>
      <pre>services:
  radarr:
    image: lscr.io/linuxserver/radarr:5.2.1
    labels:
      - dummy.enable=true            # required
      - dummy.env_var=RADARR_VER    # optional: keep .env in sync
      - dummy.compose_file=/compose/docker-compose.yml  # optional</pre>
    </div>
  {% endif %}

</div>

<!-- ── Settings modal ──────────────────────────────────────────────────────── -->
<div class="overlay" id="settings-overlay">
  <div class="modal" style="max-width:520px">
    <div class="modal-head">
      <span class="modal-icon">&#9881;</span>
      <span class="modal-title-text">Settings</span>
    </div>
    <div class="settings-grid">

      <div class="setting-row">
        <span class="setting-lbl">Check Interval</span>
        <span class="setting-desc">How often to poll for updates</span>
        <div class="setting-val">
          <select id="cfg-interval">
            <option value="3600">Every 1 hour</option>
            <option value="7200">Every 2 hours</option>
            <option value="21600" selected>Every 6 hours</option>
            <option value="43200">Every 12 hours</option>
            <option value="86400">Every 24 hours</option>
          </select>
        </div>
      </div>

      <div class="setting-row">
        <span class="setting-lbl">History Limit</span>
        <span class="setting-desc">Versions to keep per container</span>
        <div class="setting-val">
          <select id="cfg-history">
            <option value="3">3 versions</option>
            <option value="5" selected>5 versions</option>
            <option value="10">10 versions</option>
            <option value="20">20 versions</option>
          </select>
        </div>
      </div>

      <div class="setting-row">
        <span class="setting-lbl">Pre-releases</span>
        <span class="setting-desc">Include alpha/beta/RC tags</span>
        <div class="toggle-wrap">
          <label class="toggle">
            <input type="checkbox" id="cfg-prerelease">
            <span class="toggle-slider"></span>
          </label>
          <span class="toggle-label" id="cfg-prerelease-lbl">Off</span>
        </div>
      </div>

      <div class="setting-row">
        <span class="setting-lbl">Auto-update</span>
        <span class="setting-desc">Apply updates without confirmation</span>
        <div class="toggle-wrap">
          <label class="toggle">
            <input type="checkbox" id="cfg-autoupdate">
            <span class="toggle-slider"></span>
          </label>
          <span class="toggle-label" id="cfg-autoupdate-lbl">Off</span>
        </div>
      </div>

    </div>
    <div class="settings-footer">
      <span class="settings-saved" id="settings-saved">&#10003; Saved</span>
      <div style="display:flex;gap:8px">
        <button class="btn btn-ghost" onclick="closeSettings()">Cancel</button>
        <button class="btn btn-primary" onclick="saveSettings()">Save Changes</button>
      </div>
    </div>
  </div>
</div>

<!-- ── Progress modal ──────────────────────────────────────────────────────── -->
<div class="overlay" id="overlay">
  <div class="modal">
    <div class="modal-head">
      <span class="modal-icon" id="modal-icon"><div class="spinner"></div></span>
      <span class="modal-title-text" id="modal-title">Working...</span>
    </div>
    <div class="modal-log" id="modal-log"></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="modal-close" disabled onclick="closeModal()">
        Close &amp; Refresh
      </button>
    </div>
  </div>
</div>

<script>
// ── Theme ──────────────────────────────────────────────────────────────────
function initTheme(){
  const t = localStorage.getItem('dummy-theme') || 'dark';
  applyTheme(t);
}
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  const btn = document.getElementById('btn-theme');
  if(btn) btn.textContent = t === 'dark' ? '\u2600 Light' : '\u263D Dark';
  localStorage.setItem('dummy-theme', t);
}
function toggleTheme(){
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}

// ── Status polling (last checked / next check countdown) ───────────────────
let _nextCheckTs = 0;
let _countdownTick;

function initStatus(){
  fetchStatus();
  setInterval(fetchStatus, 30000);
  _countdownTick = setInterval(tickCountdown, 1000);
}

async function fetchStatus(){
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    _nextCheckTs = d.next_check_time * 1000;
    const el = document.getElementById('last-checked');
    if(el) el.textContent = d.last_check_time ? ago(d.last_check_time * 1000) : 'Never';
    tickCountdown();
  } catch(e){}
}

function tickCountdown(){
  const el = document.getElementById('next-check');
  if(!el) return;
  if(!_nextCheckTs){ el.textContent = '—'; return; }
  const ms = _nextCheckTs - Date.now();
  if(ms <= 0){ el.textContent = 'Soon...'; return; }
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  el.textContent = h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function ago(ts){
  const ms = Date.now() - ts;
  const m = Math.floor(ms / 60000);
  const h = Math.floor(m / 60);
  if(h > 0) return `${h}h ${m % 60}m ago`;
  if(m > 0) return `${m}m ago`;
  return 'Just now';
}

// ── Settings ────────────────────────────────────────────────────────────────
function openSettings(){
  // Load current values from API then show modal
  fetch('/api/settings').then(r=>r.json()).then(cfg => {
    const sel = document.getElementById('cfg-interval');
    if(sel){
      // Select closest matching option
      const opts = [...sel.options].map(o=>parseInt(o.value));
      const ci = cfg.check_interval;
      sel.value = opts.reduce((a,b) => Math.abs(b-ci) < Math.abs(a-ci) ? b : a);
    }
    const sh = document.getElementById('cfg-history');
    if(sh) sh.value = cfg.history_limit;
    setToggle('cfg-prerelease', 'cfg-prerelease-lbl', cfg.allow_prerelease);
    setToggle('cfg-autoupdate', 'cfg-autoupdate-lbl', cfg.auto_update);
    document.getElementById('settings-saved').classList.remove('show');
    document.getElementById('settings-overlay').classList.add('visible');
  }).catch(()=>{
    document.getElementById('settings-overlay').classList.add('visible');
  });
}

function closeSettings(){
  document.getElementById('settings-overlay').classList.remove('visible');
}

function setToggle(id, lblId, val){
  const cb = document.getElementById(id);
  const lbl = document.getElementById(lblId);
  if(cb) cb.checked = !!val;
  if(lbl) lbl.textContent = val ? 'On' : 'Off';
}

// Update label text when toggle is clicked
['cfg-prerelease','cfg-autoupdate'].forEach(id => {
  document.addEventListener('DOMContentLoaded', () => {
    const cb = document.getElementById(id);
    if(cb) cb.addEventListener('change', () => {
      const lbl = document.getElementById(id+'-lbl');
      if(lbl) lbl.textContent = cb.checked ? 'On' : 'Off';
    });
  });
});

async function saveSettings(){
  const payload = {
    check_interval:   parseInt(document.getElementById('cfg-interval').value),
    history_limit:    parseInt(document.getElementById('cfg-history').value),
    allow_prerelease: document.getElementById('cfg-prerelease').checked,
    auto_update:      document.getElementById('cfg-autoupdate').checked,
  };
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if(d.success){
      const saved = document.getElementById('settings-saved');
      saved.classList.add('show');
      setTimeout(() => { saved.classList.remove('show'); }, 2500);
      // Reload after short delay so dashboard reflects new settings
      setTimeout(() => { closeSettings(); location.reload(); }, 1200);
    }
  } catch(e){ alert('Save failed: ' + e.message); }
}

// Close settings on overlay click
document.getElementById('settings-overlay').addEventListener('click', function(e){
  if(e.target === this) closeSettings();
});

// ── Check Now ───────────────────────────────────────────────────────────────
async function checkNow(){
  const btn = document.getElementById('btn-check');
  const icon = document.getElementById('check-icon');
  btn.disabled = true;
  icon.innerHTML = '<div class="spinner" style="width:14px;height:14px;display:inline-block"></div>';

  openModal('Checking for updates...');

  try {
    const r = await fetch('/api/check', {method:'POST'});
    const d = await r.json();
    const jobId = d.job_id;
    if(jobId){
      const result = await pollJob(jobId);
      modalDone(true, result && result.message ? result.message : 'Check complete');
    }
  } catch(e){
    appendLog('Error: ' + e.message, 'error');
    modalDone(false, 'Check failed: ' + e.message);
  }

  btn.disabled = false;
  icon.textContent = '\u21BB';
  fetchStatus();
}

// ── Update ──────────────────────────────────────────────────────────────────
async function doUpdate(container, tag){
  openModal(`Updating ${container} \u2192 ${tag}`);
  try {
    const r = await fetch('/api/update', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({container, tag})
    });
    const d = await r.json();
    if(d.job_id){
      const result = await pollJob(d.job_id);
      if(result && result.success){
        modalDone(true, result.message || 'Update complete', true);
      } else {
        modalDone(false, result ? result.error : 'Unknown error');
      }
    }
  } catch(e){
    modalDone(false, 'Network error: ' + e.message);
  }
}

// ── Rollback ────────────────────────────────────────────────────────────────
async function rollbackTo(container, tag){
  if(!confirm(`Restore ${container} to ${tag}?`)) return;
  openModal(`Restoring ${container} \u2192 ${tag}`);
  try {
    const r = await fetch('/api/rollback', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({container, tag})
    });
    const d = await r.json();
    if(d.job_id){
      const result = await pollJob(d.job_id);
      if(result && result.success){
        modalDone(true, result.message || 'Rollback complete', true);
      } else {
        modalDone(false, result ? result.error : 'Unknown error');
      }
    }
  } catch(e){
    modalDone(false, 'Network error: ' + e.message);
  }
}

// ── Job polling ─────────────────────────────────────────────────────────────
let _lastLogCount = 0;

async function pollJob(jobId){
  _lastLogCount = 0;
  return new Promise(resolve => {
    const iv = setInterval(async () => {
      try {
        const r = await fetch('/api/jobs/' + jobId);
        const d = await r.json();

        // Append any new log lines
        const newLines = d.log.slice(_lastLogCount);
        newLines.forEach(l => appendLog(l.msg, l.lvl));
        _lastLogCount = d.log.length;

        if(d.done){
          clearInterval(iv);
          resolve(d);
        }
      } catch(e){
        clearInterval(iv);
        resolve(null);
      }
    }, 800);
  });
}

// ── Modal ────────────────────────────────────────────────────────────────────
function openModal(title){
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-icon').innerHTML = '<div class="spinner"></div>';
  document.getElementById('modal-log').innerHTML = '';
  document.getElementById('modal-close').disabled = true;
  document.getElementById('overlay').classList.add('visible');
  _lastLogCount = 0;
}

function appendLog(msg, level){
  const log = document.getElementById('modal-log');
  const line = document.createElement('div');
  line.className = 'log-line';
  const now = new Date();
  const t = [now.getHours(), now.getMinutes(), now.getSeconds()]
    .map(n => String(n).padStart(2,'0')).join(':');
  line.innerHTML = `<span class="log-t">${t}</span><span class="log-msg ${level||''}">${escHtml(msg)}</span>`;
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function modalDone(success, msg, reload=false){
  document.getElementById('modal-icon').textContent = success ? '\u2705' : '\u274C';
  if(msg) appendLog(msg, success ? 'info' : 'error');
  const close = document.getElementById('modal-close');
  close.disabled = false;
  close.textContent = (success && reload) ? 'Close & Refresh' : 'Close';
  if(success && reload){
    close.className = 'btn btn-primary';
    close.onclick = () => closeModal(true);
  } else {
    close.className = 'btn btn-ghost';
    close.onclick = () => closeModal(false);
  }
}

function closeModal(forceReload){
  document.getElementById('overlay').classList.remove('visible');
  if(forceReload) location.reload();
}

// Close modal on overlay click
document.getElementById('overlay').addEventListener('click', function(e){
  if(e.target === this && !document.getElementById('modal-close').disabled){
    closeModal();
  }
});

function escHtml(s){
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Init ─────────────────────────────────────────────────────────────────────
initTheme();
initStatus();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    containers    = get_monitored_containers()
    updates_count = sum(1 for c in containers if c["has_update"])
    running_count = sum(1 for c in containers if c["status"] == "running")
    cfg = get_all_settings()
    ci  = cfg["check_interval"]
    interval_h = f"{ci // 3600}h" if ci >= 3600 else f"{ci // 60}m"
    for c in containers:
        c.pop("_strategy_cfg", None)
    return render_template_string(
        HTML,
        containers       = containers,
        updates_count    = updates_count,
        running_count    = running_count,
        title            = WEB_TITLE,
        check_interval_h = interval_h,
        allow_prerelease = cfg["allow_prerelease"],
        auto_update      = cfg["auto_update"],
        ntfy_enabled     = bool(NTFY_ENDPOINT),
        history_limit    = cfg["history_limit"],
        persistent       = _check_persistence(),
    )


@app.route("/api/status")
def api_status():
    cfg = get_all_settings()
    return jsonify({
        "last_check_time": _last_check_time,
        "next_check_time": _next_check_time,
        "check_interval":  cfg["check_interval"],
    })


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(get_all_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.json or {}
    allowed = {"check_interval", "allow_prerelease", "auto_update", "history_limit"}
    saved = {}
    errors = {}
    for key, value in data.items():
        if key not in allowed:
            errors[key] = "Unknown setting"
            continue
        try:
            if key == "check_interval":
                v = int(value)
                if v < 60 or v > 86400:
                    errors[key] = "Must be between 60 and 86400 seconds"
                    continue
                save_setting(key, str(v))
                saved[key] = v
            elif key == "history_limit":
                v = int(value)
                if v < 1 or v > 50:
                    errors[key] = "Must be between 1 and 50"
                    continue
                save_setting(key, str(v))
                saved[key] = v
            elif key in ("allow_prerelease", "auto_update"):
                v = "true" if str(value).lower() in ("true", "1", "yes") else "false"
                save_setting(key, v)
                saved[key] = v == "true"
        except Exception as exc:
            errors[key] = str(exc)
    log.info("Settings updated: %s", saved)
    return jsonify({"success": True, "saved": saved, "errors": errors})


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "done":    job["done"],
        "success": job["success"],
        "error":   job["error"],
        "message": job["message"],
        "log":     job["log"],
    })


@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.json or {}
    container, tag = data.get("container"), data.get("tag")
    if not container or not tag:
        return jsonify({"success": False, "error": "Missing parameters"})

    job_id = _new_job()

    def run():
        result = update_service(container, tag, job_id=job_id)
        _job_done(job_id, result.get("success", False),
                  message=result.get("message",""),
                  error=result.get("error",""))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    data = request.json or {}
    container  = data.get("container")
    target_tag = data.get("tag")
    if not container:
        return jsonify({"success": False, "error": "Missing container"})

    job_id = _new_job()

    def run():
        result = rollback_service(container, target_tag, job_id=job_id)
        _job_done(job_id, result.get("success", False),
                  message=result.get("message",""),
                  error=result.get("error",""))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/check", methods=["POST"])
def api_check():
    job_id = _new_job()

    def run():
        _check_once(job_id=job_id)
        _job_done(job_id, True, message="Check complete")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/containers")
def api_containers():
    items = get_monitored_containers()
    for c in items:
        c.pop("_strategy_cfg", None)
    return jsonify(items)


@app.route("/api/history/export")
def api_history_export():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT container, tag, deployed_at, status FROM version_history ORDER BY deployed_at ASC")
        rows = c.fetchall()
        conn.close()
        data = [{"container": r[0], "tag": r[1], "deployed_at": r[2], "status": r[3]} for r in rows]
        return Response(
            json.dumps({"exported_at": datetime.now().isoformat(), "history": data}, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=dummy-history.json"}
        )
    except Exception as exc:
        log.error("api_history_export: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/history/import", methods=["POST"])
def api_history_import():
    try:
        data = request.json or {}
        rows = data.get("history", [])
        if not rows:
            return jsonify({"success": False, "error": "No history entries in payload"})
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        inserted = skipped = 0
        for row in rows:
            try:
                c.execute(
                    """INSERT INTO version_history (container, tag, deployed_at, status)
                       SELECT ?,?,?,? WHERE NOT EXISTS (
                           SELECT 1 FROM version_history WHERE container=? AND deployed_at=?)""",
                    (row["container"], row["tag"], row["deployed_at"], row["status"],
                     row["container"], row["deployed_at"])
                )
                if c.rowcount: inserted += 1
                else: skipped += 1
            except Exception:
                skipped += 1
        conn.commit()
        conn.close()
        return jsonify({"success": True, "inserted": inserted, "skipped": skipped})
    except Exception as exc:
        log.error("api_history_import: %s", exc)
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/history/<container>")
def api_container_history(container):
    return jsonify(get_history(container))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "containers_monitored": len(get_monitored_containers())})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("=== DUMMY - Docker Update Made Manageable, Yay! ===")
    log.info("Allow prerelease: %s | Auto-update: %s | Check interval: %ds",
             ALLOW_PRERELEASE, AUTO_UPDATE, CHECK_INTERVAL)
    init_db()
    _next_check_time = time.time() + CHECK_INTERVAL
    threading.Thread(target=check_for_updates, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
