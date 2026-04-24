# Operations Runbook

## Installation baseline

1. Install system packages and Python dependencies with `bash scripts/setup_env.sh`.
2. Copy the systemd units from `deploy/systemd/` into `/etc/systemd/system/`.
3. Place the nginx example from `deploy/nginx/` behind TLS and authentication if the dashboard will leave loopback.
4. Install the logrotate policy from `deploy/logrotate/`.

## Startup checks

The daemon now validates `config/settings.json`, creates the configured runtime directories, and fails fast when required capture or decoder binaries are missing.

## Daily checks

- verify `systemctl status zedd-satellite`
- verify `systemctl status zedd-satellite-dashboard`
- verify `/api/healthz` through the reverse proxy or loopback
- confirm disk free space and recent captures in `output/`

## Incident handling

- Missing decoder/capture binaries: rerun `bash scripts/setup_env.sh` and reinstall the required external decoders.
- No SDRs detected: inspect USB power, dongle enumeration, and the LNA/bias-tee chain.
- Under-voltage or throttling: replace the PSU or improve cooling before resuming unattended operation.

## Retention

Define and enforce site-specific retention for logs, WAVs, and PNGs. The sample deployment rotates logs daily for 14 copies; captured media retention should be based on available storage and business requirements.
