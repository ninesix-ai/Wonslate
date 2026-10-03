#!/usr/bin/env bash
# run_av_baseline.sh -- Linux / macOS entry point for the AV-domain baseline
# runner. All real logic lives in script/run_av_baseline.py; this shim just
# makes `./run_av_baseline.sh` work.
#
# Common variants:
#   ./run_av_baseline.sh --preflight-only
#   ./run_av_baseline.sh --quick
#   ./run_av_baseline.sh --report-only
set -euo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
exec "$PY" "$SCRIPT_DIR/script/run_av_baseline.py" "$@"
