#!/usr/bin/env bash
# Regenerate requirements.lock from requirements.txt.
# Run after editing requirements.txt; commit both files together.
# The lock targets the Docker image (linux, Python 3.12) — the release
# binaries and other platforms keep installing from requirements.txt.
set -euo pipefail
cd "$(dirname "$0")"
uv pip compile requirements.txt -o requirements.lock \
  --python-version 3.12 --python-platform linux \
  --generate-hashes --annotation-style line
