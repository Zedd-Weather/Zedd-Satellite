"""Satellite tracking utilities.

This module is responsible for:

* Loading TLE (Two-Line Element) sets from the local cache file (or
  downloading them on demand).
* Propagating the orbit of every configured satellite using
  :mod:`skyfield`.
* Returning a list of upcoming :class:`PassEvent` objects (AOS / LOS /
  max elevation) for the next *N* hours, filtered by a minimum maximum
  elevation threshold.

The module is deliberately decoupled from the scheduler and the SDR
capture layer; it only knows about geometry and time.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import requests
from skyfield.api import EarthSatellite, Loader, Topos, wgs84

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PassEvent:
    """A single upcoming satellite pass over the ground station.

    Attributes:
        satellite: Human readable satellite name (e.g. ``"NOAA 19"``).
        frequency_mhz: Downlink frequency in MHz, copied from settings.
        mode: Decoding mode (``"NOAA_APT"`` or ``"METEOR_LRPT"``).
        aos: UTC datetime of Acquisition Of Signal (rise above horizon).
        los: UTC datetime of Loss Of Signal (set below horizon).
        max_elevation_deg: Peak elevation reached during the pass, in
            degrees.
        max_elevation_time: UTC datetime at which ``max_elevation_deg``
            is reached.
    """

    satellite: str
    frequency_mhz: float
    mode: str
    aos: datetime
    los: datetime
    max_elevation_deg: float
    max_elevation_time: datetime

    @property
    def duration_seconds(self) -> int:
        """Return the pass duration in whole seconds."""
        return max(0, int((self.los - self.aos).total_seconds()))


class TLEError(RuntimeError):
    """Raised when TLE data cannot be loaded or parsed."""


class Tracker:
    """Compute upcoming satellite passes for a ground station.

    Args:
        settings: Parsed ``config/settings.json`` dictionary.
        loader: Optional :class:`skyfield.api.Loader` (mostly used by
            tests so that ephemeris data can be cached in a tmp dir).
    """

    def __init__(
        self,
        settings: Dict,
        loader: Optional[Loader] = None,
    ) -> None:
        self._settings = settings
        self._loader = loader or Loader(os.path.join("config", ".skyfield-cache"))
        self._timescale = self._loader.timescale(builtin=True)

        station_cfg = settings["station"]
        self._station: Topos = wgs84.latlon(
            latitude_degrees=float(station_cfg["latitude_deg"]),
            longitude_degrees=float(station_cfg["longitude_deg"]),
            elevation_m=float(station_cfg.get("elevation_m", 0.0)),
        )

        self._min_elevation: float = float(
            settings["pass_filter"].get("min_elevation_deg", 20.0)
        )
        self._look_ahead: int = int(
            settings["pass_filter"].get("look_ahead_hours", 24)
        )

    # ------------------------------------------------------------------ TLE
    def load_tle_file(self, path: str) -> Dict[str, EarthSatellite]:
        """Load every TLE in ``path`` into a name->satellite mapping.

        Args:
            path: Path to a text file containing one or more TLE
                triplets (name + 2 orbital lines).

        Returns:
            A dictionary keyed by the satellite name (stripped) mapping
            to a :class:`skyfield.api.EarthSatellite`.

        Raises:
            TLEError: If the file is missing or cannot be parsed.
        """
        if not os.path.isfile(path):
            raise TLEError(f"TLE cache file not found: {path}")

        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line.rstrip("\r\n") for line in handle if line.strip()]
        except OSError as exc:  # pragma: no cover - defensive
            raise TLEError(f"Could not read TLE file {path}: {exc}") from exc

        satellites: Dict[str, EarthSatellite] = {}
        for i in range(0, len(lines) - 2, 3):
            name = lines[i].strip()
            line1 = lines[i + 1].strip()
            line2 = lines[i + 2].strip()
            if not (line1.startswith("1 ") and line2.startswith("2 ")):
                LOGGER.debug("Skipping malformed TLE block at index %d", i)
                continue
            try:
                satellites[name] = EarthSatellite(
                    line1, line2, name, self._timescale
                )
            except (ValueError, RuntimeError) as exc:
                LOGGER.warning("Failed to parse TLE for %s: %s", name, exc)

        if not satellites:
            raise TLEError(f"No valid TLEs found in {path}")
        LOGGER.info("Loaded %d TLEs from %s", len(satellites), path)
        return satellites

    def download_tles(self, destination: Optional[str] = None) -> str:
        """Download the configured TLE sources and concatenate them.

        Args:
            destination: Where to write the merged TLE file. Defaults to
                the value of ``tle.cache_file`` in settings.

        Returns:
            The absolute path of the file that was written.

        Raises:
            TLEError: If every configured source fails to download.
        """
        tle_cfg = self._settings["tle"]
        destination = destination or tle_cfg["cache_file"]
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

        chunks: List[str] = []
        for url in tle_cfg.get("sources", []):
            try:
                LOGGER.info("Fetching TLE source %s", url)
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                chunks.append(response.text)
            except requests.RequestException as exc:
                LOGGER.warning("TLE download failed for %s: %s", url, exc)

        if not chunks:
            raise TLEError("All TLE downloads failed")

        with open(destination, "w", encoding="utf-8") as handle:
            handle.write("\n".join(chunks))
        LOGGER.info("Wrote merged TLE file to %s", destination)
        return os.path.abspath(destination)

    # ------------------------------------------------------------ Pass calc
    def upcoming_passes(
        self,
        satellites: Optional[Dict[str, EarthSatellite]] = None,
        start: Optional[datetime] = None,
        hours: Optional[int] = None,
    ) -> List[PassEvent]:
        """Return all qualifying passes within the look-ahead window.

        A pass qualifies if its peak elevation is ``>=`` the configured
        ``min_elevation_deg`` (default 20°). The list is sorted in
        ascending AOS order.

        Args:
            satellites: Optional pre-loaded TLE map. If ``None``, the
                file at ``tle.cache_file`` is loaded.
            start: UTC start of the prediction window. Defaults to
                "now".
            hours: Override for ``pass_filter.look_ahead_hours``.

        Returns:
            A chronologically ordered list of :class:`PassEvent`.
        """
        if satellites is None:
            satellites = self.load_tle_file(self._settings["tle"]["cache_file"])

        start_dt = (start or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_h = int(hours or self._look_ahead)
        end_dt = start_dt + timedelta(hours=window_h)

        t0 = self._timescale.from_datetime(start_dt)
        t1 = self._timescale.from_datetime(end_dt)

        events: List[PassEvent] = []
        for sat_name, sat_cfg in self._settings["satellites"].items():
            tle_name = sat_cfg.get("tle_name", sat_name)
            sat = satellites.get(tle_name)
            if sat is None:
                LOGGER.warning(
                    "No TLE found for %s (looked up as %r); skipping",
                    sat_name,
                    tle_name,
                )
                continue

            try:
                times, kinds = sat.find_events(
                    self._station,
                    t0,
                    t1,
                    altitude_degrees=0.0,
                )
            except Exception as exc:  # pragma: no cover - skyfield internals
                LOGGER.error("Pass prediction failed for %s: %s", sat_name, exc)
                continue

            events.extend(
                self._collect_passes(sat_name, sat_cfg, sat, times, kinds)
            )

        events.sort(key=lambda ev: ev.aos)
        LOGGER.info(
            "Predicted %d qualifying passes in the next %d h "
            "(min elevation %.1f deg)",
            len(events),
            window_h,
            self._min_elevation,
        )
        return events

    # ----------------------------------------------------------- internals
    def _collect_passes(
        self,
        sat_name: str,
        sat_cfg: Dict,
        sat: EarthSatellite,
        times: Iterable,
        kinds: Iterable[int],
    ) -> List[PassEvent]:
        """Group skyfield ``find_events`` output into discrete passes.

        ``find_events`` returns a flat sequence of (rise=0, culminate=1,
        set=2) markers. We walk through them and emit a :class:`PassEvent`
        for every (rise, culminate, set) triple whose peak elevation
        passes the configured filter.
        """
        events: List[PassEvent] = []
        aos_t = None
        culm_t = None

        time_list = list(times)
        kind_list = list(kinds)

        for t, kind in zip(time_list, kind_list):
            if kind == 0:  # rise
                aos_t = t
                culm_t = None
            elif kind == 1:  # culmination
                culm_t = t
            elif kind == 2 and aos_t is not None and culm_t is not None:
                # Compute peak elevation at culmination.
                difference = (sat - self._station).at(culm_t)
                alt, _az, _dist = difference.altaz()
                max_el = float(alt.degrees)
                if max_el < self._min_elevation:
                    LOGGER.debug(
                        "Discarding %s pass at %s: peak elev %.1f < %.1f",
                        sat_name,
                        aos_t.utc_iso(),
                        max_el,
                        self._min_elevation,
                    )
                else:
                    events.append(
                        PassEvent(
                            satellite=sat_name,
                            frequency_mhz=float(sat_cfg["frequency_mhz"]),
                            mode=str(sat_cfg["mode"]),
                            aos=aos_t.utc_datetime(),
                            los=t.utc_datetime(),
                            max_elevation_deg=max_el,
                            max_elevation_time=culm_t.utc_datetime(),
                        )
                    )
                aos_t = None
                culm_t = None
        return events
