# macOS 适配说明 / macOS notes

<a id="the-only-code-change"></a>

## 1. 唯一的代码改动（The only code change）

### `src/infra/ffmpeg_paths.py` → `is_ffmpeg_binary_usable()`

上游在非 Windows 分支只接受两种文件头：

```python
# 上游
magic_ok = magic[:4] == b"\x7fELF" or magic[:2] == b"MZ"
```

macOS 上的 ffmpeg（Homebrew 版、官方 static 版）是 Mach-O，前 4 字节既不是 `7f 45 4c 46`（ELF）
也不是 `4d 5a`（PE）。于是：

```
has_ffmpeg() == False  →  抽帧 / 预览 / 导出 全部被拦，提示"未找到 FFmpeg"
```

而 `ffmpeg -version` 在终端里是完全正常的——这也是最容易误判成"路径没配好"的地方。

本分支加上 Mach-O 判断：

```python
# 本分支
magic_ok = (
    magic[:4] == b"\x7fELF"                              # ELF   (Linux)
    or magic[:2] == b"MZ"                                # PE    (Windows / Wine)
    or magic[:4] in (
        b"\xcf\xfa\xed\xfe",                             # Mach-O 64-bit little endian
        b"\xce\xfa\xed\xfe",                             # Mach-O 32-bit little endian
        b"\xfe\xed\xfa\xcf",                             # Mach-O 64-bit big endian
        b"\xfe\xed\xfa\xce",                             # Mach-O 32-bit big endian
        b"\xca\xfe\xba\xbe",                             # universal / fat binary
    )
)
```

判断依然只做"文件头合法性 + 能报出版本号"两层校验，不比上游宽松，只是把 macOS 也算作合法宿主。

> **升级上游代码后必须确认这段还在。** 如果被覆盖回去，症状是"应用什么都干不了，
> 但终端里 ffmpeg 明明能用"。

### 除这一处外，没有改动任何功能代码

索引、检索、OCR、导出、Agent API、云嵌入、UI 全部是上游原样。

<a id="paths"></a>

## 2. 路径约定（Paths）

| 内容 | 位置 | 说明 |
|------|------|------|
| 虚拟环境 | `~/.videoseek-venv` | 故意放在仓库外：不污染 git，也不怕 `git clean` |
| 应用数据 | `~/.videoseek/` | 由 `src/infra/paths.py` 决定 |
| 配置 | `~/.videoseek/config.json` | 模型 profile、FFmpeg 路径、库列表 |
| 模型 | `~/.videoseek/models/` | CLIP、OCR、理解组件 |
| 日志 | `~/.videoseek/logs/app.log` | 启动失败先看这里 |
| 索引 | `~/.videoseek/data/` | Lance 数据集 |

与 Windows 的 `%LOCALAPPDATA%\VideoSeek` 是同一套结构，只是根目录不同。
上游文档写的 `%LOCALAPPDATA%\VideoSeek\...` 在 macOS 上全部换成 `~/.videoseek/...`。

<a id="deps"></a>

## 3. 依赖差异（Dependencies）

`requirements.txt` 本身是跨平台的——平台标记已经写好：

```
onnxruntime-directml==1.23.0; sys_platform == "win32"
onnxruntime==1.23.0;          sys_platform != "win32"
```

所以**不需要**像旧文档说的那样手动把 DirectML 换成标准版，那是历史遗留说明。
真正卡住 macOS 的是另外两个包：

| 包 | 上游版本 | 问题 | 本分支处理 |
|----|----------|------|------------|
| `rookiepy` | 未锁版本 | 无 py3.13 arm64 wheel，需要 Rust 源码编译 | 跳过；代码里未引用，仅影响"从浏览器读 Cookie" |
| `rapidocr-onnxruntime` | `==1.4.4` | 1.4.4 无 arm64 wheel | 改装 `1.2.3`（有 arm64 wheel），OCR 实测可用 |

`install-macos.command` 用 `grep -v` 从 `requirements.txt` 里剔掉这两行再装，**不修改原文件**，
这样合并上游时不会冲突。

<a id="playback"></a>

## 4. 播放（VLC）

上游 Windows 版把 libvlc 的 DLL 放在仓库根的 `vlc_lib/`。macOS 没有这种东西，`python-vlc` 需要：

```bash
export DYLD_LIBRARY_PATH="/Applications/VLC.app/Contents/MacOS/lib"
export VLC_PLUGIN_PATH="/Applications/VLC.app/Contents/MacOS/plugins"
```

`run-macos.command` 检测到 `/Applications/VLC.app` 就自动导出这两个变量。

| 有 VLC | 无 VLC |
|--------|--------|
| 预览、播放、逐帧正常 | 建库、索引、搜索、导出仍正常，只是预览黑屏 |

`brew install --cask vlc` 失败时（例如受限环境报 `sandbox_apply: Operation not permitted`），
可以手动下载 dmg：

