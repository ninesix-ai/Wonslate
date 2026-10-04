#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
# One-click launcher for the Wonslate MCP stdio server (Linux / macOS).
#
# Mirror of run-mcp.bat. Cargo names the auto-target after the source file
# (src/bin/wonslate-mcp.rs -> wonslate-mcp, no .exe suffix). Point the MCP
# client at this script rather than at the raw binary so no absolute build
# path has to be hand-written into client config.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="$here/../translator-engine/target/release/wonslate-mcp"

if [ ! -x "$target" ]; then
    echo "[run-mcp] MCP server not built yet: $target" >&2
    echo "[run-mcp] build it first:  python script/build.py" >&2
    echo "[run-mcp]   or:            cd translator-engine && cargo build --release" >&2
    exit 2
fi

exec "$target" "$@"
