# VideoSeek · macOS

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%2013%2B%20arm64%20%2F%20x86__64-black)](docs/macos-notes.md)
[![Upstream](https://img.shields.io/badge/upstream-6v17%2FVideoSeek-lightgrey)](https://github.com/6v17/VideoSeek)

**中文** | [English](./README.md)

---

> ### ⚠️ 这是非官方 macOS 适配分支
>
> 原项目 **[6v17/VideoSeek](https://github.com/6v17/VideoSeek)** 由 [@6v17](https://github.com/6v17) 独立开发。
> 设计、算法、全部核心代码与产品理念均归属于原作者，本分支只是让它在 macOS 上装得上、跑得动。
>
> **Windows 用户请前往上游仓库 → <https://github.com/6v17/VideoSeek>**
> 上游提供 Windows 安装包、DirectML GPU 加速与官方技术支持，本仓库不提供也不维护这些。

---

本地视频语义检索：用**文字或截图**定位镜头，支持**硬字幕 OCR**，预览后导出 MP4 / 剪映 / NLE。
默认全程在本机索引（ONNX · Lance · FFmpeg），**不上传**素材。

<p align="center">
  <img src="docs/assets/图搜视频.png" alt="VideoSeek 搜索界面" width="800">
</p>

## 这个分支做了什么

| 项目 | 上游 | 本分支 |
|------|------|--------|
| 目标平台 | Windows 10 / 11 | macOS 13+（Apple Silicon 优先，Intel 未实测） |
| 安装方式 | 安装包 / 源码 | 双击 `install-macos.command` |
| 启动方式 | `VideoSeek.exe` | 双击 `run-macos.command` |
| FFmpeg 识别 | 只认 ELF / PE 文件头 | **增加 Mach-O 判断**（不加会在 macOS 上误判为"没有 FFmpeg"） |
| 播放组件 | `vlc_lib/` 内置 DLL | 复用 `/Applications/VLC.app` 的 libvlc |
| 文档 | Windows 优先 | **全中文 macOS 重写** |

功能层面**没有改动**：索引、搜索、导出、Agent API、云嵌入全部与上游一致。
只改了一处代码逻辑（FFmpeg 二进制识别），详见 [`docs/macos-notes.md`](docs/macos-notes.md#the-only-code-change)。

## 三步跑起来

### 0. 前提

```bash
xcode-select --install        # 命令行工具（git 需要）
brew install python@3.13 ffmpeg
brew install --cask vlc       # 可选：不装则无法预览播放，检索/建库不受影响
```

> **Python 必须是 3.11 – 3.13。** 3.14 / 3.9 没有 `onnxruntime`、`faiss-cpu` 的预编译包，
> 会报 `No matching distribution found for onnxruntime`。安装脚本会自动挑合规解释器。

### 1. 装依赖

在 Finder 里双击 **`install-macos.command`**。
如果被 Gatekeeper 拦住：右键 → 打开；或直接在终端里跑：

```bash
cd ~/Desktop/VideoSeek && ./install-macos.command
```

脚本做四件事：挑 Python（3.11–3.13，arm64 优先）→ 建虚拟环境 `~/.videoseek-venv`
→ 自动探测本机代理（10808 / 7890 / 1087 / 6152 …）并走国内镜像装约 600 MB 依赖
→ 自检关键包。**首次 5–15 分钟**，取决于网速。

### 2. 导入运行资源

仓库不含模型。从 [Releases — models](https://github.com/6v17/VideoSeek/releases/tag/models) 下载
`openai-clip.zip`（393 MB）和 `rapidocr-zh-understanding.zip`（15 MB），然后：

```bash
~/.videoseek-venv/bin/python scripts/import_runtime_resources.py openai-clip.zip rapidocr-zh-understanding.zip
~/.videoseek-venv/bin/python scripts/import_runtime_resources.py --status   # 三个 true 即就绪
```

也可以启动后在应用内用 **导入并解析** 完成同样的事（同一条写入路径）。

> 资源包和配置文件来自**上游**，国内直连 GitHub 下载慢时建议 `--proxy`，或用多线程 Range 下载。

### 3. 启动

双击 **`run-macos.command`**。然后在应用里：

**视频库** → 添加文件夹 → 勾选视频 → **同步选中视频** → **搜索** → 预览 → 导出

## 与上游的差异清单

| 现象 | 原因 | 处理 |
|------|------|------|
| `rookiepy` 装不上 | 无 py3.13 arm64 wheel，且代码未引用 | 脚本自动跳过，无影响 |
| `rapidocr-onnxruntime==1.4.4` 装不上 | 无 arm64 wheel | 脚本改装可用的 `1.2.3`，OCR 正常 |
| 提示"未检测到 FFmpeg" | 上游只认 ELF/PE 文件头 | 本分支已加 Mach-O 判断 |
| 预览黑屏 | `python-vlc` 找不到 libvlc | 装 VLC.app；`run-macos.command` 会自动导出 `DYLD_LIBRARY_PATH` |
| 搜索很慢 | CPU 推理，无 DirectML / CUDA | 属预期；见 [`docs/macos-notes.md`](docs/macos-notes.md#performance) |
| 数据存哪 | — | `~/.videoseek/`（`config.json`、`models/`、`logs/`、`data/`） |

## 文档

- [`docs/quickstart.md`](docs/quickstart.md) — macOS 从源码安装、手动命令、逐项校验
- [`docs/macos-notes.md`](docs/macos-notes.md) — 代码改动、路径约定、已知差异、排错表、上游同步
- [`docs/architecture.md`](docs/architecture.md) — 架构（上游原文）
- [`docs/for-agents.md`](docs/for-agents.md) — localhost Agent API（上游原文）

## 与上游同步

```bash
git remote add upstream https://github.com/6v17/VideoSeek.git
git fetch upstream && git merge upstream/master
```

`src/infra/ffmpeg_paths.py` 的 Mach-O 判断可能在合并时冲突，**冲突后必须重新确认这行仍在**，
否则 macOS 会再次"看不见" FFmpeg。

## 致谢与许可

- 原始项目、全部核心实现：**[@6v17](https://github.com/6v17)** — <https://github.com/6v17/VideoSeek>
- 上游官网与帮助：[lv17.top](https://www.lv17.top/)　QQ 群 `1033551438`
- 上游使用教程：[飞书文档](https://ycnwd8tcjgtu.feishu.cn/docx/ZWkrdSqA6oJTOrxQ2XscxYtmnad)
- 本分支只做平台适配，问题请先确认不是本分支引入的；针对 macOS 的问题，欢迎提到本仓库 Issues。
  **Windows 相关的 bug / 功能请求请去上游**。

许可：[AGPL-3.0](LICENSE) © 2026 [6v17](https://github.com/6v17)。本分支同样以 AGPL-3.0 发布，
修改内容按 AGPL 第 5 条在 [NOTICE](NOTICE) 中列明。

<details>
<summary>支持原作者</summary>

<p align="center">
  <img src="docs/assets/支持我.png" alt="支持原作者" width="360">
</p>

</details>
