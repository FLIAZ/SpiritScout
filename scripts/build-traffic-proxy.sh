#!/usr/bin/env bash
set -euo pipefail
task_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$task_root/.tools"
cd "$task_root/native/traffic-proxy"
GOTOOLCHAIN=local go build -trimpath -o "$task_root/.tools/spiritscout-traffic-proxy" .
