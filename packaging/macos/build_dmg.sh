#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
SPEC="$ROOT/packaging/macos/Spar.spec"
DIST_DIR="$ROOT/dist"
WORK_DIR="$ROOT/build/pyinstaller-macos-x86_64"
export PYINSTALLER_CONFIG_DIR="$ROOT/build/pyinstaller-config"
APP="$DIST_DIR/Spar.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "build_dmg.sh: ten artefakt musi być budowany na macOS" >&2
    exit 2
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "build_dmg.sh: brak interpretera: $PYTHON" >&2
    echo "Uruchom najpierw: python3 -m venv .venv && .venv/bin/pip install -e '.[gui,package-macos]'" >&2
    exit 2
fi

PYTHON_ARCH="$($PYTHON -c 'import platform; print(platform.machine())')"
if [[ "$PYTHON_ARCH" != "x86_64" ]]; then
    echo "build_dmg.sh: wymagany Python x86_64, wykryto: $PYTHON_ARCH" >&2
    exit 2
fi

if ! "$PYTHON" -c 'import PyInstaller' 2>/dev/null; then
    echo "build_dmg.sh: brak PyInstaller; zainstaluj .[package-macos]" >&2
    exit 2
fi

VERSION="$($PYTHON -c 'from importlib.metadata import version; print(version("spar-cli"))')"
DMG="$DIST_DIR/Spar-${VERSION}-macos-x86_64.dmg"

mkdir -p "$DIST_DIR" "$WORK_DIR"
"$PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$DIST_DIR" \
    --workpath "$WORK_DIR" \
    "$SPEC"

if [[ ! -d "$APP" ]]; then
    echo "build_dmg.sh: PyInstaller nie utworzył $APP" >&2
    exit 1
fi

# Ad-hoc signing is enough for a local build. Public distribution still
# requires a Developer ID signature and Apple notarization.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/spar-dmg.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/Spar.app"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
hdiutil create \
    -volname "Spar" \
    -srcfolder "$STAGE" \
    -format UDZO \
    -ov \
    "$DMG"

echo "$DMG"
