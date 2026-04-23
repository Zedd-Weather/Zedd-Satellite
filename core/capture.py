"""RF capture wrapper around ``rtl_fm``.

This module spawns ``rtl_fm`` as a subprocess at the AOS time of an
upcoming pass and pipes its raw 16-bit signed PCM output into ``sox``
to produce a properly formatted WAV file. The resulting file is saved
under ``output/`` with a timestamped, satellite-keyed filename so that
:mod:`core.decoder` can pick it up later.

The wrapper is defensive: it surfaces missing binaries, USB
disconnects, and timing issues as :class:`CaptureError` exceptions so
callers (the scheduler in :mod:`main`) can decide how to react.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from core.tracker import PassEvent

LOGGER = logging.getLogger(__name__)

# Sample rate expected by wxtoimg / meteor-demod after sox resampling.
NOAA_TARGET_SAMPLE_RATE = 11025
METEOR_TARGET_SAMPLE_RATE = 96000


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of a successful pass recording.

    Attributes:
        wav_path: Absolute path to the produced ``.wav`` file.
        pass_event: The :class:`PassEvent` that was captured.
        started_at: UTC time the recording actually started.
        duration_seconds: How long ``rtl_fm`` was allowed to run.
    """

    wav_path: str
    pass_event: PassEvent
    started_at: datetime
    duration_seconds: int


class CaptureError(RuntimeError):
    """Raised when a capture cannot be completed (SDR missing, etc.)."""


