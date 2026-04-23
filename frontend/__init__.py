"""Zedd-Satellite web frontend.

A small, read-only Flask application that surfaces the daemon's state
(upcoming passes, captured imagery, station health, recent log lines)
over HTTP for operators on the local network.

The frontend never mutates daemon state -- it only reads the same
``config/settings.json`` and on-disk artifacts (``output/``, ``logs/``)
that the daemon already produces. This keeps the surface area small and
makes the dashboard safe to expose alongside the running daemon.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