```bash
curl -L -o ~/Downloads/vlc.dmg https://get.videolan.org/vlc/last/macosx/vlc-3.0.24-arm64.dmg
hdiutil attach -nobrowse ~/Downloads/vlc.dmg
cp -R "/Volumes/VLC media player/VLC.app" /Applications/
hdiutil detach "/Volumes/VLC media player"
```

<a id="performance"></a>

## 5. 性能（Performance）

macOS 上没有 DirectML、没有 CUDA（CoreML EP 上游未接），**全部 CPU 推理**：

| 阶段 | 体感 |
|------|------|
| 首次同步索引 | 慢，视片段长度约 1 帧/秒量级；几百个短视频可能需要几十分钟 |
| 搜索 | 快，毫秒到百毫秒级 |
| OCR | 只对抽出的关键帧跑，可接受 |
| 导出 | 取决于 FFmpeg 编码，可用 `h264_videotoolbox` 走硬件编码 |

想加速索引只有两条路：换更小的视觉模型 profile，或减少抽帧密度。
两条都要**重新同步索引**。Apple Silicon 的 CPU 推理已经比同价位 x86 快，不必额外调优。

<a id="troubleshooting"></a>

## 6. 排错表（Troubleshooting）

| 现象 | 判断 | 处理 |
|------|------|------|
| `No matching distribution found for onnxruntime` | Python 版本不在 3.11–3.13 | 用 `install-macos.command`，它会自己挑 |
| `Could not build wheels for rookiepy` | 无 arm64 wheel | 跳过该包 |
| 双击 `.command` 没反应 / 一闪而过 | Gatekeeper 或没执行权限 | `chmod +x *.command`；右键 → 打开 |
| 提示"未找到 FFmpeg" | Mach-O 补丁被覆盖 | 检查 `is_ffmpeg_binary_usable()` |
| 启动崩溃 | 看日志 | `tail -30 ~/.videoseek/logs/app.log` |
| 提示缺模型 | 资源没导入 | `import_runtime_resources.py --status` |
| 预览黑屏 | 无 VLC 或环境变量没导出 | 装 VLC，用 `run-macos.command` 启动 |
| 同步后搜不到 | 索引未建 / profile 换了没重同步 | 设置里确认当前模型，重新同步 |
| pip 卡在下载 | 单线程 + 直连 GitHub/CDN | 挂代理，或用国内镜像 |
| `sandbox-exec: sandbox_apply: Operation not permitted` | 在受限沙箱里跑 brew/pip | 换普通终端执行 |

<a id="upstream-sync"></a>

## 7. 与上游同步（Syncing with upstream）

```bash
git remote add upstream https://github.com/6v17/VideoSeek.git
git fetch upstream
git merge upstream/master
```

本分支改动的文件分布，决定合并难度：

| 文件 | 冲突概率 | 处理 |
|------|----------|------|
| `src/infra/ffmpeg_paths.py` | **中**（上游若动这块必冲突） | 保留 Mach-O 分支 |
| `README.md` / `README.zh.md` / `docs/quickstart.md` | 高 | 直接保留本分支版本（`git checkout --ours`） |
| `NOTICE` | 中 | 保留本分支版本，上游版权行不动 |
| 其它 | 低 | 正常合并 |

<a id="not-done"></a>

## 8. 不做的事（Out of scope）

- **不提供 macOS 安装包**：上游的 Nuitka / Inno 打包链是 Windows 专用且未公开，本分支不重做。
- **不接 CoreML / Metal EP**：需要重新导出模型并改 ONNX 会话配置，属功能改动，超出"适配"范围。
- **不支持 Windows**：Windows 用户请回上游，本仓库的脚本、文档、补丁都只针对 macOS。
- **不修上游功能 bug**：功能问题请先在上游复现，再提到上游 Issues。

## 9. 实测记录（Verified setup）

| 项 | 值 |
|----|-----|
| 系统 | macOS 27.0.1 |
| 芯片 | Apple Silicon (arm64) |
| Python | 3.13.18 (`/opt/homebrew/bin/python3.13`) |
| PySide6 | 6.10.2 |
| onnxruntime | 1.23.0 (CPUExecutionProvider) |
| FFmpeg | 7.0 (`/usr/local/bin/ffmpeg`，识别为 system 源) |
| VLC | 3.0.24 (`/Applications/VLC.app`) |
| CLIP 模型 | `openai-clip/vit-base-patch32`，输出 512 维 |
| OCR 模型 | `rapidocr-zh`，det 4.5 MB / rec 10 MB / cls 0.5 MB |
| 自检 | `model_ready` / `ffmpeg_ready` / `resources_ready` 均为 True |

Intel Mac 未实测。理论上只要 Python 版本合规、依赖有 x86_64 wheel 就能跑，欢迎反馈。
