"""
Unit tests for DUMMY — run with: python3 -m pytest test_dummy.py -v
Covers: tag filtering, version comparison, registry URL routing, webhook,
        auth helper, settings defaults.
"""

import sys
import types
import unittest
import tempfile
import shutil
import sqlite3
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so app.py can be imported without Docker / Flask / etc.
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock as _MM
for mod in ("docker", "yaml"):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

# Build a proper requests stub with .get, .post, and .exceptions
if "requests" not in sys.modules:
    req_stub = types.ModuleType("requests")
    req_stub.get  = _MM()
    req_stub.post = _MM()
    exc_mod = types.ModuleType("requests.exceptions")
    exc_mod.ConnectionError = ConnectionError
    exc_mod.Timeout = TimeoutError
    req_stub.exceptions = exc_mod
    sys.modules["requests"] = req_stub
    sys.modules["requests.exceptions"] = exc_mod

import importlib, os

os.environ.setdefault("DB_PATH", "/tmp/dummy_test.db")
os.environ.setdefault("ENV_FILE_PATH", "/tmp/dummy_test.env")

# Patch threading so the background thread doesn't start
import threading as _threading_real
with patch("threading.Thread"):
    import app as dummy


# ===========================================================================
# Tag filtering
# ===========================================================================
class TestIsStableTag(unittest.TestCase):

    # ── Tags that MUST be accepted ──────────────────────────────────────────
    def test_simple_semver(self):
        for tag in ("1.2.3", "v1.2.3", "V1.2.3", "3.4.6"):
            with self.subTest(tag=tag):
                self.assertTrue(dummy.is_stable_tag(tag), f"Should accept: {tag}")

    def test_version_prefix(self):
        for tag in ("version-4.0.16.2944", "version-1.2"):
            with self.subTest(tag=tag):
                self.assertTrue(dummy.is_stable_tag(tag), f"Should accept: {tag}")

    def test_plex_style(self):
        self.assertTrue(dummy.is_stable_tag("1.43.0.10492-121068a07"))

    def test_qbittorrent_style(self):
        self.assertTrue(dummy.is_stable_tag("5.1.4-2"))

    def test_major_only(self):
        self.assertTrue(dummy.is_stable_tag("v2"))

    def test_uppercase_V(self):
        self.assertTrue(dummy.is_stable_tag("V1.10.1"))

    def test_four_part(self):
        self.assertTrue(dummy.is_stable_tag("6.0.4.10291"))

    # ── CI / branch tags that MUST be rejected ─────────────────────────────
    def test_pr_tag(self):
        for tag in ("pr-1633", "PR-1633", "pr-1543"):
            with self.subTest(tag=tag):
                self.assertFalse(dummy.is_stable_tag(tag), f"Should reject: {tag}")

    def test_feature_tag(self):
        for tag in ("feature-deps-180425", "feat-something", "feature-xyz"):
            with self.subTest(tag=tag):
                self.assertFalse(dummy.is_stable_tag(tag), f"Should reject: {tag}")

    def test_ci_keywords(self):
        for tag in ("nightly", "edge", "dev", "alpha", "beta", "rc1",
                    "staging", "canary", "preview", "build-123", "sha-abc"):
            with self.subTest(tag=tag):
                self.assertFalse(dummy.is_stable_tag(tag), f"Should reject: {tag}")

    def test_arch_tags(self):
        for tag in ("1.2.3-arm64", "1.2.3-amd64", "1.2.3-armv7"):
            with self.subTest(tag=tag):
                self.assertFalse(dummy.is_stable_tag(tag), f"Should reject: {tag}")

    def test_linuxserver_ls_suffix(self):
        self.assertFalse(dummy.is_stable_tag("6.0.4.10291-ls293"))

    def test_plain_words_rejected(self):
        for tag in ("latest", "main", "master", "develop", "stable"):
            with self.subTest(tag=tag):
                self.assertFalse(dummy.is_stable_tag(tag), f"Should reject: {tag}")

    # ── Pre-release mode ────────────────────────────────────────────────────
    def test_prerelease_mode_allows_rc(self):
        with patch.object(dummy, "ALLOW_PRERELEASE", True):
            self.assertTrue(dummy.is_stable_tag("v2.0.0-rc1"))

    def test_prerelease_mode_still_rejects_pr(self):
        with patch.object(dummy, "ALLOW_PRERELEASE", True):
            self.assertFalse(dummy.is_stable_tag("pr-1633"))


