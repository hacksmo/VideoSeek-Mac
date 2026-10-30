#!/bin/bash
# 启动本地视觉模型服务（Ollama），供「图片库」在生成图片描述时调用。
#
# 为什么单独一个脚本：VideoSeek 只负责读图，模型服务是独立进程。
# 双击本脚本，它会在需要时把服务拉起来，并告诉你当前装了哪些模型。
#
# 停掉服务：双击 stop-ollama.command，或执行
#   pkill -f "ollama serve"

set -u

BIN="/opt/homebrew/bin/ollama"
PORT=11434
BASE="http://127.0.0.1:${PORT}"
PLIST="$HOME/Library/LaunchAgents/com.ollama.server.plist"

pause() { [ -t 0 ] && read -r -p "按回车关闭这个窗口…" _ || true; }

if [ ! -x "$BIN" ]; then
  echo "✗ 没找到 $BIN"
  echo "  请先安装 Ollama：brew install ollama"
  pause
  exit 1
fi

if curl -s --max-time 2 "${BASE}/api/version" >/dev/null 2>&1; then
  echo "✓ 服务已在运行（$(curl -s "${BASE}/api/version")）"
else
  echo "启动服务中…"
  started=0
  # 首选登录项，重启后自动恢复；被系统拒绝时退回普通后台进程。
  if [ -f "$PLIST" ] && launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    started=1
    echo "  已注册为登录项（重启后自动启动）"
  fi
  if [ "$started" = "0" ]; then
    OLLAMA_HOST="127.0.0.1:${PORT}" OLLAMA_KEEP_ALIVE=60m \
      nohup "$BIN" serve > /tmp/ollama.log 2>&1 &
    echo "  已作为后台进程启动（pid $!）"
  fi
  for _ in $(seq 1 30); do
    if curl -s --max-time 2 "${BASE}/api/version" >/dev/null 2>&1; then
      echo "✓ 服务就绪（$(curl -s "${BASE}/api/version")）"
      break
    fi
    sleep 1
  done
  if ! curl -s --max-time 2 "${BASE}/api/version" >/dev/null 2>&1; then
    echo "✗ 服务没能启动，看 /tmp/ollama.log"
    pause
    exit 1
  fi
fi

echo
echo "已安装的模型："
"$BIN" list 2>/dev/null | sed '1d' | awk 'NF{printf "  %-18s %s\n", $1, $3" "$4}'
echo
echo "VideoSeek 里对应的设置：设置 → 理解 → 模型服务 → VLM"
echo "  base_url: ${BASE}/v1"
echo "  模型名:   $( "$BIN" list 2>/dev/null | sed '1d' | awk 'NF{print $1; exit}' )"
pause
