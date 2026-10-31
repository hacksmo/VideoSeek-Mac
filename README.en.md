# VideoSeek · macOS

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%2013%2B%20arm64%20%2F%20x86__64-black)](docs/macos-notes.md)
[![Upstream](https://img.shields.io/badge/upstream-6v17%2FVideoSeek-lightgrey)](https://github.com/6v17/VideoSeek)

[中文说明](./README.md) | **English**

---

**Local semantic search over video *and* photos**: find the shot or image you need with one sentence or one screenshot. Everything runs on your machine; media is never uploaded.

<p align="center">
  <img src="docs/assets/图片库-语义搜索.jpg" alt="Photo library semantic search: type '胖' and matching photos appear" width="800">
</p>

> Above: search "胖" (stocky) in the photo library and matching photos come up — no filenames, no manual tagging.

---

## At a glance: what this fork fixes

### The original's shortcomings

The original [6v17/VideoSeek](https://github.com/6v17/VideoSeek) is excellent, but it has real gaps:

| Gap | Why it hurts |
|-----|--------------|
| ❌ **Video only** | Photos, screenshots and reference images can't be searched — yet half a video editor's material is images |
| ❌ **Windows only** | macOS users can't install or run it |
| ❌ **Chinese search barely works** | The default model is English-only; searching "红酒瓶" returns meme images |
| ❌ **Full re-index** | Adding one image forces a full library rebuild |
| ❌ **Fiddly setup** | Manual commands and environment setup every time |

### What I changed

This macOS fork closes those gaps one by one:

- ✅ **Photo library** — semantic index for photos, screenshots and reference images, with auto-generated Chinese captions and structured people tags
- ✅ **Chinese photo search** — describe what's *in* the frame (`woman in a red dress`, `stocky person`, `light clothes`), not the filename
- ✅ **Unified search** — one box searches videos and photos together, grouped results
- ✅ **Incremental indexing** — new material only indexes the new parts, no full rebuild
- ✅ **Double-click app** — packaged as a macOS app with an icon; launch by double-clicking, no terminal
- ✅ **Editor-friendly UI** — drag images straight into Jianying, right-click to delete (to Trash), one-click start the caption service

### Why it's good

> The core pitch in one line: **make "finding material" so fast you forget it exists.**

- 🔍 **Accurate**: semantic search + attribute filters (color / build / style) + a three-level Chinese router. On 1262 real photos: black 12/12, light clothes 10/12
- ⚡ **Fast**: index once, millisecond searches after; incremental updates keep it snappy
- 🔒 **Fully local**: ONNX + Lance + FFmpeg, all on-device — not a single image is uploaded
- 🖱️ **Frictionless**: drag-to-import, right-click management, one-click service start — no learning curve for editors

---

## Quick start

### 1. Install dependencies (double-click)

Double-click **`install-macos.command`** in Finder. The script picks a Python 3.11–3.13 interpreter, creates `~/.videoseek-venv`, and installs ~600 MB of dependencies from a China-friendly mirror. Expect 5–15 minutes on first run.

> Prerequisites: Xcode command-line tools, Homebrew Python 3.13 and FFmpeg (install VLC for preview playback).

### 2. Import models

Download `openai-clip.zip` and `rapidocr-zh-understanding.zip` from
[Releases — models](https://github.com/6v17/VideoSeek/releases/tag/models), then:

```bash
~/.videoseek-venv/bin/python scripts/import_runtime_resources.py openai-clip.zip rapidocr-zh-understanding.zip
```

(You can also use the in-app "Import & parse" dialog.)

### 3. Launch

Double-click **`run-macos.command`**, then: **Library / Photo Library** → add a folder → start indexing → search.

Full details in [`docs/quickstart.md`](docs/quickstart.md).

---

## Feature overview

| Feature | What it does |
|---------|--------------|
| **Video semantic search** | Describe a shot to find it, screenshot search, hard-subtitle OCR |
| **Photo library** | Semantic image index + auto Chinese captions + people tags ([details](docs/image-library.md)) |
| **Unified search** | One box searches videos + photos together |
| **Chinese photo search** | Attribute → text → vector, three-level routing that actually works in Chinese |
| **Export** | MP4 / Jianying draft / DaVinci / Premiere, drag-and-drop import |

---

## Differences from upstream

This fork only does **macOS platform adaptation and photo-capability enhancement**; the video indexing, search, and export core stays identical to upstream. Main changes:

- New standalone **photo library** (separate index table — video index untouched)
- New **unified search** (video + photo)
- FFmpeg Mach-O detection (otherwise macOS reports "no FFmpeg")
- Reuses the system VLC's libvlc for playback
- Packaged as `.app` + icon, double-click to launch
- Full English/Chinese macOS docs

See [`docs/macos-notes.md`](docs/macos-notes.md) for the detailed delta list.

---

## Documentation

- [`docs/quickstart.md`](docs/quickstart.md) — from-source install
- [`docs/image-library.md`](docs/image-library.md) — photo library: data model, retrieval, people captions
- [`docs/unified-search.md`](docs/unified-search.md) — unified search
- [`docs/changelog.md`](docs/changelog.md) — changes & bug log

---

## Credits & license

- Original project and all core implementation: **[@6v17](https://github.com/6v17)** — <https://github.com/6v17/VideoSeek>
- This fork only adapts the platform and adds photo capability; Windows issues belong upstream.

License: [AGPL-3.0](LICENSE) © 2026 [6v17](https://github.com/6v17).

<details>
<summary>Support the original author</summary>

<p align="center">
  <img src="docs/assets/支持我.png" alt="Support upstream" width="360">
</p>

</details>

## Support this macOS fork

If this macOS port helped you, feel free to buy me a coffee:

<p align="center">
  <img src="docs/assets/donate-card.png" alt="Buy me a coffee - WeChat / Alipay" width="520">
</p>

<p align="center">
  Left: WeChat　Right: Alipay
</p>