# ===========================================================================
# Version comparison
# ===========================================================================
class TestVersionComparison(unittest.TestCase):

    def test_newer_patch(self):
        self.assertTrue(dummy.is_newer("1.2.3", "1.2.4"))

    def test_newer_minor(self):
        self.assertTrue(dummy.is_newer("1.2.3", "1.3.0"))

    def test_newer_major(self):
        self.assertTrue(dummy.is_newer("1.9.9", "2.0.0"))

    def test_not_newer_same(self):
        self.assertFalse(dummy.is_newer("1.2.3", "1.2.3"))

    def test_not_newer_older(self):
        self.assertFalse(dummy.is_newer("2.0.0", "1.9.9"))

    def test_v_prefix_stripped(self):
        self.assertTrue(dummy.is_newer("v2.5.6", "v2.5.7"))
        self.assertTrue(dummy.is_newer("v2.5.6", "2.5.7"))

    def test_version_prefix_stripped(self):
        self.assertTrue(dummy.is_newer("version-4.0.16.2944", "version-4.0.17.0"))

    def test_four_part(self):
        self.assertTrue(dummy.is_newer("6.0.4.10291", "6.0.4.10292"))

    def test_unrelated_strings_dont_crash(self):
        # should return False, not raise
        self.assertFalse(dummy.is_newer("abc", "xyz"))


# ===========================================================================
# _find_newest_tag
# ===========================================================================
class TestFindNewestTag(unittest.TestCase):

    def test_finds_highest(self):
        tags = ["v1.0.0", "v1.2.0", "v1.1.0", "v0.9.0"]
        self.assertEqual(dummy._find_newest_tag(tags, "v1.0.0"), "v1.2.0")

    def test_returns_none_when_all_older(self):
        self.assertIsNone(dummy._find_newest_tag(["v1.0.0", "v0.9.0"], "v1.2.0"))

    def test_filters_pr_tags(self):
        tags = ["pr-1000", "v2.0.0", "feature-xyz"]
        self.assertEqual(dummy._find_newest_tag(tags, "v1.0.0"), "v2.0.0")

    def test_empty_list(self):
        self.assertIsNone(dummy._find_newest_tag([], "v1.0.0"))

    def test_skips_sha_tags(self):
        tags = ["sha256abc", "v1.1.0"]
        self.assertEqual(dummy._find_newest_tag(tags, "v1.0.0"), "v1.1.0")


# ===========================================================================
# Registry URL routing (get_latest_tag dispatch)
# ===========================================================================
class TestGetLatestTagDispatch(unittest.TestCase):

    def test_ghcr_routes_to_query_ghcr(self):
        with patch.object(dummy, "query_ghcr", return_value="v2.0.0") as m:
            result = dummy.get_latest_tag("ghcr.io/myorg/myrepo", "v1.0.0")
            m.assert_called_once_with("myorg", "myrepo", "v1.0.0")
            self.assertEqual(result, "v2.0.0")

    def test_lscr_routes_to_dockerhub_linuxserver(self):
        with patch.object(dummy, "query_dockerhub", return_value="7.0.0") as m:
            dummy.get_latest_tag("lscr.io/linuxserver/radarr", "6.0.0")
            m.assert_called_once_with("linuxserver", "radarr", "6.0.0")

    def test_dockerhub_two_part(self):
        with patch.object(dummy, "query_dockerhub", return_value=None) as m:
            dummy.get_latest_tag("adguard/adguardhome", "v0.107.0")
            m.assert_called_once_with("adguard", "adguardhome", "v0.107.0")

    def test_dockerhub_with_prefix(self):
        with patch.object(dummy, "query_dockerhub", return_value=None) as m:
            dummy.get_latest_tag("docker.io/adguard/adguardhome", "v0.107.0")
            m.assert_called_once_with("adguard", "adguardhome", "v0.107.0")

    def test_unknown_image_returns_none(self):
        # image == "unknown" is filtered out in _check_once, but get_latest_tag
        # should at least not crash
        with patch.object(dummy, "query_dockerhub", return_value=None):
            result = dummy.get_latest_tag("", "1.0.0")
            # empty string falls through to library lookup — just check no crash
            self.assertIsNone(result)


