"""Zedd-Satellite daemon entry point.

This module wires the three core components together:

1. :class:`core.tracker.Tracker` predicts upcoming passes from TLE data.
2. :class:`apscheduler.schedulers.background.BackgroundScheduler`
   schedules a job to fire at each pass's AOS timestamp.
3. The job runs :class:`core.capture.Capture` followed by
   :class:`core.decoder.Decoder`, logging every state transition.

The daemon refreshes its prediction window every hour so that newly
visible passes are picked up after a TLE refresh.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.capture import Capture, CaptureError, CaptureResult
from core.decoder import Decoder, DecoderError
from core.gps import GPSReader
from core.health import HealthMonitor, HealthSnapshot
from core.lora import LoRaSX1262, LoRaUnavailable, LoRaError
from core.storage import MirroredStorage
from core.tracker import PassEvent, TLEError, Tracker

LOGGER = logging.getLogger("zedd_satellite")

DEFAULT_SETTINGS_PATH = os.path.join("config", "settings.json")
PASS_REFRESH_INTERVAL_S = 3600  # Re-poll the tracker every hour.


def load_settings(path: str = DEFAULT_SETTINGS_PATH) -> Dict:
    """Load and parse the ``settings.json`` configuration file.

    Args:
        path: Path to the JSON configuration file.

    Returns:
        The parsed settings dictionary.

    Raises:
        FileNotFoundError: If the settings file does not exist.
        ValueError: If the file is not valid JSON.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Settings file not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def configure_logging(settings: Dict) -> None:
    """Configure the root logger with a rotating file + stream handler.

    Args:
        settings: Parsed settings dictionary.
    """
    paths_cfg = settings.get("paths", {})
    log_dir = paths_cfg.get("log_dir", "logs")
    log_file = paths_cfg.get("log_file", os.path.join(log_dir, "zedd-satellite.log"))
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Prevent duplicate handlers when reloading in long-running tests.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", backupCount=7, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


def _pass_key(event: PassEvent) -> str:
    """Build a stable, hashable key uniquely identifying a pass."""
    return f"{event.satellite}@{event.aos.isoformat()}"


def _execute_pass(
    event: PassEvent,
    capture: Capture,
    decoder: Decoder,
    storage: MirroredStorage,
) -> None:
    """Capture and decode a single pass.

    Designed to be invoked by APScheduler; never raises (errors are
    logged so the scheduler thread keeps running).
    """
    LOGGER.info(
        "Pass starting: satellite=%s aos=%s los=%s max_el=%.1f deg",
        event.satellite,
        event.aos.isoformat(),
        event.los.isoformat(),
        event.max_elevation_deg,
    )
    try:
        result: CaptureResult = capture.capture_pass(event, wait_for_aos=False)
    except CaptureError as exc:
        LOGGER.error("Capture failed for %s: %s", event.satellite, exc)
        return
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("Unexpected capture error for %s: %s", event.satellite, exc)
        return

    LOGGER.info("Capture complete: %s", result.wav_path)
    storage.mirror(result.wav_path)
    try:
        decode_result = decoder.decode(result)
    except DecoderError as exc:
        LOGGER.error("Decoding failed for %s: %s", result.wav_path, exc)
        return
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("Unexpected decoder error for %s: %s", result.wav_path, exc)
        return

    LOGGER.info(
        "Decode complete: %s (decoder=%s)",
        decode_result.image_path,
        decode_result.decoder,
    )
    storage.mirror(decode_result.image_path)


