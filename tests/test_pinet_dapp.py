import json
import os
import tempfile
import unittest
import zipfile

from scripts.package_pinet_dapp import build_package

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINET_DAPP_ROOT = os.path.join(REPO_ROOT, "pinet_dapp")


class PiNetDAppTests(unittest.TestCase):
    def _load_manifest(self, name):
        with open(os.path.join(PINET_DAPP_ROOT, name), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_manifest_is_pinet_compatible(self):
        manifest = self._load_manifest("manifest.json")

        self.assertEqual(manifest["id"], "org.zedd-weather.satellite")
        self.assertEqual(manifest["kind"], "typescript")
        self.assertEqual(manifest["entry"], "index.html")
        self.assertEqual(manifest["entryPoint"], "index.html")
        self.assertIn("system.read", manifest["permissions"])
        self.assertIn("minima.rpc", manifest["permissions"])
        self.assertIn("notifications", manifest["permissions"])
        self.assertEqual(manifest["minima"]["protocol"], "RMPE-2")
        self.assertEqual(manifest["minima"]["purpose"], "provenance")

    def test_manifest_aliases_stay_in_sync(self):
        self.assertEqual(
            self._load_manifest("manifest.json"),
            self._load_manifest("dapp.json"),
        )

    def test_declared_assets_exist(self):
        manifest = self._load_manifest("manifest.json")

        for relative_path in [
            manifest["entry"],
            manifest["entryPoint"],
            manifest["icon"],
            "sdk/pinet-sdk.js",
            "static/app.js",
            "static/style.css",
        ]:
            self.assertTrue(
                os.path.isfile(os.path.join(PINET_DAPP_ROOT, relative_path)),
                relative_path,
            )

    def test_bridge_uses_pinet_postmessage_protocol(self):
        with open(os.path.join(PINET_DAPP_ROOT, "sdk", "pinet-sdk.js"), "r", encoding="utf-8") as handle:
            bridge = handle.read()

        self.assertIn("pinet-bridge-request", bridge)
        self.assertIn("pinet-bridge-response", bridge)
        self.assertIn("window.parent.postMessage", bridge)
        self.assertIn("TARGET_ORIGIN = window.location.origin", bridge)
        self.assertIn("}, TARGET_ORIGIN)", bridge)

    def test_package_build_creates_installable_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = build_package(
                dapp_root=os.path.abspath(PINET_DAPP_ROOT),
                output_dir=os.path.abspath(temp_dir),
            )

            self.assertTrue(os.path.isfile(package_path))
            self.assertTrue(os.path.isfile(f"{package_path}.sha256"))
            with zipfile.ZipFile(package_path) as archive:
                names = set(archive.namelist())

        self.assertIn("dapp.json", names)
        self.assertIn("manifest.json", names)
        self.assertIn("index.html", names)
        self.assertIn("sdk/pinet-sdk.js", names)
        self.assertNotIn("pinet_dapp/index.html", names)


if __name__ == "__main__":
    unittest.main()