# ===========================================================================
# Changelog URL generation
# ===========================================================================
class TestChangelogURLs(unittest.TestCase):

    def test_linuxserver_has_docker_prefix(self):
        url = dummy.get_changelog("lscr.io/linuxserver/radarr")
        self.assertIn("docker-radarr", url)

    def test_linuxserver_sonarr(self):
        url = dummy.get_changelog("lscr.io/linuxserver/sonarr")
        self.assertIn("docker-sonarr", url)

    def test_immich(self):
        url = dummy.get_changelog("ghcr.io/immich-app/immich-server")
        self.assertIn("immich-app/immich", url)

    def test_label_override(self):
        url = dummy.get_changelog("anything", label_override="https://custom.example.com")
        self.assertEqual(url, "https://custom.example.com")

    def test_unknown_image_returns_none(self):
        self.assertIsNone(dummy.get_changelog("my-private-registry.io/myapp"))


# ===========================================================================
# Settings
# ===========================================================================
class TestGetAllSettings(unittest.TestCase):

    def test_returns_env_defaults_when_no_db_overrides(self):
        with patch.object(dummy, "CHECK_INTERVAL", 21600), \
             patch.object(dummy, "ALLOW_PRERELEASE", False), \
             patch.object(dummy, "AUTO_UPDATE", False), \
             patch.object(dummy, "HISTORY_LIMIT", 5):
            # Simulate DB returning no overrides
            with patch("sqlite3.connect") as mock_conn:
                mock_cur = MagicMock()
                mock_cur.fetchall.return_value = []
                mock_conn.return_value.__enter__ = lambda s: s
                mock_conn.return_value.cursor.return_value = mock_cur
                mock_conn.return_value.execute.return_value = None
                # Bypass WAL pragma by mocking _get_conn
                with patch.object(dummy, "_get_conn") as mock_get:
                    mock_c = MagicMock()
                    mock_c.cursor.return_value.fetchall.return_value = []
                    mock_get.return_value = mock_c
                    cfg = dummy.get_all_settings()
                    self.assertEqual(cfg["check_interval"], 21600)
                    self.assertEqual(cfg["allow_prerelease"], False)
                    self.assertEqual(cfg["auto_update"], False)
                    self.assertEqual(cfg["history_limit"], 5)


# ===========================================================================
# Webhook helper
# ===========================================================================
class TestSendWebhook(unittest.TestCase):

    def test_no_op_when_url_empty(self):
        with patch.object(dummy, "WEBHOOK_URL", ""):
            with patch("requests.post") as mock_post:
                dummy.send_webhook("test_event", {"foo": "bar"})
                mock_post.assert_not_called()

    def test_posts_json_with_event(self):
        with patch.object(dummy, "WEBHOOK_URL", "http://hooks.example.com/test"):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            with patch.object(dummy.req, "post", return_value=mock_resp) as mock_post:
                dummy.send_webhook("update_success", {"container": "radarr"})
                mock_post.assert_called_once()
                args, kwargs = mock_post.call_args
                payload = kwargs.get("json") or args[1]
                self.assertEqual(payload["event"], "update_success")
                self.assertEqual(payload["container"], "radarr")

    def test_logs_warning_on_4xx(self):
        with patch.object(dummy, "WEBHOOK_URL", "http://hooks.example.com/test"):
            mock_resp = MagicMock()
            mock_resp.status_code = 403
            with patch.object(dummy.req, "post", return_value=mock_resp):
                with self.assertLogs("dummy", level="WARNING"):
                    dummy.send_webhook("test", {})


