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
from typing import Callable, Dict, List, Optional

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
        # Redundant decoder chains. Each entry is the binary name of an
        # alternate decoder tried in order if the primary fails. Format
        # is identical to the primary (-o output input).
        self._noaa_fallbacks: List[str] = list(
            decoder_cfg.get("noaa_fallbacks", ["noaa-apt"]) or []
        )
        self._meteor_fallbacks: List[str] = list(
            decoder_cfg.get("meteor_fallbacks", ["medet"]) or []
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
        """Decode a NOAA APT WAV, trying redundant backends in order."""
        primary_cmd_factory = lambda binary, image_path: [
            binary,
            "-e", self._noaa_enhancement,
            "-o",
            capture.wav_path,
            image_path,
        ]
        # noaa-apt fallback uses a different argv layout: <input> <output>.
        fallback_cmd_factory = lambda binary, image_path: [
            binary, capture.wav_path, image_path,
        ]
        return self._decode_with_fallbacks(
            capture,
            primary=self._wxtoimg,
            primary_cmd_factory=primary_cmd_factory,
            fallbacks=self._noaa_fallbacks,
            fallback_cmd_factory=fallback_cmd_factory,
            kind="NOAA",
        )

    def _decode_meteor(self, capture: CaptureResult) -> DecodeResult:
        """Decode a Meteor-M2 LRPT WAV, trying redundant backends in order."""
        primary_cmd_factory = lambda binary, image_path: [
            binary, "-o", image_path, capture.wav_path,
        ]
        # medet uses positional input + -o output.
        fallback_cmd_factory = lambda binary, image_path: [
            binary, capture.wav_path, image_path,
        ]
        return self._decode_with_fallbacks(
            capture,
            primary=self._meteor_demod,
            primary_cmd_factory=primary_cmd_factory,
            fallbacks=self._meteor_fallbacks,
            fallback_cmd_factory=fallback_cmd_factory,
            kind="Meteor",
        )

    def _decode_with_fallbacks(
        self,
        capture: CaptureResult,
        primary: str,
        primary_cmd_factory: Callable[[str, str], List[str]],
        fallbacks: List[str],
        fallback_cmd_factory: Callable[[str, str], List[str]],
        kind: str,
    ) -> DecodeResult:
        """Run ``primary`` then each entry in ``fallbacks`` until one wins."""
        attempts: List[tuple] = [(primary, primary_cmd_factory)]
        attempts.extend((b, fallback_cmd_factory) for b in fallbacks)

        last_error: Optional[Exception] = None
        for binary, factory in attempts:
            if shutil.which(binary) is None:
                LOGGER.warning(
                    "%s decoder %r not on $PATH; trying next fallback",
                    kind, binary,
                )
                continue
            image_path = self._build_image_path(capture)
            try:
                self._run(factory(binary, image_path), binary)
                self._assert_image(image_path)
                LOGGER.info(
                    "%s decode succeeded with %s -> %s",
                    kind, binary, image_path,
                )
                return DecodeResult(
                    image_path=os.path.abspath(image_path),
                    source_wav=capture.wav_path,
                    decoder=binary,
                    decoded_at=datetime.now(timezone.utc),
                )
            except DecoderError as exc:
                last_error = exc
                LOGGER.warning(
                    "%s decode with %s failed: %s", kind, binary, exc
                )
                # Clean up partial output before trying the next backend.
                try:
                    if os.path.isfile(image_path):
                        os.remove(image_path)
                except OSError:
                    pass

        raise DecoderError(
            f"All {kind} decoders failed for {capture.wav_path}; "
            f"last error: {last_error}"
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
