"""
DUMMY - Docker Update Made Manageable, Yay!

Three update strategies, auto-detected from Docker labels:

  1. docker_api  - pull + replace via Docker SDK. Just add dummy.enable=true.
  2. compose     - edit the compose YAML + docker compose up -d.
                   Add dummy.enable=true + dummy.compose_file=/path/to/compose.yaml
  3. env_file    - update a .env variable + replace the container.
                   Add dummy.enable=true + dummy.env_var=MY_VAR_NAME

Safety model for docker_api and env_file replacements:
  The running container is stopped and RENAMED (not removed). The new container
  is created, started and health-checked. Only after it passes is the old one
  removed. On failure the new one is discarded and the old one is renamed back
  and restarted, so no image pull is needed to roll back.
"""

import os
import re
import time
import uuid
import json
import hmac
import base64
import logging
import sqlite3
import subprocess
import threading
import contextlib
from datetime import datetime
from functools import wraps

import yaml
from flask import Flask, render_template_string, request, jsonify, Response, abort
from werkzeug.exceptions import HTTPException
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
ENV_FILE             = os.environ.get("ENV_FILE_PATH", "/env/.env")
DB_PATH              = os.environ.get("DB_PATH", "/data/versions.db")
PORT                 = int(os.environ.get("PORT", "5000"))
CHECK_INTERVAL       = int(os.environ.get("CHECK_INTERVAL", "21600"))
HEALTH_TIMEOUT       = int(os.environ.get("HEALTH_CHECK_TIMEOUT", "120"))
STABLE_SECONDS       = int(os.environ.get("STABLE_SECONDS", "15"))   # no-healthcheck containers
HISTORY_LIMIT        = int(os.environ.get("HISTORY_LIMIT", "5"))
ALLOW_PRERELEASE     = os.environ.get("ALLOW_PRERELEASE", "false").lower() == "true"
AUTO_UPDATE          = os.environ.get("AUTO_UPDATE", "false").lower() == "true"
WEB_TITLE            = os.environ.get("WEB_TITLE", "DUMMY")

NTFY_ENDPOINT        = os.environ.get("NTFY_ENDPOINT", "").rstrip("/")
NTFY_TOPIC           = os.environ.get("NTFY_TOPIC", "DockerUpdate")
NTFY_TOKEN           = os.environ.get("NTFY_TOKEN", "")
NTFY_CLICK_URL       = os.environ.get("NTFY_CLICK_URL", "")

GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN", "")

BASIC_AUTH_USER      = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS      = os.environ.get("BASIC_AUTH_PASS", "")
WEBHOOK_URL          = os.environ.get("WEBHOOK_URL", "").strip()

DEFAULT_STOP_TIMEOUT = 10
BACKUP_SUFFIX        = "-dummy-old"

# Input validation. Anything that reaches .env, compose files, or docker pull
# must pass these, so a crafted request cannot inject extra lines or images.
TAG_RE      = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
NAME_RE     = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
ENV_VAR_RE  = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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

# Shared HTTP session for registry and webhook calls
_session = req.Session()

# ---------------------------------------------------------------------------
# Docker client (one connection, reconnected if stale)
# ---------------------------------------------------------------------------
_docker_client = None
_docker_lock = threading.Lock()


def get_docker_client():
    global _docker_client
    with _docker_lock:
        if _docker_client is not None:
            try:
                _docker_client.ping()
                return _docker_client
            except Exception:
                log.warning("Docker client connection lost — reconnecting...")
                _docker_client = None
        _docker_client = dockerlib.from_env()
        return _docker_client


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
_jobs: dict = {}
_jobs_lock = threading.Lock()

_last_check_time: float = 0.0
_next_check_time: float = 0.0

# Update guard: one operation per container at a time
_active_containers: set = set()
_active_lock = threading.Lock()

# Check guard: lock-based so the debounce is atomic (no check-then-set race)
_check_lock = threading.Lock()
_check_running = threading.Event()        # mirrors _check_lock for /api/status

# Set when the check interval is changed so the scheduler re-reads it
_interval_changed = threading.Event()


def _new_job() -> str:
    job_id = str(uuid.uuid4())[:8]
    now = time.time()
    with _jobs_lock:
        _jobs[job_id] = {
            "log": [], "done": False, "success": False,
            "error": "", "message": "", "started": now,
        }
        cutoff = now - 600
        for k in [k for k, v in _jobs.items() if v["started"] < cutoff and v["done"]]:
            del _jobs[k]
    return job_id


def _jlog(job_id, msg: str, level: str = "info"):
    log_fn = {"error": log.error, "warn": log.warning, "warning": log.warning}.get(level, log.info)
    log_fn("[job:%s] %s", job_id, msg)
    if job_id:
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["log"].append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "msg": msg,
                    "lvl": level,
                })


def _job_done(job_id, success: bool, message: str = "", error: str = ""):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(done=True, success=success, message=message, error=error)


# ---------------------------------------------------------------------------
# Tag filtering
# ---------------------------------------------------------------------------
_PRE_WORDS = r"alpha|beta|rc|nightly|edge|dev|devel|develop|development|unstable|next|git|test|snapshot|experimental|staging|canary|preview|prerelease|pre-release"
_CI_WORDS  = r"pr|feature|feat|fix|hotfix|refs|sha|build|ci|main|master"
_PRE_RE    = re.compile(rf"(?:^|[-_.])(?:{_PRE_WORDS})(?=$|[-_.\d])", re.I)
_CI_RE     = re.compile(rf"(?:^|[-_.])(?:{_CI_WORDS})(?=$|[-_.\d])", re.I)
_ARCH_RE   = re.compile(r"(?:^|[-_.])(?:amd64|arm64v8|arm64|armhf|arm32v7|armv7|armv6|aarch64|i386|ppc64le|s390x|linux)(?=$|[-_.])", re.I)
# Hardware/runtime variants of the same release, not newer versions
_VARIANT_RE = re.compile(r"-(?:openvino|cuda|rocm|tensorrt|gpu|cpu|onnx|mlkit|coreml)(?=$|[-.])", re.I)
_LS_RE      = re.compile(r"-ls\d+|-lt\d+-", re.I)
_VERSION_RE = re.compile(r"^(?:version-|[vV])?\d+([.\-]\d+)*([.\-][a-zA-Z0-9]+)?$")
_NUM_RE     = re.compile(r"^(?:version-|[vV])?(\d+(?:[.\-]\d+)*)")


def is_stable_tag(tag: str) -> bool:
    if not tag or not TAG_RE.match(tag):
        return False
    if _CI_RE.search(tag) or _ARCH_RE.search(tag) or _VARIANT_RE.search(tag) or _LS_RE.search(tag):
        return False
    if not ALLOW_PRERELEASE and _PRE_RE.search(tag):
        return False
    return bool(_VERSION_RE.match(tag))


def version_tuple(tag: str) -> tuple:
    """Compare on the leading numeric version only, so hash suffixes don't count."""
    m = _NUM_RE.match(tag or "")
    if not m:
        return (0,)
    return tuple(int(n) for n in re.split(r"[.\-]", m.group(1)))


def is_newer(current: str, candidate: str) -> bool:
    try:
        return version_tuple(candidate) > version_tuple(current)
    except Exception:
        return False


def _find_newest_tag(tags, current_tag: str):
    newest = None
    for tag in tags:
        if not tag or not is_stable_tag(tag) or not is_newer(current_tag, tag):
            continue
        if newest is None or is_newer(newest, tag):
            newest = tag
    return newest


# ---------------------------------------------------------------------------
# Registry queries
# ---------------------------------------------------------------------------

def _req_get(url: str, headers: dict = None, timeout: int = 10, max_attempts: int = 3):
    """GET with exponential backoff and Retry-After / rate-limit awareness."""
    resp = None
    for attempt in range(max_attempts):
        try:
            resp = _session.get(url, headers=headers or {}, timeout=timeout)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 10))
                log.warning("Rate-limited by %s — waiting %ds", url[:60], retry_after)
                time.sleep(min(retry_after, 60))
                continue
            if resp.status_code >= 500 and attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue
            return resp
        except (req.exceptions.ConnectionError, req.exceptions.Timeout) as exc:
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
            else:
                log.error("Request failed after %d attempts: %s — %s", max_attempts, url[:60], exc)
    return resp


def _ghcr_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def query_dockerhub(org: str, repo: str, current_tag: str):
    """
    Returns the newest stable tag, current_tag if the lookup succeeded and
    nothing newer exists, or None if the registry could not be read.
    """
    url = f"https://hub.docker.com/v2/repositories/{org}/{repo}/tags?page_size=100&ordering=last_updated"
    all_tags, page, fetched = [], 0, False
    try:
        while url and page < 5:
            page += 1
            resp = _req_get(url)
            if resp is None or resp.status_code != 200:
                log.warning("DockerHub %s/%s HTTP %s", org, repo,
                            resp.status_code if resp is not None else "no-response")
                break
            fetched = True
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining and int(remaining) < 10:
                log.warning("DockerHub rate limit low: %s requests remaining", remaining)
            body = resp.json()
            all_tags.extend(t["name"] for t in body.get("results", []))
            url = body.get("next")
        if not fetched:
            return None
        return _find_newest_tag(all_tags, current_tag) or current_tag
    except Exception as exc:
        log.error("query_dockerhub %s/%s: %s", org, repo, exc)
        return None


def _parse_link_next(link_header: str) -> str:
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            url = part.split(";")[0].strip().strip("<>")
            if url.startswith("/"):
                url = "https://ghcr.io" + url
            return url
    return ""


def _query_ghcr_registry_api(org: str, repo: str, current_tag: str):
    """
    Docker Registry v2 API on ghcr.io. Tags sort alphabetically, so the scan
    starts at current_tag. Returns the newest tag, current_tag if the registry
    was read but nothing newer exists, or None if it could not be read.
    """
    image_path = f"{org}/{repo}"
    try:
        tok_resp = _req_get(f"https://ghcr.io/token?scope=repository:{image_path}:pull")
        if tok_resp is None or tok_resp.status_code != 200:
            log.warning("GHCR token %s HTTP %s", image_path,
                        tok_resp.status_code if tok_resp is not None else "no-response")
            return None
        token = tok_resp.json().get("token")
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        fetched = {"ok": False}

        def _fetch_pages(start_url: str, max_pages: int) -> list:
            tags, url, page = [], start_url, 0
            while url and page < max_pages:
                page += 1
                resp = _req_get(url, headers=headers)
                if resp is None or resp.status_code != 200:
                    log.warning("GHCR tags %s page %d HTTP %s", image_path, page,
                                resp.status_code if resp is not None else "no-response")
                    break
                fetched["ok"] = True
                tags.extend(resp.json().get("tags") or [])
                url = _parse_link_next(resp.headers.get("Link", ""))
            return tags

        cursor_url = f"https://ghcr.io/v2/{image_path}/tags/list?n=500&last={current_tag}"
        result = _find_newest_tag(_fetch_pages(cursor_url, max_pages=20), current_tag)
        if result:
            return result

        result = _find_newest_tag(
            _fetch_pages(f"https://ghcr.io/v2/{image_path}/tags/list?n=1000", max_pages=5),
            current_tag,
        )
        if result:
            return result
        return current_tag if fetched["ok"] else None
    except Exception as exc:
        log.error("_query_ghcr_registry_api %s/%s: %s", org, repo, exc)
        return None