# ===========================================================================
# _req_get retry logic
# ===========================================================================
class TestReqGet(unittest.TestCase):

    def test_returns_response_on_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(dummy.req, "get", return_value=mock_resp) as m:
            result = dummy._req_get("http://example.com")
            self.assertEqual(result, mock_resp)
            self.assertEqual(m.call_count, 1)

    def test_retries_on_500(self):
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        err_resp = MagicMock()
        err_resp.status_code = 500
        with patch.object(dummy.req, "get", side_effect=[err_resp, ok_resp]) as m:
            with patch("time.sleep"):
                result = dummy._req_get("http://example.com", max_attempts=2)
                self.assertEqual(m.call_count, 2)
                self.assertEqual(result, ok_resp)

    def test_returns_none_after_exhausting_retries(self):
        err = dummy.req.exceptions.ConnectionError("boom")
        with patch.object(dummy.req, "get", side_effect=err):
            with patch("time.sleep"):
                result = dummy._req_get("http://example.com", max_attempts=2)
                self.assertIsNone(result)

    def test_respects_retry_after_on_429(self):
        rate_resp = MagicMock()
        rate_resp.status_code = 429
        rate_resp.headers = {"Retry-After": "2"}
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        with patch.object(dummy.req, "get", side_effect=[rate_resp, ok_resp]):
            with patch("time.sleep") as mock_sleep:
                dummy._req_get("http://example.com", max_attempts=2)
                mock_sleep.assert_called_once_with(2)


# ===========================================================================
# Dismiss logic
# ===========================================================================
class TestDismiss(unittest.TestCase):

    def _mock_conn(self, fetchone_val=None):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = fetchone_val
        conn.cursor.return_value = cur
        return conn, cur

    def test_is_dismissed_true(self):
        conn, cur = self._mock_conn(fetchone_val=(1,))
        with patch.object(dummy, "_get_conn", return_value=conn):
            self.assertTrue(dummy.is_dismissed("radarr", "v7.0.0"))

    def test_is_dismissed_false(self):
        conn, cur = self._mock_conn(fetchone_val=None)
        with patch.object(dummy, "_get_conn", return_value=conn):
            self.assertFalse(dummy.is_dismissed("radarr", "v7.0.0"))


# ===========================================================================
# recreate_container: config preservation helpers
# ===========================================================================
class TestConvertHealthcheck(unittest.TestCase):

    def test_none_returns_none(self):
        self.assertIsNone(dummy._convert_healthcheck(None))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(dummy._convert_healthcheck({}))

    def test_no_test_key_returns_none(self):
        # Docker sets Config.Healthcheck to {} when the image has none,
        # but some engine versions include a dict with an empty Test.
        self.assertIsNone(dummy._convert_healthcheck({"Test": []}))

    def test_converts_capitalized_to_snake_case(self):
        raw = {
            "Test": ["CMD", "curl", "-f", "http://localhost:8191/docs"],
            "Interval": 30_000_000_000,
            "Timeout": 10_000_000_000,
            "Retries": 3,
            "StartPeriod": 30_000_000_000,
        }
        result = dummy._convert_healthcheck(raw)
        self.assertEqual(result["test"], raw["Test"])
        self.assertEqual(result["interval"], 30_000_000_000)
        self.assertEqual(result["timeout"], 10_000_000_000)
        self.assertEqual(result["retries"], 3)
        self.assertEqual(result["start_period"], 30_000_000_000)

    def test_missing_optional_fields_default_to_zero(self):
        result = dummy._convert_healthcheck({"Test": ["NONE"]})
        self.assertEqual(result["interval"], 0)
        self.assertEqual(result["timeout"], 0)
        self.assertEqual(result["retries"], 0)
        self.assertEqual(result["start_period"], 0)


class TestConvertUlimits(unittest.TestCase):

    def test_none_returns_none(self):
        self.assertIsNone(dummy._convert_ulimits(None))

    def test_empty_list_returns_none(self):
        self.assertIsNone(dummy._convert_ulimits([]))

    def test_converts_raw_dicts_to_ulimit_objects(self):
        # Stub docker.types.Ulimit since the `docker` module itself is
        # stubbed out for these tests (see module setup above).
        fake_ulimit_cls = MagicMock(side_effect=lambda name, soft, hard: {"name": name, "soft": soft, "hard": hard})
        fake_types = types.SimpleNamespace(Ulimit=fake_ulimit_cls)
        with patch.object(dummy.dockerlib, "types", fake_types, create=True):
            raw = [{"Name": "nofile", "Soft": 1024, "Hard": 2048}]
            result = dummy._convert_ulimits(raw)
        self.assertEqual(len(result), 1)
        fake_ulimit_cls.assert_called_once_with(name="nofile", soft=1024, hard=2048)

    def test_malformed_entry_is_skipped_not_fatal(self):
        fake_ulimit_cls = MagicMock(side_effect=lambda name, soft, hard: object())
        fake_types = types.SimpleNamespace(Ulimit=fake_ulimit_cls)
        with patch.object(dummy.dockerlib, "types", fake_types, create=True):
            raw = [{"Name": "nofile"}]  # missing Soft/Hard -> KeyError, should be skipped
            result = dummy._convert_ulimits(raw)
        self.assertIsNone(result)


