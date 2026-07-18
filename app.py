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

BASIC_AUTH_USER  = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS  = os.environ.get("BASIC_AUTH_PASS", "")
WEBHOOK_URL      = os.environ.get("WEBHOOK_URL", "").strip()

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
# Docker client (lazy singleton — one connection, reused across requests)
# ---------------------------------------------------------------------------
_docker_client = None
_docker_lock   = threading.Lock()

def get_docker_client():
    """
    Return a connected Docker client, reconnecting automatically if the
    previous connection is stale (e.g. after a Docker daemon restart).
    """
    global _docker_client
    if _docker_client is not None:
        try:
            _docker_client.ping()   # lightweight liveness check
            return _docker_client
        except Exception:
            log.warning("Docker client connection lost — reconnecting...")
            with _docker_lock:
                _docker_client = None
    with _docker_lock:
        if _docker_client is None:
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
_jobs: dict = {}         # {job_id: {log, done, success, error, message, started}}
_jobs_lock = threading.Lock()

# Check timing
_last_check_time: float = 0.0
_next_check_time: float = 0.0

# Active operation guard — prevents two simultaneous updates on the same container
_active_containers: set = set()
_active_lock = threading.Lock()
_check_running    = threading.Event() # set while a check is in progress
_check_started_at : float = 0.0       # timestamp when check started (stuck-flag detection)


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
    _log_fn = {"error": log.error, "warn": log.warning, "warning": log.warning}.get(level, log.info)
    _log_fn("[job:%s] %s", job_id, msg)
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
    # CI/CD build tags — never real releases
    "pr-", "-pr.", "feature-", "feat-", "fix-", "hotfix-",
    "staging", "canary", "preview", "prerelease", "pre-release",
    "refs-", "sha-", "build-", "ci-", "main-", "master-",
]
_ARCH_KEYWORDS = [
    "arm64v8", "amd64", "armhf", "arm32v7", "i386", "ppc64le",
    "s390x", "linux-", "-arm64", "-armv7", "-armv6", "-aarch64",
]
# Hardware/runtime variant suffixes — these are specialised builds of the
# same release, not newer versions. e.g. v2.7.4-openvino, v2.7.4-cuda,
# v2.7.4-rocm. Filter them so the plain release tag is always preferred.
_VARIANT_KEYWORDS = [
    "-openvino", "-cuda", "-rocm", "-tensorrt", "-gpu",
    "-cpu", "-onnx", "-mlkit", "-coreml",
]

# Tags must look like a version to be considered.
# Accepts: 1.2.3  v1.2.3  V1.2.3  version-1.2.3  1.2.3-4  1.43.0.10492-abc07
# Rejects: pr-1633  feature-xyz  latest  main
_VERSION_RE = re.compile(r"^(?:version-|[vV])?(\d+)([.\-]\d+)*([.\-][a-zA-Z0-9]+)?$")


def is_stable_tag(tag: str) -> bool:
    if ALLOW_PRERELEASE:
        # Even with pre-releases on, reject obvious CI noise
        tl = tag.lower()
        return not any(kw in tl for kw in ("pr-", "feature-", "feat-", "sha-", "build-", "refs-"))
    tl = tag.lower()
    if any(kw in tl for kw in _DEV_KEYWORDS):
        return False
    if any(arch in tl for arch in _ARCH_KEYWORDS):
        return False
    if re.search(r"-ls\d+", tl):
        return False
    if re.search(r"-lt\d+-", tl):
        return False
    # Reject hardware/runtime variant tags (e.g. v2.7.4-openvino, v2.7.4-cuda)
    # These are specialised builds, not newer releases.
    if any(kw in tl for kw in _VARIANT_KEYWORDS):
        return False
    # Must look like a version number — rejects 'latest', 'main', 'pr-1633', etc.
    if not _VERSION_RE.match(tag):
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

def _req_get(url: str, headers: dict = None, timeout: int = 10,
             max_attempts: int = 3) -> "req.Response | None":
    """GET with exponential-backoff retry and Retry-After / rate-limit awareness."""
    for attempt in range(max_attempts):
        try:
            resp = req.get(url, headers=headers or {}, timeout=timeout)
            # Honour rate-limit backoff headers
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 10))
                log.warning("Rate-limited by %s — waiting %ds", url[:60], retry_after)
                time.sleep(min(retry_after, 60))
                continue
            # On transient server errors, back off and retry
            if resp.status_code >= 500 and attempt < max_attempts - 1:
                delay = 2 ** attempt
                log.debug("HTTP %s from %s, retrying in %ds", resp.status_code, url[:60], delay)
                time.sleep(delay)
                continue
            return resp
        except (req.exceptions.ConnectionError, req.exceptions.Timeout) as exc:
            if attempt < max_attempts - 1:
                delay = 2 ** attempt
                log.debug("Request error (%s), retrying in %ds: %s", type(exc).__name__, delay, url[:60])
                time.sleep(delay)
            else:
                log.error("Request failed after %d attempts: %s — %s", max_attempts, url[:60], exc)
    return None


def _ghcr_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def query_dockerhub(org: str, repo: str, current_tag: str):
    # Fetch up to 5 pages of 100 tags each (500 total) following Docker Hub
    # "next" pagination URL. Uses _req_get for retry + rate-limit handling.
    url = f"https://hub.docker.com/v2/repositories/{org}/{repo}/tags?page_size=100&ordering=last_updated"
    all_tags = []
    page = 0
    try:
        while url and page < 5:
            page += 1
            resp = _req_get(url)
            if resp is None or resp.status_code != 200:
                log.warning("DockerHub %s/%s HTTP %s", org, repo,
                            resp.status_code if resp else "no-response")
                break
            # Warn when rate limit is running low
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining and int(remaining) < 10:
                log.warning("DockerHub rate limit low: %s requests remaining", remaining)
            body = resp.json()
            all_tags.extend(t["name"] for t in body.get("results", []) if is_stable_tag(t["name"]))
            url = body.get("next")
        return _find_newest_tag(all_tags, current_tag)
    except Exception as exc:
        log.error("query_dockerhub %s/%s: %s", org, repo, exc)
        return None


