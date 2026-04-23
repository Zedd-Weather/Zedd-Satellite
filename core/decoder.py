"""Routing decoder for captured satellite WAV files.

A captured pass is handed to :meth:`Decoder.decode`, which inspects the
:class:`core.capture.CaptureResult` to choose the right backend:

* ``NOAA_APT``  -> ``wxtoimg``  (NOAA 15 / 18 / 19 APT imagery).
* ``METEOR_LRPT`` -> ``meteor-demod`` (Meteor-M2 LRPT imagery).

The decoder writes a timestamped ``.png`` next to the source ``.wav`` in
the configured ``output/`` directory.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

from core.capture import CaptureResult

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecodeResult:
    """Outcome of a successful decode operation.

    Attributes:
        image_path: Absolute path to the produced PNG image.
        source_wav: Absolute path of the WAV file that was decoded.
        decoder: Name of the backend used (``"wxtoimg"`` /
            ``"meteor-demod"``).
        decoded_at: UTC timestamp at which decoding finished.
    """

    image_path: str
    source_wav: str
    decoder: str
    decoded_at: datetime


class DecoderError(RuntimeError):
    """Raised when a WAV cannot be decoded into an image."""


class Decoder:
    """Route captured WAV files to the appropriate decoder backend.

    Args:
        settings: Parsed ``config/settings.json`` dictionary.
    """

    def __init__(self, settings: Dict) -> None:
        self._settings = settings
        decoder_cfg = settings.get("decoder", {})
        self._wxtoimg: str = decoder_cfg.get("wxtoimg_binary", "wxtoimg")
        self._meteor_demod: str = decoder_cfg.get(
            "meteor_demod_binary", "meteor-demod"
        )
        self._noaa_enhancement: str = decoder_cfg.get(
            "noaa_enhancement", "MCIR"
        )
        self._output_dir: str = decoder_cfg.get(
            "output_dir",
            settings.get("paths", {}).get("output_dir", "output"),
        )
        os.makedirs(self._output_dir, exist_ok=True)

    # ------------------------------------------------------------------ API
    def decode(self, capture: CaptureResult) -> DecodeResult:
        """Decode ``capture`` into a PNG image.

        The mode of the originating :class:`core.tracker.PassEvent`
        controls which backend is used.

        Args:
            capture: Capture result emitted by
                :meth:`core.capture.Capture.capture_pass`.

        Returns:
            A :class:`DecodeResult` referencing the generated image.

        Raises:
            DecoderError: If the mode is unknown, the backend binary is
                missing, or the subprocess fails / produces no output.
        """
        mode = capture.pass_event.mode
        LOGGER.info(
            "Decoding %s capture %s in mode %s",
            capture.pass_event.satellite,
            capture.wav_path,
            mode,
        )

        if mode == "NOAA_APT":
            return self._decode_noaa(capture)
        if mode == "METEOR_LRPT":
            return self._decode_meteor(capture)
        raise DecoderError(f"Unsupported decoding mode: {mode!r}")

    # --------------------------------------------------------- internals
    def _decode_noaa(self, capture: CaptureResult) -> DecodeResult:
        """Run ``wxtoimg`` against a NOAA APT WAV recording."""
        if shutil.which(self._wxtoimg) is None:
            raise DecoderError(
                f"NOAA decoder binary {self._wxtoimg!r} not found on $PATH"
            )

        image_path = self._build_image_path(capture)
        cmd: List[str] = [
            self._wxtoimg,
            "-e", self._noaa_enhancement,
            "-o",
            capture.wav_path,
            image_path,
        ]
        self._run(cmd, "wxtoimg")
        self._assert_image(image_path)
        return DecodeResult(
            image_path=os.path.abspath(image_path),
            source_wav=capture.wav_path,
            decoder="wxtoimg",
            decoded_at=datetime.now(timezone.utc),
        )

    def _decode_meteor(self, capture: CaptureResult) -> DecodeResult:
        """Run ``meteor-demod`` against a Meteor-M2 LRPT WAV recording."""
        if shutil.which(self._meteor_demod) is None:
            raise DecoderError(
                f"Meteor decoder binary {self._meteor_demod!r} not found "
                "on $PATH"
            )

        image_path = self._build_image_path(capture)
        # meteor-demod typically emits a soft-symbol .s file that is then
        # piped through medet to produce the PNG. Many distributions ship
        # a wrapper that performs the full chain when given -o <png>.
        cmd: List[str] = [
            self._meteor_demod,
            "-o", image_path,
            capture.wav_path,
        ]
        self._run(cmd, "meteor-demod")
        self._assert_image(image_path)
        return DecodeResult(
            image_path=os.path.abspath(image_path),
            source_wav=capture.wav_path,
            decoder="meteor-demod",
            decoded_at=datetime.now(timezone.utc),
        )

    def _build_image_path(self, capture: CaptureResult) -> str:
        """Derive the PNG path for ``capture`` (timestamped)."""
        stem, _ext = os.path.splitext(os.path.basename(capture.wav_path))
        # Append a decode timestamp so re-decodes don't clobber output.
        decode_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{stem}_{decode_ts}.png"
        return os.path.join(self._output_dir, filename)

    def _run(self, cmd: List[str], name: str) -> None:
        """Execute a decoder subprocess, surfacing its stderr on failure."""
        LOGGER.debug("%s cmd: %s", name, " ".join(cmd))
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DecoderError(f"{name} not found: {exc}") from exc
        except OSError as exc:
            raise DecoderError(f"{name} failed to start: {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise DecoderError(
                f"{name} exited with code {completed.returncode}: {stderr}"
            )

    @staticmethod
    def _assert_image(image_path: str) -> None:
        """Ensure the decoder produced a non-empty file."""
        if not os.path.isfile(image_path) or os.path.getsize(image_path) == 0:
            raise DecoderError(
                f"Decoder did not produce an image at {image_path}"
            )
