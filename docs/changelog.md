# 变更与 Bug 记录（Changelog & Bug Log）

> **给后续 AI 会话的快速导航（先读这两个文档，避免全量读代码）：**
>
> 1. **本文 `docs/changelog.md`** —— 改动历史 + bug 根因，按时间倒序，每条标「现象/根因/修复位置/commit」。
> 2. **`docs/image-library.md`** —— 图片库功能细节（数据模型、检索、代码位置）。
>
> 需要整体架构再看 `docs/architecture.md`，工程约定看 `docs/engineering.md`。
> 切换模型后建议直接读这两个文档 + 按需读架构文档，而非重读源码。

---

## 快速索引（按主题）

| 主题 | 章节 | 一句话 |
|------|------|--------|
| 图片库删除功能 | [已提交 85eb211](#已提交2026-10-31--commit-85eb211) | 右键/详情删除，走废纸篓，新增 `delete_image` |
| 启动 VLM 服务 | [已提交 85eb211](#已提交2026-10-31--commit-85eb211) | 图片库「启动服务」按钮，`launch_vlm_service` |
| 界面空间精简 | [已提交 85eb211](#已提交2026-10-31--commit-85eb211) | 隐藏 subtitle/提示行/侧边栏低频按钮 |
| 删侧边栏按钮 | [已提交 23477f8](#已提交2026-10-31--commit-23477f8) | 隐藏语言/主题/GitHub/B站/QQ |
| 图片库左栏布局 | [已提交 23477f8](#已提交2026-10-31--commit-23477f8) | 功能模块挪到左侧，右侧全给图片 |
| 照片目录进侧边栏 | [已提交后续](#已提交2026-10-31--commit-后续) | 去 LOCAL AI hero，照片目录挂到全局侧边栏 |
| 拖拽变成框选 | [2026-10-31](#2026-10-31) | 模型缺 `ItemIsDragEnabled` 标志 |
| 自动增量索引 | [2026-10-31](#2026-10-31) | 打开软件自动检查新增图并索引+描述 |
| git 推 GitHub 502 | [2026-10-30](#2026-10-30) | 代理被污染，per-url 代理 + 重试别名 |
| 缩略图底部灰条 | [历史](#历史-2026-10-中旬) | QPainterPath OddEvenFill 挖洞 |
| VLM 截断 JSON | [历史](#历史-2026-10-中旬) | `_repair_truncated_json` 兜底 |
| 中文搜图不准 | [历史](#历史-2026-10-中旬) | 三级检索分流 + 属性筛选 |

---

## 已提交（2026-10-31 · commit `85eb211`）

**图片库四项升级**（删除、启动服务、界面精简、侧边栏精简）：

1. **删除图片功能**
   - 根因：图片库原本只能"看"，无法删除素材，用户没法在库内清理。
   - 改动：
     - `src/services/image_index_service.py` 新增 `delete_image(image_id, delete_file)` 与 `_trash_file(path)`。删除索引记录走 `store.delete_images`；删除源文件**走系统废纸篓**（macOS 用 `osascript` 调 Finder `delete`，Windows/Linux 用 `send2trash`），**绝不 `os.remove`**。
     - `ui/file_actions.py` 的 `attach_result_menu` 新增可选 `extra_actions(menu, paths, resolved)` 回调参数，让调用方可追加自定义右键菜单项（向后兼容，不传则行为不变）。
     - `ui/widgets/image_library_page.py`：详情面板加「删除并移入废纸篓」按钮 `btn_delete`；右键菜单加「从索引移除」「删除并移入废纸篓」两项；新增 `_DeleteWorker` 后台线程执行删除。
   - 交互安全：删除源文件前有确认框；删文件进废纸篓可找回。

2. **启动 VLM 服务按钮**
   - 根因：图片库的语义描述依赖外部 VLM（LM Studio / Ollama），服务没启动时只能手动开，用户希望一键启动。
   - 改动：
     - `src/services/image_index_service.py` 新增 `launch_vlm_service()`：按 `provider_preset` 启动本地服务（`ollama` → `ollama serve` / `open -a Ollama`；`lm_studio` → `open -a LM Studio`）；云端 preset（openai/dashscope/siliconflow）返回"无需启动"；自定义本地端点返回提示手动启动。
     - `ui/widgets/image_library_page.py`：新增「启动服务」按钮 `btn_start_service`，`_render_status` 里仅当「VLM 未就绪且 `provider_mode=local`」时显示；点击后走 `_LaunchWorker`，成功则 1.5s 后 `refresh_status()` 重新探测。
   - 当前用户配置是 `ollama` 本地预设，会正常显示该按钮。

3. **界面空间精简**
   - 根因：图片库顶部说明文字/提示行/侧边栏低频按钮挤占了缩略图网格空间。
   - 改动：
     - `image_library_page.py`：隐藏 header 副标题、`lbl_attr_hint`、`lbl_grid_hint` 三行说明性文字（信息仍在 tooltip/右键菜单）。
     - `ui/widgets/components.py`：`NavigationSidebar` 隐藏 `btn_notice`/`btn_about`/`btn_donate`（对象保留，仅 `hide()`，避免 `gui.py` 等处的引用报错）。

---

## 已提交（2026-10-31 · commit `23477f8`）

**三项后续调整**（删侧边栏按钮、图片库左栏布局、文档导航）：

1. **删除侧边栏低频按钮**
   - 需求：删掉左下角的英文切换、主题切换、GitHub、B 站、QQ 群按钮。
   - 改动：
     - `ui/widgets/components.py`：隐藏 `btn_language`、`btn_theme`、`btn_github`、`btn_bilibili`、`btn_qq`，并隐藏空的 `footer_icon_row`。
     - `ui/windows/gui.py`：`_refresh_sidebar_icon_buttons` 里把这些按钮改为恒 `setVisible(False)`（否则会因 social 配置 `github_url`/`bilibili_url`/`qq_url` 存在而被重新 show）。
   - 注意：主题切换入口仍在「设置」页；语言切换功能仍可用（按钮隐藏不代表功能删除）。

2. **图片库左栏布局重构**
   - 需求：把「照片目录」整块（添加/移除/打开、路径列表、重扫/清理、开始索引/停止、描述/强制 checkbox、启动服务）从顶部挪到页面左侧，右侧全给检索 + 缩略图网格 + 详情。
   - 改动：`ui/widgets/image_library_page.py` 的 `_build_library_card` / `_build_search_card` 改为 `return card`，`__init__` 用水平 `QSplitter`（`ImageLibraryMainSplitter`）组装：左 `library_card`（min 260 / max 420 宽，竖排紧凑布局），右 `search_card`（拉伸 1）。可拖动分栏调宽。
   - 原 `library_card` 内的按钮从「水平一排」改为「窄栏竖排 + 两两并排」，适配窄侧栏。

3. **文档导航明确「先读两个文档」**
   - 需求：切换模型时希望新 AI 能直接找到技术文档和所有更改，不读代码。
   - 改动：`docs/changelog.md` 顶部导航明确「先读 `docs/changelog.md` + `docs/image-library.md`」，架构再读 `architecture.md` / `engineering.md`。

---

## 已提交（2026-10-31 · commit 后续）

**去掉 LOCAL AI hero 卡片 + 「照片目录」挪进全局侧边栏**：

1. **隐藏侧边栏顶部 hero 卡片与副标题**
   - 需求：左上角「LOCAL AI」hero 卡片占空间，去掉后侧边栏顶部腾空。
   - 改动：`ui/widgets/components.py` 隐藏 `subtitle` 和 `hero_card`（对象保留，仅 `hide()`）。

2. **「照片目录」整块移到全局侧边栏**
   - 需求：把图片库的「照片目录」控制面板从页面内部挪到最左侧全局侧边栏，让侧边栏显示「导航项 + 照片目录」两项，图片库页面右侧全给检索 + 图片网格。
   - 改动：
     - `components.py`：`NavigationSidebar` 新增 `library_panel_host`（`SidebarLibraryPanelHost`）容器，位于导航项之后、`addStretch()` 之前；新增 `attach_library_panel(widget)`（reparent 到 host）与 `set_library_panel_visible(bool)`。
     - `image_library_page.py`：`__init__` 不再用 splitter 把 `library_card` 放进页面，而是 `self.library_card = self._build_library_card()` 暴露给主窗口；页面 `content_layout` 只放 `search_card`（检索 + 网格 + 详情，占满）。`library_card` 去掉 260~420px 宽度限制，改为自适应侧边栏宽度。
     - `gui.py`：构建 images_page 后 `self.sidebar.attach_library_panel(self.images_page.library_card)`；`switch_page` 里 `self.sidebar.set_library_panel_visible(page_name == "images")`。
   - 关键点：卡片 reparent 到 sidebar 后，其按钮信号连接不受影响（信号在 widget 间，与 parent 无关）。

---

## 2026-10-31

### 拖拽图片/视频到剪映失效（commit `24e60fc`）

- **现象**：从图片库/搜索结果直接拖动图片，目标不是拖到剪映，而是变成 rubber band 框选。
- **根因**：Qt model/view 里，仅 `view.setDragEnabled(True)` 不够——**模型必须让 `flags()` 返回 `Qt.ItemIsDragEnabled`**，否则视图退化为框选而非拖拽。view 的 `dragEnabled`/`DragOnly` 其实一直是对的，问题在 model 缺标志。
- **修复**：`ImageGridModel`（`ui/widgets/image_library_page.py`）和 `VideoHitModel`（`ui/widgets/unified_search_page.py`）的 `flags()` 都加上 `ItemIsDragEnabled`。
- **测试**：`test_image_library_ui.py` 新增 `test_flags_mark_items_draggable`。

### 打开软件自动增量索引 + 描述（commit `1de6116`）

- **现象**：新增图片后打开软件不会自动索引，需手动点；且自动索引时不会生成描述（中文搜图不可用）。
- **根因**：索引只在手动点「开始索引」时触发；描述开关默认关闭，旧 clip-only 图（caption 为空）被增量跳过。
- **修复**：
  - `ui/widgets/image_library_page.py`：新增 `_auto_check_new()`（30s 节流、防重入、有 roots 才触发）、`auto_index_on_startup()`；`showEvent` 每次显示都检查。
  - `ui/windows/gui.py`：`_finish_startup_sequence` 里 `QTimer.singleShot(2200, ...)` 调 `_auto_index_images`。
  - 描述默认 `chk_describe.setChecked(True)`；自动索引时 `describe=True`，旧图因 caption 空会被重描述。
- **关键点**：`index_images` 的跳过逻辑是「describe_enabled 且 caption 非空才跳过」，所以 VLM 可用时 clip-only 旧图会自动补描述。

---

## 2026-10-30

### git 推 GitHub 动不动 502（未提交，写进 `~/.gitconfig`）

- **现象**：`git push` GitHub 频繁 502 / 超时。
- **根因**：真实稳定代理是 `127.0.0.1:10808`（`.zshrc` 里配置），但 WorkBuddy 会在子进程注入 `HTTPS_PROXY=51275`（该端口连 GitHub 3/3 超时），git 默认读环境变量走 51275 被污染的代理。
- **修复（根治）**，写入 `~/.gitconfig`：
  1. `http.https://github.com.proxy = http://127.0.0.1:10808` —— per-url 代理**优先级高于** `HTTPS_PROXY` 环境变量，git 无视被污染的 51275。
  2. `http.postBuffer = 524288000`（500MB，避免大 pack 慢连接被断）。
  3. 别名 `pushr`：自动重试 8 次、5s 退避，兜底偶发故障。用法 `git pushr origin master`。
- **注意**：只修了 git 层；其他程序若也 502 系同源，可各自针对配置绕过。实测 10808 连 GitHub 5/5 全 200。

### 赞赏卡片替换 + 其他仓库加赞赏（commit `aaef338` 等）

- 用双平台赞赏卡片 `docs/assets/donate-card.png` 替换 README 里两个独立二维码。
- 5 个 hacksmo 仓库 README 末尾追加赞赏区块；`jianying-headless` 图片移到 `docs/media/` 并加 `.gitignore` 白名单；`tg_monitor` 有远程冲突，rebase 时采用远程完整版+末尾追加。

---

## 历史（2026-10 中旬）

### 缩略图底部灰色横条（commit `7652cb8`）

- **根因**：`ImageGridDelegate.paint` 里缩略图裁切用了 `QPainterPath`，默认 `OddEvenFill` 会让圆角矩形与底部补丁重叠处被"挖洞"，露出卡片背景，形成底部灰条。
- **修复**：`thumb_clip.setFillRule(Qt.FillRule.WindingFill)`。

### VLM 截断 JSON 丢失人物信息（commit `718481d`）

- **根因**：多人场景回复超 token 预算被截断，JSON 不完整解析失败，整段退化为 prose caption，丢失所有人。
- **修复**：`src/services/image_understanding.py` 的 `_repair_truncated_json()` 从后往前找最后一个分隔符，补齐括号，挽救 caption 和已完整的人名条目。相关 `UNPARSED_SUFFIX` 标记让索引重描述。

### 中文搜图不准（commit `c514c0e` / `f059e65`）

- **根因**：默认 `openai-clip/vit-base-patch32` 文本塔只认英文，中文查询进 CLIP 基本是噪声（实测 `红酒瓶`→搞笑表情包等）。
- **修复**：`image_index_service.search_by_query` 按语言分流——中文走「属性筛选 → 字面扫描（文件名/caption/tags/people）→ CLIP 向量兜底」三级；英文直连 CLIP。属性检索（颜色/体型/风格）由 `image_attributes.py` 零样本打分实现，不依赖 VLM。

### 拔盘误删索引（commit `db794b6`）

- **根因**：外部硬盘未挂载时，`prune` 会把所有文件当"已删除"清掉索引。
- **修复**：`_offline_roots()` 识别不可达目录，prune 跳过这些行并提示。

---

## 维护约定

- 每次有实质改动，按时间倒序在本文件顶部新增一节，写清**现象 / 根因 / 修复位置 / commit**。
- Bug 和 feature 都记这里；纯文档改动（README 等）可精简记录。
- 涉及图片库的细节同步更新 `docs/image-library.md`。
