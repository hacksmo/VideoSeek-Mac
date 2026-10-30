# VideoSeek macOS 快速上手

本文件只讲 **macOS**。Windows 请回上游：[`6v17/VideoSeek`](https://github.com/6v17/VideoSeek)。

- 只想用 → 看 [README](../README.zh.md)
- 想看为什么这么改、踩过哪些坑 → 看 [`macos-notes.md`](macos-notes.md)

---

## 1) 前置条件

| 依赖 | 要求 | 检查 |
|------|------|------|
| macOS | 13 Ventura 及以上（Apple Silicon 实测通过） | `sw_vers` |
| 命令行工具 | 提供 `git`、`clang` | `xcode-select -p` |
| Python | **3.11 – 3.13**，推荐 3.13 arm64 | `python3 -V` |
| FFmpeg | 任意近期版本，Homebrew 装即可 | `ffmpeg -version` |
| VLC | 可选，仅影响预览播放 | `ls /Applications/VLC.app` |

```bash
xcode-select --install
brew install python@3.13 ffmpeg
brew install --cask vlc        # 可选
```

**为什么锁 3.11–3.13**：`onnxruntime==1.23.0`、`faiss-cpu==1.13.2` 没有 cp39 / cp314 的 macOS
wheel。解释器版本不对时 pip 会直接报 `from versions: none`，看起来像"包不存在"，其实是"这个 Python
版本没有包"。

```bash
# 自查：确认 python3 到底指向哪一个
which -a python3
/opt/homebrew/bin/python3.13 -c 'import platform; print(platform.machine())'   # 期望 arm64
```

## 2) 安装依赖

### 方式一：用脚本（推荐）

```bash
cd ~/Desktop/VideoSeek
./install-macos.command          # 或在 Finder 里双击
```

脚本行为：

1. 依次探测 `/opt/homebrew/bin/python3.13`、`3.12`、`3.11`、`/usr/local/bin/*`、`$PATH` 上的 `python3`，
   只接受 3.11–3.13；
2. 建虚拟环境 `~/.videoseek-venv`；已存在但版本不符则自动重建；
3. 探测本机代理端口（10808 → 7890 → 1087 → 6152 → 8080 → 8888），有则给 pip 带上 `--proxy`；
4. 用阿里云镜像装依赖，失败自动回退官方 PyPI；
5. 单独补装两个"上游锁死但 arm64 无 wheel"的可选包；
6. 自检 `PySide6 / onnxruntime / cv2 / numpy / lancedb / faiss / fastapi`。

### 方式二：手动

```bash
cd ~/Desktop/VideoSeek

# 1. 选解释器
PY=/opt/homebrew/bin/python3.13
$PY -m venv ~/.videoseek-venv
V=~/.videoseek-venv/bin/python

# 2. 装主依赖（剔除 mac 上装不上的两个）
grep -v -e '^rookiepy' -e '^rapidocr-onnxruntime' requirements.txt > /tmp/req-mac.txt
$V -m pip install -r /tmp/req-mac.txt -i https://mirrors.aliyun.com/pypi/simple/ --proxy http://127.0.0.1:10808

# 3. OCR：上游锁的 1.4.4 没有 arm64 wheel，改装可用版本
$V -m pip install rapidocr-onnxruntime -i https://mirrors.aliyun.com/pypi/simple/

# 4. 自检
$V -c 'import PySide6, onnxruntime, cv2, numpy, lancedb, faiss, fastapi; print("ok")'
```

`--proxy` 按需删掉；不用镜像就去掉 `-i`。`rookiepy` 只用于"从浏览器读 Cookie"，
无 py3.13 arm64 wheel，代码里也没有引用，跳过无副作用。

## 3) 导入运行资源

仓库不带模型、不带 ffmpeg 二进制。

| 资源 | 是否必须 | 来源 |
|------|----------|------|
| `openai-clip.zip`（393 MB，默认检索模型） | **必须**，否则无法建索引 | [Releases — models](https://github.com/6v17/VideoSeek/releases/tag/models) |
| `rapidocr-zh-understanding.zip`（15 MB） | 需要硬字幕 OCR 时装 | 同上 |
| FFmpeg | 用 Homebrew 的即可 | `brew install ffmpeg` |
| VLC | 要预览播放时装 | `brew install --cask vlc` |

```bash
V=~/.videoseek-venv/bin/python
$V scripts/import_runtime_resources.py openai-clip.zip rapidocr-zh-understanding.zip
$V scripts/import_runtime_resources.py --status
```

`--status` 期望：

```
model_ready: True | ffmpeg_ready: True | resources_ready: True
```

导入前请**先退出 VideoSeek**，否则配置可能被运行中的实例覆盖。
这条命令与应用内「导入并解析」走同一套写入逻辑，产物落在 `~/.videoseek/`。

## 4) 启动与首次使用

```bash
./run-macos.command        # 或 Finder 双击
```

启动脚本会做三件事：

1. 确认 `~/.videoseek-venv` 存在且 Python 版本合规（不合规就提示你重跑安装）；
2. 检测 `/Applications/VLC.app`，有则导出 `DYLD_LIBRARY_PATH` 与 `VLC_PLUGIN_PATH`；
3. `exec $VENV/bin/python main.py`。

首次使用路径：

**视频库** → 添加文件夹 → 勾选视频 → **同步选中视频** → **搜索** → 预览 → 导出

同步是 CPU 推理，短片段大约 1 帧/秒量级，几十个短视频的首轮索引要等一会儿，之后搜索是毫秒级。

## 5) 排错

| 报错 / 现象 | 原因 | 处理 |
|-------------|------|------|
| `No matching distribution found for onnxruntime==1.23.0` | `python3` 是 3.14 / 3.9 | 用 `install-macos.command`，或显式指定 3.13 |
| `ERROR: Could not build wheels for rookiepy` | 无 arm64 wheel | 跳过，或从 requirements 里删掉 |
| 启动即报 `ModuleNotFoundError: PySide6` | 装到了别的解释器 / 别的 venv | 确认用的是 `~/.videoseek-venv/bin/python` |
| 应用打不开、无窗口 | 见 `~/.videoseek/logs/app.log` 末尾 | 多为模型缺失或 ONNX 加载失败 |
| 提示"未找到 FFmpeg"但 `ffmpeg -version` 正常 | 上游只认 ELF/PE 文件头 | 确认 `src/infra/ffmpeg_paths.py` 的 Mach-O 分支还在 |
| 预览黑屏 / 无声 | 没装 VLC 或 libvlc 未加载 | 装 VLC.app 后用 `run-macos.command` 启动 |
| 同步完成但"新增向量为 0" | 预检拦截、重复文件、解码失败 | 看 UI 构建摘要；试单个文件复现 |
| 下载模型极慢 | GitHub 直连限速 | 挂代理；或用 `curl -r` 分段并发下载再合并 |
| 装依赖时终端里 `sandbox-exec` 报错 | 在受限环境里跑 brew | 在普通终端里手动执行 `brew install` |

清空重来：

```bash
rm -rf ~/.videoseek-venv     # 只删环境，不动数据和模型
rm -rf ~/.videoseek          # 连数据、模型、日志一起删（谨慎）
```

## 附录 A：手动摆模型

一般不需要，`import_runtime_resources.py` 会处理。要手工放时：

```
~/.videoseek/models/openai-clip/vit-base-patch32/
├── clip_visual.onnx        # 351 MB
├── clip_text.onnx          # 254 MB
├── bpe_simple_vocab_16e6.txt.gz
└── model_manifest.json     # 必须是这个名字，不是 manifest.json
```

`config.json` 中对应 `models.active_profile = clip_onnx_default`。
换模型 profile 后**必须重新同步索引**，旧向量维度不同不能混用。

## 附录 B：测试

```bash
~/.videoseek-venv/bin/python -m pytest tests/ -q
```

macOS 基线是 **1624 passed / 21 failed / 7 skipped**。失败项全部是 Windows 语义用例
（长路径、junction、盘符、管理员 Cookie），属于上游既有状态，不是回归。
判断有没有改坏，只看这个数字有没有变多；原因见 [`macos-notes.md`](macos-notes.md#test-baseline)。

## 附录 C：其它

- **应用数据**：`~/.videoseek/`（`config.json`、`models/`、`logs/`、`data/`）
- **日志**：`~/.videoseek/logs/app.log`
- **虚拟环境**：`~/.videoseek-venv`（故意放在仓库外，避免被 git 和 rsync 带上）
- **代理**：安装脚本只对 pip 生效；应用内下载走系统代理设置
- **硬件加速**：上游的 DirectML / D3D11VA 是 Windows 专属，macOS 只能 CPU 推理
- **版本号规则、打包脚本**：上游内容，本分支不适用，见上游 `docs/quickstart.md`
