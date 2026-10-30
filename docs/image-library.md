# 图片库：理解、索引与检索

给「照片 / 截图 / 素材图」建语义索引，并且**不只看图上的文字**——每张图会得到一句描述、一组结构化人物信息、一组标签，全部可检索。

与视频库的关系：**完全独立**。图片走自己的 `image_assets` 表，视频的 `frames` / `chunks` / `dialogue_segments` 一个字节都不动。

---

## 1. 一张图会存下什么

| 字段 | 内容 | 来源 |
|------|------|------|
| `vector` | 512 维 CLIP 向量（跟随当前模型 profile） | 本地 ONNX，CPU |
| `caption` | 一句话描述整张图 | VLM |
| `people_json` | 结构化人物数组 | VLM |
| `tags_json` | ≤12 个短标签（场景 / 物品 / 风格 / 颜色） | VLM |
| `image_path` / `rel_path` / `library_root` | 定位信息 | 文件系统 |
| `width` / `height` / `bytes` / `mtime` | 增量索引依据 | 文件系统 |
| `describer` | 谁生成的描述（`vlm:<model>` / `vlm-error:<type>` / `clip-only`） | 内部 |

`people[]` 每条的结构：

```json
{
  "count": 1,
  "gender": "女",
  "age": "青年",
  "clothing": "红色连衣裙",
  "action": "站在窗边看手机",
  "position": "画面左侧",
  "features": "长发，戴细框眼镜"
}
```

这就是「人物描述」的落点。它**不放进 `tags`**——`src/services/understanding_tags.py` 里的 `_GENERIC_FILLERS`
会把 `男人 / 女人 / person / people` 当成 C 级填充词丢掉（视频镜头打标签这么做是对的），
所以人物信息走独立字段，检索时才不会被过滤掉。

## 2. 三步用起来

```bash
cd ~/Desktop/VideoSeek
V=~/.videoseek-venv/bin/python

$V scripts/image_index.py add ~/Pictures/travel     # 登记目录（可多次）
$V scripts/image_index.py scan                      # 看有多少张待索引
$V scripts/image_index.py index                     # 编码 + 描述
$V scripts/image_index.py status                    # 看总量 / 维度 / VLM 是否就绪
```

`index` 的输出是结构化统计：

```json
{
  "scanned": 128, "added": 128, "updated": 0, "skipped": 0,
  "failed": 0, "described": 128, "describe_failed": 0,
  "describe_enabled": true, "describe_reason": ""
}
```

增量规则：文件 `mtime` 没变、且已有非空 `caption` 的行直接跳过。想强制重算用 `--force`。
只想算向量、不调模型用 `--no-describe`（几百张图几分钟内完成，之后可随时补描述）。

## 3. 三种检索

```bash
# 语义（CLIP 文本向量）—— 描述性查询
$V scripts/image_index.py search "穿红裙的女人站在窗边" --top-k 10

# 关键词（caption / people / tags 子串）—— 确定词
$V scripts/image_index.py search "红裙" --mode keyword
$V scripts/image_index.py search "青年" --mode keyword --min-people 1 --max-people 2

# 以图搜图（CLIP 图片向量）
$V scripts/image_index.py search --mode image --image ~/Pictures/q.jpg
```

| 模式 | 适合 | 局限 |
|------|------|------|
| `text` | "海边日落"、"会议室白板" 这类整体语义 | CLIP 对细粒度属性（衣服颜色、人数）不敏感 |
| `keyword` | 已知确切片语："红裙"、"眼镜"、"2人" | 需要先有描述；子串匹配，不做同义词扩展 |
| `image` | 找同款构图 / 同一场景 | 只比整体视觉，不比文字 |

实测：以图搜图时查询图自身的得分是 `1.000` 且排第一，属正常的自匹配基线。

## 4. 人物描述从哪来

复用你已有的 VLM 配置，**不新增任何依赖、不下载新模型**：

**设置 → 理解 → 模型服务 → VLM**

