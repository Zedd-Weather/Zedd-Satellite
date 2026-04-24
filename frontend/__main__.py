"""``python -m frontend`` -- launch the Zedd-Satellite dashboard.

Usage::

    python -m frontend                    # default: 0.0.0.0:8080
    python -m frontend --host 127.0.0.1   # bind loopback only
    python -m frontend --port 9000        # custom port
    python -m frontend --debug            # Flask debug + reloader

The frontend is read-only and reads the same ``config/settings.json``
the daemon uses, so it can be run side-by-side with ``main.py``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from core.config import SettingsValidationError, load_settings, run_startup_preflight

from .app import DEFAULT_SETTINGS_PATH, create_app


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m frontend",
        description="Zedd-Satellite web dashboard.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind (default: 127.0.0.1, loopback only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="TCP port to listen on (default: 8080).",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_SETTINGS_PATH,
        help=f"Path to settings.json (default: {DEFAULT_SETTINGS_PATH}).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode + auto-reloader (development only).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        settings = load_settings(args.config)
        run_startup_preflight(settings, service="frontend")
    except (FileNotFoundError, ValueError, SettingsValidationError) as exc:
        print(f"FATAL: cannot load dashboard settings: {exc}", file=sys.stderr)
        return 2
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        logging.warning(
            "Binding the dashboard to %s exposes it beyond loopback; place it behind an authenticated TLS reverse proxy before production use.",
            args.host,
        )
    app = create_app(settings_path=args.config)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
