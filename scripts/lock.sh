#!/bin/sh
# Regenerate the hash-pinned locks with pip-tools (FOSS). Hashes cover every distribution PyPI
# publishes for each pinned version, so the lock installs on aarch64 (Pi) and x86_64 alike.
# pip-tools lives only in a throw-away venv; the installer never needs it.
set -eu
cd "$(dirname "$0")/.."
# PyPI only: ignore system pip config (Raspberry Pi OS adds piwheels as an extra index).
export PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL=https://pypi.org/simple
unset PIP_EXTRA_INDEX_URL
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 -m venv "$TMP/venv"
"$TMP/venv/bin/pip" install -q --upgrade pip "pip-tools==7.6.1"
rm -f requirements.lock requirements-dev.lock requirements-tts.lock
"$TMP/venv/bin/pip-compile" -q --generate-hashes --allow-unsafe --strip-extras --index-url https://pypi.org/simple --no-emit-index-url \
    --output-file requirements.lock requirements.in
"$TMP/venv/bin/pip-compile" -q --generate-hashes --allow-unsafe --strip-extras --index-url https://pypi.org/simple --no-emit-index-url \
    --output-file requirements-dev.lock requirements-dev.in
"$TMP/venv/bin/pip-compile" -q --generate-hashes --allow-unsafe --strip-extras --index-url https://pypi.org/simple --no-emit-index-url \
    --output-file requirements-tts.lock requirements-tts.in
rm -f requirements-dev.txt
echo "locks written: $(grep -c '==' requirements.lock) runtime / $(grep -c '==' requirements-dev.lock) dev pins"
