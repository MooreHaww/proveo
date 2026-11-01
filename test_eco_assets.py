"""Tests for the /assets/ route (the logo slot) and the settings fields the screens need.

A temp assets folder and a temp ECO_DATA_DIR only. No models, no cloud calls.
Run with: python -m unittest -v test_eco_assets
"""
import http.client
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import eco_server
from test_eco_server import FAKE_KEY, ServerCase

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR fake png bytes \xff\x00"
ICO = b"\x00\x00\x01\x00 fake ico bytes"


def make_assets(folder):
    """A temp assets folder with allowed files, plus files and folders that must stay unreachable."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "logo.svg").write_text(SVG, encoding="utf-8")
    (folder / "logo.png").write_bytes(PNG)
    (folder / "favicon.ico").write_bytes(ICO)
    (folder / "logo.v2.svg").write_text(SVG, encoding="utf-8")
    for name in ("app.js", "page.html", "tool.py", "logo.svg.py", ".hidden.svg"):
        (folder / name).write_text("not allowed", encoding="utf-8")
    (folder / "x.svg").mkdir()                       # a folder with an allowed-looking name
    (folder / "sub").mkdir()
    (folder / "sub" / "x.svg").write_text(SVG, encoding="utf-8")
    (folder.parent / "outside.svg").write_text(SVG, encoding="utf-8")  # next to assets, not in it
    return folder


class AssetFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = make_assets(os.path.join(self.tmp.name, "assets"))

    def test_allowed_names(self):
        for name in ("logo.svg", "logo.png", "favicon.ico", "logo.v2.svg"):
            path = eco_server.asset_file(name, folder=self.folder)
            self.assertIsNotNone(path, name)
            self.assertEqual(path.parent, self.folder.resolve())

    def test_refused_names(self):
        refused = [
            "", "missing.svg", "x.svg",                       # missing; a folder
            "app.js", "page.html", "tool.py", "logo.svg.py",  # other types
            ".hidden.svg", "-logo.svg", "logo .svg", "logo.SVG.exe",
            "../outside.svg", "..%2Foutside.svg", "%2e%2e/outside.svg", "%2e%2e%2foutside.svg",
            "..\\outside.svg", "..%5Coutside.svg", "sub/x.svg", "sub%2Fx.svg", "sub\\x.svg",
            "..svg", "a..svg", "/etc/passwd.svg", "C:/Windows/win.ini", "C:%5CWindows%5Cx.svg",
            "logo.svg%00.png", "logo.svg\x00", "logo.svg?x=1", "logo.svg#x", "%252e%252e%252foutside.svg",
            "con.svg/", "logo.svg/", "logo.svg:stream", "a" * 70 + ".svg",
        ]
        for name in refused:
            self.assertIsNone(eco_server.asset_file(name, folder=self.folder), repr(name))

    def test_missing_folder_is_safe(self):
        self.assertIsNone(eco_server.asset_file("logo.svg", folder=os.path.join(self.tmp.name, "nope")))


class AssetRouteTests(ServerCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {})
        self._env.start()
        os.environ.pop("MODEL_API_KEY", None)
        self.addCleanup(self._env.stop)
        self.start()
        self.folder = make_assets(os.path.join(self.tmp.name, "web_assets", "assets"))
        patcher = mock.patch.object(eco_server, "ASSETS", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def raw_get(self, raw_path):
        """GET with the path sent exactly as written (no client-side normalisation)."""
        host, port = self.server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=10)
        try:
            conn.putrequest("GET", raw_path, skip_accept_encoding=True)
            conn.putheader("Host", f"127.0.0.1:{port}")
            conn.endheaders()
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def test_serves_allowed_types_with_the_sandbox_policy(self):
        for name, ctype, body in (("logo.svg", "image/svg+xml; charset=utf-8", SVG.encode()),
                                  ("logo.png", "image/png", PNG), ("favicon.ico", "image/x-icon", ICO)):
            status, headers, data = self.raw_get("/assets/" + name)
            self.assertEqual(status, 200, name)
            self.assertEqual(headers["Content-Type"], ctype)
            self.assertEqual(data, body, "bytes are sent unchanged")
            self.assertEqual(headers["Content-Security-Policy"], eco_server.ASSET_CSP)
            self.assertIn("sandbox", headers["Content-Security-Policy"])
            self.assertNotIn("script-src", headers["Content-Security-Policy"])
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_query_string_is_ignored(self):
        status, _, data = self.raw_get("/assets/logo.svg?v=3")
        self.assertEqual((status, data), (200, SVG.encode()))

    def test_rejects_traversal_other_types_and_folders(self):
        repo_file = Path(eco_server.__file__).read_bytes()[:200]
        paths = [
            "/assets/", "/assets/missing.svg", "/assets/x.svg", "/assets/sub/x.svg", "/assets/sub%2Fx.svg",
            "/assets/../eco_server.py", "/assets/..%2Feco_server.py", "/assets/%2e%2e/eco_server.py",
            "/assets/%2e%2e%2f%2e%2e%2feco_server.py", "/assets/..\\outside.svg", "/assets/..%5Coutside.svg",
            "/assets/../outside.svg", "/assets/.hidden.svg", "/assets/C:/Windows/win.ini",
            "/assets/app.js", "/assets/page.html", "/assets/tool.py", "/assets/logo.svg.py",
            "/assets/logo.svg%00.png", "/assets//etc/passwd", "/assets/%252e%252e%252foutside.svg",
            "/assets/../web/index.html", "/assets/../../.env",
        ]
        for raw in paths:
            status, headers, data = self.raw_get(raw)
            self.assertEqual(status, 404, raw)
            self.assertEqual(json.loads(data), {"error": "not found"}, raw)
            self.assertNotIn(repo_file, data, raw)

    def test_svg_text_is_scrubbed_of_the_key(self):
        status, _ = self.request("/api/key", {"key": FAKE_KEY})
        self.assertEqual(status, 200)
        (self.folder / "leak.svg").write_text(SVG.replace("<circle", f"<!-- {FAKE_KEY} --><circle"), encoding="utf-8")
        status, _, data = self.raw_get("/assets/leak.svg")
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_KEY.encode(), data)

    def test_assets_route_keeps_the_host_check(self):
        host, port = self.server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.putrequest("GET", "/assets/logo.svg", skip_host=True)
        conn.putheader("Host", "evil.example")
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 403)
        conn.close()


class PageAndSettingsFieldTests(ServerCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {})
        self._env.start()
        os.environ.pop("MODEL_API_KEY", None)
        self.addCleanup(self._env.stop)

    def test_page_csp_allows_same_origin_images_only(self):
        self.start()
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            self.assertEqual(r.status, 200)
            csp = r.headers["Content-Security-Policy"]
        self.assertEqual(csp, eco_server.PAGE_CSP)
        self.assertIn("img-src 'self' data:", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)

    def test_saved_flag_and_defaults(self):
        self.start()
        status, text = self.request("/api/settings")
        view = json.loads(text)
        self.assertEqual(status, 200)
        self.assertIs(view["saved"], False, "never saved yet: the welcome card shows")
        self.assertEqual([t["id"] for t in view["defaults"]], [t["id"] for t in view["tiers"]])
        status, text = self.request("/api/settings", {"tiers": view["tiers"]})
        self.assertEqual(status, 200, text)
        self.assertIs(json.loads(text)["saved"], True)
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, "settings.json")))
        self.assertIs(json.loads(self.request("/api/settings")[1])["saved"], True)


if __name__ == "__main__":
    unittest.main()
