#!/bin/bash
# VideoSeek macOS 依赖安装（双击运行）
# macOS fork of https://github.com/6v17/VideoSeek — 原项目作者 6v17
# 要求 Python 3.11 - 3.13；3.14 / 3.9 缺少 onnxruntime、faiss 等预编译包
set -u
cd "$(dirname "$0")" || exit 1

VENV="$HOME/.videoseek-venv"
MIRROR="https://mirrors.aliyun.com/pypi/simple/"

say() { printf "\n\033[1;36m== %s\033[0m\n" "$1"; }
warn() { printf "\033[1;33m%s\033[0m\n" "$1"; }
die() { warn "$1"; read -r -n 1 -p "按任意键退出..."; exit 1; }

# ---------- 1. 自动探测代理 ----------
PROXY=""
for p in 10808 7890 7897 1087 6152 8080 8888; do
  if nc -z -G 1 127.0.0.1 "$p" 2>/dev/null; then PROXY="http://127.0.0.1:$p"; break; fi
done
PROXY_ARG=""
[ -n "$PROXY" ] && PROXY_ARG="--proxy $PROXY"

# ---------- 2. 挑选可用的 Python（3.11-3.13 优先）----------
py_ok() {
  "$1" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' >/dev/null 2>&1
}

pick_python() {
  local from_path p
  local cands=(
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11
    /usr/local/bin/python3.13
    /usr/local/bin/python3.12
    /usr/local/bin/python3.11
    "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
    /usr/bin/python3
  )
  from_path="$(command -v python3 || true)"
  [ -n "$from_path" ] && cands=("$from_path" "${cands[@]}")
  for p in "${cands[@]}"; do
    [ -x "$p" ] || continue
    py_ok "$p" && { echo "$p"; return 0; }
  done
  return 1
}

PY="$(pick_python || true)"
if [ -z "$PY" ]; then
  warn "未找到 Python 3.11 - 3.13。当前 python3 = $(python3 -V 2>&1 || echo 无)"
  die "请先安装：brew install python@3.12  （装完重新双击本脚本）"
fi

# ---------- 3. 虚拟环境（版本不符则重建）----------
say "环境"
echo "项目目录 : $(pwd)"
echo "Python   : $PY ($("$PY" -V 2>&1), $("$PY" -c 'import platform;print(platform.machine())'))"
echo "虚拟环境 : $VENV"
echo "镜像     : $MIRROR"
if [ -n "$PROXY" ]; then echo "代理     : $PROXY"; else echo "代理     : 未检测到，走直连"; fi

if [ -x "$VENV/bin/python" ] && ! py_ok "$VENV/bin/python"; then
  warn "已有虚拟环境 Python 版本不兼容，正在重建..."
  rm -rf "$VENV"
fi

if [ ! -x "$VENV/bin/python" ]; then
  say "创建虚拟环境"
  "$PY" -m venv "$VENV" || die "创建虚拟环境失败"
fi

PIP=("$VENV/bin/python" -m pip)

say "升级 pip"
# shellcheck disable=SC2086
"${PIP[@]}" install -q -U pip -i "$MIRROR" $PROXY_ARG || warn "pip 升级失败，继续"

# ---------- 4. 依赖清单（剔除本机无 wheel 的可选包）----------
REQ="/tmp/videoseek-req-$$.txt"
grep -v -e '^rookiepy' -e '^rapidocr-onnxruntime' requirements.txt | grep -v '^$' > "$REQ"

say "安装主依赖（约 600 MB，首次较慢）"
# shellcheck disable=SC2086
if ! "${PIP[@]}" install -r "$REQ" -i "$MIRROR" $PROXY_ARG --timeout 30; then
  warn "镜像源安装失败，回退官方源重试..."
  # shellcheck disable=SC2086
  "${PIP[@]}" install -r "$REQ" --timeout 30 $PROXY_ARG || { rm -f "$REQ"; die "依赖安装失败，请把报错发给维护者"; }
fi
rm -f "$REQ"

say "安装可选组件（失败不影响主流程）"
# shellcheck disable=SC2086
"${PIP[@]}" install rapidocr-onnxruntime -i "$MIRROR" $PROXY_ARG >/dev/null 2>&1 \
  || "${PIP[@]}" install rapidocr -i "$MIRROR" $PROXY_ARG >/dev/null 2>&1 \
  || warn "硬字幕 OCR 组件未安装（仅影响 OCR 功能）"

# shellcheck disable=SC2086
"${PIP[@]}" install rookiepy -i "$MIRROR" $PROXY_ARG >/dev/null 2>&1 \
  || warn "rookiepy 未安装（仅影响从浏览器读取 Cookie）"

# ---------- 5. 校验 ----------
say "校验关键依赖"
"$VENV/bin/python" -c '
mods = ["PySide6", "onnxruntime", "cv2", "numpy", "lancedb", "faiss", "fastapi"]
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        bad.append(m + ": " + type(e).__name__)
print("全部就绪" if not bad else "缺少/异常: " + ", ".join(bad))
'

say "完成"
echo "双击「run-macos.command」即可运行。"
echo "首次启动需在应用内导入 CLIP ONNX 模型，才能建库搜索，见 docs/quickstart.md 第 3 节。"
echo "Windows 用户请改用上游仓库：https://github.com/6v17/VideoSeek"
read -r -n 1 -p "按任意键关闭窗口..."
exit 0
