"""Mirrored output storage for redundant capture/decoded image retention.

Captures and decoded images are written to a primary directory (typically
the Pi 5 NVMe HAT or a USB SSD) and then mirrored to one or more
secondary directories (the microSD card or a second USB drive). A
mirror failure is logged but never aborts the capture pipeline -- the
primary write always wins.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Dict, List

LOGGER = logging.getLogger(__name__)


class MirroredStorage:
    """Copy newly produced files into a list of redundant directories.

    Args:
        settings: Parsed ``config/settings.json``. Reads
            ``storage.mirror_dirs`` (list[str]).
    """

    def __init__(self, settings: Dict) -> None:
        storage_cfg = settings.get("storage", {}) or {}
        self._mirrors: List[str] = list(storage_cfg.get("mirror_dirs", []) or [])
        for mirror in self._mirrors:
            try:
                os.makedirs(mirror, exist_ok=True)
            except OSError as exc:
                LOGGER.warning(
                    "Cannot prepare mirror dir %s: %s", mirror, exc
                )

    @property
    def mirrors(self) -> List[str]:
        """Return the configured mirror directories."""
        return list(self._mirrors)

    def mirror(self, source_path: str) -> List[str]:
        """Copy ``source_path`` into every mirror directory.

        Returns the list of files that were successfully written.
        """
        copied: List[str] = []
        if not self._mirrors:
            return copied
        if not os.path.isfile(source_path):
            LOGGER.warning("Cannot mirror missing file: %s", source_path)
            return copied
        basename = os.path.basename(source_path)
        for mirror in self._mirrors:
            destination = os.path.join(mirror, basename)
            # Skip self-copies (source already inside this mirror).
            try:
                if os.path.abspath(source_path) == os.path.abspath(destination):
                    continue
            except OSError:
                continue
            try:
                shutil.copy2(source_path, destination)
                copied.append(destination)
                LOGGER.info("Mirrored %s -> %s", source_path, destination)
            except OSError as exc:
                LOGGER.warning(
                    "Mirror copy to %s failed: %s", destination, exc
                )
        return copied
