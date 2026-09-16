#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR"

APP_SOURCE="$SCRIPT_DIR/dist/Linuxdoday.app"
if [[ ! -d "$APP_SOURCE" ]]; then
  ./build_mac_app.sh
fi

if [[ "${1:-}" == "--system" ]]; then
  DEST_DIR="/Applications"
  sudo mkdir -p "$DEST_DIR"
  sudo rm -rf "$DEST_DIR/Linuxdoday.app"
  sudo ditto "$APP_SOURCE" "$DEST_DIR/Linuxdoday.app"
else
  DEST_DIR="$HOME/Applications"
  mkdir -p "$DEST_DIR"
  rm -rf "$DEST_DIR/Linuxdoday.app"
  ditto "$APP_SOURCE" "$DEST_DIR/Linuxdoday.app"
fi

xattr -dr com.apple.quarantine "$DEST_DIR/Linuxdoday.app" 2>/dev/null || true
open "$DEST_DIR/Linuxdoday.app"
print "已安装并打开：$DEST_DIR/Linuxdoday.app"
