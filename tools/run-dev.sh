#!/usr/bin/env bash
# Run Chippy from the source tree. Extra arguments go to the app (e.g. --fake).
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CHIPPY_EXTENSION_DIR="$repo_root/extension/chippy@eboye.github.io"
exec "$repo_root/.venv/bin/chippy" "$@"
