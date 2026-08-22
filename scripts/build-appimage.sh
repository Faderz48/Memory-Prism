#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v podman >/dev/null 2>&1; then
  engine=podman
elif command -v docker >/dev/null 2>&1; then
  engine=docker
else
  echo "Install Podman or Docker to build the AppImage." >&2
  exit 1
fi

"$engine" run --rm \
  -v "$root:/src:Z" \
  -w /src \
  ubuntu:20.04 \
  bash scripts/build-in-container.sh
