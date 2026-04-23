"""Flask application powering the Zedd-Satellite dashboard.

Routes
------
* ``/``                -- HTML dashboard (server-rendered Jinja template).
* ``/api/status``      -- JSON snapshot: station + health + counts.
* ``/api/passes``      -- JSON list of upcoming :class:`PassEvent`.
* ``/api/captures``    -- JSON list of captured WAV/PNG artifacts.
* ``/api/logs``        -- JSON list of the last *N* log lines.
* ``/output/<file>``   -- Static serve from the daemon's ``output/`` dir
  so decoded PNGs are visible inline in the gallery.

The application is intentionally read-only: no route writes to disk or
mutates the daemon. This keeps the dashboard safe to bind on the local
network without authentication while the daemon is running.
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    send_from_directory,
)

from core.health import HealthMonitor
from core.tracker import PassEvent, TLEError, Tracker

LOGGER = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = os.path.join("config", "settings.json")
# Image / audio extensions the gallery surfaces.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
_AUDIO_EXTS = {".wav"}
# Hard cap on how many log lines a single response will return so a
# multi-MB log file never blows up the dashboard.
_MAX_LOG_LINES = 500


def _load_settings(path: str) -> Dict[str, Any]:
    """Read and parse ``settings.json`` from ``path``."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _list_artifacts(output_dir: str) -> List[Dict[str, Any]]:
    """Return captured WAV / PNG artifacts, newest first.

    Each entry contains ``name``, ``kind`` (``image`` / ``audio``),
    ``size_bytes`` and ``modified_utc`` so the template can render a
    rich gallery without further file IO.
    """
    if not os.path.isdir(output_dir):
        return []
    entries: List[Dict[str, Any]] = []
    for name in os.listdir(output_dir):
        full = os.path.join(output_dir, name)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in _IMAGE_EXTS:
            kind = "image"
        elif ext in _AUDIO_EXTS:
            kind = "audio"
        else:
            continue
        try:
            stat = os.stat(full)
        except OSError:
            continue
        entries.append({
            "name": name,
            "kind": kind,
            "size_bytes": int(stat.st_size),
            "modified_utc": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(),
        })
    entries.sort(key=lambda e: e["modified_utc"], reverse=True)
    return entries


def _tail_log(path: str, max_lines: int) -> List[str]:
    """Return the last ``max_lines`` lines of ``path`` (utf-8)."""
    if not os.path.isfile(path):
        return []
    capped = max(1, min(int(max_lines), _MAX_LOG_LINES))
    buf: deque = deque(maxlen=capped)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                buf.append(line.rstrip("\n"))
    except OSError as exc:
        LOGGER.warning("Could not read log file %s: %s", path, exc)
        return []
    return list(buf)


def _serialize_pass(event: PassEvent) -> Dict[str, Any]:
    """Convert a :class:`PassEvent` to a JSON-friendly dict."""
    return {
        "satellite": event.satellite,
        "frequency_mhz": event.frequency_mhz,
        "mode": event.mode,
        "aos_utc": event.aos.isoformat(),
        "los_utc": event.los.isoformat(),
        "max_elevation_deg": round(event.max_elevation_deg, 2),
        "max_elevation_time_utc": event.max_elevation_time.isoformat(),
        "duration_seconds": event.duration_seconds,
    }


def _safe_upcoming_passes(settings: Dict[str, Any]) -> Tuple[List[PassEvent], Optional[str]]:
    """Run :meth:`Tracker.upcoming_passes`, swallowing all errors.

    The dashboard must keep rendering even when TLEs are missing or
    skyfield blows up, so any exception is converted into a short,
    human-readable message the template can surface to the operator.
    Detailed exception text is logged server-side only -- it is never
    returned to the HTTP client to avoid leaking stack-trace-style
    information through the JSON API.
    """
    try:
        tracker = Tracker(settings)
        return tracker.upcoming_passes(), None
    except TLEError as exc:
        LOGGER.warning("TLE error during pass prediction: %s", exc)
        return [], "TLE data unavailable"
    except FileNotFoundError as exc:
        LOGGER.warning("TLE cache missing: %s", exc)
        return [], "TLE cache missing"
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("Pass prediction failed")
        return [], "Pass prediction failed"


def _safe_health_snapshot(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sample the health monitor, returning ``None`` on any failure."""
    try:
        snap = HealthMonitor(settings).sample()
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("Health sample failed: %s", exc)
        return None
    return json.loads(snap.to_json())


def create_app(settings_path: Optional[str] = None) -> Flask:
    """Build the Flask app bound to ``settings_path``.

    Args:
        settings_path: Override for the settings file location. Defaults
            to ``config/settings.json``.

    Returns:
        A configured :class:`flask.Flask` instance.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["SETTINGS_PATH"] = settings_path or DEFAULT_SETTINGS_PATH

    def _settings() -> Dict[str, Any]:
        # Re-read on every request so config edits show up without a
        # frontend restart. The file is small (~1 KB) so this is cheap.
        return _load_settings(app.config["SETTINGS_PATH"])

    def _output_dir(settings: Dict[str, Any]) -> str:
        return (settings.get("paths", {}) or {}).get("output_dir", "output")

    def _log_file(settings: Dict[str, Any]) -> str:
        paths = settings.get("paths", {}) or {}
        return paths.get(
            "log_file",
            os.path.join(paths.get("log_dir", "logs"), "zedd-satellite.log"),
        )

    # ------------------------------------------------------------- routes
    @app.route("/")
    def dashboard():
        settings = _settings()
        passes, pass_error = _safe_upcoming_passes(settings)
        artifacts = _list_artifacts(_output_dir(settings))
        health = _safe_health_snapshot(settings)
        log_lines = _tail_log(_log_file(settings), max_lines=80)
        station = settings.get("station", {}) or {}
        return render_template(
            "dashboard.html",
            station=station,
            passes=[_serialize_pass(ev) for ev in passes],
            pass_error=pass_error,
            artifacts=artifacts,
            health=health,
            log_lines=log_lines,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @app.route("/api/status")
    def api_status():
        settings = _settings()
        artifacts = _list_artifacts(_output_dir(settings))
        return jsonify({
            "station": settings.get("station", {}),
            "health": _safe_health_snapshot(settings),
            "capture_count": len(artifacts),
            "image_count": sum(1 for a in artifacts if a["kind"] == "image"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    @app.route("/api/passes")
    def api_passes():
        settings = _settings()
        passes, error = _safe_upcoming_passes(settings)
        return jsonify({
            "passes": [_serialize_pass(ev) for ev in passes],
            "error": error,
        })

    @app.route("/api/captures")
    def api_captures():
        return jsonify({
            "captures": _list_artifacts(_output_dir(_settings())),
        })

    @app.route("/api/logs")
    def api_logs():
        return jsonify({
            "lines": _tail_log(_log_file(_settings()), max_lines=_MAX_LOG_LINES),
        })

    @app.route("/output/<path:filename>")
    def serve_output(filename: str):
        # ``send_from_directory`` rejects path traversal (``..``) so this
        # is safe to expose: only files inside the configured output
        # directory can ever be served.
        output_dir = os.path.abspath(_output_dir(_settings()))
        if not os.path.isdir(output_dir):
            abort(404)
        return send_from_directory(output_dir, filename)

    return app
