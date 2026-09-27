#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 ninesix-ai studio
# Unix (Linux/macOS) one-click launcher: forwards all arguments to the local
# verify gate. The XAML lint stage runs everywhere; the WPF launch smoke reports
# SKIP because the client only builds on Windows.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/script/verify/build_check.py" "$@"
