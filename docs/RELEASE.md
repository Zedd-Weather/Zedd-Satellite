# Release Process

## Versioning

Use semantic versioning for commercial releases.

## Release checklist

1. Run `python3 -m unittest discover -s tests -v`.
2. Run `python3 -m compileall .`.
3. Review deployment assets and README instructions for drift.
4. Validate on target Raspberry Pi hardware with real SDR, decoder, and storage devices.
5. Publish release notes describing hardware prerequisites, security posture, and rollback steps.

## Rollback

Keep the previous tagged release and its matching `config/settings.json` backed up. If a deployment fails, stop both systemd services, reinstall the previous release, restore the previous config, and restart the services.
