# VideoSeek · macOS

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%2013%2B%20arm64%20%2F%20x86__64-black)](docs/macos-notes.md)
[![Upstream](https://img.shields.io/badge/upstream-6v17%2FVideoSeek-lightgrey)](https://github.com/6v17/VideoSeek)

**中文** | [English](./README.en.md)

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

## 为什么值得用

这个分支真正出彩的地方，是让「找素材」这件事对剪视频的人足够顺手：

### 1. 视频 + 图片，一个框就能搜

剪视频最花时间的不是剪，而是**找那段能用的素材**。上游只搜视频，这个分支补上了：

- **中文搜图片** —— 说人话就能找到想要的图。`穿红裙的女人站在窗边`、`浅色衣服`、`肚子胖的人`、`穿得花哨的人`，这些问的是画面内容而不是文件名，也能精准命中（[图片库](docs/image-library.md)）。
- **统一搜索** —— 一个输入框同时搜视频片段和图片，结果分组显示，视频走原来的语义引擎、图片走图片检索，各不干扰（[统一搜索](docs/unified-search.md)）。
- **视频搜图不变** —— 原有视频语义检索、硬字幕 OCR、截图搜视频、导出 MP4/剪映，全部保留。

### 2. 一次索引，之后秒开，还能增量更新

- 素材**只需索引一次**，向量落盘（Lance），之后每次搜索都是毫秒级，不用重复等建库。
- 支持**增量索引** —— 新加了素材只索引新增的部分，不必整库重跑，越用越顺手。

### 3. 双击就能跑的 macOS 应用

- 打包了 **VideoSeek.app** 和图标，放进 `/Applications` 或拖到 Dock，**双击图标直接启动**，不用每次去点脚本。
- 想分享给没装环境的人，可以用 [`packaging/build-app.command`](packaging/build-app.command) 一键打包成独立整包。

> 以上所有能力都**在本机完成**（ONNX · Lance · FFmpeg），素材**不上传**。

## 这个分支做了什么

| 项目 | 上游 | 本分支 |
|------|------|--------|
| 目标平台 | Windows 10 / 11 | macOS 13+（Apple Silicon 优先，Intel 未实测） |
| 安装方式 | 安装包 / 源码 | 双击 `install-macos.command` |
| 启动方式 | `VideoSeek.exe` | **双击 `VideoSeek.app`**（也可从 `/Applications` / Dock 启动，见 [`packaging/`](packaging/)） |
| 打包 | 官方安装包 | **轻量 `.app` 启动器 + 图标**，`packaging/build-app.command` 一键重建，也可集成整包 |
| 索引 | 一次性建库 | **支持增量索引**，新素材只索引新增部分 |
| FFmpeg 识别 | 只认 ELF / PE 文件头 | **增加 Mach-O 判断**（不加会在 macOS 上误判为"没有 FFmpeg"） |
| 播放组件 | `vlc_lib/` 内置 DLL | 复用 `/Applications/VLC.app` 的 libvlc |
| 图片理解与检索 | 不支持图片文件 | **新增图片库**（独立 `image_assets` 表 + 人物描述 + 应用内页签），见 [`docs/image-library.md`](docs/image-library.md) |
| 文档 | Windows 优先 | **全中文 macOS 重写** |

视频链路**没有改动**：索引、搜索、导出、Agent API、云嵌入全部与上游一致。
代码改动只有两处：FFmpeg 二进制识别（一处函数），以及新增图片库所需的最小接线
（导航页注册、缩略图缓存、一张独立索引表）。视频表与视频索引流程一行未改。

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

## 新增能力：图片库（含界面）

上游只索引视频。本分支加了一个**独立**的图片库：照片、截图、素材图都能建语义索引，并且不只看图上的文字——每张图会得到一句描述、一组**结构化人物信息**和标签，三种方式都能查。

侧边栏第二项就是 **图片库**：加文件夹 → 开始索引 → 在缩略图网格里看结果，右侧显示大图与人物标签。也支持把文件夹或单张图片直接拖进页面。

```bash
V=~/.videoseek-venv/bin/python
$V scripts/image_index.py add ~/Pictures/travel    # 登记目录
$V scripts/image_index.py index                    # CLIP 向量 + VLM 描述
$V scripts/image_index.py search "穿红裙的女人站在窗边"   # 语义
$V scripts/image_index.py search "浅色衣服"              # 属性（颜色/体型/风格）
$V scripts/image_index.py search "红裙" --mode keyword  # 关键词
$V scripts/image_index.py search --mode image --image q.jpg  # 以图搜图
```

检索按查询语言分流，中文走三级：**属性筛选 → 文件名/描述文字 → 向量兜底**；纯英文直接走语义。原因是默认 `openai-clip` 的文本塔只认英文，中文直连基本是噪声（`红酒瓶` 会返回表情包）。

第一级是新增的**属性检索**：`浅色衣服`、`黑色衣服`、`肚子胖的人`、`穿得花哨的人` 这类查询问的是画面而不是文件名，所以用一组英文提示词与已存的图片向量做零样本打分，**不写索引、不调 VLM、不下载模型**，首次查询 2.5 秒（之后 3 毫秒）。1262 张素材上目检准确率：黑色 12/12、浅色 10/12、体型 9/12、花哨 10/12；身高不可靠，已标注仅供参考。要精确到具体人物关系仍需要 VLM 描述，详见 [`docs/image-library.md`](docs/image-library.md#属性检索不装模型也能按画面筛)。

人物描述长这样，落在独立字段里（**不放进 tags**，否则会被上游的通用词过滤器当噪声丢掉）：

```json
{"count": 1, "gender": "女", "age": "青年", "clothing": "红色连衣裙",
 "action": "站在窗边看手机", "position": "画面左侧", "features": "长发，戴细框眼镜"}
```

- 复用现有 VLM 配置（LM Studio / Ollama / 任意 OpenAI 兼容端点），**不新增依赖、不下载新模型**
- 服务没启动时自动降级为"只算向量"，语义检索仍可用，不会把图记成失败
- 边界：**不做人脸识别、不推测身份姓名职业**，只描述画面可见属性
- 图片走独立 `image_assets` 表，视频索引一个字节不动
- **中文检索注意**：默认的 OpenAI CLIP 是英文训练，中文命中率低；想搜中文请把模型切成 Chinese CLIP 或 SigLIP2，页面也会提示

细节（数据模型、界面、缩略图缓存、性能、维护、局限）见 [`docs/image-library.md`](docs/image-library.md)。

## 新增能力：视频 + 图片的统一搜索

侧边栏第二项 **统一搜索**：一个输入框同时查视频片段和图片，结果分组显示（上面视频条、下面图片墙）。

- 视频走 Agent API 那套引擎（`execute_agent_search`），图片走图片库的三级路由（属性 → 文字 → 向量），界面层不重复实现任何检索逻辑
- 两侧**各自成败**：视频索引没建好，图片照常返回；反过来也一样
- 双击视频命中 → 切到「搜索」页在播放器里定位这一段；双击图片 → 系统看图打开
- 不做跨模态混排：视频片段分（0.3–0.45）和图片属性分（0.95+）不在同一把尺子上，混排只会让图片永远压住视频
- 实测（1262 张素材 + 现有视频库）：`黑色衣服` 0.85 s，只搜图片 16 ms，首次含模型加载 15 s

细节见 [`docs/unified-search.md`](docs/unified-search.md)。

## 文档

- [`docs/quickstart.md`](docs/quickstart.md) — macOS 从源码安装、手动命令、逐项校验
- [`docs/image-library.md`](docs/image-library.md) — 图片库：数据模型、三种检索、人物描述、性能、局限
- [`docs/unified-search.md`](docs/unified-search.md) — 统一搜索：两侧如何合并、为什么不分混排、实测耗时
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
