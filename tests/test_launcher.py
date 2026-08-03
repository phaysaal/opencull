"""The launcher page, its asset route, and the window shell."""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import shell  # noqa: E402
from opencull_gui.desktop_server import LAUNCHER_ROOT, DesktopBridgeServer  # noqa: E402


class LauncherAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = DesktopBridgeServer(
            ("127.0.0.1", 0), jobs=mock.Mock(), providers=mock.Mock(),
            open_review=lambda *a, **k: {})
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, token=None):
        request = urllib.request.Request(self.base + path)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)

    def test_the_page_is_reachable_without_a_token(self):
        # The page is what obtains the token, so it cannot present one.
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Darkimiya</title>", body)
        self.assertIn("text/html", headers["Content-Type"])

    def test_the_page_carries_the_session_token(self):
        _status, body, _headers = self.get("/")
        self.assertIn(b"__DARKIMIYA_TOKEN__", body)
        self.assertIn(self.server.token.encode(), body)

    def test_stylesheet_and_script_are_served(self):
        for path, kind in (("/launcher.css", "text/css"),
                           ("/launcher.js", "text/javascript")):
            status, body, headers = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn(kind, headers["Content-Type"])
            self.assertTrue(body)

    def test_the_api_still_requires_the_token(self):
        # Serving the page must not have opened the data routes.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/state")
        self.assertEqual(caught.exception.code, 403)

    def test_an_unknown_path_is_not_served_from_the_launcher_directory(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/../desktop_server.py")
        self.assertIn(caught.exception.code, (403, 404))

    def test_state_reports_home_so_paths_can_be_abbreviated(self):
        self.server.jobs.public.return_value = {"jobs": [], "revision": 0}
        self.server.providers.public.return_value = {"profiles": [], "revision": 0}
        _status, body, _headers = self.get("/state", token=self.server.token)
        self.assertEqual(json.loads(body)["home"], str(Path.home()))


class LauncherPageTests(unittest.TestCase):
    """The page is static, so its contract with the script can be checked."""

    @classmethod
    def setUpClass(cls):
        cls.html = (LAUNCHER_ROOT / "index.html").read_text(encoding="utf-8")
        cls.js = (LAUNCHER_ROOT / "launcher.js").read_text(encoding="utf-8")

    def test_every_element_the_script_looks_up_exists_in_the_page(self):
        import re

        referenced = set(re.findall(r"\$\(\"([a-z0-9-]+)\"\)", self.js))
        present = set(re.findall(r'id="([a-z0-9-]+)"', self.html))
        self.assertEqual(
            referenced - present, set(),
            "the script reads element ids the page does not define")

    def test_the_stored_credential_is_shown_as_a_placeholder_not_a_value(self):
        # A value would be submitted on the next save and would replace the
        # stored key with bullet characters.
        self.assertIn('input.placeholder = "•".repeat(24)', self.js)
        self.assertNotIn('input.value = "•"', self.js)


class ShellTests(unittest.TestCase):
    def test_a_missing_pywebview_is_explained_rather_than_fatal(self):
        with mock.patch.dict(sys.modules, {"webview": None}):
            available, reason = shell.native_window_available()
        self.assertFalse(available)
        self.assertIn("pywebview", reason)

    def test_the_browser_is_used_when_no_native_window_exists(self):
        opened = []
        with mock.patch.object(
            shell, "native_window_available", return_value=(False, "no")
        ), mock.patch.object(shell.webbrowser, "open", opened.append):
            used = shell.open_launcher("http://127.0.0.1:1/")
        self.assertEqual(used, "browser")

    def test_a_native_window_is_preferred_when_available(self):
        webview = mock.Mock()
        with mock.patch.dict(sys.modules, {"webview": webview}), \
                mock.patch.object(
                    shell, "native_window_available", return_value=(True, "")):
            used = shell.open_launcher("http://127.0.0.1:1/")
        self.assertEqual(used, "native")
        webview.create_window.assert_called_once()
        webview.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
