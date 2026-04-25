"""Configuration loading and startup validation for Zedd-Satellite."""

from __future__ import annotations

import json
import os
import shutil
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SUPPORTED_MODES = {"NOAA_APT", "METEOR_LRPT"}
SUPPORTED_LORA_BANDWIDTHS = {
    7800,
    10400,
    15600,
    20800,
    31250,
    41700,
    62500,
    125000,
    250000,
    500000,
}


class SettingsValidationError(ValueError):
    """Raised when settings are malformed or unsafe for startup."""


def load_settings(path: str) -> dict:
    """Load, parse, and validate a ``settings.json`` file."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Settings file not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        settings = json.load(handle)
    validate_settings(settings)
    return settings


def validate_settings(settings: dict) -> None:
    """Validate the structure and key values in ``settings``."""
    errors: list[str] = []
    station = _require_dict(settings, "station", errors)
    sdr = _require_dict(settings, "sdr", errors)
    pass_filter = _require_dict(settings, "pass_filter", errors)
    tle = _require_dict(settings, "tle", errors)
    satellites = _require_dict(settings, "satellites", errors)
    decoder = _require_dict(settings, "decoder", errors)
    paths = _require_dict(settings, "paths", errors)
    storage = _require_dict(settings, "storage", errors)
    gps = _require_dict(settings, "gps", errors)
    lora = _require_dict(settings, "lora", errors)
    health = _require_dict(settings, "health", errors)

    _require_string(station, "name", errors, "station.name")
    _require_timezone(station, "timezone", errors, "station.timezone")
    _require_number(
        station, "latitude_deg", errors, "station.latitude_deg", minimum=-90, maximum=90
    )
    _require_number(
        station, "longitude_deg", errors, "station.longitude_deg", minimum=-180, maximum=180
    )
    _require_number(
        station, "elevation_m", errors, "station.elevation_m", minimum=-500, maximum=10000
    )

    _require_number(sdr, "gain", errors, "sdr.gain")
    _require_integer(sdr, "ppm_correction", errors, "sdr.ppm_correction")
    _require_integer(
        sdr, "sample_rate_hz", errors, "sdr.sample_rate_hz", minimum=1
    )
    _require_string(sdr, "rtl_fm_binary", errors, "sdr.rtl_fm_binary")
    _require_string(sdr, "sox_binary", errors, "sdr.sox_binary")
    if "rtl_biast_binary" in sdr:
        _require_string(sdr, "rtl_biast_binary", errors, "sdr.rtl_biast_binary")
    device_indexes = sdr.get("device_indexes", [])
    if not isinstance(device_indexes, list) or not device_indexes:
        errors.append("sdr.device_indexes must be a non-empty list")
    else:
        for index, value in enumerate(device_indexes):
            if not isinstance(value, int) or value < 0:
                errors.append(f"sdr.device_indexes[{index}] must be an integer >= 0")

    _require_number(
        pass_filter,
        "min_elevation_deg",
        errors,
        "pass_filter.min_elevation_deg",
        minimum=0,
        maximum=90,
    )
    _require_integer(
        pass_filter,
        "look_ahead_hours",
        errors,
        "pass_filter.look_ahead_hours",
        minimum=1,
        maximum=168,
    )
    _require_integer(
        pass_filter, "lead_in_seconds", errors, "pass_filter.lead_in_seconds", minimum=0
    )
    _require_integer(
        pass_filter, "lead_out_seconds", errors, "pass_filter.lead_out_seconds", minimum=0
    )

    _require_string(tle, "cache_file", errors, "tle.cache_file")
    sources = tle.get("sources", [])
    if not isinstance(sources, list) or not sources:
        errors.append("tle.sources must be a non-empty list of URLs")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, str) or not source.strip():
                errors.append(f"tle.sources[{index}] must be a non-empty string")
                continue
            parsed = urlparse(source)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"tle.sources[{index}] must be an absolute HTTP(S) URL")

    if not isinstance(satellites, dict) or not satellites:
        errors.append("satellites must be a non-empty object")
    else:
        for name, satellite in satellites.items():
            if not isinstance(satellite, dict):
                errors.append(f"satellites.{name} must be an object")
                continue
            _require_number(
                satellite,
                "frequency_mhz",
                errors,
                f"satellites.{name}.frequency_mhz",
                minimum=1,
            )
            mode = satellite.get("mode")
            if mode not in SUPPORTED_MODES:
                errors.append(
                    f"satellites.{name}.mode must be one of {sorted(SUPPORTED_MODES)}"
                )
            _require_string(satellite, "tle_name", errors, f"satellites.{name}.tle_name")

    _require_string(decoder, "wxtoimg_binary", errors, "decoder.wxtoimg_binary")
    _require_string(
        decoder, "meteor_demod_binary", errors, "decoder.meteor_demod_binary"
    )
    _require_string(decoder, "noaa_enhancement", errors, "decoder.noaa_enhancement")
    _require_string(decoder, "output_dir", errors, "decoder.output_dir")
    _require_string_list(decoder, "noaa_fallbacks", errors, "decoder.noaa_fallbacks")
    _require_string_list(
        decoder, "meteor_fallbacks", errors, "decoder.meteor_fallbacks"
    )

    _require_string(paths, "output_dir", errors, "paths.output_dir")
    _require_string(paths, "log_dir", errors, "paths.log_dir")
    _require_string(paths, "log_file", errors, "paths.log_file")
    _require_string_list(storage, "mirror_dirs", errors, "storage.mirror_dirs")

    if bool(gps.get("enabled", False)):
        _require_string(gps, "gpsd_host", errors, "gps.gpsd_host")
        _require_integer(gps, "gpsd_port", errors, "gps.gpsd_port", minimum=1, maximum=65535)
        _require_string(gps, "serial_port", errors, "gps.serial_port")
        _require_integer(gps, "serial_baud", errors, "gps.serial_baud", minimum=1)
        _require_number(gps, "read_timeout_s", errors, "gps.read_timeout_s", minimum=0.1)
        _require_number(
            gps, "max_clock_drift_s", errors, "gps.max_clock_drift_s", minimum=0
        )

    if bool(lora.get("enabled", False)):
        _require_integer(
            lora, "frequency_hz", errors, "lora.frequency_hz", minimum=1000000
        )
        _require_integer(
            lora,
            "spreading_factor",
            errors,
            "lora.spreading_factor",
            minimum=7,
            maximum=12,
        )
        bandwidth = _require_integer(
            lora, "bandwidth_hz", errors, "lora.bandwidth_hz", minimum=1
        )
        if bandwidth is not None and bandwidth not in SUPPORTED_LORA_BANDWIDTHS:
            errors.append(
                f"lora.bandwidth_hz must be one of {sorted(SUPPORTED_LORA_BANDWIDTHS)}"
            )
        _require_integer(
            lora, "coding_rate", errors, "lora.coding_rate", minimum=5, maximum=8
        )
        _require_integer(
            lora, "tx_power_dbm", errors, "lora.tx_power_dbm", minimum=-9, maximum=22
        )
        _require_integer(
            lora,
            "heartbeat_period_s",
            errors,
            "lora.heartbeat_period_s",
            minimum=1,
        )

    if bool(health.get("enabled", True)):
        _require_integer(health, "period_s", errors, "health.period_s", minimum=1)
        _require_number(
            health,
            "cpu_temp_warn_c",
            errors,
            "health.cpu_temp_warn_c",
            minimum=1,
            maximum=120,
        )
        _require_number(
            health,
            "disk_free_warn_pct",
            errors,
            "health.disk_free_warn_pct",
            minimum=1,
            maximum=99,
        )

    if errors:
        raise SettingsValidationError("Invalid settings: " + "; ".join(errors))


def run_startup_preflight(settings: dict, service: str) -> list[str]:
    """Validate the runtime environment for ``service`` and create directories."""
    warnings: list[str] = []
    errors: list[str] = []

    paths = settings.get("paths", {}) or {}
    decoder = settings.get("decoder", {}) or {}
    tle = settings.get("tle", {}) or {}
    sdr = settings.get("sdr", {}) or {}
    storage = settings.get("storage", {}) or {}
    satellites = settings.get("satellites", {}) or {}

    for label, path in (
        ("paths.output_dir", paths.get("output_dir")),
        ("paths.log_dir", paths.get("log_dir")),
        ("decoder.output_dir", decoder.get("output_dir")),
        ("tle.cache_file", os.path.dirname(str(tle.get("cache_file", "")))),
    ):
        _ensure_directory(path, label, errors)
    for index, mirror in enumerate(storage.get("mirror_dirs", []) or []):
        _ensure_directory(mirror, f"storage.mirror_dirs[{index}]", errors)

    log_file = str(paths.get("log_file", "")).strip()
    if log_file:
        _ensure_directory(os.path.dirname(log_file) or ".", "paths.log_file", errors)

    if service != "daemon":
        if errors:
            raise SettingsValidationError("Invalid startup environment: " + "; ".join(errors))
        return warnings

    required_binaries = [str(sdr.get("rtl_fm_binary", "rtl_fm")), str(sdr.get("sox_binary", "sox"))]
    missing = [binary for binary in required_binaries if shutil.which(binary) is None]
    if missing:
        errors.append("missing required capture binaries: " + ", ".join(missing))

    available_noaa = _find_available_binaries(
        [str(decoder.get("wxtoimg_binary", "wxtoimg"))]
        + list(decoder.get("noaa_fallbacks", []) or [])
    )
    available_meteor = _find_available_binaries(
        [str(decoder.get("meteor_demod_binary", "meteor-demod"))]
        + list(decoder.get("meteor_fallbacks", []) or [])
    )
    modes = {satellite.get("mode") for satellite in satellites.values() if isinstance(satellite, dict)}
    if "NOAA_APT" in modes and not available_noaa:
        errors.append("no NOAA decoder binaries are available on $PATH")
    if "METEOR_LRPT" in modes and not available_meteor:
        errors.append("no Meteor decoder binaries are available on $PATH")

    if bool(sdr.get("bias_tee", False)):
        rtl_biast = str(sdr.get("rtl_biast_binary", "rtl_biast"))
        if shutil.which(rtl_biast) is None:
            warnings.append(
                f"Bias-tee is enabled but {rtl_biast!r} is not on $PATH; captures will continue without LNA power control."
            )

    if errors:
        raise SettingsValidationError("Invalid startup environment: " + "; ".join(errors))
    return warnings


def _require_dict(settings: dict, key: str, errors: list[str]) -> dict:
    value = settings.get(key)
    if not isinstance(value, dict):
        errors.append(f"{key} must be an object")
        return {}
    return value


def _require_string(settings: dict, key: str, errors: list[str], label: str) -> str | None:
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return None
    return value.strip()


def _require_string_list(settings: dict, key: str, errors: list[str], label: str) -> list[str]:
    value = settings.get(key, [])
    if not isinstance(value, list):
        errors.append(f"{label} must be a list of strings")
        return []
    result: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"{label}[{index}] must be a non-empty string")
            continue
        result.append(entry.strip())
    return result


def _require_number(
    settings: dict,
    key: str,
    errors: list[str],
    label: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    value = settings.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"{label} must be numeric")
        return None
    numeric = float(value)
    if minimum is not None and numeric < minimum:
        errors.append(f"{label} must be >= {minimum}")
    if maximum is not None and numeric > maximum:
        errors.append(f"{label} must be <= {maximum}")
    return numeric


def _require_integer(
    settings: dict,
    key: str,
    errors: list[str],
    label: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    value = settings.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        errors.append(f"{label} must be an integer")
        return None
    if minimum is not None and value < minimum:
        errors.append(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        errors.append(f"{label} must be <= {maximum}")
    return value


def _require_timezone(settings: dict, key: str, errors: list[str], label: str) -> None:
    value = _require_string(settings, key, errors, label)
    if value is None:
        return
    try:
        ZoneInfo(value)
    except Exception:
        errors.append(f"{label} must be a valid IANA timezone")


def _ensure_directory(path: str | None, label: str, errors: list[str]) -> None:
    if path is None:
        errors.append(f"{label} must be configured")
        return
    text = str(path).strip()
    if not text:
        errors.append(f"{label} must be configured")
        return
    try:
        os.makedirs(text, exist_ok=True)
    except OSError as exc:
        errors.append(f"{label} is not writable: {exc}")


def _find_available_binaries(candidates: list[str]) -> list[str]:
    return [candidate for candidate in candidates if candidate and shutil.which(candidate)]
