#!/bin/bash
# 生成 VideoSeek.app 并部署到 /Applications
#
# 用法：双击或执行  ./packaging/build-app.command
# 作用：
#   1. 用 packaging/icon_1024.png 生成 .icns（若 PNG 更新）
#   2. 组装 dist/VideoSeek.app 骨架（Info.plist + 启动脚本 + 图标）
#   3. 复制到 /Applications/VideoSeek.app
#
# 说明：这是「轻量启动器」，复用 ~/.videoseek-venv 虚拟环境和项目源码，
#       不把 Python 打进包。适合本地自用；要分享给别人的完整独立包另说。

set -u
cd "$(dirname "$0")/.." || exit 1   # 回到项目根

PROJECT_HOME="$(pwd)"
APP_NAME="VideoSeek"
ICON_PNG="packaging/icon_1024.png"
ICONSET_DIR="/tmp/videoseek.iconset"
ICNS_OUT="/tmp/${APP_NAME}.icns"

echo "==> 项目根：$PROJECT_HOME"

# 1. 生成 .icns（PNG 存在时）
if [ -f "$ICON_PNG" ]; then
  echo "==> 生成 .icns 图标"
  rm -rf "$ICONSET_DIR" && mkdir -p "$ICONSET_DIR"
  sips -z 16 16     "$ICON_PNG" --out "$ICONSET_DIR/icon_16x16.png"       >/dev/null 2>&1
  sips -z 32 32     "$ICON_PNG" --out "$ICONSET_DIR/icon_16x16@2x.png"    >/dev/null 2>&1
  sips -z 32 32     "$ICON_PNG" --out "$ICONSET_DIR/icon_32x32.png"       >/dev/null 2>&1
  sips -z 64 64     "$ICON_PNG" --out "$ICONSET_DIR/icon_32x32@2x.png"    >/dev/null 2>&1
  sips -z 128 128   "$ICON_PNG" --out "$ICONSET_DIR/icon_128x128.png"     >/dev/null 2>&1
  sips -z 256 256   "$ICON_PNG" --out "$ICONSET_DIR/icon_128x128@2x.png"  >/dev/null 2>&1
  sips -z 256 256   "$ICON_PNG" --out "$ICONSET_DIR/icon_256x256.png"     >/dev/null 2>&1
  sips -z 512 512   "$ICON_PNG" --out "$ICONSET_DIR/icon_256x256@2x.png"  >/dev/null 2>&1
  sips -z 512 512   "$ICON_PNG" --out "$ICONSET_DIR/icon_512x512.png"     >/dev/null 2>&1
  sips -z 1024 1024 "$ICON_PNG" --out "$ICONSET_DIR/icon_512x512@2x.png"  >/dev/null 2>&1
  iconutil -c icns "$ICONSET_DIR" -o "$ICNS_OUT" 2>/dev/null
  cp "$ICNS_OUT" packaging/AppIcon.icns
  echo "    已生成 packaging/AppIcon.icns"
else
  echo "==> 未找到 $ICON_PNG，使用已有的 packaging/AppIcon.icns"
fi

# 2. 组装 .app
APP_DIR="dist/${APP_NAME}.app"
echo "==> 组装 $APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp packaging/AppIcon.icns "$APP_DIR/Contents/Resources/AppIcon.icns"

# 3. Info.plist
cat > "$APP_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>com.videoseek.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
EOF

# 4. 启动脚本
cat > "$APP_DIR/Contents/MacOS/${APP_NAME}" <<EOF
#!/bin/bash
# VideoSeek.app 启动器（轻量：复用 ~/.videoseek-venv + 项目源码）
set -u
PROJECT_HOME="${PROJECT_HOME}"
APP_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
if [ -d "\$PROJECT_HOME" ] && [ -f "\$PROJECT_HOME/main.py" ]; then
  PROJECT_DIR="\$PROJECT_HOME"
else
  PROJECT_DIR="\$(cd "\$APP_DIR/../../../.." && pwd)"
fi
cd "\$PROJECT_DIR" || exit 1
VENV="\$HOME/.videoseek-venv"
if [ ! -x "\$VENV/bin/python" ]; then
  osascript -e 'display dialog "尚未安装依赖。请先双击项目里的 install-macos.command。" buttons {"好"} default button 1 with title "VideoSeek"' >/dev/null 2>&1
  exit 1
fi
if ! "\$VENV/bin/python" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
  osascript -e 'display dialog "虚拟环境 Python 版本不匹配（需要 3.11-3.13）。请重新运行 install-macos.command。" buttons {"好"} default button 1 with title "VideoSeek"' >/dev/null 2>&1
  exit 1
fi
VLC_APP="/Applications/VLC.app"
if [ -d "\$VLC_APP" ]; then
  export DYLD_LIBRARY_PATH="\$VLC_APP/Contents/MacOS/lib:\${DYLD_LIBRARY_PATH:-}"
  export VLC_PLUGIN_PATH="\$VLC_APP/Contents/MacOS/plugins"
fi
export TMPDIR="\${TMPDIR:-/tmp}"
exec "\$VENV/bin/python" main.py "\$@"
EOF
chmod +x "$APP_DIR/Contents/MacOS/${APP_NAME}"

# 5. 部署到 /Applications
echo "==> 部署到 /Applications/${APP_NAME}.app"
rm -rf "/Applications/${APP_NAME}.app"
cp -R "$APP_DIR" "/Applications/${APP_NAME}.app"

echo
echo "完成！已生成并部署 /Applications/${APP_NAME}.app"
echo "现在可以从「启动台」或「应用程序」文件夹双击启动，也可拖进 Dock。"
echo "首次启动若被系统拦截，请右键图标 → 打开，或在 系统设置→隐私与安全性 里允许。"
