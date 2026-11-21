#!/bin/sh
# build.sh — build and set up the maltware Sober launcher.
#
# Usage:
#   build.sh [--build]      validate + byte-compile the launcher only
#   build.sh --install      build and set up the launcher (default)
#   build.sh --uninstall    remove the launcher, menu entries, icon, fallback copy
#   build.sh --check        show repo/setup status
#   build.sh --no-menu      do not install Applications menu entries
#   build.sh --rebuild      same as --install
#   build.sh --help         show this help
#
# What it does (install / default):
#   * validates the checkout (python3 + git, launcher, actions.json)
#   * byte-compiles the launcher (nothing to build into a binary)
#   * installs ~/.local/bin/maltware-launcher (wrapper)
#   * installs desktop entries + icon into ~/.local/share/applications
#   * writes ~/.config/maltware/install.json (repo pointer)
#   * copies a fallback launcher into ~/.local/share/maltware/
#
set -eu

REPO=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
MODE="install"
MENU=1

while [ $# -gt 0 ]; do
    case "$1" in
        --build)    MODE="build" ;;
        --install|--rebuild) MODE="install" ;;
        --uninstall) MODE="uninstall" ;;
        --check)    MODE="check" ;;
        --no-menu)  MENU=0 ;;
        --help|-h)  sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "build.sh: unknown option '$1' (see --help)" >&2; exit 2 ;;
    esac
    shift
done

log() { printf 'maltware build: %s\n' "$*"; }
err() { printf 'maltware build: ERROR: %s\n' "$*" >&2; }

LAUNCHER="$REPO/launcher/maltware-launcher.py"
ACTIONS="$REPO/launcher/actions.json"

missing=0
for cmd in python3 git; do
    command -v "$cmd" >/dev/null 2>&1 || { err "'$cmd' not found in PATH"; missing=1; }
done
[ "$missing" -eq 0 ] || { echo "Install python3 and git first." >&2; exit 1; }

# --- build ---
if [ "$MODE" = "install" ] || [ "$MODE" = "build" ]; then
    log "validating checkout…"
    python3 - "$LAUNCHER" "$ACTIONS" <<'PY'
import json, py_compile, sys
launcher, actions = sys.argv[1], sys.argv[2]
with open(actions, "r", encoding="utf-8") as fh:
    data = json.load(fh)
actions_list = data.get("actions") if isinstance(data, dict) else data
if not isinstance(actions_list, list) or not actions_list:
    sys.exit("actions.json has no usable action list")
py_compile.compile(launcher, cfile="/tmp/maltware-launcher-check.pyc", doraise=True)
print(f"  launcher compiles OK; actions.json valid ({len(actions_list)} action(s))")
PY
    rm -f /tmp/maltware-launcher-check.pyc
    log "build ok (pure Python — nothing to compile into a binary)"
fi

# --- check ---
if [ "$MODE" = "check" ]; then
    log "status:"
    python3 "$LAUNCHER" status
    exit 0
fi

# --- uninstall ---
if [ "$MODE" = "uninstall" ]; then
    log "removing Applications entries…"
    python3 "$LAUNCHER" applications --remove
    log "removing wrapper…"
    rm -f "$HOME/.local/bin/maltware-launcher"
    log "removing fallback copy…"
    rm -rf "$HOME/.local/share/maltware"
    log "removing icon…"
    rm -rf "$HOME/.local/share/icons/hicolor/scalable/apps/maltware.svg"
    log "done. The launcher was removed; ~/.config/maltware/install.json was left in place."
    log "Remove it manually with: rm -rf ~/.config/maltware"
    exit 0
fi

# --- install ---
if [ "$MODE" = "install" ]; then
    if [ "$MENU" -eq 1 ]; then
        log "installing wrapper + Applications menu entries + icon…"
        python3 "$LAUNCHER" applications
    else
        log "installing wrapper + icon (no Applications menu entries)…"
        python3 "$LAUNCHER" applications --no-entries
    fi
    log "copying fallback launcher to ~/.local/share/maltware/…"
    mkdir -p "$HOME/.local/share/maltware"
    cp "$LAUNCHER" "$HOME/.local/share/maltware/maltware-launcher.py"
    cp "$ACTIONS" "$HOME/.local/share/maltware/actions.json"
    log "done."
    log "Run it with: maltware-launcher   (or open it from your Applications menu)"
    log "Enable auto-checks by opening the launcher once; it also checks on every launch."
fi

exit 0