def query_ghcr(org: str, repo: str, current_tag: str):
    # Attempt 1: GitHub Packages API (org, then user). Needs a token for some packages.
    try:
        url = f"https://api.github.com/orgs/{org}/packages/container/{repo}/versions?per_page=100"
        resp = _req_get(url, headers=_ghcr_headers())
        if resp is not None and resp.status_code in (401, 404):
            url = f"https://api.github.com/users/{org}/packages/container/{repo}/versions?per_page=100"
            resp = _req_get(url, headers=_ghcr_headers())
        if resp is not None and resp.status_code == 200:
            all_tags = []
            for version in resp.json():
                all_tags.extend(version.get("metadata", {}).get("container", {}).get("tags", []))
            result = _find_newest_tag(all_tags, current_tag)
            if result is not None:
                return result
    except Exception as exc:
        log.warning("GHCR packages API %s/%s error: %s", org, repo, exc)

    # Attempt 2: registry v2 API, works anonymously for public images
    return _query_ghcr_registry_api(org, repo, current_tag)


def _split_image(image_ref: str):
    """'registry:5000/repo/name:tag' -> ('registry:5000/repo/name', 'tag'). Digests are dropped."""
    ref = (image_ref or "").split("@", 1)[0]
    last_slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > last_slash:
        return ref[:colon], ref[colon + 1:]
    return ref, "latest"


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
_SCHEMA_VERSION = 2

_MIGRATIONS = [
    (1, """CREATE TABLE IF NOT EXISTS version_history (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               container TEXT, tag TEXT, deployed_at TEXT, status TEXT)"""),
    (1, """CREATE TABLE IF NOT EXISTS available_updates (
               container TEXT PRIMARY KEY, available_tag TEXT, checked_at TEXT)"""),
    (1, """CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"""),
    (2, """CREATE TABLE IF NOT EXISTS dismissed_updates (
               container TEXT, tag TEXT, dismissed_at TEXT,
               PRIMARY KEY (container, tag))"""),
]


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextlib.contextmanager
def _db():
    """Connection that is always closed, and committed on clean exit."""
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    os.makedirs(db_dir, exist_ok=True)
    if not _check_persistence():
        log.warning("PERSISTENCE WARNING: %s is not a bind-mounted volume. History will be lost on restart.", db_dir)
    else:
        log.info("Persistence OK — %s is a bind-mounted volume.", db_dir)

    with _db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
        for version, sql in _MIGRATIONS:
            if version > current:
                log.info("DB migration: applying schema v%d", version)
                conn.executescript(sql)
                conn.execute("INSERT OR REPLACE INTO schema_version VALUES (?)", (version,))
    log.info("DB schema up to date (v%d)", _SCHEMA_VERSION)


def add_to_history(container: str, tag: str, status: str = "deployed"):
    try:
        limit = get_all_settings()["history_limit"]
        with _db() as conn:
            conn.execute("INSERT INTO version_history (container,tag,deployed_at,status) VALUES (?,?,?,?)",
                         (container, tag, datetime.now().isoformat(), status))
            conn.execute("""DELETE FROM version_history WHERE container=? AND id NOT IN (
                SELECT id FROM version_history WHERE container=? ORDER BY deployed_at DESC LIMIT ?)""",
                         (container, container, limit))
    except Exception as exc:
        log.error("add_to_history: %s", exc)


def get_history(container: str) -> list:
    """Most recent first, capped by the Settings history limit (not the env default)."""
    try:
        limit = get_all_settings()["history_limit"]
        with _db() as conn:
            rows = conn.execute(
                "SELECT tag,deployed_at,status FROM version_history WHERE container=? "
                "ORDER BY deployed_at DESC LIMIT ?", (container, limit)).fetchall()
        return [{"tag": r[0], "date": r[1], "status": r[2]} for r in rows]
    except Exception:
        return []


def save_available_update(container: str, tag: str):
    try:
        with _db() as conn:
            conn.execute("INSERT OR REPLACE INTO available_updates VALUES (?,?,?)",
                         (container, tag, datetime.now().isoformat()))
    except Exception as exc:
        log.error("save_available_update: %s", exc)


def clear_available_update(container: str):
    try:
        with _db() as conn:
            conn.execute("DELETE FROM available_updates WHERE container=?", (container,))
    except Exception as exc:
        log.error("clear_available_update: %s", exc)


def is_dismissed(container: str, tag: str) -> bool:
    try:
        with _db() as conn:
            return conn.execute("SELECT 1 FROM dismissed_updates WHERE container=? AND tag=?",
                                (container, tag)).fetchone() is not None
    except Exception:
        return False


def dismiss_update(container: str, tag: str):
    try:
        with _db() as conn:
            conn.execute("INSERT OR IGNORE INTO dismissed_updates VALUES (?,?,?)",
                         (container, tag, datetime.now().isoformat()))
        log.info("Dismissed update %s → %s", container, tag)
    except Exception as exc:
        log.error("dismiss_update: %s", exc)


def undismiss_update(container: str, tag: str = None):
    try:
        with _db() as conn:
            if tag:
                conn.execute("DELETE FROM dismissed_updates WHERE container=? AND tag=?", (container, tag))
            else:
                conn.execute("DELETE FROM dismissed_updates WHERE container=?", (container,))
    except Exception as exc:
        log.error("undismiss_update: %s", exc)


def _fetch_container_data_bulk(container_names: list) -> dict:
    """One DB round-trip for available updates, history and dismissals."""
    if not container_names:
        return {}
    try:
        limit = get_all_settings()["history_limit"]
        ph = ",".join("?" * len(container_names))
        with _db() as conn:
            avail = {r[0]: (r[1], r[2]) for r in conn.execute(
                f"SELECT container, available_tag, checked_at FROM available_updates WHERE container IN ({ph})",
                container_names)}
            hist_raw: dict = {}
            for r in conn.execute(
                    f"SELECT container, tag, deployed_at, status FROM version_history "
                    f"WHERE container IN ({ph}) ORDER BY deployed_at DESC", container_names):
                hist_raw.setdefault(r[0], []).append({"tag": r[1], "date": r[2], "status": r[3]})
            dismissed: dict = {}
            for r in conn.execute(
                    f"SELECT container, tag, dismissed_at FROM dismissed_updates "
                    f"WHERE container IN ({ph}) ORDER BY dismissed_at DESC", container_names):
                dismissed.setdefault(r[0], []).append({"tag": r[1], "dismissed_at": r[2]})

        result = {}
        for name in container_names:
            av_tag, checked_at = avail.get(name, (None, ""))
            if av_tag and not is_stable_tag(av_tag):
                av_tag = None
            dismissed_list = dismissed.get(name, [])
            result[name] = {
                "available_tag": av_tag,
                "checked_at": checked_at or "",
                "history": hist_raw.get(name, [])[:limit],
                "dismissed": dismissed_list,
                "dismissed_tags": {d["tag"] for d in dismissed_list},
            }
        return result
    except Exception as exc:
        log.error("_fetch_container_data_bulk: %s", exc)
        return {}


def cleanup_stale_containers():
    """Drop update/dismiss rows for containers that no longer exist. History is kept."""
    try:
        live = {c.name for c in get_docker_client().containers.list(all=True)}
        with _db() as conn:
            stale = [r[0] for r in conn.execute("SELECT DISTINCT container FROM available_updates")
                     if r[0] not in live]
            if stale:
                ph = ",".join("?" * len(stale))
                conn.execute(f"DELETE FROM available_updates WHERE container IN ({ph})", stale)
                conn.execute(f"DELETE FROM dismissed_updates WHERE container IN ({ph})", stale)
                log.info("Cleaned up stale rows for: %s", ", ".join(stale))
    except Exception as exc:
        log.warning("cleanup_stale_containers: %s", exc)


# ---------------------------------------------------------------------------
# Settings (runtime overrides stored in SQLite)
# ---------------------------------------------------------------------------

def save_setting(key: str, value: str) -> bool:
    try:
        with _db() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
        return True
    except Exception as exc:
        log.error("save_setting %s: %s", key, exc)
        return False


def get_all_settings() -> dict:
    try:
        with _db() as conn:
            overrides = {r[0]: r[1] for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN (?,?,?,?)",
                ("check_interval", "allow_prerelease", "auto_update", "history_limit"))}
    except Exception:
        overrides = {}

    def _bool(key, default):
        v = overrides.get(key)
        return (v == "true") if v is not None else default

    def _int(key, default):
        try:
            return int(overrides[key]) if key in overrides else default
        except (ValueError, TypeError):
            return default

    return {
        "check_interval":   _int("check_interval", CHECK_INTERVAL),
        "allow_prerelease": _bool("allow_prerelease", ALLOW_PRERELEASE),
        "auto_update":      _bool("auto_update", AUTO_UPDATE),
        "history_limit":    _int("history_limit", HISTORY_LIMIT),
    }


# ---------------------------------------------------------------------------
# .env helpers
# ---------------------------------------------------------------------------

def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        if end != -1:
            return value[1:end]
    # Strip an inline comment only when it is preceded by whitespace,
    # so values like PASS=abc#def keep their '#'.
    m = re.search(r"\s#", value)
    if m:
        value = value[:m.start()].rstrip()
    return value


def read_env() -> dict:
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = _parse_env_value(v)
    except OSError as exc:
        log.error("read_env: %s", exc)
    return env


_env_cache: dict = {}
_env_cache_mtime: float = 0.0


def read_env_cached() -> dict:
    global _env_cache, _env_cache_mtime
    try:
        mtime = os.path.getmtime(ENV_FILE)
        if mtime != _env_cache_mtime:
            _env_cache = read_env()
            _env_cache_mtime = mtime
    except OSError:
        pass
    return _env_cache


def write_env_var(var: str, value: str) -> bool:
    """
    Set one variable in .env, keeping every other line (comments, export
    prefixes) intact. The file is written in place because it is a single-file
    bind mount, which cannot be atomically renamed. If the write fails midway,
    the original content is written back.
    """
    if not ENV_VAR_RE.match(var) or not TAG_RE.match(value):
        log.error("write_env_var: refusing unsafe name/value for %r", var)
        return False
    try:
        with open(ENV_FILE) as f:
            original = f.read()
    except OSError as exc:
        log.error("write_env_var: cannot read %s: %s", ENV_FILE, exc)
        return False

    lines = original.splitlines(keepends=True)
    pattern = re.compile(r"^(export\s+)?" + re.escape(var) + r"\s*=")
    idx = next((i for i, line in enumerate(lines) if pattern.match(line.lstrip())), None)
    if idx is None:
        log.warning("write_env_var: %s not found in %s", var, ENV_FILE)
        return False

    prefix = "export " if pattern.match(lines[idx].lstrip()).group(1) else ""
    lines[idx] = f"{prefix}{var}={value}\n"
    try:
        with open(ENV_FILE, "w") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        return True
    except OSError as exc:
        log.error("write_env_var: write failed, restoring original: %s", exc)
        try:
            with open(ENV_FILE, "w") as f:
                f.write(original)
        except OSError:
            log.error("write_env_var: RESTORE ALSO FAILED for %s — check the file manually", ENV_FILE)
        return False


