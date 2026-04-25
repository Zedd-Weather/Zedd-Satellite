# Security Policy

## Supported deployment model

Zedd-Satellite is intended to run as a managed appliance on a trusted host. The Flask dashboard now defaults to loopback-only binding and must sit behind an authenticated TLS reverse proxy before it is exposed beyond the device itself.

## Reporting a vulnerability

Please report vulnerabilities privately to the Zedd-Weather maintainers before public disclosure. Include the affected version, deployment model, reproduction steps, and any relevant logs with secrets removed.

## Hardening expectations

- Keep Raspberry Pi OS and Python dependencies current.
- Do not expose the dashboard directly to the public internet.
- Rotate operator credentials used by the reverse proxy.
- Treat station coordinates, logs, and captured artifacts as sensitive operational data.
