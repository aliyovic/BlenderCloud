#!/bin/sh
set -eu

task_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
exec "${PYTHON_BIN:-python3}" "$task_root/scripts/build_plugin.py" scan_viewer "$@"