# ---------------------------------------------------------------------------
# Compose file helpers
# ---------------------------------------------------------------------------
_INTERP_RE = re.compile(r"\$\{(\w+)(?::?-([^}]*))?\}")


def _resolve_interp(text: str, env: dict) -> str:
    def repl(m):
        return env.get(m.group(1)) or (m.group(2) or "")
    return _INTERP_RE.sub(repl, text or "")


def read_compose(path: str):
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception as exc:
        log.error("read_compose %s: %s", path, exc)
        return None


def _compose_tag(compose_path: str, service_name: str):
    """Current tag for a compose service, with ${VAR} references resolved from .env."""
    data = read_compose(compose_path)
    if not data:
        return None
    image = (data.get("services", {}).get(service_name, {}) or {}).get("image", "")
    if not image:
        return None
    return _split_image(_resolve_interp(image, read_env_cached()))[1]


_SERVICE_HEADER_CACHE: dict = {}


def _locate_service_image(lines, service: str):
    """Find the `image:` line belonging to a service, returning (index, match) or None."""
    header = _SERVICE_HEADER_CACHE.get(service)
    if header is None:
        header = re.compile(r"^(\s*)" + re.escape(service) + r"\s*:\s*(#.*)?$")
        _SERVICE_HEADER_CACHE[service] = header
    image_re = re.compile(r"^(\s*)image\s*:\s*(['\"]?)([^'\"\s#]+)\2")

    svc_indent = None
    for i, line in enumerate(lines):
        if svc_indent is None:
            m = header.match(line.rstrip("\r\n"))
            if m:
                svc_indent = len(m.group(1))
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped and not stripped.startswith("#") and indent <= svc_indent:
            return None   # left the service block without an image line
        if indent > svc_indent:
            m = image_re.match(line)
            if m:
                return i, m
    return None


def set_compose_image_tag(compose_path: str, service_name: str, new_tag: str):
    """
    Rewrite a service's image tag in place, changing only the tag characters.
    Comments, quoting and indentation are untouched. Returns (ok, old_tag).
    """
    try:
        with open(compose_path) as f:
            lines = f.readlines()
    except OSError as exc:
        log.error("set_compose_image_tag read %s: %s", compose_path, exc)
        return False, ""

    found = _locate_service_image(lines, service_name)
    if not found:
        log.error("set_compose_image_tag: no image line for %r in %s", service_name, compose_path)
        return False, ""
    idx, m = found
    raw = m.group(3)
    if "${" in raw:
        log.error("set_compose_image_tag: %r uses a variable tag (%s) — use the env_file strategy", service_name, raw)
        return False, ""

    base, old_tag = _split_image(raw)
    line = lines[idx]
    lines[idx] = line[:m.start(3)] + f"{base}:{new_tag}" + line[m.end(3):]
    try:
        with open(compose_path, "w") as f:
            f.writelines(lines)
        return True, old_tag
    except OSError as exc:
        log.error("set_compose_image_tag write %s: %s", compose_path, exc)
        return False, old_tag


def run_compose_up(compose_path: str, service_name: str):
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_path, "up", "-d", "--no-deps", service_name],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip() or result.stdout.strip()
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Container replacement (docker_api and env_file strategies)
# ---------------------------------------------------------------------------

def _stop_timeout(attrs: dict) -> int:
    """Honour the container's own stop grace period (set by compose stop_grace_period)."""
    t = (attrs.get("Config") or {}).get("StopTimeout")
    try:
        return int(t) if t else DEFAULT_STOP_TIMEOUT
    except (TypeError, ValueError):
        return DEFAULT_STOP_TIMEOUT


def _health_timeout(attrs: dict) -> int:
    """Wait at least as long as the container's own start_period, plus margin."""
    hc = (attrs.get("Config") or {}).get("Healthcheck") or {}
    start_period_s = int((hc.get("StartPeriod") or 0) / 1e9)
    return max(HEALTH_TIMEOUT, start_period_s + 60)


def _clean_aliases(net_data: dict) -> list:
    # Docker adds 12-char container-id aliases automatically; those must not be re-sent
    return [a for a in (net_data.get("Aliases") or []) if not re.match(r"^[0-9a-f]{12}$", a)]


def _create_spec(attrs: dict, new_image: str, name: str):
    """
    Build create_container arguments from the old container's inspect data,
    preserving config the old one had: healthcheck, log rotation, tmpfs, limits,
    security options, DNS, stop timeout and network aliases.
    Returns (spec, extra_networks) where extra_networks is [(name, aliases)].
    """
    cfg = attrs.get("Config") or {}
    host = dict(attrs.get("HostConfig") or {})
    host.pop("Links", None)
    host["RestartPolicy"] = {"Name": "no"}   # enabled only after the health check passes

    nets = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    mode = host.get("NetworkMode") or "default"

    primary = None
    networking_config = None
    if mode not in ("host", "none", "default") and not mode.startswith("container:") and mode in nets:
        primary = mode
        networking_config = {"EndpointsConfig": {primary: {"Aliases": _clean_aliases(nets[primary])}}}

    extra = [
        (n, _clean_aliases(d)) for n, d in nets.items()
        if n != primary and n not in ("bridge", "host", "none")
    ]

    spec = {
        "image": new_image,
        "name": name,
        "command": cfg.get("Cmd"),
        "entrypoint": cfg.get("Entrypoint"),
        "environment": cfg.get("Env"),
        "user": cfg.get("User") or None,
        "hostname": cfg.get("Hostname") or None,
        "domainname": cfg.get("Domainname") or None,
        "working_dir": cfg.get("WorkingDir") or None,
        "labels": cfg.get("Labels") or None,
        "tty": bool(cfg.get("Tty")),
        "stdin_open": bool(cfg.get("OpenStdin")),
        "stop_signal": cfg.get("StopSignal") or None,
        "stop_timeout": _stop_timeout(attrs),
        "healthcheck": cfg.get("Healthcheck") or None,
        "ports": list((cfg.get("ExposedPorts") or {}).keys()) or None,
        "host_config": host,
        "networking_config": networking_config,
    }
    return spec, extra


def _discard_new(client, name: str, jlog):
    try:
        client.containers.get(name).remove(force=True)
    except dockerlib.errors.NotFound:
        pass
    except Exception as exc:
        log.error("discard new container %s: %s", name, exc)


def _restore_old(live, name: str, orig_restart: dict, jlog) -> bool:
    try:
        live.reload()
        if live.name != name:
            live.rename(name)
        live.update(restart_policy=orig_restart)
        live.start()
        jlog(f"Restored previous container {name}", "warn")
        return True
    except Exception as exc:
        log.error("CRITICAL: could not restore previous %s: %s", name, exc)
        jlog(f"CRITICAL: could not restore previous {name}: {exc}", "error")
        return False


def _recover_leftover(client, name: str, jlog):
    """
    A previous run may have died mid-replacement and left {name}-dummy-old.
    If nothing is running under the real name, the backup is the only copy:
    rename it back and start it. If something is running under the real name,
    the state is ambiguous, so refuse to guess and report it.
    Returns an error string, or None if there is nothing to worry about.
    """
    backup = name + BACKUP_SUFFIX
    try:
        leftover = client.containers.get(backup)
    except dockerlib.errors.NotFound:
        return None
    try:
        client.containers.get(name)
        return (f"Leftover backup {backup} exists alongside {name}, probably from an interrupted "
                f"update. Resolve it manually (remove the backup if {name} is healthy).")
    except dockerlib.errors.NotFound:
        pass
    try:
        jlog(f"Recovering {name} from leftover backup {backup}", "warn")
        leftover.rename(name)
        leftover.update(restart_policy={"Name": "unless-stopped"})
        leftover.start()
        return None
    except Exception as exc:
        return f"Could not recover {name} from {backup}: {exc}"


def check_container_health(container_name: str, timeout: int = None, jlog=None) -> bool:
    """
    Wait for a container to become healthy. Containers with a Docker healthcheck
    must report 'healthy' (a start_period is tolerated). Containers without one
    must stay running for STABLE_SECONDS without restarting. Any restart, exit,
    or 'unhealthy' result fails immediately.
    """
    timeout = timeout or HEALTH_TIMEOUT

    def _log(msg, level="info"):
        if jlog:
            jlog(msg, level)

    try:
        container = get_docker_client().containers.get(container_name)
    except dockerlib.errors.NotFound:
        _log(f"Container {container_name} not found after recreate", "error")
        return False

    start = time.time()
    baseline_restarts = None
    _log(f"Health check — waiting up to {timeout}s...")
    while True:
        try:
            container.reload()
        except Exception as exc:
            _log(f"Health check error: {exc}", "error")
            return False

        attrs = container.attrs
        state = attrs.get("State") or {}
        has_hc = bool((attrs.get("Config") or {}).get("Healthcheck"))
        restarts = attrs.get("RestartCount", 0)
        if baseline_restarts is None:
            baseline_restarts = restarts
        elif restarts > baseline_restarts:
            _log("Container restarted during health check ✗", "error")
            return False

        status = state.get("Status")
        if status in ("exited", "dead"):
            _log(f"Container {status} during health check ✗", "error")
            return False

        elapsed = time.time() - start
        if status == "running":
            health = (state.get("Health") or {}).get("Status")
            if has_hc:
                if health == "healthy":
                    _log("Health check passed ✓")
                    return True
                if health == "unhealthy":
                    _log("Container reports unhealthy ✗", "error")
                    return False
            elif elapsed >= STABLE_SECONDS:
                _log(f"Running and stable for {STABLE_SECONDS}s ✓")
                return True

        if elapsed > timeout:
            _log("Health check timed out ✗", "error")
            return False
        time.sleep(2)


