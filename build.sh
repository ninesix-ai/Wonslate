#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
# Unix (Linux/macOS) one-click launcher: forwards all arguments to build.py
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/script/build.py" "$@"
