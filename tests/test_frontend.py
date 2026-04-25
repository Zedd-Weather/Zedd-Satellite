import json
import os
import tempfile
import unittest
from unittest import mock

from frontend.__main__ import _parse_args
from frontend.app import create_app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_SETTINGS = os.path.join(REPO_ROOT, "config", "settings.json")


class FrontendTests(unittest.TestCase):
    def _settings_path(self):
        with open(BASE_SETTINGS, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        settings["paths"]["output_dir"] = os.path.join(temp_dir.name, "output")
        settings["paths"]["log_dir"] = os.path.join(temp_dir.name, "logs")
        settings["paths"]["log_file"] = os.path.join(temp_dir.name, "logs", "zedd-satellite.log")
        settings["decoder"]["output_dir"] = settings["paths"]["output_dir"]
        settings["tle"]["cache_file"] = os.path.join(temp_dir.name, "config", "weather.tle")
        os.makedirs(settings["paths"]["output_dir"], exist_ok=True)
        os.makedirs(settings["paths"]["log_dir"], exist_ok=True)
        path = os.path.join(temp_dir.name, "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        return path, settings

    def test_parse_args_defaults_to_loopback(self):
        args = _parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8080)

    def test_dashboard_sets_security_headers(self):
        settings_path, _settings = self._settings_path()
        app = create_app(settings_path)
        app.testing = True
        with mock.patch("frontend.app._safe_upcoming_passes", return_value=([], None)), mock.patch(
            "frontend.app._safe_health_snapshot", return_value=None
        ):
            response = app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_api_status_exposes_next_pass_and_healthz(self):
        settings_path, _settings = self._settings_path()
        app = create_app(settings_path)
        app.testing = True
        fake_passes = [
            {
                "satellite": "NOAA 19",
                "frequency_mhz": 137.1,
                "mode": "NOAA_APT",
                "aos_utc": "2026-01-01T00:00:00+00:00",
                "los_utc": "2026-01-01T00:10:00+00:00",
                "max_elevation_deg": 42.0,
                "max_elevation_time_utc": "2026-01-01T00:05:00+00:00",
                "duration_seconds": 600,
            }
        ]
        with mock.patch(
            "frontend.app._safe_upcoming_passes",
            return_value=(
                [mock.Mock(
                    satellite="NOAA 19",
                    frequency_mhz=137.1,
                    mode="NOAA_APT",
                    aos=mock.Mock(isoformat=lambda: fake_passes[0]["aos_utc"]),
                    los=mock.Mock(isoformat=lambda: fake_passes[0]["los_utc"]),
                    max_elevation_deg=42.0,
                    max_elevation_time=mock.Mock(isoformat=lambda: fake_passes[0]["max_elevation_time_utc"]),
                    duration_seconds=600,
                )],
                None,
            ),
        ), mock.patch("frontend.app._safe_health_snapshot", return_value={"cpu_temp_c": 40.0}):
            client = app.test_client()
            status_response = client.get("/api/status")
            health_response = client.get("/api/healthz")

        self.assertEqual(status_response.status_code, 200)
        payload = status_response.get_json()
        self.assertEqual(payload["next_pass"]["satellite"], "NOAA 19")
        self.assertIsNone(payload["pass_prediction_error"])
        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(health_response.get_json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
