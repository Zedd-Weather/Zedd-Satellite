"""Hardware GPS reader for Zedd-Satellite.

The ground station benefits from a real GPS receiver (e.g. a u-blox
USB dongle) for two reasons:

1. Station coordinates -- when ``gps.discipline_station`` is enabled in
   ``config/settings.json`` the live fix overrides ``station.latitude_deg``
   / ``station.longitude_deg`` so pass predictions are accurate even if
   the ground station is moved.
2. Time -- weather satellite passes are timed to within a second; if the
   system clock has drifted from GPS time by more than the configured
   threshold the daemon logs a loud warning so an operator can adjust
   ``chrony`` / ``ntpd``.

Two **redundant** acquisition paths are supported (no simulation):

* **Primary**: the system ``gpsd`` daemon via the ``gpsd-py3`` Python
  client. This is the recommended setup on Raspberry Pi OS because
  ``gpsd`` shares the device with ``chrony``.
* **Fallback**: direct NMEA-over-serial reads using ``pyserial`` and
  ``pynmea2`` against the configured ``serial_port``. Used when
  ``gpsd`` is not running or refuses connections.

Both paths use real hardware only -- if neither succeeds the reader
returns ``None`` and callers MUST treat the absence of a fix as a
hardware fault, never silently fabricate data.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GPSFix:
    """A 3-D positional + time fix from the GPS receiver."""

    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    fix_time_utc: datetime
    source: str  # "gpsd" or "serial"


class GPSError(RuntimeError):
    """Raised when a hard GPS error occurs (bad config, etc.)."""


class GPSReader:
    """Read fixes from a real GPS receiver with two redundant backends.

    Args:
        settings: Parsed ``config/settings.json``. Reads ``gps.*``.
    """

    def __init__(self, settings: Dict) -> None:
        gps_cfg = settings.get("gps", {}) or {}
        self._enabled: bool = bool(gps_cfg.get("enabled", False))
        self._gpsd_host: str = str(gps_cfg.get("gpsd_host", "127.0.0.1"))
        self._gpsd_port: int = int(gps_cfg.get("gpsd_port", 2947))
        self._serial_port: str = str(gps_cfg.get("serial_port", "/dev/ttyACM0"))
        self._serial_baud: int = int(gps_cfg.get("serial_baud", 9600))
        self._read_timeout_s: float = float(gps_cfg.get("read_timeout_s", 5.0))
        self._discipline_station: bool = bool(
            gps_cfg.get("discipline_station", False)
        )
        self._max_clock_drift_s: float = float(
            gps_cfg.get("max_clock_drift_s", 2.0)
        )

    # ------------------------------------------------------------------ API
    @property
    def enabled(self) -> bool:
        """Whether the GPS subsystem is enabled in config."""
        return self._enabled

    @property
    def discipline_station(self) -> bool:
        """Whether station coordinates should be overridden by GPS."""
        return self._discipline_station

    def read_fix(self) -> Optional[GPSFix]:
        """Return a fresh fix, trying gpsd first then raw serial.

        Returns ``None`` if neither backend produced a valid fix within
        the configured timeout.
        """
        if not self._enabled:
            return None

        fix = self._read_via_gpsd()
        if fix is not None:
            return fix

        LOGGER.info("gpsd backend produced no fix; trying serial NMEA fallback")
        return self._read_via_serial()

    def check_clock_drift(self, fix: GPSFix) -> Optional[float]:
        """Return system-clock drift in seconds against ``fix``.

        Logs a warning when the absolute drift exceeds
        ``max_clock_drift_s``. Returns ``None`` if the fix has no
        timestamp.
        """
        if fix.fix_time_utc is None:
            return None
        drift = (
            datetime.now(timezone.utc) - fix.fix_time_utc
        ).total_seconds()
        if abs(drift) > self._max_clock_drift_s:
            LOGGER.warning(
                "System clock drifted %.2fs from GPS time (threshold %.2fs); "
                "verify chrony/ntpd is running",
                drift,
                self._max_clock_drift_s,
            )
        else:
            LOGGER.debug("System clock within %.2fs of GPS time", drift)
        return drift

    # --------------------------------------------------------- internals
    def _read_via_gpsd(self) -> Optional[GPSFix]:
        """Primary path: connect to a running ``gpsd`` and read a fix."""
        try:
            import gpsd  # type: ignore  # gpsd-py3
        except ImportError:
            LOGGER.debug("gpsd-py3 not installed; skipping gpsd backend")
            return None

        try:
            gpsd.connect(host=self._gpsd_host, port=self._gpsd_port)
        except Exception as exc:  # gpsd raises broad exceptions
            LOGGER.warning(
                "Cannot connect to gpsd at %s:%d -- %s",
                self._gpsd_host,
                self._gpsd_port,
                exc,
            )
            return None

        deadline = time.monotonic() + self._read_timeout_s
        while time.monotonic() < deadline:
            try:
                packet = gpsd.get_current()
            except Exception as exc:  # transient / no-fix yet
                LOGGER.debug("gpsd get_current failed: %s", exc)
                time.sleep(0.5)
                continue
            # mode >= 2 means a 2D fix; >= 3 is full 3D.
            if getattr(packet, "mode", 0) >= 2:
                lat = float(packet.lat)
                lon = float(packet.lon)
                alt = float(packet.alt) if getattr(packet, "mode", 0) >= 3 else 0.0
                fix_time = self._parse_gpsd_time(getattr(packet, "time", None))
                LOGGER.info("GPS fix acquired via gpsd (mode=%d)",
                            getattr(packet, "mode", 0))
                return GPSFix(
                    latitude_deg=lat,
                    longitude_deg=lon,
                    elevation_m=alt,
                    fix_time_utc=fix_time,
                    source="gpsd",
                )
            time.sleep(0.5)
        LOGGER.warning("gpsd produced no fix within %.1fs", self._read_timeout_s)
        return None

    def _read_via_serial(self) -> Optional[GPSFix]:
        """Fallback path: read NMEA sentences directly from the serial port."""
        try:
            import serial  # type: ignore  # pyserial
            import pynmea2  # type: ignore
        except ImportError:
            LOGGER.warning(
                "pyserial / pynmea2 not installed; cannot use serial GPS fallback"
            )
            return None

        try:
            ser = serial.Serial(
                self._serial_port,
                baudrate=self._serial_baud,
                timeout=1.0,
            )
        except (OSError, serial.SerialException) as exc:
            LOGGER.warning(
                "Cannot open GPS serial port %s @ %d baud: %s",
                self._serial_port,
                self._serial_baud,
                exc,
            )
            return None

        try:
            deadline = time.monotonic() + self._read_timeout_s
            while time.monotonic() < deadline:
                try:
                    raw = ser.readline().decode("ascii", errors="replace").strip()
                except (OSError, serial.SerialException) as exc:
                    LOGGER.warning("Serial read error: %s", exc)
                    return None
                if not raw or not raw.startswith("$"):
                    continue
                try:
                    msg = pynmea2.parse(raw)
                except pynmea2.ParseError:
                    continue
                # GGA = fix data, including altitude
                if isinstance(msg, pynmea2.types.talker.GGA) and msg.gps_qual:
                    try:
                        lat = float(msg.latitude)
                        lon = float(msg.longitude)
                        alt = float(msg.altitude) if msg.altitude is not None else 0.0
                    except (TypeError, ValueError):
                        continue
                    fix_time = self._combine_nmea_time(msg.timestamp)
                    LOGGER.info("GPS fix acquired via serial NMEA")
                    return GPSFix(
                        latitude_deg=lat,
                        longitude_deg=lon,
                        elevation_m=alt,
                        fix_time_utc=fix_time,
                        source="serial",
                    )
            LOGGER.warning(
                "Serial GPS produced no fix within %.1fs", self._read_timeout_s
            )
            return None
        finally:
            try:
                ser.close()
            except Exception:  # pragma: no cover - defensive
                pass

    @staticmethod
    def _parse_gpsd_time(value) -> Optional[datetime]:
        """Parse the ISO-8601 timestamp gpsd reports into a UTC datetime."""
        if value in (None, "n/a", ""):
            return None
        try:
            # gpsd returns e.g. "2026-04-23T21:00:00.000Z"
            text = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(text).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _combine_nmea_time(nmea_time) -> Optional[datetime]:
        """Combine the time-of-day from NMEA with today's UTC date."""
        if nmea_time is None:
            return None
        today = datetime.now(timezone.utc).date()
        try:
            return datetime.combine(
                today, nmea_time, tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            return None