def replace_container(client, container_obj, new_image: str, jlog):
    """
    Replace a container with one running new_image, keeping the old one until
    the new one is healthy. Returns (ok, message). The message says whether the
    previous container was restored when a replacement fails.
    """
    try:
        container_obj.reload()
    except Exception:
        pass
    name = container_obj.name
    attrs = container_obj.attrs
    backup = name + BACKUP_SUFFIX
    orig_restart = (attrs.get("HostConfig") or {}).get("RestartPolicy") or {"Name": "unless-stopped"}

    leftover_err = _recover_leftover(client, name, jlog)
    if leftover_err:
        return False, leftover_err

    jlog(f"Pulling {new_image}...")
    try:
        client.images.pull(new_image)
    except Exception as exc:
        return False, f"Pull failed: {exc}"

    live = None
    try:
        live = client.containers.get(name)
    except dockerlib.errors.NotFound:
        pass

    if live is not None:
        try:
            jlog(f"Stopping {name} (grace {_stop_timeout(attrs)}s)...")
            live.stop(timeout=_stop_timeout(attrs))
            jlog(f"Keeping previous container as {backup} until the new one is healthy")
            live.rename(backup)
            live.update(restart_policy={"Name": "no"})
        except Exception as exc:
            restored = _restore_old(live, name, orig_restart, jlog) if live else False
            note = "previous container restored" if restored else "WARNING: previous container could not be restored"
            return False, f"Stop failed: {exc} ({note})"

    def _rollback_to_old(reason: str):
        _discard_new(client, name, jlog)
        if live is None:
            return False, f"{reason} (no previous container existed)"
        restored = _restore_old(live, name, orig_restart, jlog)
        note = "previous container restored" if restored else "WARNING: previous container could not be restored"
        return False, f"{reason} ({note})"

    spec, extra_networks = _create_spec(attrs, new_image, name)
    try:
        jlog(f"Creating {name} with {new_image}...")
        created = client.api.create_container(**spec)
        for net_name, aliases in extra_networks:
            try:
                client.networks.get(net_name).connect(created["Id"], aliases=aliases or None)
                jlog(f"Attached to network: {net_name}")
            except Exception as exc:
                log.warning("Could not attach %s to %s: %s", name, net_name, exc)
        new = client.containers.get(created["Id"])
        jlog(f"Starting {name}...")
        new.start()
    except Exception as exc:
        return _rollback_to_old(f"Recreate failed: {exc}")

    if not check_container_health(name, timeout=_health_timeout(attrs), jlog=jlog):
        return _rollback_to_old("Health check failed")

    try:
        new.reload()
        new.update(restart_policy=orig_restart)
    except Exception as exc:
        jlog(f"Could not restore restart policy on {name}: {exc}", "warn")

    if live is not None:
        try:
            live.remove(force=True)
            jlog(f"Removed previous container {backup}")
        except Exception as exc:
            jlog(f"Could not remove {backup}: {exc}", "warn")
    return True, ""


# ---------------------------------------------------------------------------
# Strategy resolution, labels, monitoring
# ---------------------------------------------------------------------------

def _get_dummy_labels(container_obj) -> dict:
    labels = (container_obj.attrs.get("Config") or {}).get("Labels") or {}
    return {k[len("dummy."):]: v for k, v in labels.items() if k.startswith("dummy.")}


def _is_monitored(container_obj) -> bool:
    return _get_dummy_labels(container_obj).get("enable", "").lower() == "true" \
        or container_obj.name in VERSION_MAP


def _resolve_strategy(container_name: str, dummy_labels: dict) -> dict:
    compose_file = dummy_labels.get("compose_file")
    env_var = dummy_labels.get("env_var") or VERSION_MAP.get(container_name)
    compose_svc = dummy_labels.get("compose_service", container_name)
    if compose_file:
        return {"strategy": STRATEGY_COMPOSE, "compose_file": compose_file, "compose_svc": compose_svc}
    if env_var:
        return {"strategy": STRATEGY_ENV_FILE, "env_var": env_var}
    return {"strategy": STRATEGY_DOCKER_API}


def get_image_parts(container_obj):
    """Image base and tag from the reference the container was created with."""
    ref = (container_obj.attrs.get("Config") or {}).get("Image") or ""
    if not ref:
        return "unknown", "unknown"
    return _split_image(ref)


def _effective_current(strategy_cfg: dict, run_tag: str, env_vars: dict) -> str:
    """The version DUMMY considers 'current' for a container: .env / compose file / running image."""
    if strategy_cfg["strategy"] == STRATEGY_ENV_FILE:
        return env_vars.get(strategy_cfg["env_var"], run_tag)
    if strategy_cfg["strategy"] == STRATEGY_COMPOSE:
        return _compose_tag(strategy_cfg["compose_file"], strategy_cfg["compose_svc"]) or run_tag
    return run_tag