class TestRecreateContainerStopTimeout(unittest.TestCase):
    """
    Verifies recreate_container reads HostConfig.StopTimeout (compose's
    stop_grace_period) instead of always hardcoding 30s.
    """

    def _make_container(self, stop_timeout=None, tmpfs=None):
        c = MagicMock()
        c.name = "qbittorrent"
        c.attrs = {
            "HostConfig": {
                "NetworkMode": "host",
                "Binds": [],
                **({"StopTimeout": stop_timeout} if stop_timeout is not None else {}),
                **({"Tmpfs": tmpfs} if tmpfs is not None else {}),
            },
            "NetworkSettings": {"Networks": {}},
            "Config": {"Env": [], "Labels": {}},
        }
        return c

    def test_uses_configured_stop_timeout(self):
        client = MagicMock()
        container = self._make_container(stop_timeout=1800, tmpfs={"/tmp": ""})
        client.containers.create.return_value = MagicMock()
        ok, err = dummy.recreate_container(client, container, "qbittorrentofficial/qbittorrent-nox:5.2.4-1")
        self.assertTrue(ok, err)
        container.stop.assert_called_once_with(timeout=1800)
        create_kwargs = client.containers.create.call_args.kwargs
        self.assertEqual(create_kwargs.get("stop_timeout"), 1800)
        self.assertEqual(create_kwargs.get("tmpfs"), {"/tmp": ""})

    def test_falls_back_to_30s_when_unset(self):
        client = MagicMock()
        container = self._make_container(stop_timeout=None)
        client.containers.create.return_value = MagicMock()
        ok, err = dummy.recreate_container(client, container, "someimage:latest")
        self.assertTrue(ok, err)
        container.stop.assert_called_once_with(timeout=30)


