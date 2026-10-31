# VideoSeek · macOS

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%2013%2B%20arm64%20%2F%20x86__64-black)](docs/macos-notes.md)
[![Upstream](https://img.shields.io/badge/upstream-6v17%2FVideoSeek-lightgrey)](https://github.com/6v17/VideoSeek)

[中文说明](./README.md) | **English**

---

> ### ⚠️ Unofficial macOS fork
>
> The original project **[6v17/VideoSeek](https://github.com/6v17/VideoSeek)** was created by
> [@6v17](https://github.com/6v17). The design, algorithms, and all core implementation belong to
> the upstream author. This fork only makes the app installable and runnable on macOS.
>
> **Windows users: go to the upstream repository → <https://github.com/6v17/VideoSeek>**
> Upstream ships the Windows installer, DirectML GPU acceleration, and official support.
> None of that is offered or maintained here.

---

Search a local video library by **text or screenshot**, with **hard-subtitle OCR**; preview hits and
export MP4, Jianying, or NLE timelines. Indexing stays on your machine (ONNX · Lance · FFmpeg) and
**media is not uploaded**.

<p align="center">
  <img src="docs/assets/图搜视频.png" alt="VideoSeek search UI" width="800">
</p>

## Why this fork is worth using

Three things make this fork genuinely useful for people who cut video:

### 1. Search videos *and* photos from one box

The painful part of editing is finding the right clip. Upstream only searches video; this fork adds:

- **Chinese photo search** — describe the picture in plain words and it finds it. Queries like
  "woman in a red dress by the window", "light clothes", "stocky person", "flashy outfit" ask about
  what's *in* the frame, not filenames, and still hit ([image library](docs/image-library.md)).
- **Unified search** — one box searches both video moments and photos, grouped results
  ([unified search](docs/unified-search.md)).
- **Video search unchanged** — semantic video search, hard-subtitle OCR, screenshot search, and
  MP4 / Jianying export all remain.

### 2. Index once, search instantly, update incrementally

- Material is **indexed once**; vectors live on disk (Lance), so later searches are millisecond-fast.
- **Incremental indexing** — new material only indexes the newly added parts, no full rebuild.

### 3. A double-click macOS app

- Ships as a **VideoSeek.app** with an icon — drop it in `/Applications` or the Dock and
  **double-click to launch**, no script every time.
- Use [`packaging/build-app.command`](packaging/build-app.command) to rebuild, or bundle a
  standalone package for people without the environment.

> Everything runs **locally** (ONNX · Lance · FFmpeg); media is **not uploaded**.

## What this fork changes

| Area | Upstream | This fork |
|------|----------|-----------|
| Target platform | Windows 10 / 11 | macOS 13+ (Apple Silicon primary, Intel untested) |
| Install | Installer / from source | Double-click `install-macos.command` |
| Launch | `VideoSeek.exe` | **double-click `VideoSeek.app`** (also `/Applications` / Dock; see [`packaging/`](packaging/)) |
| Packaging | official installer | **lightweight `.app` launcher + icon**; `packaging/build-app.command` rebuilds, or bundle standalone |
| Indexing | one-shot build | **incremental indexing** — only new material is indexed |
| FFmpeg detection | ELF / PE magic only | **adds Mach-O** (without it macOS reports "no FFmpeg") |
| Playback | bundled `vlc_lib/` DLLs | reuses `libvlc` inside `/Applications/VLC.app` |
| Docs | Windows-first | rewritten for macOS |

No functional changes: indexing, search, export, Agent API and cloud embedding behave exactly as
upstream. Exactly one code path was touched (FFmpeg binary detection) — see
[`docs/macos-notes.md`](docs/macos-notes.md#the-only-code-change).

## Three steps

### 0. Prerequisites

```bash
xcode-select --install        # command line tools (git)
brew install python@3.13 ffmpeg
brew install --cask vlc       # optional: without it preview playback is unavailable
```

> **Python must be 3.11 – 3.13.** 3.14 / 3.9 have no prebuilt wheels for `onnxruntime` or
> `faiss-cpu` and fail with `No matching distribution found for onnxruntime`. The installer picks a
> compliant interpreter automatically.

### 1. Install dependencies

Double-click **`install-macos.command`** in Finder. If Gatekeeper blocks it: right-click → Open, or:

```bash
cd ~/Desktop/VideoSeek && ./install-macos.command
```

The script picks a Python 3.11–3.13 interpreter (arm64 first), creates `~/.videoseek-venv`,
auto-detects a local proxy (10808 / 7890 / 1087 / 6152 …), installs ~600 MB of dependencies from a
China-friendly mirror, then self-checks the critical packages. Expect 5–15 minutes on first run.

### 2. Import runtime assets

The repo ships no models. Download `openai-clip.zip` (393 MB) and
`rapidocr-zh-understanding.zip` (15 MB) from
[Releases — models](https://github.com/6v17/VideoSeek/releases/tag/models), then:

```bash
~/.videoseek-venv/bin/python scripts/import_runtime_resources.py openai-clip.zip rapidocr-zh-understanding.zip
~/.videoseek-venv/bin/python scripts/import_runtime_resources.py --status   # three trues = ready
```

The in-app **Import & parse** dialog writes through the same code path.

### 3. Launch

Double-click **`run-macos.command`**, then:

**Local Library** → add a folder → select videos → **Sync selected** → **Search** → preview → export

## Known deltas

| Symptom | Cause | Handling |
|---------|-------|----------|
| `rookiepy` fails to build | no py3.13 arm64 wheel; unused in code | installer skips it |
| `rapidocr-onnxruntime==1.4.4` fails | no arm64 wheel | installer falls back to `1.2.3`, OCR works |
| "FFmpeg not found" | upstream only accepts ELF/PE magic | patched here for Mach-O |
| Black preview | `python-vlc` can't locate libvlc | install VLC.app; `run-macos.command` exports `DYLD_LIBRARY_PATH` |
| Slow indexing | CPU inference only, no DirectML/CUDA | expected; see [`docs/macos-notes.md`](docs/macos-notes.md#performance) |
| Where is my data | — | `~/.videoseek/` (`config.json`, `models/`, `logs/`, `data/`) |

## New: unified search over videos and photos

The second sidebar item, **Unified Search**, runs one query against both indexes and
returns two groups: video moments on top, photos below.

- Videos go through the same engine the Agent API uses (`execute_agent_search`);
  photos reuse the photo library's three recall channels (attributes → text →
  vectors). The widget layer implements no retrieval logic of its own.
- The two sides **fail independently**: a missing video index still returns photos,
  and vice versa.
- Double-click a video hit to jump to the search page and play that moment;
  double-click a photo to open it with the system viewer.
- No cross-modal re-ranking: video scores (0.3–0.45) and photo attribute scores
  (0.95+) are not on the same scale, so one merged list would bury every video.
- Measured on 1262 photos plus the existing video library: `黑色衣服` in 0.85 s,
  photos-only in 16 ms, 15 s once per process for the CLIP warm-up.

Details in [`docs/unified-search.md`](docs/unified-search.md).

## Documentation

- [`docs/quickstart.md`](docs/quickstart.md) — macOS from-source setup, manual commands, checks
- [`docs/image-library.md`](docs/image-library.md) — photo library: data model, three recall channels, captions, limits
- [`docs/unified-search.md`](docs/unified-search.md) — unified search: how the two sides merge, why no merged ranking
- [`docs/macos-notes.md`](docs/macos-notes.md) — code change, paths, deltas, troubleshooting, upstream sync
- [`docs/architecture.md`](docs/architecture.md) — architecture (upstream text)
- [`docs/for-agents.md`](docs/for-agents.md) — localhost Agent API (upstream text)

## Syncing with upstream

```bash
git remote add upstream https://github.com/6v17/VideoSeek.git
git fetch upstream && git merge upstream/master
```

The Mach-O check in `src/infra/ffmpeg_paths.py` may conflict on merge. **After resolving, verify the
line is still there**, otherwise macOS silently loses FFmpeg again.

## Credits & license

- Original project and all core implementation: **[@6v17](https://github.com/6v17)** —
  <https://github.com/6v17/VideoSeek>
- Upstream site / help: [lv17.top](https://www.lv17.top/) · QQ group `1033551438`
- Upstream user guide (ZH): [Feishu doc](https://ycnwd8tcjgtu.feishu.cn/docx/ZWkrdSqA6oJTOrxQ2XscxYtmnad)
- This fork only adapts the platform. macOS-specific problems are welcome in this repo's issues;
  **Windows bugs and feature requests belong upstream**.

License: [AGPL-3.0](LICENSE) © 2026 [6v17](https://github.com/6v17). This fork is released under the
same license; changes are listed in [NOTICE](NOTICE) as required by AGPL section 5.

<details>
<summary>Support the original author</summary>

<p align="center">
  <img src="docs/assets/支持我.png" alt="Support upstream" width="360">
</p>

</details>

## Support this macOS fork

If this macOS port helped you, feel free to buy me a coffee or get in touch:

<p align="center">
  <img src="docs/assets/donate.jpg" alt="Donate QR" width="240">
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="docs/assets/contact.jpg" alt="Contact QR" width="240">
</p>

<p align="center">
  Left: donation (WeChat) &nbsp;·&nbsp; Right: contact (WeChat)
</p>
