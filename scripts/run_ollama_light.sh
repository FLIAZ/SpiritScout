#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export LITELLM_LOCAL_MODEL_COST_MAP=True
if [[ ! -f .spiritscout-ollama/system.yaml || ! -f .spiritscout-ollama/models.yaml ]]; then
  .venv/bin/python scripts/setup_ollama_light.py
fi
exec .venv/bin/python -m spiritscout --config .spiritscout-ollama/system.yaml "$@"
