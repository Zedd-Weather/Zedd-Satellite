import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from core.config import SettingsValidationError, load_settings, run_startup_preflight

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_SETTINGS = os.path.join(REPO_ROOT, "config", "settings.json")


class ConfigValidationTests(unittest.TestCase):
    def _base_settings(self):
        with open(BASE_SETTINGS, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_settings(self, settings):
        temp_dir = tempfile.TemporaryDirectory()
        path = os.path.join(temp_dir.name, "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        self.addCleanup(temp_dir.cleanup)
        return path

    def test_load_settings_accepts_repository_defaults(self):
        settings = load_settings(BASE_SETTINGS)
        self.assertEqual(settings["station"]["timezone"], "UTC")

    def test_load_settings_rejects_invalid_station_latitude(self):
        settings = self._base_settings()
        settings["station"]["latitude_deg"] = 95
        path = self._write_settings(settings)

        with self.assertRaises(SettingsValidationError) as exc:
            load_settings(path)

        self.assertIn("station.latitude_deg", str(exc.exception))

    def test_startup_preflight_creates_runtime_directories(self):
        settings = self._base_settings()
        with tempfile.TemporaryDirectory() as temp_dir:
            settings["paths"]["output_dir"] = os.path.join(temp_dir, "output")
            settings["paths"]["log_dir"] = os.path.join(temp_dir, "logs")
            settings["paths"]["log_file"] = os.path.join(temp_dir, "logs", "daemon.log")
            settings["decoder"]["output_dir"] = os.path.join(temp_dir, "decoded")
            settings["tle"]["cache_file"] = os.path.join(temp_dir, "tle", "weather.tle")
            settings["storage"]["mirror_dirs"] = [os.path.join(temp_dir, "mirror")]
            settings["satellites"] = {
                "NOAA 19": copy.deepcopy(settings["satellites"]["NOAA 19"]),
            }
            with mock.patch(
                "core.config.shutil.which",
                side_effect=lambda binary: f"/usr/bin/{binary}",
            ):
                warnings = run_startup_preflight(settings, service="daemon")

            self.assertEqual(warnings, [])
            self.assertTrue(os.path.isdir(settings["paths"]["output_dir"]))
            self.assertTrue(os.path.isdir(settings["paths"]["log_dir"]))
            self.assertTrue(os.path.isdir(settings["decoder"]["output_dir"]))
            self.assertTrue(os.path.isdir(os.path.dirname(settings["tle"]["cache_file"])))
            self.assertTrue(os.path.isdir(settings["storage"]["mirror_dirs"][0]))

    def test_startup_preflight_requires_decoder_for_enabled_modes(self):
        settings = self._base_settings()

        def fake_which(binary):
            if binary in {"rtl_fm", "sox"}:
                return f"/usr/bin/{binary}"
            return None

        with mock.patch("core.config.shutil.which", side_effect=fake_which):
            with self.assertRaises(SettingsValidationError) as exc:
                run_startup_preflight(settings, service="daemon")

        self.assertIn("NOAA", str(exc.exception))
        self.assertIn("Meteor", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
