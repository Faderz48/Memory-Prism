#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x bin/ps2vmc-tool ]]; then
  mkdir -p bin
  make -C third_party/ps2vmc-tool src/main
  cp third_party/ps2vmc-tool/ps2vmc-tool bin/ps2vmc-tool
fi
exec python app.py "$@"
