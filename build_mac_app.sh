#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "Linuxdoday.app 只能在 macOS 上构建。"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  ./mac_setup.sh
fi

.venv/bin/python -m pip install --upgrade pyinstaller

# 从现有图标生成 macOS icns。失败时仍可使用系统默认应用图标构建。
ICON_ARGS=()
ICONSET_DIR="build/Linuxdoday.iconset"
ICNS_PATH="build/Linuxdoday.icns"
mkdir -p build
if [[ -f icon.ico ]] && sips -s format png icon.ico --out build/icon-source.png >/dev/null 2>&1; then
  rm -rf "$ICONSET_DIR"
  mkdir -p "$ICONSET_DIR"
  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" build/icon-source.png --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" build/icon-source.png --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
  done
  if iconutil -c icns "$ICONSET_DIR" -o "$ICNS_PATH"; then
    ICON_ARGS=(--icon "$ICNS_PATH")
  fi
fi

.venv/bin/pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name Linuxdoday \
  --osx-bundle-identifier com.guyungy.linuxdoday \
  --collect-all DrissionPage \
  "${ICON_ARGS[@]}" \
  linux_do_gui.py

APP_PATH="$SCRIPT_DIR/dist/Linuxdoday.app"
PLIST="$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion 12.0" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :NSHighResolutionCapable true" "$PLIST"

# Ad-hoc 签名可保证应用包内容一致；公开分发时可再换成 Developer ID 签名。
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"

print ""
print "构建完成：$APP_PATH"
print "安装并打开：./install_mac_app.sh"