def get_monitored_containers() -> list:
    items = []
    try:
        client = get_docker_client()
        env_vars = read_env_cached()

        candidates = []
        # all=True so stopped monitored containers still show on the dashboard
        for c in client.containers.list(all=True):
            if _is_monitored(c):
                candidates.append((c, _get_dummy_labels(c)))
        if not candidates:
            return []

        db_data = _fetch_container_data_bulk([c.name for c, _ in candidates])

        for c, dummy_labels in candidates:
            image, run_tag = get_image_parts(c)
            cfg = _resolve_strategy(c.name, dummy_labels)
            pinned = dummy_labels.get("pin", "").lower() == "true"
            current = _effective_current(cfg, run_tag, env_vars)
            drift = current != run_tag and cfg["strategy"] != STRATEGY_DOCKER_API

            d = db_data.get(c.name, {})
            available = None if pinned else d.get("available_tag")
            if available and available in d.get("dismissed_tags", set()):
                available = None
            # Config says one version but the container runs another: offer to reconcile
            if drift and not pinned and not available:
                available = current
            has_update = bool(available) and not pinned and available != run_tag

            items.append({
                "container": c.name,
                "image": image,
                "current_tag": current,
                "running_tag": run_tag,
                "drift": drift,
                "available_tag": available,
                "has_update": has_update,
                "pinned": pinned,
                "status": c.status,
                "strategy": cfg["strategy"],
                "strategy_label": STRATEGY_LABELS[cfg["strategy"]],
                "changelog": get_changelog(image, dummy_labels.get("changelog")),
                "history": d.get("history", []),
                "dismissed": d.get("dismissed", []),
                "checked_at": d.get("checked_at", ""),
                "_strategy_cfg": cfg,
            })
    except Exception as exc:
        log.error("get_monitored_containers: %s", exc)

    return sorted(items, key=lambda x: (not x["has_update"], x["container"]))


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
        _session.post(f"{NTFY_ENDPOINT}/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers, timeout=5)
    except Exception as exc:
        log.warning("notify failed: %s", exc)


def send_webhook(event: str, data: dict):
    if not WEBHOOK_URL:
        return
    payload = {"event": event, "timestamp": datetime.now().isoformat(), **data}
    try:
        resp = _session.post(WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code >= 400:
            log.warning("Webhook %s returned HTTP %s", event, resp.status_code)
    except Exception as exc:
        log.warning("Webhook failed (%s): %s", event, exc)


def _announce_success(container: str, old_tag: str, new_tag: str, status: str, updated: list):
    notify(f"Updated: {container}", f"{container}: {old_tag} → {new_tag}", tags="white_check_mark")
    send_webhook("update_success", {"container": container, "from_tag": old_tag, "to_tag": new_tag,
                                    "status": status, "containers_updated": updated})


def _announce_failure(container: str, attempted: str, reason: str):
    notify(f"Update Failed: {container}", f"{container} → {attempted}: {reason}", priority="4", tags="warning")
    send_webhook("update_failed", {"container": container, "attempted_tag": attempted, "reason": reason})


def _record_success(name: str, old_tag: str, new_tag: str, status: str):
    if old_tag and old_tag != new_tag:
        add_to_history(name, old_tag, "previous")
    add_to_history(name, new_tag, status)
    clear_available_update(name)


# ---------------------------------------------------------------------------
# Update / rollback
# ---------------------------------------------------------------------------

def _update_docker_api(client, obj, container, image, run_tag, new_tag, jl, status) -> dict:
    if run_tag == new_tag:
        return {"success": True, "message": f"Already on {new_tag}", "updated": []}
    ok, msg = replace_container(client, obj, f"{image}:{new_tag}", jl)
    if not ok:
        _announce_failure(container, new_tag, msg)
        return {"success": False, "error": msg}
    _record_success(container, run_tag, new_tag, status)
    _announce_success(container, run_tag, new_tag, status, [container])
    return {"success": True, "message": f"Updated to {new_tag}", "updated": [container]}


def _update_env_file(client, obj, container, image, run_tag, cfg, new_tag, jl, status, env_monitored) -> dict:
    var = cfg["env_var"]
    env_current = read_env().get(var, "unknown")
    jl(f"Running:    {run_tag}")
    jl(f".env:      {env_current}  ({var})")

    if run_tag == new_tag and env_current == new_tag:
        return {"success": True, "message": f"Already on {new_tag}", "updated": []}

    # Peers share this variable (e.g. immich_server and immich_machine_learning)
    peers = [p for p in env_monitored
             if p["container"] != container and p["_strategy_cfg"].get("env_var") == var]
    if peers:
        jl(f"Peers sharing {var}: {', '.join(p['container'] for p in peers)}")

    if env_current != new_tag:
        jl(f"Writing {var}={new_tag} to .env...")
        if not write_env_var(var, new_tag):
            return {"success": False, "error": f"Failed to write {var} to {ENV_FILE}"}

    updated, failures = [], []

    if run_tag != new_tag:
        ok, msg = replace_container(client, obj, f"{image}:{new_tag}", jl)
        if not ok:
            if env_current != new_tag:
                write_env_var(var, env_current)
                msg += " — .env reverted"
            _announce_failure(container, new_tag, msg)
            return {"success": False, "error": msg}
        _record_success(container, run_tag, new_tag, status)
        updated.append(container)

    for peer in peers:
        pname = peer["container"]
        if peer["running_tag"] == new_tag:
            continue
        try:
            peer_obj = client.containers.get(pname)
        except dockerlib.errors.NotFound:
            failures.append(f"{pname}: not found")
            continue
        jl(f"Replacing peer {pname} with {peer['image']}:{new_tag}...")
        ok, msg = replace_container(client, peer_obj, f"{peer['image']}:{new_tag}", jl)
        if ok:
            _record_success(pname, peer["running_tag"], new_tag, status)
            updated.append(pname)
        else:
            failures.append(f"{pname}: {msg}")

    if failures:
        # .env already points at new_tag, so the failed peers will show as drift
        # on the dashboard until they are reconciled.
        reason = "; ".join(failures)
        _announce_failure(container, new_tag, f"peer failures: {reason}")
        return {"success": False, "error": f"Peer failures: {reason}", "updated": updated}

    _announce_success(container, run_tag, new_tag, status, updated)
    return {"success": True, "message": f"Updated to {new_tag}" +
            (f" ({len(updated)} containers)" if len(updated) > 1 else ""), "updated": updated}


def _update_compose(obj, container, cfg, new_tag, jl, status) -> dict:
    compose_file, svc = cfg["compose_file"], cfg["compose_svc"]
    current = _compose_tag(compose_file, svc) or "unknown"
    jl(f"Current:    {current}")
    if current == new_tag:
        return {"success": True, "message": f"Already on {new_tag}", "updated": []}

    try:
        with open(compose_file) as f:
            snapshot = f.readlines()
    except OSError as exc:
        return {"success": False, "error": f"Cannot read {compose_file}: {exc}"}

    jl(f"Editing image tag in {compose_file}...")
    ok, _ = set_compose_image_tag(compose_file, svc, new_tag)
    if not ok:
        return {"success": False, "error": f"Failed to edit {compose_file}"}

    def _restore_file():
        try:
            with open(compose_file, "w") as f:
                f.writelines(snapshot)
        except OSError as exc:
            log.error("CRITICAL: could not restore %s: %s", compose_file, exc)

    jl(f"Running: docker compose up -d {svc}")
    ok, err = run_compose_up(compose_file, svc)
    if not ok:
        jl("Restoring compose file from snapshot...", "warn")
        _restore_file()
        _announce_failure(container, new_tag, f"compose up failed: {err}")
        return {"success": False, "error": f"docker compose up failed: {err}"}

    if not check_container_health(container, timeout=_health_timeout(obj.attrs), jlog=jl):
        jl("Rolling back compose file and redeploying previous version...", "warn")
        _restore_file()
        run_compose_up(compose_file, svc)
        _announce_failure(container, new_tag, f"health check failed, rolled back to {current}")
        return {"success": False, "error": "Health check failed — auto-rolled back"}

    _record_success(container, current, new_tag, status)
    _announce_success(container, current, new_tag, status, [container])
    return {"success": True, "message": f"Updated to {new_tag}", "updated": [container]}


def update_service(container: str, new_tag: str, job_id: str = None,
                   history_status: str = "deployed") -> dict:
    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    if not NAME_RE.match(container or "") or not TAG_RE.match(new_tag or ""):
        return {"success": False, "error": "Invalid container name or tag"}

    with _active_lock:
        if container in _active_containers:
            return {"success": False, "error": f"{container} is already being updated"}
        _active_containers.add(container)

    try:
        client = get_docker_client()
        try:
            obj = client.containers.get(container)
        except dockerlib.errors.NotFound:
            return {"success": False, "error": f"Container {container} not found"}
        if not _is_monitored(obj):
            return {"success": False, "error": f"{container} is not monitored by DUMMY"}

        image, run_tag = get_image_parts(obj)
        if image == "unknown":
            return {"success": False, "error": "Cannot determine image name"}
        cfg = _resolve_strategy(container, _get_dummy_labels(obj))
        strategy = cfg["strategy"]

        jl(f"Container:  {container}")
        jl(f"Strategy:   {STRATEGY_LABELS[strategy]}")
        jl(f"Target tag: {new_tag}")

        if strategy == STRATEGY_ENV_FILE:
            return _update_env_file(client, obj, container, image, run_tag, cfg, new_tag, jl,
                                    history_status, get_monitored_containers())
        if strategy == STRATEGY_COMPOSE:
            return _update_compose(obj, container, cfg, new_tag, jl, history_status)
        return _update_docker_api(client, obj, container, image, run_tag, new_tag, jl, history_status)

    except Exception as exc:
        log.error("update_service %s: %s", container, exc, exc_info=True)
        return {"success": False, "error": str(exc)}
    finally:
        with _active_lock:
            _active_containers.discard(container)


def rollback_service(container: str, target_tag: str = None, job_id: str = None) -> dict:
    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    if not NAME_RE.match(container or ""):
        return {"success": False, "error": "Invalid container name"}
    try:
        obj = get_docker_client().containers.get(container)
    except dockerlib.errors.NotFound:
        return {"success": False, "error": f"Container {container} not found"}
    if not _is_monitored(obj):
        return {"success": False, "error": f"{container} is not monitored by DUMMY"}

    _, run_tag = get_image_parts(obj)
    cfg = _resolve_strategy(container, _get_dummy_labels(obj))
    current = _effective_current(cfg, run_tag, read_env_cached())

    if not target_tag:
        candidates = [h["tag"] for h in get_history(container)
                      if h["tag"] != current and h["status"] in ("previous", "deployed", "rolled_back")]
        if not candidates:
            return {"success": False, "error": "No previous version to roll back to"}
        target_tag = candidates[0]

    if not TAG_RE.match(target_tag):
        return {"success": False, "error": "Invalid target tag"}

    jl(f"Rolling back {container} to {target_tag}")
    return update_service(container, target_tag, job_id=job_id, history_status="rolled_back")


# ---------------------------------------------------------------------------
# Background checker
# ---------------------------------------------------------------------------

def _check_one_container(item: dict, job_id: str = None):
    container, image, current = item["container"], item["image"], item["current_tag"]
    if image == "unknown":
        return None
    _jlog(job_id, f"Checking {container} ({current})...")
    latest = get_latest_tag(image, current)
    if latest is None:
        _jlog(job_id, "  → Registry lookup failed, keeping previous result", "warn")
        return None
    if latest == current:
        clear_available_update(container)   # stale badges go away once we're current
        _jlog(job_id, "  → Up to date")
        return None
    if is_dismissed(container, latest):
        _jlog(job_id, f"  → {latest} available but dismissed")
        return None
    _jlog(job_id, f"  → Update available: {latest}")
    save_available_update(container, latest)
    return (container, current, latest)


def _check_once(job_id: str = None, target_container: str = None) -> str:
    """Returns 'ok', 'busy', 'notfound' or 'error'."""
    global _last_check_time

    if not _check_lock.acquire(blocking=False):
        _jlog(job_id, "A check is already in progress — skipping", "warn")
        return "busy"
    _check_running.set()

    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    try:
        cfg = get_all_settings()
        containers = get_monitored_containers()
        if target_container:
            containers = [c for c in containers if c["container"] == target_container]
            if not containers:
                jl(f"Container '{target_container}' not found or not monitored", "warn")
                return "notfound"

        jl(f"Checking {len(containers)} container(s)...")
        updates_found = []
        handled = set()   # containers already updated this run, including peers

        for item in containers:
            name = item["container"]
            if name in handled:
                continue
            result = _check_one_container(item, job_id)
            if not result:
                continue
            updates_found.append(result)

            if cfg["auto_update"] and item["status"] == "running" and not item["pinned"]:
                jl(f"  → AUTO_UPDATE: applying {result[2]}...")
                res = update_service(name, result[2], job_id=job_id)
                handled.update(res.get("updated") or [name])
                if not res["success"]:
                    jl(f"  → Auto-update failed: {res['error']}", "error")

        _last_check_time = time.time()
        pending = [u for u in updates_found if u[0] not in handled]

        if pending:
            lines = "\n".join(f"- {n}: {c} → {l}" for n, c, l in pending)
            notify(f"{len(pending)} update(s) available", f"Updates ready:\n{lines}")
            send_webhook("updates_found", {"count": len(pending),
                         "updates": [{"container": n, "from": c, "to": l} for n, c, l in pending]})
            jl(f"Found {len(pending)} pending update(s). Notifications sent.")
        else:
            jl("All containers are up to date.")
        return "ok"

    except Exception as exc:
        log.error("_check_once: %s", exc, exc_info=True)
        jl(f"Check failed: {exc}", "error")
        return "error"
    finally:
        _check_running.clear()
        _check_lock.release()


def reconcile_leftovers():
    """At startup, recover any container left as {name}-dummy-old by an interrupted run."""
    try:
        client = get_docker_client()
        for c in client.containers.list(all=True):
            if not c.name.endswith(BACKUP_SUFFIX):
                continue
            base = c.name[: -len(BACKUP_SUFFIX)]
            err = _recover_leftover(client, base, lambda m, lvl="info": log.warning(m))
            if err:
                log.error("Leftover recovery: %s", err)
                notify(f"Manual action needed: {base}", err, priority="5", tags="rotating_light")
    except Exception as exc:
        log.warning("reconcile_leftovers: %s", exc)


def check_for_updates():
    global _next_check_time
    reconcile_leftovers()
    cleanup_stale_containers()
    while True:
        _check_once()
        cleanup_stale_containers()
        # Re-read the interval each time; a settings change wakes this wait early
        # and reschedules without running another check.
        while True:
            interval = get_all_settings()["check_interval"]
            _next_check_time = time.time() + interval
            if not _interval_changed.wait(interval):
                break
            _interval_changed.clear()


def _check_persistence() -> bool:
    try:
        db_dir = os.path.dirname(DB_PATH)
        return os.stat(db_dir).st_dev != os.stat(os.path.dirname(db_dir.rstrip("/"))).st_dev
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Flask app + auth
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.errorhandler(Exception)
def _handle_exception(exc):
    if isinstance(exc, HTTPException):
        return jsonify({"success": False, "error": exc.description, "status": exc.code}), exc.code
    log.error("Unhandled exception: %s", exc, exc_info=True)
    return jsonify({"success": False, "error": "Internal server error", "status": 500}), 500


def _basic_auth_ok() -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        creds = base64.b64decode(auth[6:]).decode("utf-8")
    except Exception:
        return False
    user, _, passwd = creds.partition(":")
    return hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(passwd, BASIC_AUTH_PASS)


@app.before_request
def _check_auth():
    if not BASIC_AUTH_USER or request.path == "/health":
        return
    if _basic_auth_ok():
        return
    return Response("Authentication required", status=401,
                    headers={"WWW-Authenticate": 'Basic realm="DUMMY"'})


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
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
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:var(--bg);min-height:100vh;padding:20px;
     color:var(--text-hi);transition:background .3s,color .3s}
.wrap{max-width:1100px;margin:0 auto}
.dash{background:var(--surface);backdrop-filter:blur(12px);
      border:1px solid var(--border);border-radius:18px;padding:24px;margin-bottom:20px}
.dash-title-row{display:flex;justify-content:space-between;align-items:flex-start;
                margin-bottom:20px;flex-wrap:wrap;gap:10px}
.dash-title h1{color:var(--text-hi);font-size:1.7em;margin-bottom:2px}
.dash-title p{color:var(--text-lo);font-size:.88em;font-style:italic}
.dash-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
.stat-card{background:var(--surface-2);border:1px solid var(--border);
           border-radius:12px;padding:16px 18px;text-align:center}
.stat-num{font-size:2em;font-weight:700;color:var(--accent);line-height:1}
.stat-lbl{color:var(--text-md);font-size:.82em;margin-top:5px}
.stat-card.has-updates .stat-num{color:#f59e0b}
.stat-card.all-good .stat-num{color:#34d399}
.dash-info-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.dash-panel{background:var(--surface-2);border:1px solid var(--border);
            border-radius:12px;padding:16px}
.dash-panel-lbl{font-size:.75em;font-weight:700;text-transform:uppercase;
                letter-spacing:.07em;color:var(--text-lo);margin-bottom:10px}
.flags{display:flex;flex-wrap:wrap;gap:7px}
.flag{font-size:.78em;font-weight:600;padding:4px 10px;border-radius:20px}
.flag-on {background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.flag-off{background:rgba(100,116,139,.1);color:var(--text-md);border:1px solid rgba(100,116,139,.2)}
.flag-warn{background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)}
.flag-ok {background:rgba(52,211,153,.1);color:#34d399;border:1px solid rgba(52,211,153,.2)}
.timing{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
.timing-row{display:flex;gap:6px;align-items:center;font-size:.85em;color:var(--text-md)}
.timing-row .t-val{color:var(--text-hi);font-weight:600}
.action-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
.warn-bar{background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);
          border-radius:10px;padding:10px 16px;margin-bottom:16px;color:#f59e0b;font-size:.87em}
.warn-bar code{background:var(--code-bg);padding:1px 6px;border-radius:4px;font-family:monospace}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border:none;
     border-radius:8px;font-weight:600;cursor:pointer;font-size:.87em;
     transition:all .18s;text-decoration:none;white-space:nowrap}
.btn:disabled{opacity:.4;cursor:not-allowed;pointer-events:none}
.btn-ghost{background:var(--surface-2);border:1px solid var(--border-2);color:var(--text-md)}
.btn-ghost:hover{background:var(--surface);color:var(--text-hi)}
.btn-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff}
.btn-primary:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 4px 14px rgba(99,102,241,.4)}
.btn-warn{background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)}
.btn-warn:hover:not(:disabled){background:rgba(245,158,11,.25)}
.btn-sm{padding:4px 10px;font-size:.78em;border-radius:6px}
.btn-dismiss{background:rgba(100,116,139,.12);color:var(--text-lo);border:1px solid rgba(100,116,139,.2)}
.btn-dismiss:hover{background:rgba(100,116,139,.25);color:var(--text-md)}
.card{background:var(--surface);border:1px solid var(--border);
      border-radius:14px;margin-bottom:10px;overflow:hidden;
      transition:border-color .2s,box-shadow .2s}
.card:hover{box-shadow:0 4px 20px var(--shadow)}
.card.has-update{border-left:3px solid #f59e0b}
.card.stopped-card{opacity:.7}
.card.updating{border-left:3px solid #6366f1;opacity:.85}
.card.hidden{display:none}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;
             padding:14px 18px;gap:12px}
.card-left{flex:1;min-width:0}
.cname{font-size:1.1em;font-weight:700;color:var(--text-hi);word-break:break-word;padding-top:1px}
.badges{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0}
.badge{padding:3px 9px;border-radius:20px;font-size:.72em;font-weight:700;
       text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}
.b-running {background:rgba(52,211,153,.15);color:#34d399;border:1px solid rgba(52,211,153,.3)}
.b-stopped {background:rgba(248,113,113,.15);color:#f87171;border:1px solid rgba(248,113,113,.3)}
.b-update  {background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.3)}
.b-drift   {background:rgba(245,158,11,.1);color:#f59e0b;border:1px dashed rgba(245,158,11,.4);font-size:.68em}
.b-strategy{background:rgba(99,102,241,.1);color:#a5b4fc;border:1px solid rgba(99,102,241,.2);font-size:.68em}
.b-pinned  {background:rgba(148,163,184,.1);color:var(--text-md);border:1px solid rgba(148,163,184,.25);font-size:.68em}
.b-updating{background:rgba(99,102,241,.2);color:#a5b4fc;border:1px solid rgba(99,102,241,.4);font-size:.68em}
.card-inner{padding:0 18px 18px}
.info-block{background:rgba(0,0,0,.15);border-radius:10px;padding:12px 14px;margin-bottom:12px}
[data-theme="light"] .info-block{background:rgba(0,0,0,.04)}
.info-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:6px 0}
.lbl{font-weight:600;color:var(--text-lo);min-width:80px;font-size:.83em}
.val{font-family:monospace;background:var(--val-bg);border:1px solid var(--val-border);
     padding:2px 9px;border-radius:5px;font-size:.84em;color:var(--val-text)}
.val.available{background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.3);color:#fbbf24}
.history-block{margin-bottom:12px}
.history-lbl{font-size:.75em;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
             color:var(--text-lo);margin-bottom:8px}
.h-item{display:flex;align-items:center;gap:8px;padding:5px 0;
        border-bottom:1px solid var(--border);flex-wrap:wrap}
.h-item:last-child{border-bottom:none}
.h-tag{font-family:monospace;font-size:.82em;color:var(--text-md);flex:1;min-width:120px}
.h-date{font-size:.78em;color:var(--text-lo)}
.h-pill{font-size:.7em;padding:2px 8px;border-radius:10px;font-weight:600}
.h-deployed{background:rgba(52,211,153,.1);color:#34d399}
.h-rolled_back{background:rgba(245,158,11,.1);color:#f59e0b}
.h-previous{background:rgba(100,116,139,.1);color:var(--text-md)}
.btns{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:10px;display:none;font-size:.87em}
.checked-at{font-size:.75em;color:var(--text-lo);margin-top:4px;font-style:italic}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);z-index:1000;
         display:flex;align-items:center;justify-content:center;
         opacity:0;pointer-events:none;transition:opacity .2s}
.overlay.visible{opacity:1;pointer-events:all}
.modal{background:var(--surface-2);border:1px solid var(--border-2);
       border-radius:16px;width:min(560px,95vw);display:flex;flex-direction:column;
       max-height:80vh;box-shadow:0 20px 60px var(--shadow)}
.modal-head{display:flex;align-items:center;gap:12px;padding:18px 20px;border-bottom:1px solid var(--border)}
.modal-icon{font-size:1.3em;flex-shrink:0}
.modal-title-text{font-weight:700;color:var(--text-hi);font-size:1.05em}
.modal-log{flex:1;overflow-y:auto;padding:14px 18px;font-family:monospace;
           font-size:.82em;line-height:1.6;min-height:200px;max-height:50vh}
.log-line{display:flex;gap:10px;margin:1px 0}
.log-t{color:var(--text-lo);flex-shrink:0}
.log-msg{color:var(--text-md)}
.log-msg.warn{color:#f59e0b}
.log-msg.error{color:#f87171}
.modal-foot{padding:14px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{display:inline-block;width:18px;height:18px;border:2px solid var(--border-2);
         border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
.filter-bar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.filter-input{flex:1;min-width:160px;background:var(--surface-2);border:1px solid var(--border-2);
              border-radius:8px;padding:8px 12px;color:var(--text-hi);font-size:.87em;outline:none}
.filter-input:focus{border-color:var(--accent)}
.filter-input::placeholder{color:var(--text-lo)}
.btn-filter.active{background:var(--accent-2);color:#fff;border-color:var(--accent-2)}
.setup-box{background:rgba(99,102,241,.07);border:1px solid rgba(99,102,241,.2);
           border-radius:14px;padding:28px;text-align:center;color:var(--text-md)}
.setup-box h2{color:#a5b4fc;margin-bottom:10px}
.setup-box pre{background:var(--code-bg);padding:12px 16px;border-radius:8px;text-align:left;
               font-size:.85em;color:var(--val-text);margin:12px 0;overflow-x:auto}
#btn-theme{position:fixed;top:14px;right:14px;z-index:500;padding:6px 14px;font-size:.82em;
           box-shadow:0 2px 10px var(--shadow)}
.settings-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:18px 20px}
.setting-row{display:flex;flex-direction:column;gap:6px}
.setting-lbl{font-size:.82em;font-weight:700;color:var(--text-md);text-transform:uppercase;letter-spacing:.05em}
.setting-desc{font-size:.76em;color:var(--text-lo);margin-top:-2px}
.setting-val select{background:var(--val-bg);border:1px solid var(--val-border);color:var(--text-hi);
  padding:7px 10px;border-radius:8px;font-size:.88em;width:100%;outline:none}
.toggle-wrap{display:flex;align-items:center;gap:10px;margin-top:4px}
.toggle{position:relative;width:42px;height:24px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:rgba(100,116,139,.3);border-radius:24px;cursor:pointer;transition:background .2s}
.toggle-slider:before{content:"";position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}
.toggle input:checked + .toggle-slider{background:#6366f1}
.toggle input:checked + .toggle-slider:before{transform:translateX(18px)}
.toggle-label{font-size:.88em;color:var(--text-md)}
.settings-footer{padding:14px 20px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:8px}
.settings-saved{font-size:.85em;color:#34d399;opacity:0;transition:opacity .4s}
.settings-saved.show{opacity:1}
@media(max-width:680px){
  .stat-grid{grid-template-columns:repeat(2,1fr)}
  .dash-info-row{grid-template-columns:1fr}
  .settings-grid{grid-template-columns:1fr}
}
@media(max-width:420px){
  .card-header{padding:12px 14px}
  .card-inner{padding:0 14px 14px}
}
</style>
</head>
<body>
<div class="wrap">

  <div class="dash">
    <div class="dash-title-row">
      <div class="dash-title">
        <h1>&#x1F433; {{ title }}</h1>
        <p>Docker Update Made Manageable, Yay!</p>
      </div>
      <div class="dash-controls">
        <button class="btn btn-ghost" onclick="openSettings()">&#9881; Settings</button>
      </div>
      <button class="btn btn-ghost" id="btn-theme" onclick="toggleTheme()">&#9788; Light</button>
    </div>

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

    <div class="dash-info-row">
      <div class="dash-panel">
        <div class="dash-panel-lbl">Configuration</div>
        <div class="flags">
          <span class="flag {{ 'flag-on' if allow_prerelease else 'flag-off' }}">Pre-releases {{ 'ON' if allow_prerelease else 'OFF' }}</span>
          <span class="flag {{ 'flag-on' if auto_update else 'flag-off' }}">Auto-update {{ 'ON' if auto_update else 'OFF' }}</span>
          <span class="flag {{ 'flag-on' if ntfy_enabled else 'flag-off' }}">Notifications {{ 'ON' if ntfy_enabled else 'OFF' }}</span>
          <span class="flag {{ 'flag-ok' if persistent else 'flag-warn' }}">Persistence {{ 'OK' if persistent else 'WARNING' }}</span>
          <span class="flag {{ 'flag-on' if auth_enabled else 'flag-warn' }}">Auth {{ 'ON' if auth_enabled else 'OFF' }}</span>
          <span class="flag flag-off">History {{ history_limit }} versions</span>
        </div>
      </div>

      <div class="dash-panel">
        <div class="dash-panel-lbl">Update Checks (every {{ check_interval_h }})</div>
        <div class="timing">
          <div class="timing-row"><span>&#x23F0; Last checked:</span><span class="t-val" id="last-checked">Loading...</span></div>
          <div class="timing-row"><span>&#x23F3; Next check in:</span><span class="t-val" id="next-check">Loading...</span></div>
        </div>
        <div class="action-row">
          <button class="btn btn-primary" id="btn-check" onclick="checkNow()">
            <span id="check-icon">&#8635;</span> Check Now
          </button>
          <a class="btn btn-ghost" href="/api/history/export" download="dummy-history.json">&#x2193; Export History</a>
        </div>
      </div>
    </div>
  </div>

  {% if not persistent %}
  <div class="warn-bar">
    &#9888; <strong>Persistence warning:</strong> <code>/data</code> is not bind-mounted.
    History and rollback data will be lost on restart.
  </div>
  {% endif %}

  {% if containers %}
  <div class="filter-bar">
    <input class="filter-input" id="filter-input" type="text" placeholder="&#128269; Filter containers..." oninput="filterCards()">
    <button class="btn btn-ghost btn-filter" id="btn-filter-updates" onclick="toggleFilterUpdates()">Updates only</button>
  </div>

    {% for u in containers %}
    <div class="card {% if u.has_update %}has-update{% endif %} {% if u.status != 'running' %}stopped-card{% endif %}"
         id="card-{{ u.container }}">
      <div class="card-header">
        <div class="card-left">
          <span class="cname">{{ u.container }}</span>
        </div>
        <div class="badges">
          <span class="badge {{ 'b-running' if u.status == 'running' else 'b-stopped' }}">{{ u.status }}</span>
          <span class="badge b-strategy">{{ u.strategy_label }}</span>
          {% if u.drift %}<span class="badge b-drift">config ≠ running</span>{% endif %}
          {% if u.pinned %}
          <span class="badge b-pinned">&#128274; PINNED</span>
          {% elif u.has_update %}
          <span class="badge b-update">&#x2B06; {{ u.available_tag }}</span>
          {% endif %}
        </div>
      </div>

      <div class="card-inner">
        <div class="info-block">
          <div class="info-row"><span class="lbl">Image</span><span class="val">{{ u.image }}</span></div>
          <div class="info-row"><span class="lbl">Config</span><span class="val">{{ u.current_tag }}</span></div>
          <div class="info-row"><span class="lbl">Running</span><span class="val {{ 'available' if u.drift }}">{{ u.running_tag }}</span></div>
          {% if u.available_tag %}
          <div class="info-row"><span class="lbl">Available</span><span class="val available">{{ u.available_tag }}</span></div>
          {% endif %}
        </div>

        {% if u.history %}
        <div class="history-block">
          <div class="history-lbl">Version History</div>
          {% for h in u.history %}
          <div class="h-item">
            <span class="h-tag">{{ h.tag }}</span>
            <span class="h-date">{{ h.date[:10] }}</span>
            <span class="h-pill h-{{ h.status }}">{{ h.status }}</span>
            {% if not loop.first %}
            <button class="btn btn-warn btn-sm" data-action="rollback"
                    data-container="{{ u.container }}" data-tag="{{ h.tag }}">&#x21A9; Restore</button>
            {% endif %}
          </div>
          {% endfor %}
        </div>
        {% endif %}

        <div class="msg msg-ok"  id="msg-ok-{{ u.container }}"></div>
        <div class="msg msg-err" id="msg-err-{{ u.container }}"></div>

        <div class="btns">
          {% if u.has_update and not auto_update %}
          <button class="btn btn-primary" data-action="update"
                  data-container="{{ u.container }}" data-tag="{{ u.available_tag }}">
            &#x2B06; {{ 'Reconcile to' if u.drift and u.available_tag == u.current_tag else 'Update to' }} {{ u.available_tag }}
          </button>
          <button class="btn btn-dismiss btn-sm" data-action="dismiss"
                  data-container="{{ u.container }}" data-tag="{{ u.available_tag }}"
                  title="Hide this update">&#x2715; Dismiss</button>
          {% endif %}
          {% if u.changelog %}
          <a class="btn btn-ghost" href="{{ u.changelog }}" target="_blank" rel="noopener">&#x1F4DD; Changelog</a>
          {% endif %}
        </div>
        {% if u.checked_at %}
        <div class="checked-at">Last checked: {{ u.checked_at[:16]|replace("T"," ") }}</div>
        {% endif %}
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
            <option value="21600">Every 6 hours</option>
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
            <option value="5">5 versions</option>
            <option value="10">10 versions</option>
            <option value="20">20 versions</option>
          </select>
        </div>
      </div>
      <div class="setting-row">
        <span class="setting-lbl">Pre-releases</span>
        <span class="setting-desc">Include alpha/beta/RC tags</span>
        <div class="toggle-wrap">
          <label class="toggle"><input type="checkbox" id="cfg-prerelease"><span class="toggle-slider"></span></label>
          <span class="toggle-label" id="cfg-prerelease-lbl">Off</span>
        </div>
      </div>
      <div class="setting-row">
        <span class="setting-lbl">Auto-update</span>
        <span class="setting-desc">Apply updates without confirmation</span>
        <div class="toggle-wrap">
          <label class="toggle"><input type="checkbox" id="cfg-autoupdate"><span class="toggle-slider"></span></label>
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

<div class="overlay" id="overlay">
  <div class="modal">
    <div class="modal-head">
      <span class="modal-icon" id="modal-icon"><div class="spinner"></div></span>
      <span class="modal-title-text" id="modal-title">Working...</span>
    </div>
    <div class="modal-log" id="modal-log"></div>
    <div class="modal-foot">
      <button class="btn btn-ghost" id="modal-close" disabled onclick="closeModal()">Close</button>
    </div>
  </div>
</div>

<script>
function initTheme(){ applyTheme(localStorage.getItem('dummy-theme') || 'dark'); }
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  const btn = document.getElementById('btn-theme');
  if(btn) btn.textContent = t === 'dark' ? '\u2600 Light' : '\u263D Dark';
  localStorage.setItem('dummy-theme', t);
}
function toggleTheme(){
  applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
}

let _nextCheckTs = 0;
function initStatus(){
  fetchStatus();
  setInterval(fetchStatus, 5000);
  setInterval(tickCountdown, 1000);
}
async function fetchStatus(){
  try {
    const d = await (await fetch('/api/status')).json();
    _nextCheckTs = d.next_check_time * 1000;
    const el = document.getElementById('last-checked');
    if(el) el.textContent = d.last_check_time ? ago(d.last_check_time * 1000) : 'Never';
    tickCountdown();
    document.querySelectorAll('.card[id^="card-"]').forEach(card => {
      const name = card.id.replace('card-', '');
      const isActive = d.active_updates.includes(name);
      card.classList.toggle('updating', isActive);
      const badge = card.querySelector('.b-updating');
      if(isActive && !badge){
        const b = document.createElement('span');
        b.className = 'badge b-updating';
        b.textContent = '⟳ UPDATING';
        const badges = card.querySelector('.badges');
        if(badges) badges.appendChild(b);
      } else if(!isActive && badge){
        badge.remove();
      }
    });
  } catch(e){}
}
function tickCountdown(){
  const el = document.getElementById('next-check');
  if(!el) return;
  if(!_nextCheckTs){ el.textContent = '—'; return; }
  const ms = _nextCheckTs - Date.now();
  if(ms <= 0){ el.textContent = 'Soon...'; return; }
  const h = Math.floor(ms / 3600000), m = Math.floor((ms % 3600000) / 60000), s = Math.floor((ms % 60000) / 1000);
  el.textContent = h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
}
function ago(ts){
  const m = Math.floor((Date.now() - ts) / 60000), h = Math.floor(m / 60);
  if(h > 0) return `${h}h ${m % 60}m ago`;
  if(m > 0) return `${m}m ago`;
  return 'Just now';
}

function openSettings(){
  fetch('/api/settings').then(r => r.json()).then(cfg => {
    const sel = document.getElementById('cfg-interval');
    const opts = [...sel.options].map(o => parseInt(o.value));
    sel.value = opts.reduce((a, b) => Math.abs(b - cfg.check_interval) < Math.abs(a - cfg.check_interval) ? b : a);
    document.getElementById('cfg-history').value = cfg.history_limit;
    setToggle('cfg-prerelease', 'cfg-prerelease-lbl', cfg.allow_prerelease);
    setToggle('cfg-autoupdate', 'cfg-autoupdate-lbl', cfg.auto_update);
    document.getElementById('settings-saved').classList.remove('show');
    document.getElementById('settings-overlay').classList.add('visible');
  }).catch(() => document.getElementById('settings-overlay').classList.add('visible'));
}
function closeSettings(){ document.getElementById('settings-overlay').classList.remove('visible'); }
function setToggle(id, lblId, val){
  document.getElementById(id).checked = !!val;
  document.getElementById(lblId).textContent = val ? 'On' : 'Off';
}
['cfg-prerelease','cfg-autoupdate'].forEach(id => {
  document.getElementById(id).addEventListener('change', e => {
    document.getElementById(id + '-lbl').textContent = e.target.checked ? 'On' : 'Off';
  });
});
async function saveSettings(){
  const payload = {
    check_interval: parseInt(document.getElementById('cfg-interval').value),
    history_limit: parseInt(document.getElementById('cfg-history').value),
    allow_prerelease: document.getElementById('cfg-prerelease').checked,
    auto_update: document.getElementById('cfg-autoupdate').checked,
  };
  try {
    const d = await (await fetch('/api/settings', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)
    })).json();
    if(d.success){
      const saved = document.getElementById('settings-saved');
      saved.classList.add('show');
      setTimeout(() => saved.classList.remove('show'), 2500);
      setTimeout(() => { closeSettings(); location.reload(); }, 1200);
    }
  } catch(e){ alert('Save failed: ' + e.message); }
}
document.getElementById('settings-overlay').addEventListener('click', function(e){
  if(e.target === this) closeSettings();
});

async function checkNow(){
  const btn = document.getElementById('btn-check');
  const icon = document.getElementById('check-icon');
  btn.disabled = true;
  icon.innerHTML = '<div class="spinner" style="width:14px;height:14px;display:inline-block"></div>';
  openModal('Checking for updates...');
  try {
    const r = await fetch('/api/check', {method:'POST', headers:{'Content-Type':'application/json','Accept':'application/json'}, body:'{}'});
    const ct = r.headers.get('Content-Type') || '';
    if(!ct.includes('application/json')) throw new Error(`Server returned HTTP ${r.status}`);
    const d = await r.json();
    if(d.error) throw new Error(d.error);
    const result = await pollJob(d.job_id);
    if(result && result.success) modalDone(true, result.message || 'Check complete');
    else modalDone(false, result ? (result.error || 'Check failed') : 'Lost contact with server');
  } catch(e){
    appendLog('Error: ' + e.message, 'error');
    modalDone(false, 'Check failed: ' + e.message);
  }
  btn.disabled = false;
  icon.textContent = '\u21BB';
  fetchStatus();
}

async function runJob(endpoint, body, title){
  openModal(title);
  try {
    const r = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if(!d.job_id){ modalDone(false, d.error || 'Request failed'); return; }
    const result = await pollJob(d.job_id);
    if(result && result.success) modalDone(true, result.message || 'Done', true);
    else modalDone(false, result ? (result.error || 'Unknown error') : 'Lost contact with server');
  } catch(e){
    modalDone(false, 'Network error: ' + e.message);
  }
}
function doUpdate(container, tag){
  return runJob('/api/update', {container, tag}, `Updating ${container} \u2192 ${tag}`);
}
function rollbackTo(container, tag){
  if(!confirm(`Restore ${container} to ${tag}?`)) return;
  return runJob('/api/rollback', {container, tag}, `Restoring ${container} \u2192 ${tag}`);
}
async function dismissUpdate(container, tag){
  if(!confirm(`Dismiss update to ${tag} for ${container}?\n\nIt won't appear again until a newer version is found.`)) return;
  try {
    await fetch('/api/dismiss', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({container, tag})});
    location.reload();
  } catch(e){ alert('Dismiss failed: ' + e.message); }
}

document.addEventListener('click', e => {
  const btn = e.target.closest('[data-action]');
  if(!btn) return;
  const {container, tag} = btn.dataset;
  if(btn.dataset.action === 'update') doUpdate(container, tag);
  else if(btn.dataset.action === 'rollback') rollbackTo(container, tag);
  else if(btn.dataset.action === 'dismiss') dismissUpdate(container, tag);
});

let _lastLogCount = 0;
async function pollJob(jobId){
  _lastLogCount = 0;
  return new Promise(resolve => {
    const started = Date.now();
    const iv = setInterval(async () => {
      try {
        const d = await (await fetch('/api/jobs/' + jobId)).json();
        if(!d.log) { clearInterval(iv); resolve(null); return; }
        d.log.slice(_lastLogCount).forEach(l => appendLog(l.msg, l.lvl));
        _lastLogCount = d.log.length;
        if(d.done){ clearInterval(iv); resolve(d); }
        else if(Date.now() - started > 1800000){ clearInterval(iv); resolve(null); }
      } catch(e){
        clearInterval(iv);
        resolve(null);
      }
    }, 800);
  });
}

function openModal(title){
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-icon').innerHTML = '<div class="spinner"></div>';
  document.getElementById('modal-log').innerHTML = '';
  const close = document.getElementById('modal-close');
  close.disabled = true;
  close.textContent = 'Close';
  close.className = 'btn btn-ghost';
  close.onclick = () => closeModal(false);
  document.getElementById('overlay').classList.add('visible');
  _lastLogCount = 0;
}
function appendLog(msg, level){
  const logEl = document.getElementById('modal-log');
  const line = document.createElement('div');
  line.className = 'log-line';
  const now = new Date();
  const t = [now.getHours(), now.getMinutes(), now.getSeconds()].map(n => String(n).padStart(2,'0')).join(':');
  line.innerHTML = `<span class="log-t">${t}</span><span class="log-msg ${level||''}">${escHtml(msg)}</span>`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}
function modalDone(success, msg, reload=false){
  document.getElementById('modal-icon').textContent = success ? '\u2705' : '\u274C';
  if(msg) appendLog(msg, success ? 'info' : 'error');
  const close = document.getElementById('modal-close');
  close.disabled = false;
  if(success && reload){
    close.textContent = 'Close & Refresh';
    close.className = 'btn btn-primary';
    close.onclick = () => closeModal(true);
  } else {
    close.textContent = 'Close';
    close.className = 'btn btn-ghost';
    close.onclick = () => closeModal(false);
  }
}
function closeModal(forceReload){
  document.getElementById('overlay').classList.remove('visible');
  if(forceReload) location.reload();
}
document.getElementById('overlay').addEventListener('click', function(e){
  if(e.target === this && !document.getElementById('modal-close').disabled) closeModal();
});
function escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

let _filterUpdatesOnly = false;
function filterCards(){
  const q = (document.getElementById('filter-input').value || '').toLowerCase();
  document.querySelectorAll('.card[id^="card-"]').forEach(card => {
    const name = card.id.replace('card-', '').toLowerCase();
    const matchText = !q || name.includes(q);
    const matchFilter = !_filterUpdatesOnly || card.classList.contains('has-update');
    card.classList.toggle('hidden', !matchText || !matchFilter);
  });
}
function toggleFilterUpdates(){
  _filterUpdatesOnly = !_filterUpdatesOnly;
  document.getElementById('btn-filter-updates').classList.toggle('active', _filterUpdatesOnly);
  filterCards();
}

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
    containers = get_monitored_containers()
    updates_count = sum(1 for c in containers if c["has_update"])
    running_count = sum(1 for c in containers if c["status"] == "running")
    cfg = get_all_settings()
    ci = cfg["check_interval"]
    interval_h = f"{ci // 3600}h" if ci >= 3600 else f"{ci // 60}m"
    for c in containers:
        c.pop("_strategy_cfg", None)
    return render_template_string(
        HTML,
        containers=containers,
        updates_count=updates_count,
        running_count=running_count,
        title=WEB_TITLE,
        check_interval_h=interval_h,
        allow_prerelease=cfg["allow_prerelease"],
        auto_update=cfg["auto_update"],
        ntfy_enabled=bool(NTFY_ENDPOINT),
        auth_enabled=bool(BASIC_AUTH_USER),
        history_limit=cfg["history_limit"],
        persistent=_check_persistence(),
    )


@app.route("/api/status")
def api_status():
    with _active_lock:
        active = list(_active_containers)
    return jsonify({
        "last_check_time": _last_check_time,
        "next_check_time": _next_check_time,
        "check_running": _check_running.is_set(),
        "active_updates": active,
    })


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(get_all_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Expected a JSON object"}), 400

    allowed = {"check_interval", "allow_prerelease", "auto_update", "history_limit"}
    saved, errors = {}, {}
    for key, value in data.items():
        if key not in allowed:
            errors[key] = "Unknown setting"
            continue
        try:
            if key == "check_interval":
                v = int(value)
                if not 60 <= v <= 86400:
                    errors[key] = "Must be between 60 and 86400 seconds"
                    continue
                save_setting(key, str(v))
                _interval_changed.set()
                saved[key] = v
            elif key == "history_limit":
                v = int(value)
                if not 1 <= v <= 50:
                    errors[key] = "Must be between 1 and 50"
                    continue
                save_setting(key, str(v))
                saved[key] = v
            else:
                v = "true" if str(value).lower() in ("true", "1", "yes") else "false"
                save_setting(key, v)
                saved[key] = v == "true"
        except (TypeError, ValueError) as exc:
            errors[key] = str(exc)
    log.info("Settings updated: %s", saved)
    return jsonify({"success": not errors, "saved": saved, "errors": errors})


@app.route("/api/jobs/<job_id>")
def api_job(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({k: job[k] for k in ("done", "success", "error", "message", "log")})


def _start_job(fn, *args, **kwargs):
    job_id = _new_job()

    def run():
        try:
            result = fn(*args, job_id=job_id, **kwargs)
            _job_done(job_id, result.get("success", False),
                      message=result.get("message", ""), error=result.get("error", ""))
        except Exception as exc:
            log.error("job %s crashed: %s", job_id, exc, exc_info=True)
            _job_done(job_id, False, error=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return job_id


@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.get_json(silent=True) or {}
    container, tag = data.get("container"), data.get("tag")
    if not NAME_RE.match(container or "") or not TAG_RE.match(tag or ""):
        return jsonify({"success": False, "error": "Invalid container or tag"}), 400
    return jsonify({"job_id": _start_job(update_service, container, tag)})


@app.route("/api/rollback", methods=["POST"])
def api_rollback():
    data = request.get_json(silent=True) or {}
    container, target = data.get("container"), data.get("tag")
    if not NAME_RE.match(container or ""):
        return jsonify({"success": False, "error": "Invalid container"}), 400
    if target is not None and not TAG_RE.match(target):
        return jsonify({"success": False, "error": "Invalid tag"}), 400
    return jsonify({"job_id": _start_job(rollback_service, container, target)})


@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(silent=True) or {}
    target = data.get("container")
    if target is not None and not NAME_RE.match(target):
        return jsonify({"success": False, "error": "Invalid container"}), 400
    if _check_running.is_set():
        return jsonify({"success": False, "error": "A check is already in progress"}), 429

    job_id = _new_job()

    def run():
        outcome = _check_once(job_id=job_id, target_container=target)
        messages = {
            "ok": (True, "Check complete", ""),
            "busy": (False, "", "A check is already in progress"),
            "notfound": (False, "", "Container not found or not monitored"),
            "error": (False, "", "Check failed — see log"),
        }
        success, message, error = messages.get(outcome, (False, "", "Unknown result"))
        _job_done(job_id, success, message=message, error=error)

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
        with _db() as conn:
            rows = conn.execute(
                "SELECT container, tag, deployed_at, status FROM version_history ORDER BY deployed_at ASC").fetchall()
        data = [{"container": r[0], "tag": r[1], "deployed_at": r[2], "status": r[3]} for r in rows]
        return Response(
            json.dumps({"exported_at": datetime.now().isoformat(), "history": data}, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=dummy-history.json"},
        )
    except Exception as exc:
        log.error("api_history_export: %s", exc)
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/history/import", methods=["POST"])
def api_history_import():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "Expected a JSON object"}), 400
    rows = data.get("history", [])
    if not isinstance(rows, list) or not rows:
        return jsonify({"success": False, "error": "No history entries in payload"}), 400

    inserted = skipped = 0
    with _db() as conn:
        for row in rows:
            try:
                container, tag = row["container"], row["tag"]
                deployed_at, status = row["deployed_at"], row["status"]
                if not (NAME_RE.match(container) and TAG_RE.match(tag)
                        and status in ("deployed", "previous", "rolled_back")):
                    skipped += 1
                    continue
                cur = conn.execute(
                    """INSERT INTO version_history (container, tag, deployed_at, status)
                       SELECT ?,?,?,? WHERE NOT EXISTS (
                           SELECT 1 FROM version_history WHERE container=? AND deployed_at=?)""",
                    (container, tag, deployed_at, status, container, deployed_at))
                if cur.rowcount:
                    inserted += 1
                else:
                    skipped += 1
            except (KeyError, TypeError, sqlite3.Error):
                skipped += 1
    return jsonify({"success": True, "inserted": inserted, "skipped": skipped})


@app.route("/api/history/<container>")
def api_container_history(container):
    if not NAME_RE.match(container):
        abort(400)
    return jsonify(get_history(container))


@app.route("/api/dismiss", methods=["POST"])
def api_dismiss():
    data = request.get_json(silent=True) or {}
    container, tag = data.get("container"), data.get("tag")
    if not NAME_RE.match(container or "") or not TAG_RE.match(tag or ""):
        return jsonify({"success": False, "error": "Invalid container or tag"}), 400
    dismiss_update(container, tag)
    return jsonify({"success": True})


@app.route("/api/undismiss", methods=["POST"])
def api_undismiss():
    data = request.get_json(silent=True) or {}
    container, tag = data.get("container"), data.get("tag")
    if not NAME_RE.match(container or ""):
        return jsonify({"success": False, "error": "Invalid container"}), 400
    if tag is not None and not TAG_RE.match(tag):
        return jsonify({"success": False, "error": "Invalid tag"}), 400
    undismiss_update(container, tag)
    return jsonify({"success": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("=== DUMMY - Docker Update Made Manageable, Yay! ===")
    log.info("Allow prerelease: %s | Auto-update: %s | Check interval: %ds",
             ALLOW_PRERELEASE, AUTO_UPDATE, CHECK_INTERVAL)
    if not BASIC_AUTH_USER:
        log.warning("BASIC_AUTH_USER is not set — the web UI and API are unauthenticated.")
    init_db()
    _next_check_time = time.time() + CHECK_INTERVAL
    threading.Thread(target=check_for_updates, daemon=True).start()
    # Single process only: job state and operation locks live in memory.
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