# ===========================================================================
# Constant-time auth comparison
# ===========================================================================
class TestDbBackup(unittest.TestCase):
    """Scheduled SQLite backups via the online backup API + pruning/listing."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "versions.db")
        self.backup_dir = os.path.join(self.tmpdir, "backups")
        self._patches = [
            patch.object(dummy, "DB_PATH", self.db_path),
            patch.object(dummy, "DB_BACKUP_DIR", self.backup_dir),
            patch.object(dummy, "DB_BACKUP_KEEP", 3),
        ]
        for p in self._patches:
            p.start()
        dummy.init_db()
        dummy.add_to_history("plex", "1.43.3.10828-00f62d37d", status="deployed")

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_backup_creates_file_with_matching_data(self):
        path = dummy.backup_db()
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))

        # The backup should be a fully independent, queryable SQLite DB
        # with the same data as the live one at backup time.
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT container, tag, status FROM version_history").fetchall()
        conn.close()
        self.assertIn(("plex", "1.43.3.10828-00f62d37d", "deployed"), rows)

    def test_backup_survives_missing_db_gracefully(self):
        os.remove(self.db_path)
        # sqlite3.connect() on a missing path just creates an empty DB file,
        # so this should still succeed rather than raise.
        path = dummy.backup_db()
        self.assertIsNotNone(path)

    def test_prune_keeps_only_most_recent_n(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        # Create 5 fake backup files with distinct sortable names
        for i in range(5):
            open(os.path.join(self.backup_dir, f"versions-2026070{i}-000000.db"), "w").close()

        dummy._prune_old_backups()

        remaining = sorted(os.listdir(self.backup_dir))
        self.assertEqual(len(remaining), 3)
        # Should have kept the lexicographically-latest (most recent) three
        self.assertEqual(remaining, [
            "versions-20260702-000000.db",
            "versions-20260703-000000.db",
            "versions-20260704-000000.db",
        ])

    def test_list_db_backups_reports_size_and_time(self):
        dummy.backup_db()
        dummy.backup_db()
        backups = dummy.list_db_backups()
        self.assertEqual(len(backups), 2)
        for b in backups:
            self.assertIn("filename", b)
            self.assertIn("size_kb", b)
            self.assertIn("created_at", b)
            self.assertGreater(b["size_kb"], 0)

    def test_list_db_backups_empty_when_no_dir(self):
        self.assertEqual(dummy.list_db_backups(), [])

    def test_backup_never_raises_on_unwritable_dir(self):
        with patch.object(dummy, "DB_BACKUP_DIR", "/nonexistent/root/path/nope"), \
             patch("os.makedirs", side_effect=PermissionError("nope")):
            result = dummy.backup_db()
        self.assertIsNone(result)


class TestJobTracking(unittest.TestCase):
    """Crash-safe job tracking: active_jobs table read/write + startup check."""

    def _mock_conn(self, fetchall_val=None):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = fetchall_val or []
        conn.cursor.return_value = cur
        return conn, cur

    def test_job_begin_writes_row(self):
        conn, cur = self._mock_conn()
        with patch.object(dummy, "_get_conn", return_value=conn):
            dummy._job_begin("plex", "job-1", "env_file", "1.43.2", "1.43.3")
        sql, params = cur.execute.call_args[0]
        self.assertIn("INSERT OR REPLACE INTO active_jobs", sql)
        self.assertEqual(params[0], "plex")
        self.assertEqual(params[3], "1.43.2")
        self.assertEqual(params[4], "1.43.3")
        self.assertEqual(params[5], "started")
        conn.commit.assert_called_once()

    def test_job_step_updates_step_column(self):
        conn, cur = self._mock_conn()
        with patch.object(dummy, "_get_conn", return_value=conn):
            dummy._job_step("plex", "recreated")
        sql, params = cur.execute.call_args[0]
        self.assertIn("UPDATE active_jobs SET step=?", sql)
        self.assertEqual(params, ("recreated", "plex"))

    def test_job_end_deletes_row(self):
        conn, cur = self._mock_conn()
        with patch.object(dummy, "_get_conn", return_value=conn):
            dummy._job_end("plex")
        sql, params = cur.execute.call_args[0]
        self.assertIn("DELETE FROM active_jobs WHERE container=?", sql)
        self.assertEqual(params, ("plex",))

    def test_job_helpers_never_raise_on_db_error(self):
        with patch.object(dummy, "_get_conn", side_effect=Exception("db locked")):
            # None of these should propagate — a DB hiccup shouldn't break an update
            dummy._job_begin("plex", "job-1", "env_file", "a", "b")
            dummy._job_step("plex", "recreated")
            dummy._job_end("plex")

    def test_check_interrupted_jobs_noop_when_no_rows(self):
        conn, cur = self._mock_conn(fetchall_val=[])
        with patch.object(dummy, "_get_conn", return_value=conn), \
             patch.object(dummy, "notify") as mock_notify:
            dummy.check_for_interrupted_jobs()
        mock_notify.assert_not_called()
        # SELECT happened but no DELETE (nothing to clear)
        executed_sql = [c[0][0] for c in cur.execute.call_args_list]
        self.assertFalse(any("DELETE FROM active_jobs" in s for s in executed_sql))

    def test_check_interrupted_jobs_warns_notifies_and_clears(self):
        stale_row = ("plex", "job-1", "env_file", "1.43.2.10687", "1.43.3.10828", "env_written", "2026-07-18T00:00:00")
        conn, cur = self._mock_conn(fetchall_val=[stale_row])
        with patch.object(dummy, "_get_conn", return_value=conn), \
             patch.object(dummy, "notify") as mock_notify, \
             patch.object(dummy.log, "warning") as mock_warn:
            dummy.check_for_interrupted_jobs()

        # Warned about it
        self.assertTrue(mock_warn.called)
        # Notified via ntfy so it's not buried in logs only
        mock_notify.assert_called_once()
        notify_args = mock_notify.call_args[0]
        self.assertIn("plex", notify_args[0])
        # Cleared the stale marker so it doesn't re-alert every restart
        executed_sql = [c[0][0] for c in cur.execute.call_args_list]
        self.assertTrue(any("DELETE FROM active_jobs" in s for s in executed_sql))
        conn.commit.assert_called()


class TestEnvFileCurrentTagUsesLiveContainer(unittest.TestCase):
    """
    Regression test: 'Current' for env_file-strategy containers must reflect
    the actual running container's image tag, not a possibly-stale .env
    value (which can drift if an update/rollback job is interrupted).
    """

    def _make_plex_container(self, running_tag):
        c = MagicMock()
        c.name = "plex"
        c.image.tags = [f"plexinc/pms-docker:{running_tag}"]
        c.attrs = {
            "Config": {"Labels": {"dummy.enable": "true", "dummy.env_var": "PLEX_VER"}},
        }
        return c

    def test_current_tag_matches_live_container_not_stale_env(self):
        container = self._make_plex_container("1.43.3.10828-00f62d37d")
        fake_client = MagicMock()
        fake_client.containers.list.return_value = [container]

        with patch.object(dummy, "get_docker_client", return_value=fake_client), \
             patch.object(dummy, "read_env_cached", return_value={"PLEX_VER": "1.43.2.10687-563d026ea"}), \
             patch.object(dummy, "_fetch_container_data_bulk", return_value={
                 "plex": {"available_tag": None, "checked_at": "", "history": [], "dismissed": [], "dismissed_tags": set()}
             }):
            items = dummy.get_monitored_containers()

        self.assertEqual(len(items), 1)
        # Must show the live container tag (1.43.3), not the stale .env value (1.43.2)
        self.assertEqual(items[0]["current_tag"], "1.43.3.10828-00f62d37d")

    def test_no_drift_no_warning_needed(self):
        container = self._make_plex_container("1.43.3.10828-00f62d37d")
        fake_client = MagicMock()
        fake_client.containers.list.return_value = [container]

        with patch.object(dummy, "get_docker_client", return_value=fake_client), \
             patch.object(dummy, "read_env_cached", return_value={"PLEX_VER": "1.43.3.10828-00f62d37d"}), \
             patch.object(dummy, "_fetch_container_data_bulk", return_value={
                 "plex": {"available_tag": None, "checked_at": "", "history": [], "dismissed": [], "dismissed_tags": set()}
             }), \
             patch.object(dummy.log, "warning") as mock_warn:
            items = dummy.get_monitored_containers()

        self.assertEqual(items[0]["current_tag"], "1.43.3.10828-00f62d37d")
        mock_warn.assert_not_called()


class TestValidCreds(unittest.TestCase):

    def test_correct_credentials_pass(self):
        with patch.object(dummy, "BASIC_AUTH_USER", "ducky"), \
             patch.object(dummy, "BASIC_AUTH_PASS", "hunter2"):
            self.assertTrue(dummy._valid_creds("ducky", "hunter2"))

    def test_wrong_password_fails(self):
        with patch.object(dummy, "BASIC_AUTH_USER", "ducky"), \
             patch.object(dummy, "BASIC_AUTH_PASS", "hunter2"):
            self.assertFalse(dummy._valid_creds("ducky", "wrong"))

    def test_wrong_username_fails(self):
        with patch.object(dummy, "BASIC_AUTH_USER", "ducky"), \
             patch.object(dummy, "BASIC_AUTH_PASS", "hunter2"):
            self.assertFalse(dummy._valid_creds("nope", "hunter2"))

    def test_uses_compare_digest_not_equality(self):
        # Ensures we didn't accidentally revert to the timing-unsafe `==`
        with patch.object(dummy, "BASIC_AUTH_USER", "ducky"), \
             patch.object(dummy, "BASIC_AUTH_PASS", "hunter2"), \
             patch.object(dummy._hmac, "compare_digest", wraps=dummy._hmac.compare_digest) as mock_cd:
            dummy._valid_creds("ducky", "hunter2")
            self.assertTrue(mock_cd.called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
