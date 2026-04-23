"""Core package for the Zedd-Satellite ground station.

Submodules:
    * :mod:`core.tracker` -- TLE handling and pass prediction.
    * :mod:`core.capture` -- ``rtl_fm`` based RF recording.
    * :mod:`core.decoder` -- WAV to PNG decoding routing.
"""

from __future__ import annotations

__all__: list[str] = ["tracker", "capture", "decoder"]
