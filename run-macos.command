#!/bin/bash
# VideoSeek macOS 启动器（双击运行）
# macOS fork of https://github.com/6v17/VideoSeek — 原项目作者 6v17
set -u
cd "$(dirname "$0")" || exit 1

VENV="$HOME/.videoseek-venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "尚未安装依赖。请先双击「install-macos.command」。"
  read -r -n 1 -p "按任意键退出..."
  exit 1
fi

if ! "$VENV/bin/python" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
  echo "虚拟环境版本为 $("$VENV/bin/python" -V 2>&1)，需要 Python 3.11-3.13。"
  echo "请重新双击「install-macos.command」，它会自动重建环境。"
  read -r -n 1 -p "按任意键退出..."
  exit 1
fi

# macOS: python-vlc 需要指向 VLC.app 内的 libvlc（无 VLC 时预览播放不可用，其余功能正常）
VLC_APP="/Applications/VLC.app"
if [ -d "$VLC_APP" ]; then
  export DYLD_LIBRARY_PATH="$VLC_APP/Contents/MacOS/lib:${DYLD_LIBRARY_PATH:-}"
  export VLC_PLUGIN_PATH="$VLC_APP/Contents/MacOS/plugins"
fi

export TMPDIR="${TMPDIR:-/tmp}"
exec "$VENV/bin/python" main.py "$@"