def _find_newest_tag(tags: list, current_tag: str):
    """Return the highest stable tag newer than current_tag, or None."""
    newest = None
    for tag in tags:
        if not tag or tag.startswith("sha") or not is_stable_tag(tag):
            continue
        if is_newer(current_tag, tag):
            if newest is None or is_newer(newest, tag):
                newest = tag
    return newest


def _parse_link_next(link_header: str) -> str:
    """Extract the next page URL from a Link header."""
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            url = part.split(";")[0].strip().strip("<>")
            # Make absolute if registry returns a relative path
            if url.startswith("/"):
                url = "https://ghcr.io" + url
            return url
    return ""


def _query_ghcr_registry_api(org: str, repo: str, current_tag: str):
    """
    Query the Docker Registry v2 API on ghcr.io.

    Images like immich-server have 20,000+ tags sorted alphabetically.
    Naive forward pagination would need 20+ pages to reach v2.x tags.

    Strategy:
      1. Use current_tag as a `last` cursor — the registry returns tags
         that sort AFTER it alphabetically, so we land exactly where
         newer versions live. Fetch 3 pages of 200 tags (600 candidates).
      2. If that finds nothing (e.g. current_tag is not a real version,
         or the image has no newer tags in that range), fall back to a
         forward scan from the beginning capped at 5 pages.

    This means 1-3 requests for the common case instead of 10-20+.
    """
    image_path = f"{org}/{repo}"
    try:
        tok_resp = _req_get(f"https://ghcr.io/token?scope=repository:{image_path}:pull")
        if tok_resp is None or tok_resp.status_code != 200:
            log.warning("GHCR registry token %s HTTP %s", image_path,
                        tok_resp.status_code if tok_resp else "no-response")
            return None
        token = tok_resp.json().get("token")
        if not token:
            return None

        headers = {"Authorization": f"Bearer {token}"}

        def _fetch_pages(start_url: str, max_pages: int) -> list:
            tags = []
            url  = start_url
            page = 0
            while url and page < max_pages:
                page += 1
                resp = _req_get(url, headers=headers)
                if resp is None or resp.status_code != 200:
                    log.warning("GHCR tags %s page %d HTTP %s", image_path, page,
                                resp.status_code if resp else "no-response")
                    break
                tags.extend(resp.json().get("tags") or [])
                url = _parse_link_next(resp.headers.get("Link", ""))
            return tags

        # ── Pass 1: cursor from current_tag ───────────────────────────────
        # The registry returns tags alphabetically after the cursor, so we
        # land just past the current version. Fetch 20 pages of 500 (10,000
        # candidates) which covers even immich's gap of thousands of dev tags
        # between releases.
        cursor = current_tag.lstrip("vV")
        if current_tag.startswith(("v", "V")):
            cursor = "v" + cursor
        cursor_url = (f"https://ghcr.io/v2/{image_path}/tags/list"
                      f"?n=500&last={cursor}")
        all_tags = _fetch_pages(cursor_url, max_pages=20)
        result   = _find_newest_tag(all_tags, current_tag)

        if result:
            log.info("GHCR registry API (cursor) found %s for %s (current: %s)",
                     result, image_path, current_tag)
            return result

        # ── Pass 2: fallback forward scan from start ───────────────────────
        # Handles cases where current_tag cursor is unreliable (e.g. 'latest')
        log.debug("GHCR cursor pass found nothing for %s — trying forward scan",
                  image_path)
        all_tags = _fetch_pages(
            f"https://ghcr.io/v2/{image_path}/tags/list?n=1000",
            max_pages=5
        )
        result = _find_newest_tag(all_tags, current_tag)
        if result:
            log.info("GHCR registry API (forward) found %s for %s (current: %s)",
                     result, image_path, current_tag)
        return result

    except Exception as exc:
        log.error("_query_ghcr_registry_api %s/%s: %s", org, repo, exc)
        return None


def query_ghcr(org: str, repo: str, current_tag: str):
    """
    Try the GitHub Packages API first (requires token for some packages).
    Fall back to the Docker Registry v2 API on ghcr.io, which works
    anonymously for all public images.
    """
    # ── Attempt 1: GitHub Packages API (org endpoint) ──────────────────────
    url = f"https://api.github.com/orgs/{org}/packages/container/{repo}/versions?per_page=100"
    try:
        resp = _req_get(url, headers=_ghcr_headers())
        if resp is not None and resp.status_code in (401, 404):
            # Try user endpoint (personal accounts like FlareSolverr, advplyr)
            url = f"https://api.github.com/users/{org}/packages/container/{repo}/versions?per_page=100"
            resp = _req_get(url, headers=_ghcr_headers())

        if resp is not None and resp.status_code == 200:
            all_tags = []
            for version in resp.json():
                all_tags.extend(
                    version.get("metadata", {}).get("container", {}).get("tags", [])
                )
            result = _find_newest_tag(all_tags, current_tag)
            if result is not None:
                return result
            # Got 200 but found nothing newer — could be auth hiding versions
            log.debug("GHCR packages API returned 200 for %s/%s but no newer tag found", org, repo)
        else:
            log.warning("GHCR packages API %s/%s HTTP %s — falling back to registry API",
                        org, repo, resp.status_code if resp else "no-response")
    except Exception as exc:
        log.warning("GHCR packages API %s/%s error: %s — falling back to registry API", org, repo, exc)

    # ── Attempt 2: Docker Registry v2 API (always works for public images) ─
    return _query_ghcr_registry_api(org, repo, current_tag)


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

