"""Hardware health monitor for the Raspberry Pi ground station.

Multiple **redundant** data sources are queried so that a single missing
file or absent binary never blinds the daemon:

* CPU temperature -- ``/sys/class/thermal/thermal_zone0/temp`` (primary)
  with ``vcgencmd measure_temp`` as a fallback.
* Throttle / under-voltage status -- ``vcgencmd get_throttled``.
* Disk usage -- :func:`shutil.disk_usage` against the configured output
  directory and every storage mirror.
* SDR enumeration -- ``rtl_test -t`` (primary) with ``lsusb | grep RTL``
  as a fallback so a USB descriptor change still produces a count.

The :class:`HealthSnapshot` is small and JSON-serializable so it can be
emitted over the LoRa beacon defined in :mod:`core.lora`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

LOGGER = logging.getLogger(__name__)


@dataclass
class HealthSnapshot:
    """A single health reading suitable for logging or telemetry."""

    timestamp_utc: str
    cpu_temp_c: Optional[float] = None
    throttle_flags: Optional[int] = None
    throttled_now: bool = False
    under_voltage_now: bool = False
    disk_usage: Dict[str, Dict[str, int]] = field(default_factory=dict)
    sdr_count: int = 0
    sdr_source: Optional[str] = None

    def to_json(self) -> str:
        """Serialize to a compact JSON string (LoRa-friendly)."""
        return json.dumps(asdict(self), separators=(",", ":"))


class HealthMonitor:
    """Sample Pi 5 health from multiple redundant sources.

    Args:
        settings: Parsed ``config/settings.json``. Reads ``health.*`` and
            ``paths.output_dir`` / ``storage.mirror_dirs``.
    """

    # Bit positions for vcgencmd's get_throttled response (RPi docs).
    _THROTTLE_BIT_UNDER_VOLTAGE_NOW = 1 << 0
    _THROTTLE_BIT_THROTTLED_NOW = 1 << 2

    def __init__(self, settings: Dict) -> None:
        health_cfg = settings.get("health", {}) or {}
        self._enabled: bool = bool(health_cfg.get("enabled", True))
        self._cpu_temp_warn_c: float = float(
            health_cfg.get("cpu_temp_warn_c", 75.0)
        )
        self._disk_free_warn_pct: float = float(
            health_cfg.get("disk_free_warn_pct", 10.0)
        )
        self._period_s: int = int(health_cfg.get("period_s", 300))

        paths_cfg = settings.get("paths", {}) or {}
        primary = paths_cfg.get("output_dir", "output")
        mirrors = (
            (settings.get("storage", {}) or {}).get("mirror_dirs", []) or []
        )
        self._monitored_paths: List[str] = [primary] + list(mirrors)

    # ------------------------------------------------------------------ API
    @property
    def enabled(self) -> bool:
        """Whether the monitor is enabled in config."""
        return self._enabled

    @property
    def period_s(self) -> int:
        """Configured snapshot cadence in seconds."""
        return self._period_s

    def sample(self) -> HealthSnapshot:
        """Take an immediate health snapshot."""
        snap = HealthSnapshot(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        )
        snap.cpu_temp_c = self._read_cpu_temp()
        flags = self._read_throttle_flags()
        if flags is not None:
            snap.throttle_flags = flags
            snap.throttled_now = bool(flags & self._THROTTLE_BIT_THROTTLED_NOW)
            snap.under_voltage_now = bool(
                flags & self._THROTTLE_BIT_UNDER_VOLTAGE_NOW
            )
        snap.disk_usage = self._read_disk_usage()
        count, source = self._enumerate_sdrs()
        snap.sdr_count = count
        snap.sdr_source = source

        self._emit_warnings(snap)
        return snap

    # --------------------------------------------------------- internals
    @staticmethod
    def _read_cpu_temp() -> Optional[float]:
        """Read CPU temperature in °C from sysfs, fall back to vcgencmd."""
        sysfs = "/sys/class/thermal/thermal_zone0/temp"
        try:
            with open(sysfs, "r", encoding="utf-8") as fh:
                raw = fh.read().strip()
            return round(int(raw) / 1000.0, 1)
        except (OSError, ValueError):
            LOGGER.debug("sysfs thermal read failed; trying vcgencmd")

        try:
            out = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
            match = re.search(r"temp=([0-9.]+)'C", out.stdout or "")
            if match:
                return float(match.group(1))
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        return None

    @staticmethod
    def _read_throttle_flags() -> Optional[int]:
        """Read the Pi's vcgencmd ``get_throttled`` bitmask."""
        try:
            out = subprocess.run(
                ["vcgencmd", "get_throttled"],
                capture_output=True, text=True, timeout=2.0, check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        match = re.search(r"throttled=0x([0-9a-fA-F]+)", out.stdout or "")
        if not match:
            return None
        try:
            return int(match.group(1), 16)
        except ValueError:
            return None

    def _read_disk_usage(self) -> Dict[str, Dict[str, int]]:
        """Return ``{path: {total, free, free_pct}}`` for each watched path."""
        result: Dict[str, Dict[str, int]] = {}
        for path in self._monitored_paths:
            if not path:
                continue
            try:
                # disk_usage requires the path to exist. Walk up if needed.
                target = path
                while target and not os.path.exists(target):
                    parent = os.path.dirname(target)
                    if parent == target:
                        target = None
                        break
                    target = parent
                if not target:
                    continue
                usage = shutil.disk_usage(target)
                result[path] = {
                    "total": int(usage.total),
                    "free": int(usage.free),
                    "free_pct": int(round(usage.free * 100 / usage.total)),
                }
            except OSError as exc:
                LOGGER.debug("disk_usage failed for %s: %s", path, exc)
        return result

    @staticmethod
    def _enumerate_sdrs() -> tuple:
        """Count attached RTL-SDR dongles via ``rtl_test`` then ``lsusb``."""
        # Primary: rtl_test -t exits after listing devices on stderr.
        if shutil.which("rtl_test"):
            try:
                proc = subprocess.run(
                    ["rtl_test", "-t"],
                    capture_output=True, text=True, timeout=5.0, check=False,
                )
                # rtl_test prints "Found N device(s):" on its stderr.
                match = re.search(
                    r"Found\s+(\d+)\s+device", (proc.stderr or "") + (proc.stdout or "")
                )
                if match:
                    return (int(match.group(1)), "rtl_test")
            except (FileNotFoundError, subprocess.SubprocessError):
                pass
        # Fallback: lsusb regex match against known RTL chips.
        if shutil.which("lsusb"):
            try:
                proc = subprocess.run(
                    ["lsusb"],
                    capture_output=True, text=True, timeout=5.0, check=False,
                )
                count = len(re.findall(
                    r"RTL2832|RTL2838|Realtek.*DVB", proc.stdout or ""
                ))
                return (count, "lsusb")
            except (FileNotFoundError, subprocess.SubprocessError):
                pass
        return (0, None)

    def _emit_warnings(self, snap: HealthSnapshot) -> None:
        """Log high-severity warnings derived from the snapshot."""
        if snap.cpu_temp_c is not None and snap.cpu_temp_c >= self._cpu_temp_warn_c:
            LOGGER.warning(
                "Pi CPU temperature %.1f°C >= warn threshold %.1f°C; "
                "verify the active cooler is running",
                snap.cpu_temp_c, self._cpu_temp_warn_c,
            )
        if snap.throttled_now:
            LOGGER.warning("Pi reports active CPU throttling (vcgencmd)")
        if snap.under_voltage_now:
            LOGGER.warning(
                "Pi reports active under-voltage (vcgencmd); "
                "use the official 27W USB-C PSU"
            )
        for path, usage in snap.disk_usage.items():
            if usage.get("free_pct", 100) < self._disk_free_warn_pct:
                LOGGER.warning(
                    "Disk %s low on free space: %d%% remaining",
                    path, usage["free_pct"],
                )
        if snap.sdr_count == 0:
            LOGGER.warning(
                "No RTL-SDR dongles detected (source=%s); next capture will fail",
                snap.sdr_source,
            )
