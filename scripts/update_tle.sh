#!/usr/bin/env bash
# update_tle.sh -- Download fresh TLE (Two-Line Element) data for the
# weather satellites tracked by Zedd-Satellite.
#
# The destination file and source URLs are read from config/settings.json
# so that this script stays in sync with the Python code. Run via cron
# every 6-12 hours, e.g.:
#
#     0 */6 * * * /opt/Zedd-Satellite/scripts/update_tle.sh
#
# Exit codes:
#   0  success
#   1  configuration error
#   2  every download attempt failed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SETTINGS_FILE="${REPO_ROOT}/config/settings.json"

if [[ ! -f "${SETTINGS_FILE}" ]]; then
    echo "ERROR: settings file not found at ${SETTINGS_FILE}" >&2
    exit 1
fi

# Use python to parse JSON (always available; jq may not be installed).
read_setting() {
    local query="$1"
    python3 - "${SETTINGS_FILE}" "${query}" <<'PY'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
keys = sys.argv[2].split(".")
ref = data
for k in keys:
    ref = ref[k]
if isinstance(ref, list):
    print("\n".join(ref))
else:
    print(ref)
PY
}

CACHE_FILE="$(read_setting tle.cache_file)"
mapfile -t SOURCES < <(read_setting tle.sources)

if [[ ${#SOURCES[@]} -eq 0 ]]; then
    echo "ERROR: no TLE sources configured in tle.sources" >&2
    exit 1
fi

# Make the cache path absolute relative to the repo root.
case "${CACHE_FILE}" in
    /*) ABS_CACHE="${CACHE_FILE}" ;;
    *)  ABS_CACHE="${REPO_ROOT}/${CACHE_FILE}" ;;
esac
mkdir -p "$(dirname "${ABS_CACHE}")"

TMP_FILE="$(mktemp)"
trap 'rm -f "${TMP_FILE}"' EXIT

success=0
for url in "${SOURCES[@]}"; do
    echo "[update_tle] Fetching ${url}"
    if curl --silent --show-error --fail --max-time 30 "${url}" >> "${TMP_FILE}"; then
        printf '\n' >> "${TMP_FILE}"
        success=$((success + 1))
    else
        echo "[update_tle] WARNING: failed to fetch ${url}" >&2
    fi
done

if [[ ${success} -eq 0 ]]; then
    echo "[update_tle] ERROR: all TLE downloads failed" >&2
    exit 2
fi

mv "${TMP_FILE}" "${ABS_CACHE}"
trap - EXIT
echo "[update_tle] Wrote $(wc -l < "${ABS_CACHE}") lines to ${ABS_CACHE}"