# Current schema version — increment when adding migrations below
_SCHEMA_VERSION = 2

# Each entry is (version, sql) — run in order when upgrading
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
    """Open a WAL-mode connection to the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


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

    conn = _get_conn()
    c = conn.cursor()
    # Bootstrap schema_version table
    c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    c.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version")
    current = c.fetchone()[0]
    for version, sql in _MIGRATIONS:
        if version > current:
            log.info("DB migration: applying schema v%d", version)
            c.executescript(sql)
            c.execute("INSERT OR REPLACE INTO schema_version VALUES (?)", (version,))
    conn.commit()
    conn.close()
    log.info("DB schema up to date (v%d)", _SCHEMA_VERSION)


def add_to_history(container: str, tag: str, status: str = "deployed"):
    try:
        limit = get_all_settings()["history_limit"]
        conn = _get_conn()
        c = conn.cursor()
        c.execute("INSERT INTO version_history (container,tag,deployed_at,status) VALUES (?,?,?,?)",
                  (container, tag, datetime.now().isoformat(), status))
        c.execute("""DELETE FROM version_history WHERE id NOT IN (
            SELECT id FROM version_history WHERE container=? ORDER BY deployed_at DESC LIMIT ?)""",
                  (container, limit))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("add_to_history: %s", exc)


def get_history(container: str) -> list:
    try:
        conn = _get_conn()
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
        conn = _get_conn()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO available_updates VALUES (?,?,?)",
                  (container, tag, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("save_available_update: %s", exc)


def clear_available_update(container: str):
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("DELETE FROM available_updates WHERE container=?", (container,))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("clear_available_update: %s", exc)


# ---------------------------------------------------------------------------
# Dismissed updates
# ---------------------------------------------------------------------------

def is_dismissed(container: str, tag: str) -> bool:
    """Return True if the user has dismissed this specific update."""
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT 1 FROM dismissed_updates WHERE container=? AND tag=?", (container, tag))
        found = c.fetchone() is not None
        conn.close()
        return found
    except Exception:
        return False


def dismiss_update(container: str, tag: str):
    """Dismiss an available update so it no longer shows as actionable."""
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO dismissed_updates VALUES (?,?,?)",
                  (container, tag, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        log.info("Dismissed update %s → %s", container, tag)
    except Exception as exc:
        log.error("dismiss_update: %s", exc)


def undismiss_update(container: str, tag: str = None):
    """Re-enable a dismissed update (or all for a container if tag is None)."""
    try:
        conn = _get_conn()
        c = conn.cursor()
        if tag:
            c.execute("DELETE FROM dismissed_updates WHERE container=? AND tag=?", (container, tag))
        else:
            c.execute("DELETE FROM dismissed_updates WHERE container=?", (container,))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error("undismiss_update: %s", exc)


def get_dismissed(container: str) -> list:
    """Return all dismissed tags for a container."""
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT tag, dismissed_at FROM dismissed_updates WHERE container=? ORDER BY dismissed_at DESC",
                  (container,))
        rows = c.fetchall()
        conn.close()
        return [{"tag": r[0], "dismissed_at": r[1]} for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def send_webhook(event: str, data: dict):
    """POST a JSON event payload to WEBHOOK_URL if configured."""
    if not WEBHOOK_URL:
        return
    payload = {"event": event, "timestamp": datetime.now().isoformat(), **data}
    try:
        resp = req.post(WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code >= 400:
            log.warning("Webhook %s returned HTTP %s", event, resp.status_code)
        else:
            log.debug("Webhook %s sent OK", event)
    except Exception as exc:
        log.warning("Webhook failed (%s): %s", event, exc)


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
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def save_setting(key: str, value: str):
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        log.error("save_setting %s: %s", key, exc)
        return False


def get_all_settings() -> dict:
    """Return all current effective settings (DB override or env default).
    Uses a single DB connection and one query for all keys.
    """
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings WHERE key IN (?,?,?,?)",
                  ("check_interval", "allow_prerelease", "auto_update", "history_limit"))
        overrides = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
    except Exception:
        overrides = {}

    def _bool(key, default):
        v = overrides.get(key)
        return (v == "true") if v is not None else default

    def _int(key, default):
        v = overrides.get(key)
        try: return int(v) if v is not None else default
        except (ValueError, TypeError): return default

    return {
        "check_interval":   _int("check_interval", CHECK_INTERVAL),
        "allow_prerelease": _bool("allow_prerelease", ALLOW_PRERELEASE),
        "auto_update":      _bool("auto_update", AUTO_UPDATE),
        "history_limit":    _int("history_limit", HISTORY_LIMIT),
    }


def get_available_update(container: str):
    try:
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT available_tag FROM available_updates WHERE container=?", (container,))
        row = c.fetchone()
        if not row:
            conn.close()
            return None
        tag = row[0]
        # Validate cached tag against current filter — clears stale bad tags
        # (e.g. pr-*, feature-* cached before the filter was tightened)
        if not is_stable_tag(tag):
            log.info("Clearing invalid cached tag %r for %s", tag, container)
            c.execute("DELETE FROM available_updates WHERE container=?", (container,))
            conn.commit()
            conn.close()
            return None
        conn.close()
        return tag
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
    """
    Update a variable in the .env file, preserving all other content.
    Handles all common .env formats:
      VAR=value
      VAR = value      (spaces around =)
      VAR=             (empty value)
      export VAR=value (export prefix)
    Write is atomic via a temp file + rename.
    """
    import re as _re
    # Matches: optional 'export ', the var name, optional spaces, '=', anything
    pattern = _re.compile(
        r"^(export\s+)?" + _re.escape(var) + r"\s*=.*$"
    )
    try:
        with open(ENV_FILE) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if pattern.match(line.rstrip("\n\r")):
                # Preserve 'export ' prefix if present
                prefix = "export " if line.lstrip().startswith("export ") else ""
                lines[i] = f"{prefix}{var}={value}\n"
                # Write directly — single-file bind mounts prevent atomic
                # rename across overlay/host filesystem boundary (EXDEV).
                # The file is small so direct write is safe here.
                with open(ENV_FILE, "w") as f:
                    f.writelines(lines)
                log.debug("write_env_var: set %s=%s", var, value)
                return True
        log.warning("write_env_var: %s not found in %s — check the variable name matches exactly", var, ENV_FILE)
        return False
    except PermissionError:
        log.error("write_env_var: no write permission on %s — check the volume is not mounted :ro", ENV_FILE)
    except OSError as exc:
        log.error("write_env_var: OS error writing %s: %s", ENV_FILE, exc)
    except Exception as exc:
        log.error("write_env_var: unexpected error: %s", exc)
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


def get_compose_image_tag(compose_path: str, service_name: str):
    """Read current image tag for a service from the compose file."""
    data = read_compose(compose_path)
    if not data:
        return None
    image = data.get("services", {}).get(service_name, {}).get("image", "")
    return image.rsplit(":", 1)[1] if ":" in image else None


def set_compose_image_tag(compose_path: str, service_name: str, new_tag: str):
    """
    Update a service's image tag in the compose file using line-by-line
    regex replacement. This preserves comments, formatting, key order,
    and quote styles — yaml.dump would destroy all of that.

    Returns (success, old_tag).
    """
    import re as _re
    try:
        with open(compose_path) as f:
            lines = f.readlines()
    except Exception as exc:
        log.error("set_compose_image_tag read %s: %s", compose_path, exc)
        return False, ""

    # First pass: find the service block and its indentation
    svc_indent = None
    in_service = False
    for line in lines:
        stripped = line.lstrip()
        indent   = len(line) - len(stripped)
        if stripped.startswith(f"{service_name}:"):
            svc_indent = indent
            in_service = True
            continue
        if in_service:
            if stripped and indent <= svc_indent and not stripped.startswith("#"):
                break   # left the service block
            m = _re.match(r"(\s*image\s*:\s*)(.+)", line)
            if m:
                old_image = m.group(2).strip().strip("'\"")
                base, old_tag = old_image.rsplit(":", 1) if ":" in old_image else (old_image, "latest")
                break
    else:
        old_tag = "unknown"

    if svc_indent is None:
        log.error("set_compose_image_tag: service %r not found in %s", service_name, compose_path)
        return False, ""

    # Second pass: rewrite the image line inside the service block
    svc_indent   = None
    in_service   = False
    replaced     = False
    new_lines    = []
    for line in lines:
        stripped = line.lstrip()
        indent   = len(line) - len(stripped)
        if stripped.startswith(f"{service_name}:"):
            svc_indent = indent
            in_service = True
            new_lines.append(line)
            continue
        if in_service and not replaced:
            if stripped and indent <= svc_indent and not stripped.startswith("#"):
                in_service = False   # left the service block without finding image
            else:
                m = _re.match(r"(\s*image\s*:\s*)(.+)", line)
                if m:
                    old_image = m.group(2).strip().strip("'\"")
                    base = old_image.rsplit(":", 1)[0] if ":" in old_image else old_image
                    new_lines.append(f"{m.group(1)}{base}:{new_tag}\n")
                    replaced = True
                    continue
        new_lines.append(line)

    if not replaced:
        log.error("set_compose_image_tag: image line not found for %r in %s", service_name, compose_path)
        return False, old_tag

    try:
        with open(compose_path, "w") as f:
            f.writelines(new_lines)
        log.debug("set_compose_image_tag: %s → %s:%s", service_name, base, new_tag)
        return True, old_tag
    except Exception as exc:
        log.error("set_compose_image_tag write %s: %s", compose_path, exc)
        return False, old_tag


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


def _convert_healthcheck(hc: dict):
    """
    Convert a container's raw Config.Healthcheck (Docker Engine API format,
    CapitalizedKeys, durations in nanoseconds) into the snake_case dict that
    docker-py's containers.create(healthcheck=...) expects.
    Returns None if no healthcheck override is set (image default applies).
    """
    if not hc or not hc.get("Test"):
        return None
    return {
        "test":         hc.get("Test"),
        "interval":     hc.get("Interval", 0),
        "timeout":      hc.get("Timeout", 0),
        "retries":      hc.get("Retries", 0),
        "start_period": hc.get("StartPeriod", 0),
    }


def _convert_ulimits(raw_ulimits: list):
    """Convert raw HostConfig.Ulimits dicts into docker-py Ulimit objects."""
    if not raw_ulimits:
        return None
    out = []
    for u in raw_ulimits:
        try:
            out.append(dockerlib.types.Ulimit(name=u["Name"], soft=u["Soft"], hard=u["Hard"]))
        except Exception:
            continue
    return out or None


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

    # Preserve the container's configured stop timeout / grace period
    # (compose's `stop_grace_period`). Falls back to 30s if unset.
    stop_timeout = host_cfg.get("StopTimeout")
    if not stop_timeout or stop_timeout <= 0:
        stop_timeout = 30

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
        # --- previously dropped on recreate ---
        "tmpfs":          host_cfg.get("Tmpfs") or {},
        "dns":            host_cfg.get("Dns") or [],
        "dns_search":     host_cfg.get("DnsSearch") or [],
        "sysctls":        host_cfg.get("Sysctls") or {},
        "security_opt":   host_cfg.get("SecurityOpt") or [],
        "ulimits":        _convert_ulimits(host_cfg.get("Ulimits")),
        "stop_signal":    cfg.get("StopSignal"),
        "stop_timeout":   stop_timeout,
        "healthcheck":    _convert_healthcheck(cfg.get("Healthcheck")),
    }
    create_kwargs = {k: v for k, v in create_kwargs.items() if v is not None and v != {} and v != []}

    _log(f"Pulling {new_image}...")
    try:
        client.images.pull(new_image)
    except Exception as exc:
        return False, f"Pull failed: {exc}"

    _log(f"Stopping {name}...")
    try:
        container_obj.stop(timeout=stop_timeout)
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
        client = get_docker_client()
        # Retry the initial containers.get() — Docker may not have registered
        # the new container immediately after recreate_container returns.
        container = None
        for _attempt in range(6):
            try:
                container = client.containers.get(container_name)
                break
            except dockerlib.errors.NotFound:
                if _attempt < 5:
                    time.sleep(1)
        if container is None:
            if jlog:
                jlog(f"Container {container_name} not found after recreate", "error")
            return False
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


def _fetch_container_data_bulk(container_names: list) -> dict:
    """
    Single DB round-trip to load available_updates, version_history, and
    dismissed_updates for all monitored containers at once.
    Returns a dict keyed by container name.
    """
    if not container_names:
        return {}
    try:
        limit = get_all_settings()["history_limit"]
        placeholders = ",".join("?" * len(container_names))
        conn = _get_conn()
        c = conn.cursor()

        # available_updates
        c.execute(f"SELECT container, available_tag, checked_at FROM available_updates "
                  f"WHERE container IN ({placeholders})", container_names)
        avail = {row[0]: (row[1], row[2]) for row in c.fetchall()}

        # version_history — fetch all rows then trim per container in Python
        c.execute(f"SELECT container, tag, deployed_at, status FROM version_history "
                  f"WHERE container IN ({placeholders}) ORDER BY deployed_at DESC",
                  container_names)
        hist_raw: dict = {}
        for row in c.fetchall():
            hist_raw.setdefault(row[0], []).append(
                {"tag": row[1], "date": row[2], "status": row[3]}
            )
        history = {k: v[:limit] for k, v in hist_raw.items()}

        # dismissed_updates
        c.execute(f"SELECT container, tag, dismissed_at FROM dismissed_updates "
                  f"WHERE container IN ({placeholders}) ORDER BY dismissed_at DESC",
                  container_names)
        dismissed: dict = {}
        for row in c.fetchall():
            dismissed.setdefault(row[0], []).append(
                {"tag": row[1], "dismissed_at": row[2]}
            )

        conn.close()
        result = {}
        for name in container_names:
            av_tag, checked_at = avail.get(name, (None, ""))
            dismissed_tags = {d["tag"] for d in dismissed.get(name, [])}
            # Validate cached tag still passes filter
            if av_tag and not is_stable_tag(av_tag):
                av_tag = None
            result[name] = {
                "available_tag": av_tag,
                "checked_at":    checked_at or "",
                "history":       history.get(name, []),
                "dismissed":     dismissed.get(name, []),
                "dismissed_tags": dismissed_tags,
            }
        return result
    except Exception as exc:
        log.error("_fetch_container_data_bulk: %s", exc)
        return {}


# .env cache — avoid re-reading from disk on every page load
_env_cache: dict = {}
_env_cache_mtime: float = 0.0

def read_env_cached() -> dict:
    """Return .env contents, re-reading only when the file has changed."""
    global _env_cache, _env_cache_mtime
    try:
        mtime = os.path.getmtime(ENV_FILE)
        if mtime != _env_cache_mtime:
            _env_cache = read_env()
            _env_cache_mtime = mtime
    except OSError:
        pass   # file missing — return whatever we have
    return _env_cache


def get_monitored_containers() -> list:
    items = []
    try:
        client   = get_docker_client()
        env_vars = read_env_cached()

        # Collect all monitored containers first (no DB yet)
        candidates = []
        for c in client.containers.list():
            dummy_labels   = _get_dummy_labels(c)
            in_version_map = c.name in VERSION_MAP
            if dummy_labels.get("enable", "").lower() != "true" and not in_version_map:
                continue
            candidates.append((c, dummy_labels))

        if not candidates:
            return []

        # Single DB round-trip for all container data
        names    = [c.name for c, _ in candidates]
        db_data  = _fetch_container_data_bulk(names)

        for c, dummy_labels in candidates:
            image, tag   = get_image_parts(c)
            strategy_cfg = _resolve_strategy(c.name, dummy_labels)
            strategy     = strategy_cfg["strategy"]
            pinned       = dummy_labels.get("pin", "").lower() == "true"

            if strategy == STRATEGY_ENV_FILE:
                current_tag = env_vars.get(strategy_cfg["env_var"], tag)
            elif strategy == STRATEGY_COMPOSE:
                current_tag = get_compose_image_tag(strategy_cfg["compose_file"], strategy_cfg["compose_svc"]) or tag
            else:
                current_tag = tag

            d             = db_data.get(c.name, {})
            available_tag = d.get("available_tag") if not pinned else None
            dismissed_tags= d.get("dismissed_tags", set())

            # Suppress dismissed available tag from display
            if available_tag and available_tag in dismissed_tags:
                available_tag = None

            has_update = bool(
                available_tag
                and available_tag != current_tag
                and not pinned
            )

            items.append({
                "container":      c.name,
                "image":          image,
                "current_tag":    current_tag,
                "available_tag":  available_tag,
                "has_update":     has_update,
                "pinned":         pinned,
                "status":         c.status,
                "strategy":       strategy,
                "strategy_label": STRATEGY_LABELS[strategy],
                "changelog":      get_changelog(image, dummy_labels.get("changelog")),
                "history":        d.get("history", []),
                "dismissed":      d.get("dismissed", []),
                "checked_at":     d.get("checked_at", ""),
                "_strategy_cfg":  strategy_cfg,
            })

    except Exception as exc:
        log.error("get_monitored_containers: %s", exc)

    return sorted(items, key=lambda x: (not x["has_update"], x["container"]))


# ---------------------------------------------------------------------------
# Update / rollback
# ---------------------------------------------------------------------------

def update_service(container: str, new_tag: str, job_id: str = None, history_status: str = "deployed") -> dict:
    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    with _active_lock:
        if container in _active_containers:
            return {"success": False, "error": f"{container} is already being updated"}
        _active_containers.add(container)

    try:
        client        = get_docker_client()
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
                return {"success": False, "error": f"Failed to write {var} to {ENV_FILE} — check DUMMY logs for details (common cause: volume mounted :ro)"}

            # Find all OTHER monitored containers that share the same env var
            # (e.g. immich_server and immich_machine_learning both use IMMICH_VER)
            # so they can be updated in the same operation.
            all_monitored = get_monitored_containers()
            peers = [
                c for c in all_monitored
                if c["container"] != container
                and c["_strategy_cfg"].get("env_var") == var
            ]
            if peers:
                peer_names = [p["container"] for p in peers]
                jl(f"Found {len(peers)} peer container(s) sharing {var}: {', '.join(peer_names)}")

            # Recreate primary container
            jl(f"Recreating {container} with {image}:{new_tag}...")
            ok, err = recreate_container(
                client, container_obj, f"{image}:{new_tag}",
                jlog=lambda m: jl(m)
            )
            if not ok:
                write_env_var(var, current_tag)
                return {"success": False, "error": err}

            if not check_container_health(container, jlog=jl):
                jl(f"Rolling back — restoring {var}={current_tag}", "warn")
                write_env_var(var, current_tag)
                try:
                    failed_obj = get_docker_client().containers.get(container)
                    recreate_container(client, failed_obj, f"{image}:{current_tag}",
                                       jlog=lambda m: jl(m))
                except Exception:
                    pass
                check_container_health(container, timeout=30)
                notify(f"Update Failed: {container}", f"Rolled back to {current_tag}", priority="4", tags="warning")
                send_webhook("update_failed", {"container": container, "attempted_tag": new_tag, "rolled_back_to": current_tag})
                return {"success": False, "error": "Health check failed — auto-rolled back"}

            add_to_history(container, new_tag, status=history_status)
            clear_available_update(container)

            # Recreate peer containers sharing the same env var
            peer_failures = []
            for peer in peers:
                peer_name  = peer["container"]
                peer_image = peer["image"]
                jl(f"Recreating peer {peer_name} with {peer_image}:{new_tag}...")
                try:
                    peer_obj = client.containers.get(peer_name)
                    add_to_history(peer_name, peer["current_tag"], status="previous")
                    p_ok, p_err = recreate_container(
                        client, peer_obj, f"{peer_image}:{new_tag}",
                        jlog=lambda m: jl(m)
                    )
                    if p_ok and check_container_health(peer_name, jlog=jl):
                        add_to_history(peer_name, new_tag, status=history_status)
                        clear_available_update(peer_name)
                        jl(f"Peer {peer_name} updated ✓")
                    else:
                        jl(f"Peer {peer_name} failed: {p_err}", "warn")
                        peer_failures.append(peer_name)
                except Exception as exc:
                    jl(f"Peer {peer_name} error: {exc}", "error")
                    log.error("peer update %s: %s", peer_name, exc, exc_info=True)
                    peer_failures.append(peer_name)

            all_updated = [container] + [p["container"] for p in peers if p["container"] not in peer_failures]
            summary = f"Updated to {new_tag}" + (f" ({len(all_updated)} containers)" if peers else "")
            if peer_failures:
                summary += f" — peer failures: {', '.join(peer_failures)}"

            jl(f"Done! {current_tag} → {new_tag}")
            notify(f"Updated: {container}", f"{container}: {current_tag} → {new_tag}", tags="white_check_mark")
            send_webhook("update_success", {"container": container, "from_tag": current_tag, "to_tag": new_tag,
                         "status": history_status, "peers_updated": all_updated})
            return {"success": True, "message": summary}

        # ── COMPOSE FILE ──────────────────────────────────────────────────
        elif strategy == STRATEGY_COMPOSE:
            compose_file = strategy_cfg["compose_file"]
            compose_svc  = strategy_cfg["compose_svc"]
            current_tag  = get_compose_image_tag(compose_file, compose_svc) or "unknown"
            jl(f"Current:    {current_tag}")
            add_to_history(container, current_tag, status="previous")

            # Snapshot the original lines before touching the file so we
            # can reliably restore even if the rollback file-edit also fails.
            try:
                with open(compose_file) as _f:
                    _original_compose_lines = _f.readlines()
            except Exception as _exc:
                return {"success": False, "error": f"Cannot read {compose_file}: {_exc}"}

            jl(f"Editing image tag in {compose_file}...")
            ok, old_tag = set_compose_image_tag(compose_file, compose_svc, new_tag)
            if not ok:
                return {"success": False, "error": f"Failed to edit {compose_file}"}

            jl(f"Running: docker compose up -d {compose_svc}")
            ok, err = run_compose_up(compose_file, compose_svc)
            if not ok:
                jl("Restoring compose file from snapshot...", "warn")
                try:
                    with open(compose_file, "w") as _f:
                        _f.writelines(_original_compose_lines)
                except Exception:
                    pass
                return {"success": False, "error": f"docker compose up failed: {err}"}

            if not check_container_health(container, jlog=jl):
                jl("Rolling back compose file from snapshot...", "warn")
                try:
                    with open(compose_file, "w") as _f:
                        _f.writelines(_original_compose_lines)
                    run_compose_up(compose_file, compose_svc)
                except Exception:
                    pass
                notify(f"Update Failed: {container}", f"Rolled back to {old_tag}", priority="4", tags="warning")
                send_webhook("update_failed", {"container": container, "attempted_tag": new_tag, "rolled_back_to": old_tag})
                return {"success": False, "error": "Health check failed — auto-rolled back"}

            add_to_history(container, new_tag, status=history_status)
            clear_available_update(container)
            jl(f"Done! {current_tag} → {new_tag}")
            notify(f"Updated: {container}", f"{container}: {current_tag} → {new_tag}", tags="white_check_mark")
            send_webhook("update_success", {"container": container, "from_tag": current_tag, "to_tag": new_tag, "status": history_status})
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
                send_webhook("update_failed", {"container": container, "attempted_tag": new_tag, "rolled_back_to": current_tag})
                return {"success": False, "error": "Health check failed — auto-rolled back"}

            add_to_history(container, new_tag, status=history_status)
            clear_available_update(container)
            jl(f"Done! {current_tag} → {new_tag}")
            notify(f"Updated: {container}", f"{container}: {current_tag} → {new_tag}", tags="white_check_mark")
            send_webhook("update_success", {"container": container, "from_tag": current_tag, "to_tag": new_tag, "status": history_status})
            return {"success": True, "message": f"Updated to {new_tag}"}

    except Exception as exc:
        log.error("update_service %s: %s", container, exc)
        return {"success": False, "error": str(exc)}
    finally:
        with _active_lock:
            _active_containers.discard(container)


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
        result = update_service(container, target_tag, job_id=job_id,
                                history_status="rolled_back")
        return result

    except Exception as exc:
        log.error("rollback_service %s: %s", container, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Stale data cleanup
# ---------------------------------------------------------------------------

def cleanup_stale_containers():
    """
    Remove available_updates and dismissed_updates rows for containers that
    no longer exist in Docker. Runs at startup and periodically.
    History is kept — it's useful even after a container is removed.
    """
    try:
        client = get_docker_client()
        live   = {c.name for c in client.containers.list(all=True)}
        conn   = _get_conn()
        c      = conn.cursor()
        c.execute("SELECT DISTINCT container FROM available_updates")
        stale  = [r[0] for r in c.fetchall() if r[0] not in live]
        if stale:
            placeholders = ",".join("?" * len(stale))
            c.execute(f"DELETE FROM available_updates WHERE container IN ({placeholders})", stale)
            c.execute(f"DELETE FROM dismissed_updates WHERE container IN ({placeholders})", stale)
            conn.commit()
            log.info("Cleaned up stale rows for %d removed container(s): %s",
                     len(stale), ", ".join(stale))
        conn.close()
    except Exception as exc:
        log.warning("cleanup_stale_containers: %s", exc)


# ---------------------------------------------------------------------------
# Background checker
# ---------------------------------------------------------------------------

def _check_one_container(item: dict, cfg: dict, job_id: str = None) -> tuple:
    """Check a single container item for updates. Returns (container, current, latest) or None."""
    container   = item["container"]
    image       = item["image"]
    current_tag = item["current_tag"]
    if image == "unknown":
        return None
    _jlog(job_id, f"Checking {container} ({current_tag})...")
    latest = get_latest_tag(image, current_tag)
    if latest and latest != current_tag and not is_dismissed(container, latest):
        _jlog(job_id, f"  → Update available: {latest}")
        save_available_update(container, latest)
        return (container, current_tag, latest)
    else:
        if latest and is_dismissed(container, latest):
            _jlog(job_id, f"  → {latest} available but dismissed")
        else:
            _jlog(job_id, f"  → Up to date")
        return None


def _check_once(job_id: str = None, target_container: str = None):
    global _last_check_time

    # Debounce: only one check at a time
    if _check_running.is_set():
        _jlog(job_id, "A check is already in progress — skipping", "warn")
        return
    _check_running.set()
    global _check_started_at
    _check_started_at = time.time()

    def jl(msg, level="info"):
        _jlog(job_id, msg, level)

    cfg = get_all_settings()
    try:
        all_containers = get_monitored_containers()
        # Filter to single container if requested
        containers = [c for c in all_containers if c["container"] == target_container]                      if target_container else all_containers

        if not containers and target_container:
            jl(f"Container '{target_container}' not found or not monitored", "warn")
            return

        updates_found = []
        jl(f"Checking {len(containers)} container(s)...")

        for item in containers:
            result = _check_one_container(item, cfg, job_id)
            if result:
                updates_found.append(result)
                container, current_tag, latest = result
                if cfg["auto_update"]:
                    jl(f"  → AUTO_UPDATE: applying {latest}...")
                    res = update_service(container, latest, job_id=job_id)
                    if not res["success"]:
                        jl(f"  → Auto-update failed: {res['error']}", "error")

        _last_check_time = time.time()

        if updates_found and not cfg["auto_update"]:
            lines = "\n".join(f"- {n}: {c} → {l}" for n, c, l in updates_found)
            notify(f"{len(updates_found)} update(s) available", f"Updates ready:\n{lines}")
            send_webhook("updates_found", {"count": len(updates_found),
                         "updates": [{"container": n, "from": c, "to": l} for n, c, l in updates_found]})
            jl(f"Found {len(updates_found)} update(s). Notifications sent.")
        elif not updates_found:
            jl("All containers are up to date.")

    except Exception as exc:
        log.error("_check_once: %s", exc)
        jl(f"Check failed: {exc}", "error")
    finally:
        _check_running.clear()


def check_for_updates():
    global _next_check_time
    # Clean up stale rows on first run
    cleanup_stale_containers()
    while True:
        log.info("Running update check...")
        _check_once()
        cleanup_stale_containers()   # clean up after each check too
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

import base64 as _base64
import hmac as _hmac
from functools import wraps
from flask import abort
from werkzeug.exceptions import HTTPException

@app.errorhandler(Exception)
def _handle_exception(exc):
    """Return all unhandled exceptions as JSON so the UI always gets parseable responses."""
    if isinstance(exc, HTTPException):
        return jsonify({"success": False, "error": exc.description, "status": exc.code}), exc.code
    log.error("Unhandled exception: %s", exc, exc_info=True)
    return jsonify({"success": False, "error": "Internal server error", "status": 500}), 500

def _valid_creds(user: str, passwd: str) -> bool:
    """Constant-time credential comparison to avoid timing side-channels."""
    user_ok = _hmac.compare_digest(user.encode("utf-8"), BASIC_AUTH_USER.encode("utf-8"))
    pass_ok = _hmac.compare_digest(passwd.encode("utf-8"), BASIC_AUTH_PASS.encode("utf-8"))
    return user_ok and pass_ok


def _require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not BASIC_AUTH_USER:
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                creds = _base64.b64decode(auth[6:]).decode("utf-8")
                user, _, passwd = creds.partition(":")
                if _valid_creds(user, passwd):
                    return f(*args, **kwargs)
            except Exception:
                pass
        return Response(
            "Authentication required",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="DUMMY"'},
        )
    return decorated

# Apply auth globally
@app.before_request
def _check_auth():
    if not BASIC_AUTH_USER:
        return  # auth disabled
    # Allow health endpoint without auth (for Docker HEALTHCHECK)
    if request.path == "/health":
        return
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            creds = _base64.b64decode(auth[6:]).decode("utf-8")
            user, _, passwd = creds.partition(":")
            if _valid_creds(user, passwd):
                return  # authenticated
        except Exception:
            pass
    return Response(
        "Authentication required",
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="DUMMY"'},
    )

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

/* ── Search / filter bar ─────────────────────────────────────────────────── */
.filter-bar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.filter-input{flex:1;min-width:160px;background:var(--surface-2);
              border:1px solid var(--border-2);border-radius:8px;
              padding:8px 12px;color:var(--text-hi);font-size:.87em;outline:none;
              transition:border-color .2s}
.filter-input:focus{border-color:var(--accent)}
.filter-input::placeholder{color:var(--text-lo)}
.btn-filter{padding:7px 14px;font-size:.82em}
.btn-filter.active{background:var(--accent-2);color:#fff;border-color:var(--accent-2)}
.card.hidden{display:none}

/* ── Pinned badge ─────────────────────────────────────────────────────────── */
.b-pinned{background:rgba(148,163,184,.1);color:var(--text-md);
          border:1px solid rgba(148,163,184,.25);font-size:.68em}

/* ── Updating badge ───────────────────────────────────────────────────────── */
.b-updating{background:rgba(99,102,241,.2);color:#a5b4fc;
            border:1px solid rgba(99,102,241,.4);font-size:.68em}
.card.updating{border-left:3px solid #6366f1;opacity:.85}

/* ── Dismiss button ───────────────────────────────────────────────────────── */
.btn-dismiss{background:rgba(100,116,139,.12);color:var(--text-lo);
             border:1px solid rgba(100,116,139,.2)}
.btn-dismiss:hover{background:rgba(100,116,139,.25);color:var(--text-md)}

/* ── Checked-at row ───────────────────────────────────────────────────────── */
.checked-at{font-size:.75em;color:var(--text-lo);margin-top:4px;font-style:italic}

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

  <!-- ── Filter bar ──────────────────────────────────────────────────────── -->
  {% if containers %}
  <div class="filter-bar">
    <input class="filter-input" id="filter-input" type="text"
           placeholder="&#128269; Filter containers..." oninput="filterCards()">
    <button class="btn btn-ghost btn-filter" id="btn-filter-updates"
            onclick="toggleFilterUpdates()">Updates only</button>
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
          {% if u.pinned %}
          <span class="badge b-pinned">&#128274; PINNED</span>
          {% elif u.has_update %}
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
            <button class="btn btn-dismiss btn-sm"
                    onclick="dismissUpdate('{{ u.container }}','{{ u.available_tag }}')"
                    title="Hide this update">
              &#x2715; Dismiss
            </button>
            {% endif %}
            {% if u.changelog %}
            <a class="btn btn-ghost"
               href="{{ u.changelog }}" target="_blank" rel="noopener">
              &#x1F4DD; Changelog
            </a>
            {% endif %}
          </div>
          {% if u.checked_at %}
          <div class="checked-at">Last checked: {{ u.checked_at[:16]|replace("T"," ") }}</div>
          {% endif %}

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
  // Poll frequently while updates are active, slower otherwise
  setInterval(async () => {
    await fetchStatus();
  }, 5000);
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
    // Mark cards that are currently being updated
    if(d.active_updates) {
      document.querySelectorAll('.card[id^="card-"]').forEach(card => {
        const name = card.id.replace('card-', '');
        const isActive = d.active_updates.includ
