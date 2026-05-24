#!/usr/bin/env bash
# vboxfront — PyInstaller build
#
# Provisions a project-local .venv (so we never touch system site-packages),
# installs runtime + build deps, then runs PyInstaller via vboxfront.spec.
# Output: dist/vboxfront
#
# Usage:
#   build/build.sh            # build standalone binary
#   build/build.sh --run      # provision .venv and run from source
#
# PyInstaller does NOT cross-compile; the binary is for the host arch.

set -euo pipefail

cd "$(dirname "$0")/.."

ROOT="$PWD"
VENV="${VENV:-$ROOT/.venv}"
PY="${PY:-python3}"
APP="vboxfront"
SPEC="$ROOT/$APP.spec"

mode="build"
if [ "${1:-}" = "--run" ]; then
    mode="run"
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo ">> creating venv in $VENV"
    "$PY" -m venv "$VENV"
fi

# shellcheck disable=SC1091
. "$VENV/bin/activate"

python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r "$ROOT/requirements.txt"

if [ "$mode" = "run" ]; then
    exec python "$ROOT/$APP.py" "$@"
fi

python -m pip install --quiet pyinstaller

echo ">> building $APP (VERSION=${VERSION:-unset})"
cd "$ROOT"
rm -rf build/pyi dist/"$APP"
python -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$ROOT/dist" \
    --workpath "$ROOT/build/pyi" \
    "$SPEC"

OUT="$ROOT/dist/$APP"
if [ ! -f "$OUT" ]; then
    echo "error: expected $OUT not produced" >&2
    exit 1
fi

echo
echo "Built: $OUT"
file "$OUT" || true