def _emit_health_beacon(
    health: HealthMonitor,
    lora: Optional[LoRaSX1262],
) -> None:
    """Sample station health and -- if LoRa is enabled -- transmit a beacon.

    Always executes the local sample so warnings are emitted even when
    the radio is offline.
    """
    try:
        snap: HealthSnapshot = health.sample()
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("Health snapshot failed: %s", exc)
        return
    LOGGER.info("Health snapshot: %s", snap.to_json())

    if lora is None or not lora.enabled:
        return
    try:
        lora.transmit(snap.to_json().encode("utf-8"))
    except LoRaError as exc:
        LOGGER.warning("LoRa heartbeat TX failed: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.exception("Unexpected LoRa error: %s", exc)


class Daemon:
    """Long-running scheduler that orchestrates captures.

    Args:
        settings: Parsed settings dictionary.
        scheduler: Optional pre-built APScheduler instance (for tests).
    """

    def __init__(
        self,
        settings: Dict,
        scheduler: Optional[BackgroundScheduler] = None,
    ) -> None:
        self._settings = settings
        # Apply GPS-derived station coordinates BEFORE building Tracker
        # so the very first pass prediction uses the live fix.
        self._gps = GPSReader(settings)
        self._apply_gps_to_settings(settings)
        self._tracker = Tracker(settings)
        self._capture = Capture(settings)
        self._decoder = Decoder(settings)
        self._storage = MirroredStorage(settings)
        self._health = HealthMonitor(settings)
        self._lora: Optional[LoRaSX1262] = self._init_lora(settings)
        self._scheduler: BackgroundScheduler = scheduler or BackgroundScheduler(
            timezone="UTC"
        )
        self._scheduled: Set[str] = set()
        self._stopping = False

    @staticmethod
    def _init_lora(settings: Dict) -> Optional[LoRaSX1262]:
        """Construct + open the LoRa radio if it is enabled in config."""
        radio = LoRaSX1262(settings)
        if not radio.enabled:
            return None
        try:
            radio.open()
        except LoRaUnavailable as exc:
            LOGGER.warning(
                "LoRa subsystem enabled but unavailable: %s. "
                "Continuing without it.", exc,
            )
            return None
        return radio

    def _apply_gps_to_settings(self, settings: Dict) -> None:
        """If GPS is enabled, override station coords with a live fix."""
        if not self._gps.enabled:
            return
        fix = self._gps.read_fix()
        if fix is None:
            LOGGER.warning(
                "GPS enabled but no fix available; using configured "
                "station coordinates"
            )
            return
        self._gps.check_clock_drift(fix)
        if not self._gps.discipline_station:
            return
        station = settings.setdefault("station", {})
        station["latitude_deg"] = fix.latitude_deg
        station["longitude_deg"] = fix.longitude_deg
        station["elevation_m"] = fix.elevation_m
        # Do not log the coordinates themselves -- they identify the
        # operator's physical location.
        LOGGER.info(
            "Station coordinates disciplined by GPS (source=%s)", fix.source,
        )

    # ------------------------------------------------------------------ API
    def run(self) -> None:
        """Start the scheduler and block until interrupted."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        LOGGER.info("Zedd-Satellite daemon starting")
        self._scheduler.start()
        self._schedule_health_job()
        try:
            while not self._stopping:
                self._refresh_passes()
                self._sleep(PASS_REFRESH_INTERVAL_S)
        finally:
            LOGGER.info("Shutting down scheduler")
            self._scheduler.shutdown(wait=False)
            if self._lora is not None:
                self._lora.close()
            LOGGER.info("Daemon stopped")

    def _schedule_health_job(self) -> None:
        """Queue a recurring health snapshot + LoRa heartbeat job."""
        if not self._health.enabled and (self._lora is None or not self._lora.enabled):
            return
        period = (
            self._lora.heartbeat_period_s
            if self._lora is not None and self._lora.enabled
            else self._health.period_s
        )
        self._scheduler.add_job(
            _emit_health_beacon,
            trigger=IntervalTrigger(seconds=period),
            args=[self._health, self._lora],
            id="health-beacon",
            name="health snapshot + LoRa beacon",
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc),
        )
        LOGGER.info("Scheduled health/LoRa beacon every %ds", period)

    # --------------------------------------------------------- internals
    def _handle_signal(self, signum: int, _frame) -> None:
        LOGGER.info("Received signal %d, requesting shutdown", signum)
        self._stopping = True

    def _sleep(self, seconds: int) -> None:
        """Sleep in 1 second chunks so signals are responsive."""
        end = time.monotonic() + seconds
        while not self._stopping and time.monotonic() < end:
            time.sleep(1)

    def _refresh_passes(self) -> None:
        """Re-poll the tracker and queue any newly visible passes."""
        try:
            events = self._tracker.upcoming_passes()
        except TLEError as exc:
            LOGGER.error("TLE error while refreshing passes: %s", exc)
            return
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("Unexpected tracker error: %s", exc)
            return

        scheduled_now = 0
        now = datetime.now(timezone.utc)
        # Forget keys for passes that are entirely in the past.
        self._scheduled = {
            key for key in self._scheduled
            if key.split("@", 1)[1] >= (now - timedelta(hours=1)).isoformat()
        }

        for event in events:
            key = _pass_key(event)
            if key in self._scheduled:
                continue
            if event.aos <= now:
                LOGGER.debug("Skipping in-progress/past pass %s", key)
                continue

            trigger = DateTrigger(run_date=event.aos)
            self._scheduler.add_job(
                _execute_pass,
                trigger=trigger,
                args=[event, self._capture, self._decoder, self._storage],
                id=key,
                name=f"capture {event.satellite}",
                misfire_grace_time=30,
                replace_existing=True,
            )
            self._scheduled.add(key)
            scheduled_now += 1
            LOGGER.info(
                "Queued capture: %s at %s (max el %.1f deg, %ds)",
                event.satellite,
                event.aos.isoformat(),
                event.max_elevation_deg,
                event.duration_seconds,
            )

        if scheduled_now == 0:
            LOGGER.info("No new passes to schedule; %d already queued",
                        len(self._scheduled))


def main(argv: Optional[list] = None) -> int:
    """Entry point for ``python3 main.py``.

    Args:
        argv: Optional argument vector (currently unused; reserved for
            future CLI flags such as ``--config``).

    Returns:
        Process exit code.
    """
    del argv  # CLI parsing reserved for future use.
    try:
        settings = load_settings()
    except (FileNotFoundError, ValueError) as exc:
        print(f"FATAL: cannot load settings: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings)
    LOGGER.info("Loaded settings for station %r",
                settings.get("station", {}).get("name", "<unnamed>"))
    Daemon(settings).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
