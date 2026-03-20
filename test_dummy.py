"""
Unit tests for DUMMY — run with: python3 -m pytest test_dummy.py -v
Covers: tag filtering, version comparison, registry URL routing, webhook,
        auth helper, settings defaults.
"""

import sys
import types
import unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