class Capture:
    """Drive ``rtl_fm`` + ``sox`` to record a single satellite pass.

    Args:
        settings: Parsed ``config/settings.json`` dictionary.
    """

    # Characters that are unsafe in cross-platform file names.
    _UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

    def __init__(self, settings: Dict) -> None:
        self._settings = settings
        sdr_cfg = settings["sdr"]
        self._rtl_fm: str = sdr_cfg.get("rtl_fm_binary", "rtl_fm")
        self._sox: str = sdr_cfg.get("sox_binary", "sox")
        self._device_index: int = int(sdr_cfg.get("device_index", 0))
        self._gain: float = float(sdr_cfg.get("gain", 42.0))
        self._ppm: int = int(sdr_cfg.get("ppm_correction", 0))
        self._sample_rate: int = int(sdr_cfg.get("sample_rate_hz", 48000))

        paths_cfg = settings.get("paths", {})
        self._output_dir: str = paths_cfg.get("output_dir", "output")
        os.makedirs(self._output_dir, exist_ok=True)

    # ------------------------------------------------------------------ API
    def capture_pass(
        self,
        pass_event: PassEvent,
        wait_for_aos: bool = True,
    ) -> CaptureResult:
        """Record a complete satellite pass to a WAV file.

        Args:
            pass_event: Pass description produced by
                :class:`core.tracker.Tracker`.
            wait_for_aos: When ``True`` (default), block in this thread
                until the AOS timestamp has been reached. When the
                scheduler fires this method exactly at AOS the wait is
                a no-op.

        Returns:
            A :class:`CaptureResult` describing the produced file.

        Raises:
            CaptureError: If the SDR is missing, the binaries are not
                installed, or the recording subprocess fails.
        """
        self._verify_binaries()

        if wait_for_aos:
            self._sleep_until(pass_event.aos)

        duration = pass_event.duration_seconds
        if duration <= 0:
            raise CaptureError(
                f"Refusing to record {pass_event.satellite}: "
                f"non-positive duration ({duration}s)"
            )

        wav_path = self._build_output_path(pass_event)
        target_rate = (
            METEOR_TARGET_SAMPLE_RATE
            if pass_event.mode == "METEOR_LRPT"
            else NOAA_TARGET_SAMPLE_RATE
        )

        rtl_cmd = [
            self._rtl_fm,
            "-d", str(self._device_index),
            "-f", f"{pass_event.frequency_mhz}M",
            "-M", "fm",
            "-s", str(self._sample_rate),
            "-g", f"{self._gain}",
            "-p", str(self._ppm),
            "-E", "deemp",
            "-F", "9",
            "-",
        ]
        sox_cmd = [
            self._sox,
            "-t", "raw",
            "-r", str(self._sample_rate),
            "-es",
            "-b", "16",
            "-c", "1",
            "-V1",
            "-",
            wav_path,
            "rate", str(target_rate),
        ]

        LOGGER.info(
            "Starting capture: sat=%s freq=%.4f MHz duration=%ds -> %s",
            pass_event.satellite,
            pass_event.frequency_mhz,
            duration,
            wav_path,
        )
        LOGGER.debug("rtl_fm cmd: %s", " ".join(rtl_cmd))
        LOGGER.debug("sox cmd:    %s", " ".join(sox_cmd))

        started_at = datetime.now(timezone.utc)
        try:
            self._run_pipeline(rtl_cmd, sox_cmd, duration)
        except FileNotFoundError as exc:
            raise CaptureError(f"Required binary not found: {exc}") from exc
        except subprocess.SubprocessError as exc:
            raise CaptureError(
                f"Capture subprocess failed for {pass_event.satellite}: {exc}"
            ) from exc

        if not os.path.isfile(wav_path) or os.path.getsize(wav_path) == 0:
            raise CaptureError(
                f"Capture produced no output: {wav_path}. "
                "Is the RTL-SDR connected?"
            )

        LOGGER.info(
            "Capture finished: %s (%.1f kB)",
            wav_path,
            os.path.getsize(wav_path) / 1024.0,
        )
        return CaptureResult(
            wav_path=os.path.abspath(wav_path),
            pass_event=pass_event,
            started_at=started_at,
            duration_seconds=duration,
        )

    # --------------------------------------------------------- internals
    def _verify_binaries(self) -> None:
        """Ensure ``rtl_fm`` and ``sox`` are on ``$PATH``.

        Raises:
            CaptureError: If a required binary cannot be found.
        """
        for binary in (self._rtl_fm, self._sox):
            if shutil.which(binary) is None:
                raise CaptureError(
                    f"Required binary {binary!r} is not installed or "
                    "is missing from $PATH"
                )

    def _sleep_until(self, when: datetime) -> None:
        """Sleep until the wall clock reaches ``when`` (UTC)."""
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        if delta > 0:
            LOGGER.info("Sleeping %.1fs until AOS at %s", delta, when.isoformat())
            time.sleep(delta)

    def _build_output_path(self, pass_event: PassEvent) -> str:
        """Derive a filesystem-safe WAV path for a pass."""
        safe_name = self._UNSAFE_FILENAME_RE.sub("_", pass_event.satellite)
        timestamp = pass_event.aos.strftime("%Y-%m-%dT%H-%M-%SZ")
        filename = f"{safe_name}_{timestamp}.wav"
        return os.path.join(self._output_dir, filename)

    def _run_pipeline(
        self,
        rtl_cmd: list,
        sox_cmd: list,
        duration: int,
    ) -> None:
        """Run ``rtl_fm | sox`` for ``duration`` seconds.

        rtl_fm is killed gracefully once the duration elapses; sox is
        then given a short grace window to flush its WAV header. Both
        return codes are checked.
        """
        rtl_proc: Optional[subprocess.Popen] = None
        sox_proc: Optional[subprocess.Popen] = None
        try:
            rtl_proc = subprocess.Popen(
                rtl_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            sox_proc = subprocess.Popen(
                sox_cmd,
                stdin=rtl_proc.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            # Allow rtl_fm to receive SIGPIPE if sox dies.
            if rtl_proc.stdout is not None:
                rtl_proc.stdout.close()

            try:
                sox_proc.wait(timeout=duration)
            except subprocess.TimeoutExpired:
                LOGGER.debug("Pass duration elapsed, terminating rtl_fm")
                rtl_proc.terminate()
                try:
                    rtl_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    LOGGER.warning("rtl_fm did not exit cleanly; killing")
                    rtl_proc.kill()
                # Give sox a moment to finalize the WAV header.
                try:
                    sox_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    LOGGER.warning("sox did not exit cleanly; killing")
                    sox_proc.kill()
        finally:
            for proc, name in ((rtl_proc, "rtl_fm"), (sox_proc, "sox")):
                if proc is None:
                    continue
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                stderr = b""
                if proc.stderr is not None:
                    try:
                        stderr = proc.stderr.read() or b""
                    except (OSError, ValueError):
                        stderr = b""
                if proc.returncode not in (0, None, -15):
                    LOGGER.warning(
                        "%s exited with code %s: %s",
                        name,
                        proc.returncode,
                        stderr.decode("utf-8", errors="replace").strip(),
                    )