| 方式 | base_url 示例 | 说明 |
|------|---------------|------|
| LM Studio + Qwen3-VL | `http://127.0.0.1:1234/v1` | 免费、离线，推荐；8B 在 M 系列上够用 |
| Ollama | `http://127.0.0.1:11434/v1` | 同上 |
| 云端 OpenAI 兼容 | 按服务商填 | 速度快、按量计费 |

提示词要求模型输出严格 JSON，并内置三条边界：

1. **不推测**姓名、身份、职业、情绪、关系；
2. **不评价**外貌；
3. 看不清的字段写「不确定」，不许编造。

这三条是刻意的：人物描述很容易滑向"识别这个人是谁"，那是另一件事（也涉及合规），本功能只做
**画面可见属性的描述**，不做人脸识别、不做身份归属。

模型不听话、返回散文时不会失败：整段文字降级为 `caption`，`people` 为空，`parse_ok=False`。
向量照常写入，语义检索仍可用。

服务没启动时也有明确降级：

```
VLM 就绪 : 否 已配置 http://127.0.0.1:1234/v1，但服务127.0.0.1:1234 不可达（ConnectionRefusedError）
```

此时 `index` 自动切到"只算向量"，并在 `describe_reason` 里说明原因，不会把每张图都记成失败。

## 5. 性能

Apple Silicon、纯 CPU：

| 阶段 | 量级 |
|------|------|
| CLIP 编码 | 每张 50–150 ms |
| VLM 描述（本地 8B） | 每张 1–4 s，`--concurrency` 默认 2，可到 4 |
| 关键词检索 | 全表扫描，万级图片仍在百毫秒内 |
| 向量检索 | 毫秒级 |

1000 张图：只算向量约 2 分钟；带描述约 30–60 分钟（取决于并发和模型大小）。建议先
`--no-describe` 建好向量，再挑重点目录补描述。

## 6. 维护

```bash
$V scripts/image_index.py prune --dry-run   # 看有多少条记录的文件已不存在
$V scripts/image_index.py prune             # 删掉这些记录
$V scripts/image_index.py remove <folder>   # 取消登记（不删已索引行）
```

- **换检索模型**：向量维度变了会自动重建 `image_assets` 表并提示重新索引（图片是派生数据，重建无损失）。
- **删索引**：直接删 `image_assets` 表即可，视频索引不受影响。
- **去重**：`image_id` 是文件内容的 SHA-1 前 32 位，同一张图放在两个目录只会索引一次。

## 7. 边界（本阶段不做）

| 不做 | 原因 |
|------|------|
| 在应用 UI 里浏览图片库 | 目前是 CLI；UI 页签是下一步 |
| 视频与图片在同一个搜索框里混排 | 需要把图片并入统一媒体模型，改动面大，单独排期 |
| 人脸识别 / 人物聚类 / 身份归属 | 合规风险，且与"描述画面"是两件事 |
| 一张图多个区域分别描述 | 单图单向量；区域级需要检测框 + 多向量 |
| 本地 VLM（ONNX） | 现有链路全是 OpenAI 兼容 HTTP，接本地 ONNX VLM 是新工程 |

## 8. 代码位置

| 文件 | 职责 |
|------|------|
| `src/media/formats.py` | `IMAGE_EXTS` / `is_supported_image_path`（与 `VIDEO_EXTS` 严格分离） |
| `src/storage/image_lance_store.py` | `image_assets` 表：schema、upsert、向量检索、全表扫描、统计、删除 |
| `src/services/image_understanding.py` | 描述 prompt、JSON 解析与容错、VLM 调用、可达性探测 |
| `src/services/image_index_service.py` | 扫描、增量索引、CLIP 编码、三种检索、状态 |
| `scripts/image_index.py` | 命令行入口 |
| `tests/test_image_library.py` | 30 个用例：格式、解析、扫描、内容 ID、Lance 读写、关键词与人数过滤 |
