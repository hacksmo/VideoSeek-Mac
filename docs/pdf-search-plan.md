# 文档库（PDF）搜索支持 · 技术方案

> 目标：让 VideoSeek 能索引并检索 PDF（内含图片或文字）。
> 定位：**「文档库」作为独立的一类库，与「图片库」「视频库」三者平级、分开存放**，逻辑更清晰。
> 状态：**Phase 1（文字型 PDF 检索）已实现**，Phase 2/3 待实现。

---

## 1. 结论先行

- **「文档库」独立平级**，不复用图片库的表，也不塞进图片库——它有自己的目录、表、页签、CLI。
- 仅**能力复用**（CLIP 向量、VLM 描述、中文三级路由、缩略图、拖拽），**数据完全隔离**。
- 核心新增：**PyMuPDF 解析** + **独立文档库目录/表/页签/CLI**。
- 检索、预览、拖拽、导出、统一搜索**能力复用**，但结果归属「文档库」。

---

## 2. 三类库的平级结构

沿用图片库的「独立目录 + 独立 manifest + 独立 Lance 表 + 独立页签」模式，文档库照抄同一套，三者互不干扰：

| 维度 | 视频库 | 图片库（已实现） | **文档库（本次新增）** |
|------|--------|-----------------|----------------------|
| 数据目录 | `data/`（frames/chunks/dialogue） | `image_library/`（`library.json`） | `document_library/`（`library.json`） |
| Lance 表 | `frames` / `chunks` / `dialogue_segments` | `image_assets` | `pdf_documents` / `pdf_pages` |
| manifest | 视频库自带 | `image_library/library.json` | `document_library/library.json` |
| UI 页签 | `library` | `images`（insert_after=library） | `documents`（insert_after=images） |
| CLI | 无独立 | `scripts/image_index.py` | `scripts/pdf_index.py` |
| 服务 | 视频索引服务 | `image_index_service.py` | `pdf_service.py` |

> 关键：文档库**不写** `image_assets`，PDF 里的图片页渲染后作为**文档库自己的行**存进 `pdf_pages`（含自己的 CLIP 向量），不回写图片库表。

---

## 3. 现状盘点（可复用什么能力）

| 现有模块 | 位置 | 复用方式 |
|---------|------|---------|
| CLIP 向量检索 | `src/core/clip_embedding.py` | 页面图 → 向量 → 语义搜图 |
| 中文三级路由 | `src/services/image_index_service.py` | 文字/属性/向量兜底，直接用于 PDF 文字 |
| VLM 描述 | `src/services/image_understanding.py` | 页面图 → 结构化描述 + 人物标签 |
| 硬字幕 OCR | rapidocr-onnxruntime（已在依赖） | 扫描版 PDF 无文字层时兜底 |
| Lance 存储 + 增量索引 | `src/storage/image_lance_store.py` | 照抄建表/读写模式 |
| 缩略图缓存 + 拖拽 + 右键 | `ui/image_thumbs.py`、`ui/file_actions.py` | 命中页预览、拖拽导出 |
| 统一搜索 | `src/services/unified_search.py` | 新增 PDF 结果组 |

---

## 4. PDF 内容怎么拆（都在文档库内，不回写图片库）

PDF 里两类内容，**拆开存**，但都落在**文档库自己的表**里，不碰图片库：

| 内容 | 提取方式 | 检索方式 | 存储 |
|------|---------|---------|------|
| **文字** | `page.get_text()` 直接抽 | 关键词（字面扫描）+ 语义（文本向量） | `pdf_pages.text` + `text_vector` |
| **图片** | `page.get_pixmap(dpi=150)` 渲染位图 | CLIP 向量 + VLM 描述 | `pdf_pages.image_vector` + `caption` |

### 关键点：能力复用，数据隔离

- 页面位图渲染后，**调 CLIP 编码函数 + VLM 描述函数**（复用 `clip_embedding` / `image_understanding`），
  但结果作为 `pdf_pages` 的字段存进**文档库表**，绝不回写 `image_assets`。
- 图片库搜图时看不到 PDF 的页；文档库搜图时也看不到图片库的图——两边边界清晰。

---

## 5. 技术选型

| 项 | 选型 | 理由 |
|----|------|------|
| PDF 解析 | **PyMuPDF (`fitz`)** | 一个库同时做文字提取 + 页面渲染，快，免装 poppler |
| 文本切块 | 按页 + 按段落，500~1000 字/块 | 命中能定位到页，粒度可控 |
| 文字语义检索 | 复用 CLIP 文本塔 / 切 Chinese CLIP | 与现有中文检索一致 |
| 图片页渲染 | 150~200 dpi | CLIP/VLM 够用，不爆磁盘 |
| OCR 兜底 | 现有 rapidocr | 覆盖「纯图片 PDF」（扫描件） |
| 依赖 | 新增 `pymupdf`（约 20MB） | 唯一新增重依赖 |

---

## 6. 表结构设计

