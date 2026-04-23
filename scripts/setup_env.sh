#!/usr/bin/env bash
# setup_env.sh -- Provision a Raspberry Pi (or any Debian based system)
# with all of the apt packages and Python dependencies required to run
# Zedd-Satellite.
#
# Usage:
#     bash scripts/setup_env.sh
#
# The script is idempotent: re-running it will simply ensure that every
# package is installed and up to date.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

APT_PACKAGES=(
    rtl-sdr
    sox
    curl
    python3
    python3-pip
    python3-venv
)

# wxtoimg is no longer in Debian's repos; users must install the Linux
# .deb manually. We only check for it here and warn if it is missing.
WARN_BINARIES=(wxtoimg meteor-demod)

echo "[setup_env] Installing apt packages..."
if [[ "$(id -u)" -ne 0 ]]; then
    SUDO="sudo"
else
    SUDO=""
fi

${SUDO} apt-get update
${SUDO} apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"

echo "[setup_env] Installing Python requirements..."
python3 -m pip install --upgrade pip
python3 -m pip install --requirement "${REPO_ROOT}/requirements.txt"

echo "[setup_env] Verifying optional decoder binaries..."
for bin in "${WARN_BINARIES[@]}"; do
    if ! command -v "${bin}" >/dev/null 2>&1; then
        echo "[setup_env] WARNING: '${bin}' not found on \$PATH."
        echo "             Install it manually (see README.md) to enable decoding."
    fi
done

mkdir -p "${REPO_ROOT}/logs" "${REPO_ROOT}/output"
chmod +x "${REPO_ROOT}/scripts/"*.sh

echo "[setup_env] Done."
