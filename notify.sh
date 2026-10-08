#!/usr/bin/env bash
# Enqueue only. The independent delivery timer receives channel credentials.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
KEY_ARGS=()
if [[ "${1:-}" == "--event-key" ]]; then
  KEY_ARGS=(--event-key "$2")
  shift 2
fi
exec python3 -m forge.launch forge.notifications enqueue --state "${FORGE_NOTIFY_STATE:-$DIR/state/notifications}" "${KEY_ARGS[@]}" -- "$*"