文档库新增两张 Lance 表（与 `image_assets` **同目录但独立命名**，互不干扰）：

### 6.1 `pdf_documents`（文档级）

| 字段 | 类型 | 说明 |
|------|------|------|
| `pdf_id` | string | 内容哈希，主键 |
| `pdf_path` | string | 绝对路径 |
| `rel_path` | string | 相对根目录路径 |
| `library_root` | string | 登记目录 |
| `page_count` | int32 | 页数 |
| `has_text` | bool | 是否有文字层 |
| `mtime` | float64 | 增量索引依据 |
| `bytes` | int64 | 文件大小 |

### 6.2 `pdf_pages`（页级，文字 + 图向量都在此）

| 字段 | 类型 | 说明 |
|------|------|------|
| `page_id` | string | `pdf_id + 页号` 哈希，主键 |
| `pdf_id` | string | 关联文档 |
| `page_no` | int32 | 页码（1-based） |
| `text` | string | 该页抽取的文字 |
| `text_vector` | vector | 文字语义向量（无文字层为空） |
| `image_vector` | vector | 页面渲染图的 CLIP 向量（语义搜图用） |
| `caption` | string | 该页 VLM 描述 |
| `thumb_path` | string | 页面缩略图缓存路径（可选） |

> 文字型检索查 `pdf_pages.text` / `text_vector`；图片型检索查 `pdf_pages.image_vector`。
> 两类都定位回 `{pdf_path, page_no}`。

---

## 7. 目录规划

```
src/
  storage/
    pdf_lance_store.py          # 两张表：schema + 读写 + 删除（照抄 image_lance_store）
  services/
    pdf_service.py              # 解析(PyMuPDF) + 索引 + 文字/图片检索
    pdf_search.py               # （可选）统一 PDF 文字/图片检索入口
  media/
    pdf_formats.py              # is_supported_pdf_path 白名单（与图片/视频分离）
scripts/
  pdf_index.py                  # CLI：add/scan/index/search/status（照抄 image_index.py）
ui/widgets/
  pdf_library_page.py           # 文档库页签（与图片库平级）
```

---

## 8. 落地顺序（三个阶段）

### Phase 1 — MVP：文字型 PDF 检索（改动最小）✅ 已实现

- 依赖加 `pymupdf`
- `pdf_lance_store` + `pdf_service`：`add root → scan → 抽文本按页存 → 字面扫描检索`
- 检索复用现有中文三级路由的字面扫描（`query_terms` / `_literal_scan` 同款分片）
- 命中返回 `{pdf_path, page_no, snippet}`，定位到页
- 产出：`scripts/pdf_index.py`（add/scan/index/search/status）+ 测试

**已落地的文件：**
- `src/media/formats.py` — `is_supported_pdf_path` / `PDF_EXTS`
- `src/storage/pdf_lance_store.py` — `pdf_documents` / `pdf_pages` 两张表
- `src/services/pdf_service.py` — manifest、扫描、抽文本、索引、字面搜索
- `scripts/pdf_index.py` — CLI
- `tests/test_pdf_library.py` + `tests/test_pdf_index_search.py`

### Phase 2 — 图文混合检索

- 页面渲染成位图 → 调 CLIP 编码 + VLM 描述 → 存 `pdf_pages.image_vector` / `caption`
- 图片型 PDF 页也能被语义搜图命中
- 命中页可预览缩略图（复用 `ImageThumbLoader`）

### Phase 3 — 增强

- 扫描版 PDF（无文字层）→ rapidocr OCR 兜底
- 文档库页签 UI + 命中页高亮
- 统一搜索接入「文档」结果组（视频 / 图片 / 文档三组平级）

---

## 9. 风险与注意

| 风险 | 应对 |
|------|------|
| PyMuPDF 是 AGPL/商业双许可 | 本项目 AGPL-3.0，兼容；但 PyMuPDF 的 AGPL 版本限制需确认与项目一致 |
| 大 PDF 渲染慢 | 增量索引（按 mtime），分页并行渲染 |
| 中文 PDF 文字层编码 | PyMuPDF 抽文本后统一 UTF-8，复用现有 CJK 处理 |
| 向量维度一致性 | 沿用 `open_image_table` 的 dimension 自检机制（文档库单独一份） |
| 磁盘占用 | 页面位图可只存缩略图 + 向量，不存原尺寸渲染图 |

---

## 10. 最小原型（可直接跑）

```bash
# 1. 装依赖
~/.videoseek-venv/bin/pip install pymupdf

# 2. 验证一个 PDF 能否抽出文字 + 渲染页面
~/.videoseek-venv/bin/python -c "
import fitz
doc = fitz.open('你的文件.pdf')
for i, page in enumerate(doc):
    text = page.get_text().strip()
    pix = page.get_pixmap(dpi=150)
    print(f'页 {i+1}: 文字 {len(text)} 字, 渲染 {pix.width}x{pix.height}')
"
```
