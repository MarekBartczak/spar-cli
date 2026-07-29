#!/usr/bin/env bash
# Install the Spar desktop launcher for the CURRENT user (no root, no
# system-wide files). Idempotent: re-run it after moving the repo or
# recreating the venv.
#
#   ./packaging/linux/install.sh              # autodetect ./.venv/bin/spar-gui
#   ./packaging/linux/install.sh /path/to/spar-gui
#
# What it does:
#   ~/.local/bin/spar-gui                      symlink, so `spar-gui` works in
#                                              any terminal without the venv path
#   ~/.local/share/applications/spar.desktop   application menu entry
#   ~/.local/share/icons/hicolor/scalable/apps/spar.svg
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

exec_path="${1:-$repo/.venv/bin/spar-gui}"
if [[ ! -x "$exec_path" ]]; then
    echo "install.sh: no spar-gui at $exec_path" >&2
    echo "  install the gui extra first:  $repo/.venv/bin/pip install -e '$repo[gui]'" >&2
    echo "  or pass the path:             $0 /path/to/spar-gui" >&2
    exit 1
fi

bin_dir="$HOME/.local/bin"
apps_dir="$HOME/.local/share/applications"
icon_dir="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$bin_dir" "$apps_dir" "$icon_dir"

ln -sfn "$exec_path" "$bin_dir/spar-gui"
# The icon ships INSIDE the package (spar/gui/assets/spar.svg) so the running
# app and the desktop entry cannot drift apart: the app loads that same file
# for its window icon.
install -m 0644 "$repo/spar/gui/assets/spar.svg" "$icon_dir/spar.svg"

desktop_file="$apps_dir/spar.desktop"
sed -e "s|@EXEC@|$exec_path|g" -e "s|@ICON@|spar|g" \
    "$here/spar.desktop.in" > "$desktop_file"
chmod 0644 "$desktop_file"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$desktop_file"
fi
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$apps_dir" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "installed:"
echo "  $bin_dir/spar-gui -> $exec_path"
echo "  $desktop_file"
echo "  $icon_dir/spar.svg"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "note: $bin_dir is not on PATH in this shell — open a new login shell" ;;
esac
