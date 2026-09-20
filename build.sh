#!/usr/bin/env bash
# Convenience wrapper around build.py for macOS and Linux.
# All arguments are forwarded, e.g. ./build.sh --clean --smoke-test
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python="$here/.venv/bin/python"
[ -x "$python" ] || python="$(command -v python3 || command -v python)"

exec "$python" "$here/build.py" "$@"
