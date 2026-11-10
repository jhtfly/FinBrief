# 金融热点编译系统 开发文档

> 版本 v2.21.0 ｜ 更新日期 2026-11-10 ｜ 维护要求：**每次修改程序后必须同步更新本文档对应章节与"更新日志"**。

---

## 1. 项目概述

本程序在本机自动完成：**境外金融信源检索 → AI 按筛选要求判定 → 依据英文原文编译中文专报 → 按《永州金融稳定专报》模板排版导出 Word** 的全流程。AI 只产出初稿，专报须经人工复核后导出。

- 交付形态：Windows 桌面 exe（PyInstaller onedir），双击运行，数据全本地。
- 程序位置（交付）：仓库 Releases 提供的预编译包（exe + _internal），解压即用。
- 源码位置：本仓库根目录（app/ + web/ + main.py + build.spec）。
- **v2.8 新增**：多线索综合专报（2-5 条融合 + 当页脚注）、AI 对话式修改专报、设置参数文件 `data\settings.json`（结构化、可手改）、AI 调用自动重试、内置试用额度（100 万 tokens）。

## 2. 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | Python 3.11 + FastAPI + uvicorn | 单进程，API 与静态页同端口 |
| 数据库 | SQLite（标准库 sqlite3） | 单文件 `data\finbrief.db`，WAL 模式 |
| 采集 | httpx + feedparser + BeautifulSoup(lxml) | 统一 UA / 超时 / 代理出口 |
| AI | OpenAI 兼容 Chat Completions（httpx 直调） | 地址/Key/模型全可配，不内置密钥 |
| 导出 | python-docx | 排版参数程序化固化 + 生成后自检 |
| 前端 | 原生 HTML/CSS/JS 单文件（web/index.html） | 无构建链，FastAPI 直接托管 |
| 窗口 | pywebview（WebView2） | 失败自动退化系统浏览器 |
| 打包 | PyInstaller（onedir，icon=assets/icon.ico） | build.spec |

## 3. 目录结构

```
FinBrief/
├── main.py                    # 入口：加载页先行 → 后端就绪后 load_url
├── build.spec                 # PyInstaller 打包配置（图标/数据文件/隐式导入）
├── requirements.txt
├── README.md                  # 用户使用说明
├── DEVELOPMENT.md             # 本文档
├── assets/icon.ico            # 程序图标（由"程序图标.jpeg"转换）
├── web/index.html             # 前端单页（含全部样式与逻辑）
├── app/
│   ├── paths.py               # 路径解析（兼容源码/frozen）；data 目录、web、sources.yaml
│   ├── db.py                  # SQLite：SCHEMA、settings KV、items/screenings/reports/multi_reports DAO、run_logs、fetch_runs、settings.json 镜像
│   ├── server.py              # FastAPI 全部路由 + 后台任务作业函数
│   ├── tasks.py               # 并发组任务通道（fetch/net/ai 组 + 组内上限排队 + 协作打断）
│   ├── scheduler.py           # 自动检索：每 60s tick，支持 daily / interval 两种模式
│   ├── trial_config.py        # v2.8 内置试用额度配置（Base URL/Key/模型/总额度；见 §7 试用体验）
│   ├── fetcher/
│   │   ├── sources.yaml       # 预置信源配置（数据驱动，新增站点只改这里）
│   │   ├── rss.py             # RSS 抓取（httpx 下载 + feedparser 解析 + 时间窗过滤）
│   │   ├── html_sites.py      # HTML 列表页（容器选择器/链接正则两种模式）、sitemap、正文提取、详情页日期提取
│   │   ├── runner.py          # 采集调度：逐站执行、失败记录继续、progress_cb 渐进回调、probe_site
│   │   └── date_backfill.py   # 无日期线索的发布日期补全
│   ├── ai/
│   │   ├── client.py          # OpenAI 兼容调用：可配重试（指数退避）+ 429/5xx 兜底、JSON 容错提取、token 记账、试用额度累计
│   │   ├── screen.py          # AI 筛选：六项要求（可被 custom_screen_criteria 整体替换）、批量 8 条/次、四维评分
│   │   ├── compile.py         # AI 编译：结构化 JSON 输出、原文不足自动放弃、ASCII 引号→中文弯引号、编译时补日期
│   │   ├── multi.py           # v2.8 综合专报编译：2-5 条融合 + [[citeK]] 脚注标记纪律 + 程序化生成脚注文本
│   │   ├── edit.py            # v2.8 AI 对话式修改：自然语言指令 → 修改后完整 JSON（只改内容不改排版）
│   │   ├── translate.py       # 英文标题批量翻译（15 条/次，自动跳过中文标题）
│   │   ├── hotspots.py        # AI 热点方向分析：归纳 3-8 个方向（排名/趋势/热度/数据佐证），保留最近 10 次
│   │   └── source_analyzer.py # AI 添加信源（探测→设计→验证→失败带反馈二次修正）、AI 检查修复失效源
│   └── exporter/
│       ├── docx_builder.py    # 模板排版（参数见 §8）+ build_docx/build_docx_multi/export_report/export_multi_report + [[citeK]] 脚注注入
│       └── selfcheck.py       # 生成后自检：ASCII 引号 FATAL、rFonts hint、固定行距、脚注引用一致性（正文+W+脚注 XML）
└── tests/                     # 探测脚本（probe_multi_export 等，可清理）
```

程序（exe）旁运行时生成：
- `data\finbrief.db`（全部业务数据）
- `data\settings.json`（v2.8 **结构化设置文件**，启动时外部编辑优先应用、界面保存后同步写回；键表见 §9.5）
- `data\startup.log`（启动日志）

## 4. 运行逻辑

### 4.1 启动流程（main.py）
1. `pick_port` 从 8765 起选空闲端口 → 写 `data\startup.log`；
2. **先** `webview.create_window(html=加载页)` 立即出窗口（避免"点了没反应"）；
3. `webview.start(callback)`：callback 在后台线程起 uvicorn，`wait_port`（上限 180s）成功后 `window.load_url(url)`，失败 `load_html(错误页)`；
4. pywebview 不可用 → 退化：起后端 + `webbrowser.open`；
5. FastAPI `startup` 事件：`init_db`（含 title_zh 列迁移）、空库时装载预置信源、启动 scheduler。

**启动耗时（v2.18.0）**：`webview.start` 改 `private_mode=False, storage_path=data\webview`——原默认 InPrivate 模式每次启动在 %TEMP% 重建全新 WebView2 profile、退出即删，是"双击后卡 1-2 秒才见加载页"的主因；持久化后热启动实测 2.19s→1.46s，且 localStorage（主题选择/排序记忆）重启保留。本地静态服务端口随机挑选（默认 42001 多开会撞车）。main.py 增加 `window.events.shown` "加载页已显示（窗口 +X.XXs）"观测日志，启动问题先看 `data\startup.log` 时间线。剩余约 1.5s 为 CLR+WebView2 固定成本；新构建 exe 首次启动被 Defender 扫描会额外 1-2s，跑过一次即恢复。

**教训（勿回退）**：绝不能等后端就绪后才创建 webview 窗口——v1.0 因此出现"127.0.0.1 拒绝连接"空白页。

### 4.2 后台任务通道（tasks.py，v2.6 并发上限 + 协作打断）
**按并发组并行 + 组内并发上限排队**：`GROUPS` 定义 stage→组（fetch 组=fetch；net 组=probe/ai_check；ai 组=screen/compile/translate/backfill/hotspots/ai_add/summarize/search）。不同组并行；**组内同时运行数受设置控制**——fetch/net 恒 1（fetch 组的站点级并发由 `fetch_concurrency` 在 run_fetch 内部用线程池实现），ai 组并发数=`ai_concurrency`（默认 2，上限 8），超出的排队，完成即出队。

**协作打断（v2.6）**：任务中心"⏹ 打断"（`POST /api/task/interrupt/{id}`）→ 取消事件 → 任务句柄 `state.check()` 在各模块**处理单元之间**抛 `TaskCancelled`：
- fetch：删除本轮已入库线索（恢复到采集前）；
- screen：删除本轮写入的判定；translate/backfill：复位本轮写入的 title_zh/published_at；
- compile：删除本轮生成的草稿；summarize/hotspots：保存前检查。
任务呈现"已打断（本轮写入已回滚）"，任务中心历史可见。

- 任务记录持久化到 `task_runs` 表，`/api/task` 返回 `{running:[], queued:[], history:[]}`；完成提醒前端 diff；`run_in_background` 返回 `(True, "已启动"/"已排队…")`。
- 任务中心：正在执行（多行，每行可打断）、排队等待（可移除）、历史（单删/一键清空）+ 检索历史。

### 4.3 采集流水线（fetch，v2.6 并发抓取）
1. `runner.run_fetch(source_keys, progress_cb, should_cancel)`：**fetch_concurrency 线程并发抓取**（站点级），完成一个站点即串行入库并回调 → 前端渐进刷新（保留）；
2. 单站失败记入 `fetch_runs.failures` 并更新该源连通状态，**不中断**；URL 去重（已存在仅补填空日期/摘要，不产生新 id——打断回滚不误删旧线索）；
3. **采集仍按 `window_days` 时间窗过滤**（无日期条目放行）；线索面板的时间选项（今日/7/14/30/全部）是独立的展示过滤，经 `/api/items?days`（COALESCE(published_at, fetched_at)）实现，与统计 `/api/stats?days` 同口径；
4. 采集完成后自动：英文标题翻译（auto_translate=1）→ 无日期线索补全（上限 15）。

### 4.4 筛选与编译
- 筛选：勾选或"一键筛选"→ 批量（8 条/次）送模型，输出 `{is_match, criteria[1-6], match_points, reason}`；**"日期待核"条目由人工核对**。**v2.21 批次并发**：多批由 `ThreadPoolExecutor` 并行调用（并发数=设置 `ai_concurrency`，上限 6，全部用同组 `ai` 闸门语义），首试 `max_tokens` 直升 8000（推理型模型思考占满 4000 → 首批失败再翻倍重试是"筛选慢"根因）；打断回滚语义不变（已写入判定全删）。
- **AI 评分（v2.2）**：筛选时模型一并输出 `scores{relevance 主题贴合性, authority 机构权威性, completeness 内容完整性}`（0-100，不额外增加调用次数）；`timeliness` 由系统按发布时间分段计分（≤1天100 / ≤2天90 / ≤3天80 / ≤5天65 / ≤7天50 / 更旧25 / 无日期50）；总分 = 四维按设置权重（默认 贴合40/时效25/权威20/完整15）归一化合成，缺维度按 50 分计，存 `screenings.score/score_detail`。旧数据未打分的可点热点卡"为未打分线索补评分"按钮补齐（重走筛选接口）。评分细则可经 `score_custom_rules` 追加。
- 编译：抓原文全文（`content_fetched` 复用）→ 不足 400 字符自动放弃并记原因 → 模型输出结构化 JSON（sufficient/title/editor_note/sections/conclusion）→ ASCII 直引号统一转中文弯引号 → 存 `reports`（draft）。
- 编译入口四处：底部批量条（选中）、热点卡"一键编译全部符合线索"（自动跳过已有草稿的）、**热点卡行内"编译专报/复核专报"按钮（v2.3，按排名单独挑编）**、API。
- **热点方向（v2.2，v2.3 改造）**：对当前时间窗内 matched 线索（≥3 条）让模型归纳 3-8 个热点方向（rank/name/description/**evidence_ids 引用线索编号**/count/trend/score）；后端把编号翻译回真实线索 id 存 `evidence_item_ids`，`GET /api/hotspots` 再附 `ref_items`（含标题/机构/日期/评分/是否有草稿）——前端"相关线索"可逐条展开，直接**编译专报 / 复核专报 / ☆ 收藏**。结果存 `hotspots` 表保留最近 10 次。

### 4.5 导出（exporter）
`export_report(report_id)`：期号 = `issue_counter + 1` → `build_docx` 按模板落版 → `selfcheck.check_docx`（FATAL 不通过则报错返回，期号不占用）→ 成功后更新 report 状态/路径并 **期号 +1**。文件名非法字符仅文件名层替换为"－"。

### 4.6 自动检索（scheduler.py）
每 60 秒 tick：`auto_fetch_mode=daily` 时匹配 `auto_fetch_time`（HH:MM，`last_auto_day` 防当日重复）；`interval` 时距上次 fetch_runs.started_at ≥ N 分钟触发。**v2.21 防风暴双闸**：`tasks.group_busy("fetch")`>0（运行+排队）即跳过本轮并落日志；提交后写 `last_auto_submit`，间隔不足 N 分钟不连发——修复"采集排队时 started_at 陈旧 → 每 tick 误判该跑 → 排队长龙加打断乱序"（用户报"排队等待 13+ 条重复任务、任务打断经常失败"）。前端排队等待卡**只展示前 5 条**，新增「🗑 清空排队」按钮（`POST /api/task/queue/clear`，可选 `{"group":"fetch"}` 只清指定组）。

### 4.7 综合专报与 AI 修改（v2.8）
**多线索综合专报**：线索面板底部批量条 / 热点卡勾选 / 收藏夹线索表均可勾选 2-5 条 →「✦ 生成综合专报」→ `POST /api/compile_multi`（stage=multi_compile，入 ai 组排队）→ `ai.multi.compile_multi_items`：
1. **材料预审逐条推进（v2.9）**：每条显示进度"读取材料 k/N：机构…"，抓不到原文的材料回退用摘要并提示；全部材料看完后再决定——合计 <1600 字符才放弃，放弃原因附**每条材料字数明细**（不再"只看第一条"）；
2. 一次 AI 调用输出融合 JSON（sufficient/title/editor_note/sections/conclusion）；**v2.9 结构残缺自动补救**：校验缺失字段 → 以"缺的是哪些字段"回问模型补齐一轮；补救后仍缺 → 落库 abandoned（原因+运行日志留模型原始输出片段）——此前失败只闪 toast 且不落库（v2.8 实测缺陷）；
3. 正文持 `[[citeK]]` 标记（K=材料编号）；程序把标记映射成脚注（**脚注文本程序生成防编造**：`  机构《材料名》，日期。`），落库 `multi_reports`。

**导出与脚注**（详见 §8）：`export_multi_report` 与单篇共用期号计数（issue_counter），**v2.20.1 文件名跟随刊头**：`{刊头}第{X}期：{标题}.docx`（默认 XXXX专报）、署名（投稿：{sign_name}）。

**AI 对话式修改**：专报编辑器双栏模式（v2.9 起**编辑器为浮动窗口**：无遮罩、标题栏可拖动、可最大化/最小化，主界面操作不中断），左=结构化整体预览（[[K]] 上标角标、脚注/材料对照），右=AI 对话框。同步与流式两条路径：
- 同步 `ai.edit.ai_edit_report(kind, report, instruction)`（`build_ai_edit_messages`/`apply_ai_edit_data` 拆分共用）→ 修改后完整 JSON 落库 `_apply_ai_edit`；
- **流式（v2.9，默认）`POST /api/report/{id}/ai_edit_stream` 与 `/api/multireport/{id}/ai_edit_stream`**（SSE）：`client.chat_stream` 按 OpenAI 流协议逐块回调，事件 `status / reasoning（模型思考流，delta.reasoning_content） / content（生成字数） / retry / done（含完整 report） / error`；前端实时显示"思考中… / 已产出 N 字"，模型不支持思考流时显示状态文案；
- **聊天记录管理（v2.9.4）**：user 气泡右键删除单轮、对话栏「🗑 清空聊天」一键清空（`POST /api/report|multireport/{id}/chat_delete {index}` / `chat_clear`）——仅裁剪 chat_log，不改动正文；前端以服务端返回重建气泡与版本链，清空后撤销/恢复由当前状态重新起算；
- **版本链（v2.9）**：`verChain/verPtr` 每次修改（含模式切换带来的人工编辑）入链——↩撤销 / ↪恢复 / ⟲恢复最初 三向回退全部经 PUT 同步数据库；「⇄对比」弹出句级 LCS diff 浮层（红=删除，绿=改后，节级贪心配对）；
- 排版类指令被提示词拒绝并 reply 说明（导出始终按模板）；[[citeN]] 标记位置语义保留。
- **任何来源的修改都不影响导出排版**——导出只用内容字段走 docx_builder 模板。

**试用体验**（见 §7 试用节）：未配置 Key 时可在设置一键启用内置试用额度。

### 4.8 设置参数文件（data\settings.json）
v2.8 起设置双存储：SQLite（源头）+ 结构化 JSON 镜像（开发人员可手改）；v2.9 起文件为**分区结构**（含 AI 提示词在内全部可改参数）：
- `sync_settings_file()` 每次 `set_setting()` 后自动镜像，按 `_SETTINGS_GROUPS` 分区写入 `{ "_目录": {组: 用途说明}, "组名": {键: 值}, ... }`（组=API与模型/网络与采集/自动检索与翻译/评分权重/提示词/导出排版/界面外观/其他设置）；
- **启动时 `apply_settings_file()` 递归读取**（叶子即键），**兼容 v2.8 的平铺写法**；外部手改优先生效，再无条件镜像回写（保证首启生成文件、维度齐全）；
- 内部状态键不进文件：`_SETTINGS_FILE_INTERNAL = {v23_translate_default_applied, v27_enabled_common_applied, last_auto_day, trial_used_in, trial_used_out}`；
- **可改键速查表见 §9**（自定义提示词类键手改时须保留各自的 JSON 契约说明）。

## 5. 数据库表

| 表 | 关键字段 | 说明 |
|---|---|---|
| sources | key UK, name_cn/en, category(cb_reg/ins/dep/cons/thinktank/media), mode(rss/html/sitemap), config JSON, enabled, sort_no, note, src_group(common/reference/custom), in_common, **last_fetch_at/last_fetch_ok/last_fetch_items/last_error（v2.4 连通状态）, pinned（v2.4 置顶）** | 预置由 sources.yaml upsert；**采集只跑 enabled=1 且 in_common=1**；每次采集/自检逐站更新连通状态；置顶可多个、排序恒优先 |
| items | source_id, title, url UK, published_at(可空→待核), summary, content, content_fetched, title_zh, read, **ai_summary（v2.4 AI 摘要缓存）** | URL 唯一即去重；ai_summary 生成后复用（重新生成会覆盖） |
| screenings | item_id UK, is_match, criteria JSON, match_points, reason, model, score, score_detail | score 为四维合成总分(0-100) |
| favorites | id, type(item/report), target_id, fav_at, UNIQUE(type,target_id) | 线索/专报收藏；支持批量清空 |
| hotspots | id, model, matched_count, analysis JSON, created_at | 保留最近 10 次；analysis 含 evidence_item_ids/ref_items |
| **task_runs** | id(uuid) PK, stage, label, status(queued/running/done/error/cancelled), progress, result, error, created/started/finished_at | **v2.4 任务中心记录**；活跃态由内存提供，历史查询排除 running/queued |
| **token_usage** | day PK, tokens_in, tokens_out, calls | **v2.4 每日 token 用量**；client.chat 每次成功调用按 usage 记账 |
| reports | item_id UK, issue_no(导出时分配), title, editor_note, body JSON([{h1, blocks:[{h2,text}]}]), conclusion, status(draft/reviewed/exported/abandoned), abandon_reason, docx_path, **kind(v2.8 'single'，留扩展), citations(v2.8 留用)** | 单篇专报 |
| **multi_reports** | id, title, editor_note, body(JSON 同 reports), conclusion, citations JSON `[{no,item_id,ref}]`（no=材料编号，ref=程序生成脚注文本）, item_ids JSON（参与线索）, status(draft/reviewed/exported/abandoned), abandon_reason, model, docx_path, issue_no, created/updated/exported_at | **v2.8 综合专报**（2-5 线索融合含脚注）；DAO：save_multi_report / update_multi_report / list_multi_reports / get_multi_report（附 sources_info 摘要）/ delete_multi_report |
| settings | key PK, value | 全部可配项，默认值见 `db.DEFAULT_SETTINGS`；同步镜像 `data\settings.json`（§4.8） |
| fetch_runs | started/finished_at, ok/fail_sites, new_items, window_days, failures JSON | 检索历史 |
| run_logs | ts, level, stage(fetch/screen/compile/export/sources/backfill/system), message | 全流程日志 |

## 6. 信源配置规范（sources.yaml）

每个源：`key`(唯一)、`name_cn/en`、`category`、`mode`、`config`、`enabled`、`note`。三种 mode 的 config：

### mode=rss
```yaml
config: { rss_url: https://... }   # 首选方式；feedparser 解析
```

### mode=html（二选一引擎）
```yaml
# A. 容器选择器模式
config:
  list_url: ...        # 列表页
  item: ".views-row"   # 条目容器 CSS
  title: ".news-title" # 可省略（缺省取容器文本）
  title_regexp: '^\S+\s+(.+?)\s+\d{4}年'  # 可选：从标题文本再提取（整卡链接类站点）
  link: a
  date: ".news-date"   # 可省略
  date_attr: datetime  # 可选；属性缺失自动回退取节点文本
  date_regexp: "\\d{4}年\\d{2}月\\d{2}日"  # 可选：容器全文日期正则（无日期节点时）
  url_date_regexp: "/(\\d{4})/(\\d{2})/(\\d{2})/"  # 可选：URL 提取日期（需年月日三组才有效，避免月粒度误杀）
# B. 链接正则模式（SPA/结构怪异站点）
config:
  list_url: ...
  link_regexp: "/en/news/(press-release|speech)/\\d{4}/\\d{2}/\\d{2}/"
  url_date_regexp: "/(\\d{4})/(\\d{2})/(\\d{2})/"
  date_in_parent: true   # 日期取自链接父容器文本
  max_items: 40
```
日期解析支持：`2026-10-23 / Oct 23, 2026 / 23 October 2026 / 2026年10月23日 / dd/mm/yyyy` 等。

### mode=sitemap（JS 渲染站兜底）
```yaml
config:
  sitemap_url: https://.../sitemap.xml   # 支持 index 自动展开（≤5 子图）
  include: "/en/news/\\d{4}/\\d{2}/"     # URL 过滤正则（必填防全站入库）
  date_from: lastmod   # 或 url（配合 url_date_regexp）
  max_items: 40
```

### 日期待核机制
列表页拿不到日期的条目 `published_at=NULL`，界面显示"日期待核"，处理途径：
1. 采集完成后自动补全（≤15 条/轮）：访问详情页按 `meta[article:published_time 等] → <time datetime> → 正文日期文本` 三级提取；
2. 线索面板「补全日期」按钮手动批量（当前视图无日期条目）；
3. 编译该线索时顺手补全；
4. 仍无法确定 → 人工核对（筛选表/原文页）。

**v2.9.4 失败原因可见**：逐条 try/except，异常或未识别到日期时逐条落 warn 运行日志（线索标题：具体异常/「页面未识别到发布日期（或访问失败）」），完成日志附「失败 N」；任务详情浮层执行日志可逐条查看；返回值附 fail_reasons 列表。

### 2026-10-24 逐站实测结论（复用时注意时效）
- 直连可用 18 源：FSB/BIS×3/SEC/ECB/FED/BOJ（官方 RSS）；ESMA(`.search-date`)、FDIC(`.views-row`)、世界银行(link_regexp)、高盛(link_regexp+date_in_parent)、摩根士丹利中国(`.row-base`+title_regexp)、IAIS(`.post`，域名已迁 **iais.org**)、IADI(link_regexp)；FINMA(`/en/sitemap.xml` lastmod)、MAS(sitemap 无 lastmod→日期留空)、CDIC(post-sitemap)。
- 需代理 6 源：IMF、IOSCO、FCA、EIOPA、麦肯锡、标普（大陆直连 403/超时）。
- 默认停用 2 源：穆迪、摩根大通（纯脚本渲染）。
- RSSHub 公共实例被墙，不可依赖。美联储 feed 偶发临时 404，重试即恢复。

### 信源三分组（v2.3，v2.4 扩充）
sources 增加 `src_group`（common 常用 / reference 参考 / custom 自定义）与 `in_common`（是否在常用组）；**每轮检索只采集 enabled=1 且 in_common=1 的源**；每次采集/自检/单源测试后逐站更新 `last_fetch_at/ok/items/error`（信源管理"最近连通情况"列）；`pinned` 置顶（多个，排序恒优先，前端另有连通性升降序）。信源管理页四个页签：
- **常用信源**：预置 26 源（原在用），默认即常用组；
- **参考信源**：依据用户书签《编译线索网址.html》整理，**2026-10-25/26 三轮实测 + 2026-10-26 v2.5 全局代理复核复测**（RSS 自动发现 → 已知 feed 直测 → sitemap 模式 → AI 分析器）后写入 sources.yaml，共 **28 个**：v2.3 十个（英格兰银行 `/rss/news`、AMRO、布鲁盖尔、大西洋理事会、CFR、NIESR、OMFIF、牛津经济研究院、Project Syndicate、国会山报）；v2.4 七个（兰德公司 sitemap、渣打银行、白宫——唯一直连可用、胡佛研究所、EIU、IMD、皮尤研究中心）；v2.6 十一个**国外权威金融媒体**（WSJ 市场 Dow Jones 官方公开 RSS、MarketWatch、经济学人 财经/社论、财富、半岛电视台、BBC 商业、卫报 商业、FT中文网、Google News 聚合·金融稳定关键词、路透（谷歌聚合替代，官方 RSS 已停用））。均需代理（白宫/MarketWatch/WSJ 例外可试直连）。默认不采集，点"＋ 加入常用"即纳入。
- **自定义信源**：AI 添加信源/AI 搜索抓取虚拟源（ai_search）固定归入此组（卡片仅在本页签显示，含"AI 搜索引擎抓取"兜底表单）；
- **异常信源**（v2.6）：最近一次连通检测异常（last_fetch_at 非空且 last_fetch_ok=0）的信源跨组汇集展示，保留普通功能并可**永久删除**（`DELETE /api/sources/{key}`；预置信源的 key 记入 `settings.excluded_preset_keys`，启动装载跳过——删除不会因 yaml 重载而复活）。

书签中**验证未通过未收录**（2026-10-26 全局代理环境二次复核确认：Cloudflare 403 连 sitemap 也拒——数据中心出口 IP 被高频防护安全策略拦截；非网络路径问题，直连 FSB/BIS 同时段正常）：OECD、WEF、WTO（页面 200 但正文 JS 壳层）、FATF、ADB、IMF 博客、布鲁金斯、PIIE、卡内基、汇丰、PwC、KPMG、The Banker、AP、DW、美财政部（sitmap 曾可用但标题为文件编号影响筛选）、桥水、欧亚集团、USCC、科尔尼、BCG、欧睿、世界大型企业联合会、UBS、德银、瑞穗、惠誉、AEI、CSIS、CEPR。待网络环境变化后可用「AI 添加信源」重试。加拿大银行 `/feed/` 为未来日程（事件日历），不适用；CSIS/CEPR feed 严重过期。

## 7. AI 模块规则

**提示词双层结构（v2.3→v2.6 六模块）**：AI（筛选/编译/评分与热点/翻译/**摘要/搜索确认**）统一支持"**整体替换**（`custom_*` 字段，空=内置）+ **附加**（`*_extra` 字段，拼到提示词末尾）"；**输出 JSON 契约由程序固定拼接**（筛选评分与输出格式段、翻译 translations 行、摘要 summary 行、搜索确认 picked 契约行），自定义替换不破坏结构化解析。设置页提示词卡含六个折叠模块，各带"查看内置默认"与"一键恢复默认"。

- **client.chat_json**：`response_format=json_object` + 失败容错（剥代码栅栏、首个平衡大括号）；**v2.8 重试增强**——重试次数取设置 `ai_retry_max`（默认 3，可 0-6）；超时/断联/5xx/**429/空内容**按指数退避重试（3s→6s→12s→24s…，单次等待上限 60s；429 先等 5×(attempt+1)）；每次重试写 `run_logs`（ai）；**4xx（密钥/参数错误，除 429）不重试**；所有调用走 settings 的 base_url/key。
- **token 记账**：每次成功调用按 usage 写 `token_usage`（今日/30 天表在设置 API 卡）；**v2.8 试用跟踪**——当前 Key 为试用 Key 时同步累计 `trial_used_in/out`（见下）。
- **筛选**（screen.py）：JUDGE_PROMPT（判定规则+`{criteria}` 六项要求，`custom_screen_criteria` 整体替换）+ OUTPUT_SPEC（评分规则+输出格式，恒定拼接）+ `screen_extra` 附加；输出 criteria 限定 1-6 整数；三维评分（relevance/authority/completeness）+ 系统算 timeliness 按权重合成总分（`score_custom_rules` 追加评分细则）。
- **热点方向**（hotspots.py）：`analyze_hotspots()` 取时间窗内 matched 线索（不足 3 条报错提示），送模型输出 `{directions:[{rank,name,description,evidence_ids,count,trend,score}]}`；后端把 evidence_ids 编号映射回真实线索 id 存 `evidence_item_ids`；`hotspot_extra` 附加。保留最近 10 次。
- **编译**（compile.py）：SYSTEM_PROMPT 规定公文体/第三人称/编者按≤300 字/正文≥2000 字/JSON 结构；`custom_compile_prompt` 整体替换 + `compile_extra` 附加；`sufficient=false` → 状态 abandoned + 原因。
- **翻译**（translate.py）：15 条/次，中文占比>30% 的标题跳过；`custom_translate_prompt` 整体替换（契约行自动补回）+ `translate_extra` 附加；模型用 `model_translate`（空则筛选模型）；**auto_translate 默认开**（存量库一次性迁移置 1，之后用户可关）。
- **综合专报编译**（multi.py，v2.8）：`compile_multi_items(item_ids)`，2-5 条（超限前置拒绝）；材料抓原文（约 14000 字/条截断），合计 <1600 字符 → abandoned；SYSTEM_PROMPT 规定**主题式正标题**（不写"机构："前缀）、单篇同源的公文体约束、正文 2200-3200 字、以及**脚注纪律**：引用具体观点/数据处在该句末插 `[[citeK]]`（K=材料 1..N）、每条材料至少引用一次、全篇 3-10 个、标记不进标题、**不自写脚注文本**；`multi_extra` 附加 / `custom_multi_prompt` 整体替换（提示词卡⑧）。**脚注文本程序化生成**（`_footnote_text`：`  机构中文名《译名或原题》，日期。`），AI 只落标记不产文本，防编造出处。
- **AI 对话式修改**（edit.py，v2.8）：`ai_edit_report(kind, report, instruction)`；SYSTEM_PROMPT：只改内容、公文体、未涉及部分原样保留、小节删除后重排编号、不虚构、**排版类指令不改并在 reply 说明"导出按模板"**、`[[citeN]]` 标记位置语义保留；输出完整 JSON（title/editor_note/sections/conclusion/reply）；`custom_edit_prompt` 整体替换 + `edit_extra` 附加（提示词卡⑦）。修改成功由 server `_apply_ai_edit` 即时落库（前端有撤销栈）。
- **试用体验**（trial_config.py + client + server，v2.8）：内置公共测试 Key（**用户明确授权内置**，源自《大模型API测试.txt》）。未填 Key 时设置页一键「启用内置试用额度」→ 写入 TRIAL_BASE_URL/TRIAL_KEY/默认模型；`/api/trial/status` 返回 {used, limit(1,000,000), remaining, percent, active}；用量在 `_record_usage` 按 Key 匹配累计 `trial_used_in/out`；`/api/trial/start` 在额度用尽（≥100 万）时拒绝开启。**开发/交付纪律不变：交付目录 data\ 交付前必须删除**。
- **搜索引擎抓取**（ai_search.py，v2.6）：针对无 RSS/被防护站。`search_clues(keywords, engine)`：SERP 三引擎链 DuckDuckGo HTML→必应→百度（auto 逐个切换，单引擎空结果自动下一个，即"失效兜底"）→ 抓原文页（extract_date_from_page 补日期）→ AI 按内置/自定义确认提示词甄别（输出 picked 契约：picked=[{no,criteria,brief}]）→ 入库虚拟信源 ai_search（src_group=custom, enabled=0, in_common=0，不参与站点采集）。入口：信源管理自定义组"AI 搜索引擎抓取"表单；进度见任务中心（stage=search，ai 组），支持打断（未入库即无回滚负担）。`custom_search_confirm_prompt` 整体替换 + `search_confirm_extra` 附加。
- **摘要**（summarize.py，v2.5 令提示词可定制）：单条点击触发；120-200 字一整段；`custom_summarize_prompt` 整体替换（契约行自动补回）+ `summarize_extra` 附加；结果存 `items.ai_summary` 缓存复用；失败在任务中心留 error 记录。
- **信源分析**（source_analyzer.py）：探测 RSS 候选 → 有可用 RSS 优先；否则 AI 依链接样本设计 html/sitemap 配置 → 30 天窗验证 → 失败带错误二次修正一次 → 仍失败停用并注明（固定 src_group=custom）。AI 检查修复保留原组归属。
- 提示词默认值可经 `/api/prompt_defaults` 获取（设置页"填入内置默认查看"）。

## 8. 专报模板排版规范（docx_builder.py）

以《参考模板_永州金融稳定专报第XX期.doc》**转 PDF 实测**为准（2026-10-24 用户修订版）：

| 元素 | 参数 |
|---|---|
| 页面 | A4；上下 2.54cm；左右 3.18cm |
| 刊头 | 华文新魏 **50pt 红色**（一行；实测 53.7pt 会折行，8 字版心内上限约 51pt） |
| 期号 | 楷体_GB2312 15pt 居中 |
| 单位与日期行 | 楷体_GB2312 14pt，**制表位两端分列**（右制表位 14.64cm） |
| 分隔线 | 宋体 15pt **红色**，内容 `———————————— ★————————————`（**★ 前后各 12 个横杠，对称**，2026-10-24 模板修订） |
| 正标题 | 黑体 18pt 居中 |
| 编者按 | "编者按："黑体 16pt + 仿宋_GB2312 16pt，首行缩进 2 字符 |
| 一级/二级标题 | 黑体 16pt / 楷体_GB2312 16pt，**首行缩进 2 字符** |
| 正文 | 仿宋_GB2312 16pt，固定行距 28 磅（lineRule=exact, line=560），首行缩进 2 字符（firstLineChars=200），**两端对齐（jc=both）** |
| 署名 | （姓名）仿宋_GB2312 16pt 右对齐 |
| 字体关键 | 所有 run 的 rFonts 显式设 ascii/hAnsi/eastAsia 且 **w:hint="eastAsia"**（否则中文标点按西文窄字形渲染） |

自检（selfcheck.py）：FATAL=正文/脚注 ASCII 双直引号、rFonts 缺 hint、脚注引用了不存在的 id；WARN=固定行距段数、刊头字体、编者按/期号缺失、未被正文引用的脚注、引用数与脚注数不一致。**v2.18.0 `check_docx(path, concise=)`：精简格式跳过刊头/期号/编者按检查**（这些元素本就不存在，此前每次精简导出都弹两条无意义 WARN）。**双引擎纪律**：排版级改动必须 Word（Word.Application）与 WPS（KWPS.Application）双 COM 导出 PDF 比对，PyMuPDF 渲染抽查后句柄立即关闭。

### 精简格式（export_style=concise，v2.18.0 规范）
- **不占期号**（issue_no 存 NULL，issue_counter 不前进）；无刊头/期号/单位日期行/**无任何分隔线**（v2.17.0 曾保留纯红线，用户要求一并去除），正文直接从标题开始；署名仍保留。
- **文件名＝导出日期(YYYYMMDD)+标题**，单篇与综合同规则（如 `20261107荷兰国际集团：….docx`）；目标重名自动追加"副本/副本2/副本3…"（`_unique_path`，防覆盖）。标准模式期号命名不变。
- 自定义专报（multi_reports）同样适用；settings.json 手改 export_style 即生效。

### 综合专报脚注（v2.8，格式严格跟随第 58 期模板实测）
| 元素 | 参数 |
|---|---|
| 正文引用 run | **v2.19.0 用户规范：字体固定 Times New Roman 三号（sz=32）上标** + `<w:footnoteReference w:id="N"/>`（**v2.20 起仅首次引用落真实引用**）；位置恒在紧邻标点（，。；：、)）**之前**——标记在标点后（`…线[[cite1]]。`）与在标点后书写（`…线。[[cite1]]`）两种情形输出均为 `…线[角标]。`（`_add_text_runs` Case A/Case B；无相邻标点原位插入） |
| footnotes.xml | 程序化构造并经 OPC 挂载（`/word/footnotes.xml` part + CONTENT_TYPE `...wordprocessingml.footnotes+xml` + document.xml.rels 关系）；每注段落 `spacing line=240 lineRule=auto + jc=both`；首 run 上标 `<w:footnoteRef/>`；次 run rFonts `hint=eastAsia ascii/hAnsi=Times New Roman eastAsia=宋体 + sz=18（9pt）`，文本 `  机构《材料名》，日期。`（两空格起头，模板原样） |
| 脚注 id | **从 0/1 起真实脚注，无 separator 项**（第 58 期模板原样；Word 可正常打开） |
| **同源去重（v2.20）** | **Word 不允许同一 footnoteReference w:id 出现多处（实测报"文件可能已经损坏"）**。v2.20 起同一线索**只在首次引用处建完整脚注**；后续再引用（含 citations 里同 URL 尾斜杠/大小写差异或同标题的重复登记，`_cite_canon`/`_new_note_state.alias` 归一）只插**静态上标序号文本**（=首次注号，`_cite_run(repeat=True)`，`_note_no` 返回 `(no, repeat)`），**不再新建页底脚注、也不再用"同注{n}。"新注**（用户反馈重复插注不美观）。静态序号与首次注号恒一致（导出一次性产物、注号按首现次序分配）。越界材料号（[[cite99]] 不在 citations）沿用 v2.13.5 行为：标记直接丢弃不建注 |
| 署名/文件名 | 综合专报：`（投稿：{sign_name}）`右对齐（**v2.20 去除"永州市分行"前缀，通用署名**）。**v2.20.1 文件名跟随刊头**：单篇/综合均为 `{masthead}第{X}期：{标题}.docx`（默认 XXXX专报第X期：…；不再硬编码"永州"——开源用户不适用且暴露单位名）；精简格式仍为 日期+标题 |

2026-11-02 双引擎实测：探针样本（4 引用 2 材料）与真实综合专报（21 引用 2 材料，含"同注"复引）在 Word 与 WPS 中均可打开并导出 PDF，页底脚注与角标渲染正常。

## 9. 设置参数表（data\settings.json，v2.9 分区结构）

设置双存储：SQLite 为准，`data\settings.json` 为**分区结构**镜像（v2.9 含 AI 提示词在内全部可改参数）。写出的文件形如：

```json
{
  "_目录": {"API与模型": "接口地址 / 密钥 / 各模型名 / AI 失败自动重试次数（0-6）", …},
  "API与模型": {"api_base_url": "…", "api_key": "", …},
  "提示词": {"custom_compile_prompt": "", "multi_extra": "", …}
}
```

**程序启动时文件优先**（递归读取：叶子即键，`_` 开头为元数据；v2.8 平铺写法同样兼容）；任一界面保存后文件即时同步。**允许手改**（文本编辑器改叶子值，保存后重启程序生效）；内部键不写入文件：`v23_translate_default_applied / v27_enabled_common_applied / v219_backfill_default_applied / last_auto_day / trial_used_in / trial_used_out / trial_active_hash / api_key / trial_backup_key`（**v2.20 起密钥类键绝不入镜像**，试用累计与迁移标记勿手改）。键的分区归属由 `_SETTINGS_GROUPS` 定义。**v2.20 密钥加密**：api_key 走 `db.set_secret/get_secret`（Windows DPAPI CryptProtectData 用户作用域，密文 `dpapi1:<base64>` 存 SQLite；settings.json 中永不出现，文件里的历史明文 Key 启动时自动收编加密并清出镜像）；`all_settings()` 与 `/api/settings` 均不返回明文（界面仅见掩码 `sk-xxx…xxxx`）；拷贝 data 到他机密文不可解（有意设计，重填即可）。

| 键 | 默认值 | 说明 |
|---|---|---|
| api_base_url | https://api.agnes-ai.cn/v1 | OpenAI 兼容接口地址（**试用模式激活期间被完全忽略**） |
| api_key | （空） | **v2.20 加密存储**（DPAPI）；界面留空/掩码值提交表示"不改 Key"；手改 settings.json 填明文仍可（启动收编加密）。交付目录 data\ 必删 |
| model_screen / model_compile | agnes-2.5-flash | 筛选模型（兼热点分析）/ 编译模型（**试用模式下不生效**——统一用试用模型） |
| model_translate | （空→用筛选模型） | 标题翻译模型（试用模式同样不生效） |
| trial_model | deepseek-v4.1-flash | **v2.20 语义**：试用模式**统一模型**（筛选/编译/翻译/摘要全部用它，不再区分任务，也不再同步改写筛选/编译模型设置；trial_config.TRIAL_MODELS 五选一） |
| auto_translate | "1" | 检索后自动翻译英文标题（v2.19 归入「检索后自动化」卡） |
| backfill_auto | "1" | **v2.17 起默认开**；v2.19 一次性迁移把旧版写入的存量 "0" 重置为 "1"（此后用户可关并保持） |
| auto_screen_after_fetch | "1" | **v2.19 新增**：检索完成后自动对本轮新增线索执行 AI 筛选（与「⚡ 一键筛选」同管线） |
| translate_bold | "1" | 译名加粗（关=常规字重） |
| proxy_enabled / proxy_url | "0" / "" | 全局代理开关与地址（http://127.0.0.1:7890） |
| window_days | "7" | 采集时间窗（天） |
| request_timeout | "20" | 单站超时（秒） |
| fetch_concurrency | "3" | 站点级并发（1-10） |
| ai_concurrency | "2" | AI 组并发（1-8，按 API 限流调低） |
| ai_retry_max | "3" | **v2.8** AI 失败自动重试次数（0-6） |
| auto_fetch_mode / auto_fetch_time / auto_fetch_interval_min | "off"/""/"180" | 自动检索：off / daily(HH:MM) / interval |
| export_dir | （空→程序目录\编译报告） | **v2.20** Word 导出目录：留空=程序运行目录下「编译报告」文件夹（不存在自动创建），可自定义 |
| masthead / org_line / sign_name | XXXX专报 / XXXX / XXX | **v2.20 默认值开源化**：刊头 / 单位（标签由"单位行"改"单位"）/ 署名（综合专报署名=「投稿：{sign_name}」） |
| issue_counter | "0" | 期号计数（导出成功+1；**手改有风险**，导出中不能动） |
| score_weight_relevance / timeliness / authority / completeness | 40/25/20/15 | 四维评分权重 |
| score_custom_rules | （空） | 评分细则附加（按钮在提示词卡③） |
| custom_screen_criteria / screen_extra | （空） | 筛选：整体替换 / 附加 |
| custom_compile_prompt / compile_extra | （空） | 编译：整体替换 / 附加 |
| custom_translate_prompt / translate_extra | （空） | 翻译：整体替换 / 附加 |
| custom_summarize_prompt / summarize_extra | （空） | 摘要：整体替换 / 附加 |
| custom_search_confirm_prompt / search_confirm_extra | （空） | AI 搜索确认：整体替换 / 附加 |
| custom_edit_prompt / edit_extra | （空） | **v2.8** AI 对话修改：整体替换 / 附加 |
| custom_multi_prompt / multi_extra | （空） | **v2.8** 综合专报编译：整体替换 / 附加 |
| hotspot_extra | （空） | 热点方向附加要求 |
| excluded_preset_keys | （空） | 用户删除过的预置信源 key（防复活，一般不手改） |
| ui_zoom / ui_font | "1" / "" | 界面缩放（0.9-1.2）/ 字体（空=系统默认） |

**手改注意**：合法定值为字符串；`*_prompt` 类替换键必须保留各内置输出 JSON 契约（契约行由程序拼接的部分可删，字段约定不可破坏——编译/修改/综合均要求 title/editor_note/sections/conclusion 等字段）。改坏单键不会崩库：程序按"缺省回退"处理，但可能使对应功能降级（如放弃编译）。

## 10. 前端结构（web/index.html）

- 布局：左侧栏（品牌"信息编译"+导航+**三段连体主题切换 ⚙自动/☀白天/☾夜间**——v2.4 改为分段控件样式（小字号、共享边框阴影），自动=跟随系统）+ 页面：内容（线索面板、热点方向）、专报管理；数据（**收藏夹**、线索数据库、信源管理、任务中心）；系统（设置、运行日志）。
- 线索面板：主色按钮"▶ 开始检索"与"⚡ 一键筛选"；**时间选项含"今日"（统计卡与列表同窗，v2.6）**；**统计卡按所选时间窗计数（v2.6：/api/stats?days，任务完成后即时刷新——修复"面板未更新/统计清零"观感）**；"符合筛选要求热点"卡——按分降序前 6 条可展开，**每行勾选框多选 + 顶部"编译选中（N）"按钮（v2.4，替代逐行编译按钮，已编行显示"已编"标签并禁选）**；"为未打分线索补评分"；feed 卡（日期分组、未读红点、☆ 收藏、**"AI 摘要"按钮——无摘要时点击触发生成，有则显示青底摘要块并变"重新摘要"**、翻译标题默认加粗）。
- 热点方向页：方向卡 + "相关线索"折叠列表（编译/复核/收藏）。
- 线索数据库页（v2.4）：月份下拉 + 筛选状态 + 关键词 + 分页（100/页），表列：日期/标题(译名+原文)/机构/筛选状态/评分/摘要有无。
- 收藏夹页：线索表（筛选状态、AI 筛选、编译/编辑专报、★ 取消）+ 专报表；各表一键清空。
- 信源管理页：四组页签（常用/参考/自定义/**异常**）；**异常页签跨组汇集 last_fetch_ok=0 的源，含"🗑 删除"永久删除（v2.6）**；**连通情况列（● 正常/异常 + 最近获取时间 + 条数/错误摘要，v2.4）**；**置顶列（📌 可多个，排序恒优先）**；**排序下拉（默认置顶优先 / 连通性异常在前 / 正常在前）**；常用切换；自定义组 AI 添加卡。
- 任务中心页（v2.6）：正在执行每行"⏹ 打断"按钮；正在执行（多行并行，spin 动画）+ 排队等待（每行"移除"）+ 任务历史（单删/一键清空）+ 检索历史。
- **专报管理页（v2.8 双页签）**：`单篇专报 / 综合专报` chips（各带计数角标）；综合行=期号/标题（融合 N 条·脚注 M 处·模型）/融合数/字数/状态/操作（人工编辑、AI 修改、导出、删除）；综合模式禁用批量导出勾选。单篇行按钮 `编辑复核`→`人工编辑` 并新增 `AI 修改`。
- **专报编辑器双通道（v2.8）**：弹窗顶部 `人工编辑 | AI 修改` chips 切换同一份内容——
  - 人工编辑=原表单（标题/编者按/小节编辑器/结论），综合专报提示保留 [[citeK]] 标记；
  - AI 修改=左栏结构化整体预览（标题、编者按、一/二级层级、`[K]` 上标角标、当页脚注预览、材料编号对照）+ 右栏对话流（消息气泡/输入框回车发送/“撤销上一次修改”逐步回滚，撤销与服务器 PUT 同步）；每次修改由后端即时落库，导出排版恒按模板。
- 线索多选综合入口（v2.8）：①线索面板底部批量条 `✦ 生成综合专报`（勾选≥2 显示）；②热点卡 `✦ 综合专报（N）`（与"编译选中"共用勾选）；③收藏夹线索表复选列 + `✦ 生成综合专报（N）`。三者均限 2-5 条并 confirm。
- 设置页（v2.8）：API 卡新增**试用体验区块**（未配 Key→启用按钮；启用后显示已用/剩余/百分比进度条，80% 起转橙）与**「获取自己的 API Key」五平台教程折叠**（DeepSeek/通义/Kimi/智谱/OpenAI：网址+四步流程+base URL 与模型名）；**AI 失败自动重试（0-6）**字段在采集卡（与并发同排）；设置页顶部注明 `data\settings.json` 参数文件；提示词卡新增⑦AI 修改与⑧综合专报编译两模块（查看内置/恢复默认）。
- **v2.9 编辑器浮窗**：#editorBg 由全屏遮罩改为 `.floatwin`（fixed 浮层、无遮罩；标题栏 mousedown 拖动，— 最小化 / □ 最大化还原 / ✕ 关闭），主界面在修改进行期间可正常操作其他模块；AI 对话栏加宽至 440px；顶部94px進度小窗（progressPill）**可点击直接跳任务中心**；专报表新增独立「原文」列（综合行显示 —）；AI Key 教程改版为「试用额度用尽时如何获取」（DeepSeek/通义/Agnes/智谱/MiniMax 五平台、网址可点击跳转、推荐模型与时价对齐）。
- 顶部进度条：多任务并显（第一个任务进度 +（+N 并行）+ 排队数）；完成提醒按任务类型 toast（前端 diff running→history；multi_compile 完成提示到「专报管理 → 综合专报」）。
- **v2.9.5 编辑器与列表**：人工编辑/AI 修改合并为带底色「编辑修改」按钮（默认进入 AI 对话界面，「人工编辑」chip 保留表单直改）；字数统计简为「全文合计 N 字」置于「原文核对」行右侧（两模式共用）；🗑 清空聊天与 ⇄ 对比同行，↩ 撤销新增恢复最近一次聊天清空/删轮的能力（chat_restore 全量回写）；编辑窗标题栏 A−/A＋ 字体缩放（zoom 0.7-1.5 持久）；[[citeK]] 说明行新增「🔍 引用校对」（开启后点击预览上标弹 #citePop：机构/中文标题/日期/导出脚注全文/原文摘要）；专报表按处理时间倒序 + 最右「修改时间」列；删除线索面板副标题与专报管理底注。
- 设置页：一键保存全部 + 分模块保存；**API 卡含 Token 用量（今日/30 天累计输入输出与调用次数，可清零）**；**自动检索卡含采集并发/AI 并发设置（按 API 限流调低，v2.6）**；AI 提示词卡含**第六模块"AI 搜索信源提示词"（v2.6）**；**界面外观卡（大小 90-120% 缩放 / 字体（微软雅黑宋体黑体楷体等线）/ 翻译标题加粗开关 / 一键恢复默认，启动即生效）**；代理独立卡；AI 提示词四折叠模块（③ 评分与热点含"查看内置评分与热点规则"只读区，v2.4）；其余同前。

## 11. API 一览（/api）

`GET /`；`POST /api/fetch|screen|compile|probe|translate|backfill|sources/ai_add|sources/ai_check|hotspots|summarize|search_clue`（后台任务）；**`POST /api/compile_multi`（v2.8：2-5 条，stage=multi_compile 入 ai 组排队）**；`GET /api/task`（{running,queued,history}）、`POST /api/task/interrupt/{id}`（v2.6 打断回滚）、`DELETE /api/task/queue/{id}`、`DELETE /api/task/{id}`、`POST /api/task/clear_history`；`GET /api/items?days&q&match&offset&limit`（days：0=全部/1=今日/N=最近N天；q 含译名）、`GET /api/archive`、`GET /api/item/{id}`、`POST /api/item/{id}/read`；`GET /api/stats?days`（v2.6 按时间窗）；`GET/POST /api/sources`、`POST /api/sources/{key}/toggle|common|pin|test`、**`DELETE /api/sources/{key}`（v2.6 永久删除）**、`POST /api/sources/reload`；`GET /api/reports`、`GET/PUT /api/report/{id}`、`POST /api/report/{id}/export`、`POST /api/reports/export`；**v2.8 综合专报：`GET /api/multireports`、`GET/PUT/DELETE /api/multireport/{id}`、`POST /api/multireport/{id}/export`**；**v2.8 AI 修改：`POST /api/report/{id}/ai_edit`、`POST /api/multireport/{id}/ai_edit`（{instruction}→{ok,report,reply}，同步调用即落库，前端撤销栈回退）**；**v2.9 流式：`POST /api/report/{id}/ai_edit_stream`、`POST /api/multireport/{id}/ai_edit_stream`（SSE：status/reasoning/content/retry/done/error，done 附完整 report 与最新 chat_log，成功即落库）**；**v2.9.4 聊天管理：`POST /api/report/{id}/chat_delete`、`POST /api/multireport/{mid}/chat_delete`（{index}→{ok,chat}）、`POST /api/report/{id}/chat_clear`、`POST /api/multireport/{mid}/chat_clear`（→{ok,chat:[]}）**；**v2.9.5 撤销恢复：`POST /api/report/{id}/chat_restore`、`/api/multireport/{mid}/chat_restore`（{chat:[…]} 全量回写 → {ok,chat}）**`GET/POST /api/hotspots`（GET 附 ref_items）；`GET/POST /api/favorites`、`POST /api/favorites/clear`；`GET/POST /api/settings`；`POST /api/test_api`；`GET /api/prompt_defaults`（v2.8 扩至八类：+edit_prompt/multi_prompt）＋提示词卡⑦⑧模块；**v2.19/v2.20 试用密匙：`GET /api/trial/status`（{used,limit,remaining,percent,active,model,models}）、`POST /api/trial/activate`（{key}→校验指纹→新密匙重置额度→记 trial_active_hash，**v2.20 起不再写 api_base_url/api_key/模型设置**）、`POST /api/trial/deactivate`（**v2.20 新增**：删 trial_active_hash 退出试用，自定义配置原样保留）、`POST /api/trial/model`（{model}）、`POST /api/custom_api/clear`（**v2.20 新增**：一键清除 api_base_url/api_key/三个模型名，不影响试用状态与额度；v2.16 的 `POST /api/trial_restore` 已随备份/还原机制移除）**；`GET /api/token_stats`、`POST /api/token_stats/reset`；`GET /api/logs`、`/api/stats`、`/api/fetch_runs`。（v2.13.4 的 `POST /api/trial/start` 已随密匙体系移除；`/api/ai_search_local*` 后端保留但 v2.19 起无 UI 入口。）

**v2.21 新增/重定义端点**：`POST /api/rsshub/heal`（**路由规则体检**：run_in_background("rsshub_heal"，归 net 组）逐源探测 RSSHub 路由、失效源自动迁移可用实例并改写 config+备注留痕）；`POST /api/sources/add_rss`（**RSS 订阅直连添加**：{url}→httpx 带代理实测拉取 → XML 头/feedparser 双验证 → key `rss_<净化域名>`、config `{"rss_url":..., "direct":1}`、默认停用不精选）；`POST /api/sources/{key}/category`（改精选分类，{category}≤20 字）；`POST /api/sources/{key}/common` 扩展 {in_common, category}（加入精选**必须带分类**）；`POST /api/task/queue/clear`（{group?} 清空排队任务，返回 cleared 数）；`POST /api/rsshub/upgrade` **重定义**为整包升级（Releases `rsshub_bundle_*.zip` → node.exe+dist/index.mjs 校验 → 整目录替换+备份回滚）。

## 12. 打包与发布流程

```bash
cd FinBrief   # 仓库根目录
.venv\Scripts\pyinstaller.exe build.spec --noconfirm --distpath dist --workpath build_tmp
# 交付到浅路径（用户要求）：
rm -rf ..\金融热点编译系统 && cp -r dist\金融热点编译系统 ..\金融热点编译系统
cp DEVELOPMENT.md README.md ..\金融热点编译系统\   # PyInstaller6 把 datas 放进 _internal，文档需手动拷到 exe 旁
rm -rf dist build_tmp            # 清理中间产物
# 删除交付目录中的 data\（不得携带开发数据/API key）
```
打包要点（build.spec）：collect_all("webview")；datas 收 web/ 与 app/fetcher/sources.yaml；excludes tkinter/numpy/pandas/PIL 等；console=False；icon=assets/icon.ico。**分发物中绝不含 API key**——密钥仅存用户本机 data\finbrief.db。

## 13. 已知限制与注意事项

- 大陆网络下 6 个站点需代理；RSSHub 公共实例不可用。
- agnes-2.0-flash 免费档 429 限流严重，默认模型 agnes-2.5-flash。
- AI 添加信源对脚本渲染站点成功率有限（验证失败即停用+注明）。
- sitemap 类源（MAS/IADI）URL 常无精确日期 → 依赖补全机制或人工核对。
- 自定义编译/修改/综合提示词必须保留 JSON 输出字段约定，否则结构化解析失败。
- **综合专报脚注纪律（v2.20 更新）**：同一来源多次引用只在首现处建完整脚注，后续落静态上标序号（Word 限制：同一脚注 id 不可多处引用；v2.20 已弃用"同注X。"新注）；改提示词时不可移除 [[citeK]] 约定。
- **密钥安全（v2.20）**：api_key 经 Windows DPAPI 加密存 SQLite，绝不写入 settings.json；密文绑定本机用户（换机不可解，重填即可）；上游试用密钥在 trial_config 中仅异或混淆（开源项目无法绝对保密，已知限制）。
- **试用密匙体系（v2.19）**：上游 a6api（api.a6api.vip/v1）；程序内嵌 1024 把 `FB-XXXX-XXXX-XXXX-XXXX` 密匙的 SHA-256 指纹（`app/trial_config.py`，明文台账在交付侧「编译系统试用API方案\试用密匙库.json」，由同目录《试用密匙管理工具.pyw》管理）。额度每把 100 万 tokens、**本机累计**（无中心服务器，同把密匙多机各自计额，分发按"一把一人一台"控制）；换新密匙重置额度，同把复激活沿用；超额由 `client._trial_gate` 拦截。换整批密匙＝管理工具生成新库→指纹段替换回 trial_config.py→重新打包。上游密钥在 trial_config 中仅异或混淆（开源项目无法绝对保密，已知限制）。
- 大陆网络下 docs.rsshub.app（Vercel）直连超时——路由目录浮窗左栏为此加了导航工具条与原因说明；右栏内置路由目录离线可用。新版官方 npm rsshub 包（≥2026-10）不再附带可运行 dist（仅 dist-lib SDK）——**v2.21「⤓ 自动下载并更新 RSSHub」按钮已重设计**：a)「🩺 路由规则体检」= 逐源探测、失效路由迁移到仍能输出该路由的实例（订阅地址自动改写+备注留痕，覆盖"上游网站改版"主诉求）；b)「⤓ 内置整包升级（Releases）」= 从本仓库 Releases 下载 `rsshub_bundle_*.zip` 完整运行包整目录换装（含 node.exe+最新路由集合；当前 Releases 尚未提供该资产时如实中止提示）；内置捆绑版不受影响。
- Windows COM 导出 PDF 校验时注意：PyMuPDF 句柄用完即关，否则锁文件导致下一次导出报"文件正被另一应用使用"。

## 14. 更新日志

| 版本 | 日期 | 内容 |
|---|---|---|
| v2.21.0 | 2026-11-10 | **7 项（用户反馈，本地版先改、未同步 GitHub）**：①**AI 筛选提速**：`screen_items` 改多批并行（`ThreadPoolExecutor`，并发=设置 `ai_concurrency` 封顶 6）；首试 `max_tokens` 4000→**8000**（根因：推理型模型思考占满 4000 → 首批"空内容"失败 → `bump_tokens` 翻倍重试，一批平白多耗十几秒——评分吞吐也随之翻倍）；锁保护 `error/screened_ids` 聚合，progress 逐未来回填；回滚语义不变。**补全日期提速**：`date_backfill.backfill_dates` 改 `ThreadPoolExecutor(fetch_concurrency)` 并行取页面（`TaskCancelled` → `reset_items_field` 语义保留）。②**全部信源=所有信源**：cnt 与渲染去掉"custom 且未精选排除"过滤（自定义/未精选/停用都在"全部"页出现），页签说明同步改写。③**RSSHub 更新机制重设计（npm dist 通道失效根治）**：a) **路由规则体检** `app/rsshub.route_heal`——逐源探测现实例拉取超时/非 2xx/无条目 → 并行试连候选实例（[原 base, rsshub_base, 内置本地, FALLBACK_BASES]），首个可拉通该路由者改写 `config.rss_url`+备注"v2.21 路由迁移 YYYY-MM-DD（原实例 X 失效）"，任务组 `rsshub_heal` 归 net、UI 按钮「🩺 路由规则体检」——覆盖用户真实诉求"上游网站格式变了路由规则也要跟着更新"；b) **整包升级重写**：原 npm tarball dist 通道废除（新版官方包无 dist），改为从本仓库 Releases 下载 `rsshub_bundle_*.zip` 完整运行包（定位 node.exe+dist/index.mjs 目录 → 整目录替换+`.bak` 备份+30×3s 存活轮询+失败回滚），当前 Releases 未提供该资产时如实中止提示；按钮改「⤓ 内置整包升级（Releases）」。④**RSS 订阅直连添加**：`POST /api/sources/add_rss {url}`——httpx 带代理/UA 实测拉取 → XML 头（`<?xml/<feed/<rss`）+feedparser 条目数双验证 → key=`rss_<净化域名>`、config `{"rss_url","direct":1}`、默认停用不精选（采集走 mode=rss 现成路径）；信源管理"自定义"页新增「RSS 订阅源直连添加」卡（测试并添加+footer 说明）。⑤**排队治理（用户报 13+ 条排队+打断常失败）**：根因=间隔判据用 `fetch_runs.started_at`（任务排队获取时陈旧）→ 每个 tick 误判"该跑了"连续入队；修为 `tasks.group_busy("fetch")`>0 即跳过+`last_auto_submit` 间隔下限；前端排队卡只显示前 5 条+计数条，新增「🗑 清空排队」（`POST /api/task/queue/clear`，可选 group）。⑥**精选信源分类**：`sources.category` 承载精选分组（默认 12 类 CAT_NAMES[cb_reg 央行与监管/intl 国际组织/cons 咨询评级投行/bank 银行与评级/market 市场与投资/tech 科技与AI/thinktank 智库/media 财经媒体/ins 保险养老/dep 存款保险/gov 政府机构/other 其他]），**加入精选必弹分类选择浮层**（chips+自定义输入新名=新建分类）；精选页按分类分组排序展示、行内/右键「改分类」；`db.update_source_flags` 修 `category` 被 `int()` 强转崩溃的隐藏 bug。⑦**sources.yaml +21 预置项**（用户库已加的精选源随下次同步开源，总 75 项无重复 key）。**回归**：`_tmp_test_v221.py` 19/19 通过（筛 8000 首试/并发在飞≥2/打断回滚/补全并发 24 条<3s/忙守卫不提交/空闲正常提交/近间隔防风暴/清空排队端点/加入精选带分类/改分类/直连 RSS 添加+direct/坏路由拒绝/非法地址拒绝/route_heal 迁移+留痕）；py_compile+AST 去重+node --check 全绿。 |
| v2.20.1 | 2026-11-07 | **6 项（用户反馈 3 + 体检发现 3）**：①**脚注归一误并根治（用户报"引用多篇只剩一个脚注"）**：multi/单篇编译落库的 citations 只有 `{no, item_id, ref}`（无 url/title），v2.20 `_cite_canon` 把空 url+空 title 的所有材料归成同键 `("","")` → 全部 alias 到第一条、导出仅一条脚注——现改为 url 优先、退标题、**都为空按材料号独立**（仅在确有同 URL/同标题时才合并）。②**测试连接支持试用通道**：请求未填任何自定义值时——已激活试用 → 测试用上游（统一试用模型）；未激活但自定义没配全 → 也退测试用上游；结果带 `channel` 字段，前端显示「试用通道/自定义通道」+ 按钮旁灰字说明。③**翻译"未返回内容"双根因**：a) 前端 SSE 帧解析分隔符写成字面 `\\n\\n`（JS 中=反斜杠+n 而非换行），流式翻译**自 v2.9.9 起从未收到过完成事件**（字节级定位）；b) `chat()`/`chat_stream()` 对"空内容/思考占满"的 AIError **直接抛出不重试**（bump_tokens 逻辑因此也是死代码）——空内容纳入重试、思考占满自动翻倍 max_tokens；流式翻译生成器取数段加 try 兜底（异常也走 error 事件，不再无声中断显示笼统"未返回内容"）；试用模式下翻译模型传空由 `_route` 统一走试用模型（不再误报"未配置模型"）。④**AI 自动中文名从未生效（体检发现）**：`/api/rsshub/ai_name` 传 `system=`（chat 无此形参 → TypeError 被静默吞）且首参传**裸字符串**当 messages——`chat()` 新增 `system` 形参 + 裸字符串兜底包成 user 消息，端到端验证 ai_used=True。⑤**导出文件名跟随刊头**（体检发现的设计/隐私问题，按推荐方案执行）：单篇/综合均 `{masthead}第{X}期：{标题}.docx`（默认 XXXX专报第X期；不再硬编码"永州"）；综合 citations 为空时 `state=None` 直接下标崩——加防御。⑥API 卡三模型输入下加灰字"试用激活期间以上模型设置不生效"。**体检记录**：九页面 Playwright 走查 13 项全过（真实 1323 条线索数据）零控制台错误零 API 4xx/5xx；端点探针（单篇/综合导出+selfcheck 无 FATAL、trial/token/rsshub 状态、设置/收藏往返、热点）全过；隐私专项——settings.json/DB/run_logs 无密钥明文、docx 元数据仅 python-docx 无个人信息、startup.log 仅端口/进程号；v2.20 全部回归（同 URL 去重、静态序号、DPAPI、试用双模式）通过。 |
| v2.20.0 | 2026-11-07 | **6 项（用户反馈）**：①**试用/自定义 API 双模式架构（根治"激活了密匙仍报未配置"）**：v2.19 激活把试用值写进共享的 api_base_url/api_key/模型设置，用户随后保存 API 卡即被清/覆盖而试用判定仍看旧字段——现改为**模式判定**：`trial_active_hash` 存在即试用模式，`client._route` 试用态恒走 TRIAL_BASE_URL+上游密钥+**统一试用模型**（不区分筛选/编译/翻译模型，trial_model 全任务生效，也不再同步改写模型设置），自定义态用设置+DPAPI 解密 Key；激活/停用**绝不读写**自定义 API 字段；新增 **`POST /api/trial/deactivate`（「✕ 停用试用，改用自定义 API」）与 `POST /api/custom_api/clear`（「🧹 一键清除自定义 API 配置」：清 api_base_url/api_key/三模型名，不影响试用）**；移除备份/还原机制（trial_restore 端点删除）。②**AI 智能线索分类灰字精简**为一句（"点击后由 AI 将当前全部线索自动归入主题分类（约 1-3 分钟，进度见任务中心）"）。③**脚注同源去重（用户报"同一篇线索重复插注"）**：`_note_no` 返回 `(注号, 是否重复)`——同一材料（含 citations 重复登记按 `_cite_canon` URL 去尾斜杠小写/标题归一 alias）只在**首次引用处建完整脚注**，后续只落**静态上标序号**（=首次注号；不再用"同注{n}。"新注——Word 禁同一 footnoteReference id 多处引用，静态序号导出一次性产物恒一致）。④**密钥加密体系（用户点名严重安全问题：key 明文写在 settings.json）**：`db.set_secret/get_secret` 用 **Windows DPAPI**（crypt32，用户作用域）加密，密文 `dpapi1:<base64>` 入库、坏/异地密文返回空提示重填；`all_settings()`/`apply_settings_file()` 均剔 api_key（**文件里的历史明文启动时收编加密并清出镜像**，上游试用键残留直接删除；v2.16 `trial_backup_key` 残留同清）；`/api/settings` 只回掩码 `sk-xxx…xxxx`（空值/掩码值提交不覆盖）；修复 `_dpapi_protect` 把结尾 NUL 一并加密的实现在测中发现。⑤**导出默认值开源化**：export_dir 留空=程序运行目录「编译报告」文件夹（自动创建，可自定义 `db.export_dir_setting`）；刊头默认 **XXXX专报**、"单位行"标签改**单位**默认 XXXX、署名默认 XXX（综合署名去"永州市分行"前缀=「投稿：{sign_name}」）。⑥**开源声明用途约束**按用户原文替换（"各渠道公开信息…不代表本开源项目或开发者的立场与观点"）。**验证**：DPAPI 22 断言 + **真实 data 目录升级演练**（明文 api_key/trial_backup_key 全清除、业务设置原样保留）12 断言；脚注去重 10 XML 断言 + Word/WPS 双 COM 导出 PDF + PyMuPDF 渲染（全文恰现 2 次=两独立来源、静态序号落位正确）10 断言 + multi 冒烟；试用双模式 35 断言（激活不动自定义、统一模型、额度门、掩码通道、清除、停用回切）；Playwright UI 19 断言；v2.19 迁移回归；node --check。 |
| v2.19.0 | 2026-11-07 | **9 项（用户反馈）**：①**脚注引用符号规范**：位置恒在紧邻标点（，。；：、)）**之前**（`[[cite]]` 写在标点后/前两种情形输出归一为 `…线[角标]。`，`_add_text_runs` Case A/Case B 重写）；字体固定 **Times New Roman 三号上标**（run 级 TNR + sz=32；脚注正文仍为模板规格 宋体/TNR 9pt）。②**设置页「检索后自动化」卡**：自动补全缺失日期（默认开——v2.17 的默认开只兜底"键不存在"，存量 "0" 由 `v219_backfill_default_applied` 一次性迁移纠正；**迁移三坑实测**：须在 apply_settings_file 之后执行、标记键须加入 `_SETTINGS_FILE_INTERNAL` 排除镜像（否则 sync 写进 settings.json、下次启动又灌回库）、实际写入须在大事务提交后用独立连接执行（否则 sync 读不到未提交值把 0 写回文件））+ 自动标题翻译 + **自动 AI 筛选（新开关，默认开可关）** 三项归拢；检索任务完成后对**本轮新增线索**自动执行筛选（screen_items 同管线，不重复消耗历史线索额度），fetch 完成提示与列表刷新带上筛选结果。③**路由目录浮窗左栏**：docs.rsshub.app 实测大陆直连超时（Vercel，非程序故障）——加 ←/→/⟳ 工具条 + 地址栏"转到"（跨域 iframe 读不到其 history，后退/前进基于程序侧导航栈）+ 失败原因说明条（右栏内置目录离线可用；右上「🌐 浏览器打开」配合代理）。④**任务历史**「详情/失败明细」合并为一个「详情」（错误行→showTaskFailById 直达失败明细卡）。⑤**试用密匙体系（大改）**：上游切 a6api（`api.a6api.vip/v1`，实测 151 模型、5 个试用模型全可用；上游密钥异或混淆存放）；程序内嵌 **1024 把 `FB-XXXX-XXXX-XXXX-XXXX` 密匙的 SHA-256 指纹**（明文台账＝交付侧「编译系统试用API方案\试用密匙库.json」，配套 GUI《试用密匙管理工具.pyw》：搜索/复制/标状态/备注/导出未分发清单/生成新库+配套指纹段）；设置页试用卡改「粘贴密匙→激活」（校验指纹→备份自定义 API→**新密匙重置 100 万 tokens、同把沿用**→切试用；试用模型 deepseek-v4.1-flash 默认 / qwen3.8-flash / gpt-6-luna / glm-5.3-flash / hy3 下拉可切，试用态同步筛选/编译模型）；额度用尽 `client._trial_gate` 拦截调用并提示换新密匙；**修复 v2.16 隐患：试用备份值存了布尔 True（str(True)="True"），一键还原会把 api_key 写成 "True"**——改存密匙原文 + restore 脏值防御；移除免密钥的 `POST /api/trial/start`。⑥**专报管理排序**：默认「按添加顺序」（added），可选倒序/修改时间/期号/标题，**移除状态排序**。⑦**自定义信源页签**：已加入精选（in_common）的源不再显示在自定义页签（计数同步；加入提示改"加入后移入精选页"）。⑧**线索面板「✦ AI 检索」按钮与前端状态机整体移除**（AI_SEARCH_*/aiSearchLocal/exitAiSearch/aiSearchBar 与轮询回调；后端 /api/ai_search_local* 保留但无 UI 入口）。⑨**RSSHub 自动升级修复**：httpx `follow_redirects=True`（npmmirror tgz 302→CDN 此前直接判失败）；包结构探测——新版官方 npm 包只带 dist-lib 无预构建 dist 时**如实中止并说明**（附包内顶层清单），内置捆绑版不受影响；dist 缺 index.mjs 的报错文案同步明确。**验证**：试用体系 7 场景 TestClient 隔离库全过；导出 3 格式自检 0 问题 + XML 有序 token 断言 + 注号唯一/无孤儿；Word/WPS 双 COM 导出 PDF 页数正常；PyMuPDF 渲染抽查（引用=10.6pt TNR 上标、位于句号前）；Playwright UI 27 项断言全过；AST 重复定义 0。 |
| v2.18.0 | 2026-11-07 | **6 项（用户反馈 + AST 军规排查）**：①**启动提速**：`webview.start` 改 `private_mode=False` + `storage_path=data\webview`（原默认 InPrivate 每次启动重建临时 WebView2 profile 是"打开卡 1-2 秒"主因；实测加载页显示 2.19s→热启动 1.46s），本地静态服务端口随机化防多开撞车，main.py 新增"加载页已显示"观测日志；附带 localStorage（主题/排序记忆）重启保留。②**外观切换回左栏左下角**：还原为左栏底部分段控件（`margin-top:auto` 吸底，样式同 v2.17.0 前），并**修复 v2.17.0 `.theme-toggle` CSS 规则截断**（`}` 提前闭合丢 border/radius，控件因此变丑）。③**精简格式导出规范**：a) 文件名=导出日期(YYYYMMDD)+标题（单篇/综合同规则，不再带"精简报告：""永州专报（精简）："前缀）；b) 重名自动追加"副本/副本2"；c) 分隔线全部去除（★线与 v2.17.0 的纯红线都不再加）；d) selfcheck 增加 `concise` 参数跳过刊头/期号/编者按检查，精简导出提示不再出现 WARN。④**脚注排版修复**：还原 v2.17.0 误改的 Times New Roman 三号（sz=32）为模板规格 宋体/Times New Roman 9pt（sz=18）；引用标点 run 恢复所在段落中文字体；**修复 v2.17.0 `_note_no` 首引孤儿脚注回归**（每份材料多登记一条从未被正文引用的注、脚注编号从 2 起算、自检报 WARN）；Word/WPS 双 COM 实测脚注均 9.0pt。⑤**自定义专报**：弹窗 760px 宽 + max-height 自适应（撤销 v2.17.0"拉高 30%"造成的下方大片空白）；新增「＋ 粘贴文本」材料类型（前端 `custTextEntry` + 后端 `type=text` intake，≥30 字成材料，与链接/上传共用 6 份上限，补充材料重编同样支持）。⑥**潜在问题修复（AST 军规检查发现）**：`db.delete_items` 被 v2.13.6 新版同名遮蔽——v2.6 打断回滚版改名 `rollback_delete_items`（此前回滚日志打出原始 dict、且"拒绝已编译线索"逻辑被错误带入回滚路径）。 |
| v2.17.0 | 2026-11-07 | **v2.17.0 专项升级与全量测试 Bug 修复（14项）**：①**外观切换优化**：白天/夜间/自动切换栏移至窗口右下角固定悬浮，不再挤占左侧导航。②**精简报告与脚注规范升级**：a) 精简专报标题上方红色五星分隔线彻底去除，纯红线；b) 导出文件名统一规范为《精简报告：标题》；c) 综合专报脚注同一材料多次引用不再插入重复的’同注...’，每条材料仅保留首次完整注；d) 正文引用序号严格移至句子末尾标点（逗号/句号/分号等）之前；e) 脚注字体排版规范为 Times New Roman 三号（Word 深度兼容且经 COM 真实打开验证）。③**opencode API 400 终极根治**：客户端自动向 `opencode.ai` 请求注入稳定持久的 `x-opencode-session` 会话头，彻底去除前端需手动填写的扩展请求头负担，用户无需任何额外配置直接可用。④**自定义专报弹窗空间优化**：高度纵向空间拉长 30%，窗口比例更加舒适平衡。⑤**日期补全默认勾选**：设置项中’检索后自动补全缺失日期’默认高亮勾选，无需用户手动开启。⑥**信源管理深度改进**：a) 表格添加 sticky 吸顶样式，向下滑动时’启用’等列标题不再被遮挡；b) 排序下拉中新增’添加顺序（新→旧）’选项；c) AI 添加信源完成后自动切换至’自定义信源’页签并高亮显示；d) AI 添加失败时细分给出反爬拦截（403/503）、人机验证、网络不可达等具体原因提示。⑦**测试清单 Bug 1~8 全量清零**：Bug 1（精简导出第None期文件名及null期toast修复）；Bug 2（多专报导出期号与 issue_no 持久化确认）；Bug 3（收藏夹勾选少于2条点击友好提示并控制按钮显示）；Bug 4（AI 摘要不再仅 9 秒 toast，热点行内展开常驻展示并支持一键复制）；Bug 5（’⇄ 对比’无修改历史时弹出友好引导 toast）；Bug 6（’今日’等时间窗统计数据与线索列表过滤口径对齐，剔除合成材料干扰）；Bug 7（AI 检索结果各处数字统一口径）；Bug 8（网页链接正文抓取优先提取真实标题，杜绝哈希乱码标题）。 |
| v2.16.0 | 2026-11-07 | **15 项**（用户反馈）：①自定义专报 modal 宽 900px/高 min(94vh,1000px)。②重复「补全日期」checkbox 删（v2.15.0 插重）。③信源 pt-sub 灰字删。④**热点展开态跨渲染保留**：HS_REFS_OPEN Set 记忆展开的方向 idx——renderHotspot 重绘后照旧展开（原勾选线索触发重绘把展开栏收起）。⑤**综合脚注两 bug 同修**：a) footnotes.xml 缺 ECMA-376 要求的 separator(-1)/continuationSeparator(0) 系统条目——Word 打开时引用全失效（用户“导出 Word 没有脚注”根因）；b) _DIRTY_MARK 正则 [【\[] 会把 [[cite1]] 的内层 [cite1] 当脏标记误清——**合法标记全被吃掉**（第二个根因；加 (?<!\[)…(?!\]) 边界）。端到端实证：mid=11 导出 in-text ref 顺序 1→14 严格递增、entries 16（14 注+2 系统）、selfcheck 0 问题。⑤b 编辑预览上标改按文中出现顺序编号（seqMap 扫描全文；data-k 仍存材料号供还原）。⑥**精简格式对综合/自定义生效**（v2.13.5 只挂单篇）：build_docx_multi 读 export_style——精简=无刊头/期号、文件名“永州专报（精简）：”、不占期号；真跑验证。⑦扩展请求头 UI 撤（opencode 自动会话头已接管；api_extra_headers 键保留兼容手改 settings.json）。⑧热点前 5 截断/展开按钮撤（全部方向直接展示）。⑨**AI 检索语义化重写**：_ai_relevance_pick 把窗口内标题清单（≤400、250/批）直接交给 AI 判强相关（JSON ids 数组，max_tokens 12000），AI 失败退化原扩展词 LIKE 链；真跑 dev 无窗口数据安全路径 ok。⑩README 全量重写（只讲当前版本、无程序位置示例、无版本史）；web/README.md 同步（help 浮窗数据源）。⑪side-foot 段删 + SRC_GROUP_NOTE 四句精简。⑫**试用/自定义 API 并存切换**：api_trial_start 允许已配 Key 时启用试用（trial_backup_* 备份现值）+新端点 POST /api/trial_restore 一键还原；前端 loadTrial 撤自购 Key 隐藏拦截+「↩ 还原自定义 API」按钮（trial_backup_key 存在时显）。⑬**自定义材料编译去脚注**（compile_no_evidence）：citations 置空+正文 [[citeK]] 全清（材料无逐条摘录无法核对；补充材料重编保留 DB 引用供校对——单篇导出本就不插）。⑭**全部 modal 可拖动**：enableModalDrag 对 .modal-bg 的 .modal>h3 绑 mousedown 位移（diffBg/taskFailBg/srcDetailBg/catBg/suppBg/customBg/taskLogBg 七窗全覆盖；排除 button/a/input 等控件）。⑮全模块真链试用：综合编译（AI 真跑成稿 2 脚注）→导出→自检→精简→清理；搜抓 auto 真跑（必应 10 条→AI 0 相关如实 message）；**P2 教训二次确认：patch 分段绝不能用相邻 "@app." 边界** |
| v2.15.0 | 2026-11-06 | **19 项**（用户反馈）：①**opencode API 接入**：cli.chat/chat_stream 请求头合成 _extra_headers——a) 新设置 api_extra_headers（JSON 键值，设置-API卡输入框，保存/回填走 loadSettings，分区表登记 API与模型 组）；b) **opencode.ai base_url 自动生成并记忆 x-opencode-session**（uuid→settings opencode_session_id，要求＝每对话稳定会话标识/官方 docs 实测不提供正式值规则，程序端自动生成即可）——不影响其它服务商（仅 base matching 时注入）。②**一键编译免确认**：btnCompile/compileMatched 规模 confirm 删除（数据覆盖保护 confirm 保留）。③**失败明细直达**：tasks.py ok:false 分支 **rec["error"] 同步 reason**（原仅 progress——toast"失败："空文案根因）；新 #taskFailBg 弹窗（showFailModal：fail/failures/errors 数组+reason 总述+「查看完整任务日志」）；taskToast error 分支自动弹一次（FAIL_SHOWN 会话去重）；任务历史失败行新「失败明细」按钮。④**专报管理**：刷新按钮删、新增检索框（标题/机构/期号/主臂 sources_info）与排序（修改时间 双向/期号 双向/状态/标题）；AI 翻译失败复案=真跑测试 a6api+deepseek-v4.1-flash paragraph 重排生效（prompt 双端改允许语义分段）。⑤**热点**：默认仅前 5 名+「展开其余」按钮（HS_TOPN_expand）；相关线索行加复选（selHsMulti）+多选≥2 条出现「编译综合专报」按钮（compileHsMulti≤5）；极左规则满。⑥**AI 检索改后台任务**：原同步 10-40s 无 UI 反馈错认无反应——POST ai_search_local→run_in_background（ai_search_local 组/ai）；settings ai_search_result_v2 结果缓存+GET result 端点；任务 done 轮询自动加载渲染。⑦**已放弃行标题可跳原文**（r.item_url 链接同款）。⑧**引用校对译文段落**：/api/translate_text{,_stream} 双 prompt 改允许语义分段（长段自动裂、段落空行）；实测返回三段多行。⑩AI 添加/搜抓灰字精简。⑪路由按钮加 teal 底色+文字简化+灰字删。⑫⑬**rsshub_upgrade/engines 空结果 db.log 明细化**（升级三失败分支/collect_serp 引擎空结果均落痕；真跑一单引擎全失败例：error/progress 明确文字到位）。⑭清空按钮去“需两次确认”。⑮**补全日期自动化**：面板按钮删（onclick 绑定端兼容清理）、新设置 backfill_auto（默认开）控检索后自动补全；_job_fetch 参照 auto_translate 逻辑读取。⑯自定义专报 modal 760→960px。⑰AI 对话发送按钮 46→34px。⑫b README「程序位置」行删除。⑲**设置尾部开源声明卡**（aboutCard）：开源/致谢（RSSHub MIT 等）/个人学习用途约束/合规提示/使用即同意效力条款。**P2 合并事故与恢复**：seg 截取误吞 ArchiveClassifyReq+_job_archive_classify 与原同步 ai_search_local 端点——NameError server 起服失败并通过及补全重建（含 month 语义)；**教训：python patch 用 find("@app.")作分段边界会把相邻 class/def 一起删——先精确串扫 assert target 段不覆盖非预期 node**。**联调**：playwright 16/16 全绿+0 JS error；真调翻译/搜抓/升级失败行为经 dev 环境（注入交付 a6api key）实证 |
| v2.14.0 | 2026-11-06 | 2 项（用户反馈）：①**AI 智能线索分类瘦身**：主题方块流改**胶囊标签流**（flex chip 自适应宽+白底渐变胶囊、圆角 999px、gap 8px）——每项只展示**主题名+N 条徽章**，删除「符合/不符/未筛」行、前 3 条标题预览与「展示前 40 条」尾注（点击标签依旧弹 #catBg 全线索表、拖拽排序+aiCatOrder 记忆保留）；实测每行排满至容器右缘（换行残余 139px≈11%，对照旧版固定 224px 三列卡右侧常年 1/3 空白）；说明文案「方块」→「标签」。②**自定义信源固定添加顺序**：srcTabs==='custom' 页签**跳过整体排序块**——保持后端 list_sources ORDER BY sort_no,id 的插入序（sort_no=0 即 id 升序）；localStorage.srcSort（含连通性/启用排序与置顶次键）不再影响自定义页签；检索框过滤保留；其他页签排序行为完全不变（回归断言 fail_first 前三=异常源精确匹配）。**联调**：playwright 9/9 全绿+0 JS error（chip 34px 高/无预览字样/拖拽保留、custom 页签键序与后端 id 序精确一致+排序设置前后不变、mock 走 route.fulfill 拦截——**let CAT_DATA 不挂 window，evaluate 打不进词法作用域，直接 mock 必须走网络层拦截**） |
| v2.13.9 | 2026-11-06 | 3 项（用户反馈）：①**一键清空所有数据卡降级下沉**：从设置页顶部大红卡移到设置模块**最底部**（grid2 闭合与 page-settings 收尾之间），改 .footer-note 12px 小字行+ghost 小按钮（宽 222px vs 原 831px 大卡），文案压缩为一行+补″含已导出专报不可恢复，慎用″。②**路由目录浮窗四改**：a) 左栏 46%→**65%**（文档阅读更宽）；b) 分类选中态由深蓝填充改**浅橄榄底 rgba(120,140,90,.14)+加粗**（财经金融不再特地标蓝）；c) 标题栏新加 **🌐 浏览器打开** 按钮（window.open docs.rsshub.app——iframe 内官方搜索交互受限时的直通出口）；d) **路由行右键试连**：oncontextmenu→routeTest(event,path)→POST /api/rsshub/test（probe_path v2.13.9 返回体补 sample 最近 6 条标题样本；空 path 守卫 ok:false″路由路径为空″——此前空 path 会算成试连根 url 得 ok:true 0 条的假成功）→#testDetail 浮层展示✓/✗+完整 url+条数/耗时+样本逐条标题日期+「＋添加为信源」（prompt 中文名→/api/rsshub/add，默认停用）+「关闭」；ROUTE_TEST_TOKEN.n 令牌防连点乱序回填。e) **中文别名模糊搜索**：ROUTE_ZH_ALIASES 36 条（彭博/路透/财联社/凤凰/卫报/香港01…）+routeExpandQ 前缀递减命中别名→搜索词并列 [中文词, 官方 ns]/OR 匹配（ns 名/路由名/路径三路）；实测「彭博」→bloomberg、「财联社」→cls 独占。③**已编译线索再次编译确认**：原 confirm 依赖 REPORTS 快照（收藏夹场景未载入即无提示）——快查不到时补 **GET /api/has_report/{item_id}**（db.get_report_by_item，has/status/title/exported）兜底，确认文案含成品名+覆盖提醒+已导出注记；**注：save_report 为 ON CONFLICT(item_id) 覆盖旧稿**——重复编译=替换该线索现有专报。**联调**：playwright 15/15 全绿（清空卡位置 rel=20px/宽 222px、左栏 714/1100、真跑试连格隆汇 12 条/5.7s+样本 6 条、双语别名命中、假 id has:false、compileItem 含兜底调用），页面 0 JS error |
| v2.13.8 | 2026-11-06 | 6 项（用户反馈）：①**失败信源检索重试**：设置采集卡新增 fetch_failed_retry select（默认关/1-3 轮）→ _job_fetch 读设置，result 后按 fails[key] 重跑 run_fetch 收敛，added_ids set 合并防重。②**⤓自动更新 RSSHub NameError 修复**：升级函数 tempfile 以 _tmpf 别名导入但调用处写 tempfile.TemporaryDirectory()→改 _tmpf.TemporaryDirectory()。③**AI 搜索引擎加谷歌且首选**：ai_search.py 新 _serp_google（google.com/search HTML 端点+CONSENT cookie），ENGINES/ENGINE_NAME 注册，auto 兜底顺序=谷歌→DuckDuckGo→必应→百度；前端 searchEngine 加谷歌选项置 ddg 前+说明文字同步。④**一键清空所有数据**：db.reset_all_content()（screenings/favorites/hotspots/items/multi_reports/reports/fetch_runs/run_logs/task_runs/token_usage+settings 全清+custom_uploads 目录清，sources 表保留）+POST /api/reset_all（sources 空则回补预置）+设置页红卡「🗑一键清空所有数据（需两次确认）」双 confirm；**连 data\settings.json 一并删除**（v2.8 外部编辑优先回填会让旧设置重启“复活”，否则破坏出厂承诺）。⑤**路由目录浮窗**：路由库 h3 括号说明+灰字删除，旧『去哪里找路由』行删除时**JS 顶层 $("#rsshubDocRow").onclick null 绑定再次致整 boot 中断（pageerror 栈抓到，v2.12.1 同型教训）**——死绑定随之清除；新「📂挑选路由」按钮+#routeBg 非模态浮窗（拖动/最大化/最小化 dock）：左 iframe docs.rsshub.app、右=内置目录（web/route_catalog.json 389KB：扫 rsshub_bundle 得 1912 命名空间/3493 路由、24 官方分类中文映射、★流行在前、ns 名/名称/路径三路搜索、一键复制 rssforever 实例完整订阅链接、超 200 行截断提示缩小范围）+GET /route_catalog.json 静态端点；onkeyup 过滤经强制渲染与真键盘双路径实测正确。⑥**收藏夹编译 bug**：compileItem 不再依赖线索面板快照 ITEMS——签名扩 (id,title?)，收藏夹行直接传标题，ITEMS/REPORTS 均 typeof undefined 守卫。**联调**：playwright 全链 17/17 通过、页面 0 JS error、div 平衡 341/341（⑤删除块曾吃掉 rsshubCard 闭合 div 致 page-settings 嵌进 page-sources 全部不显示——div 计数定位补回）。**dev 库事故与实证**：联调探测误触发 POST /api/reset_all → items/收藏/任务/上传材料清零，但 sources 58 行保留、重启后 settings 44 行自 settings.json 全量回填——**整体重置闭环实测走通**；用户真实库在独立交付运行目录不受影响 |
| v2.13.7 | 2026-11-06 | 4 项：①**线索面板时间窗加「最近3天」**（filterDays option 3；统计栏/检索/AI 检索同源生效）。②**导出格式默认标准**：loadSettings 兜底 set_export_style 空值→full（修复 v2.13.6 select 显示空白）。③**热点方向可选日期范围**：热点页按钮旁新增 hotDays select（今日/3/7/14/30/全部）→POST /api/hotspots {days}→_job_hotspots(days)→analyze_hotspots(days)（days=0→list_items(days=None) 全量；不足 3 条提示含时间窗文案）。④**全局复核**：模块 import 全链回归+关键 db 函数冒烟（list_items q/days 组合、favorites 三态、untranslated LIMIT、delete_items 空探）；**精简/标准导出回归真跑+自检**（精简 2 个 WARN 为预期无刊头/期号，0 FATAL；标准 0 问题）；热点 days 真链路实测（dev 无筛选数据时窗口过滤与错误文案正确）；routine_cleanup 挪到 init_db 之后（新装空库安全）；catBg 内 toggleFavItem 上下文安全复核（FAV_ITEMS 即时更新） || v2.13.6 | 2026-11-06 | 6 项 + 「可能存在的问题.txt」8 条全部核修：①**AI 检索改两段式（更优方案）**：AI 仅做关键词扩展（一次小调用 max_tokens 800、失败退化原词）→ **扩展词组本地多词 LIKE**（标题/译名/摘要/机构名 OR，窗口沿用）——不再整批塞给模型，根绝 reasoning 空正文；实测「人工智能」扩展 6 词命中 14 条（12s）。②README「一、快速开始」重构为 5 个 ### 小节（启动/API 配置/检索筛选/编译导出/外观）；help.js 数字列表 bullet 标签改 inlineFmt 后注入（此前被 esc 转义为字面 `<b></b>` 显示）；inlineFmt 白名单 `<b>/<code>` 占位防转义（旧版 README 残留 HTML 标签也能正常渲染）。③设置页「功能简介与使用说明」卡删除（说明已并入 📖 使用说明浮窗）。④**线索数据库**：行复选框+全选+「🗑 删除选中(N)」+行内 🗑；**db.delete_items**（连带 screenings/favorites；**已编译成专报的线索拦截**——reports.item_id 直查、multi_reports 用 item_ids JSON 匹配[该表无 item_id 列]）；**AI 分类卡重构为主题方块流**：渐变方块卡 flex wrap、**HTML5 拖拽排序（localStorage aiCatOrder 记忆）**、点击方块弹 #catBg 展示该主题全部线索表格（日期/标题/机构/筛选状态/☆收藏[复用 toggleFavItem]/🗑删除）；titles 数据补 source/match/score（_window_items JOIN screenings）。⑤**信源管理检索框**：#srcSearchBox 防抖过滤（中文名/英文名/key/rss_url/list_url/cfg.url）全域应用。⑥「可能存在的问题.txt」逐条核修：1) list_favorites 补 multi 分支（JOIN multi_reports）+ delete_multi_report 连带清理 type='multi' 收藏；2) untranslated_items SQL 补 LIMIT（原全库拉取）；3) help_doc fallback 已有 return（v2.13.5 版已带，复核确认）；4) api_items q 下推 SQL（list_items 加 q 参数 LIKE 下推，避免分页后过滤截断）；5) api_rsshub_ai_name 键名 api_base→api_base_url（恒 False bug，AI 命名恢复触发）；6) custom.py image MIME 键补前导点（.png/.jpg/.webp/.gif）；7) 启动例行清理 db.routine_cleanup（run_logs/task_runs/fetch_runs/token_usage 保留 90 天、custom_uploads 按引用 GC 超 7 天、startup.log>2MB 截尾）挂 on_startup+吞异常点 db_log_warn 落痕；8) runner._site_fetch 双定义/inserted_ids 重复、multi.py citations 死代码、custom.py messages 死代码清理，**fix_quotes 抽公共 app/textutils.py**（compile/multi/custom/edit 四处内嵌删除，docx_builder._fix_zh_quotes 保留）；requirements.txt 补 pymupdf/pydantic+关键包上界+python-docx 重复项清理。**新坑**：multi_reports 表无 item_id 列（item_ids JSON 数组）——引用检查别写 item_id 列；local_search LIKE 拼 SQL 的 where/params/limit 三处数量对齐（漏 AND、漏 LIMIT 参数各一） || v2.13.5 | 2026-11-05 | 8 项：①**AI 全接口空正文根治**：用户模型=deepseek-flash（reasoning 模型，思考占满 max_tokens 后 content 空→"API 返回空内容"×3 空转）；client.chat 识别空正文+reasoning_content 存在→AIError.bump_tokens→**重试时 max_tokens 翻倍（≤32000）**；ai_search_local 3000→6000/ai_classify 4000→9000、分类批次 60→30；实测「人工智能」检索 3 条命中零报错。②**导出三修**：a) `_EMPTY_BRACKET` 清空 []/[[ ]]/[[cite]] 半成品（AI 改写残留，导出不再留 [] 符号）；b) `_add_text_runs` 邻接同材料重复引用合并为一个脚注（[[cite1]][[cite1]] 中间仅空白只插一次）；c) 编辑窗字数口径=正文合计+「Word 含脚注约 N 字」标注（citations refs 合计+复引同注落点×3 字）——消除 UI 比 Word 少几百字的脚注字差。③**精简导出格式**：设置导出卡新增「导出格式」select（full 默认/concise）；concise 只保留红色分隔线（无刊头/期号/单位行/日期/署名），其余排版同标准；文件名「永州专报（精简）：标题.docx」；**不占期号计数**。④**编译线索网址.html→RSSHub 路由实测+入库**：225 URL 中筛 14 候选（routes 源码 grep 映射）→公共镜像两轮试连 **8 条通过**（格隆汇/华尔街见闻/第一财经/香港01×2/财联社深度/澎湃财经/经济日报突发）→已写入交付库自定义组（base=rssforever/rss.tips 按实测固定；交付库自定义 11→13 条 RSSHub 源；失败 6 条=上游拒/无对应路由如实未加）。⑤**精选信源**：新增「精选信源」页签（filter=in_common 全组横跨）+全部文案 常用→精选（加入/移出/已加入/统计 cntPicked）；全部信源语义不变（含精选+自定义+参考）。**单源测试右键**：「▶ 测试」按钮 contextmenu→runner.probe_site 附 sample（前 6 条 title/url/date）→#testDetail 浮层展示采集样本+耗时+失败原因。⑥**任务中心**：正在执行+排队等待**双卡并排 flex**（min-width 380/300 可换行）；任务历史/执行日志 h3 float:right→flex（v2.13.2 同款 float 收缩 bug 再现处），tbl-wrap 全宽+日志 52vh。⑦提示词卡标题改「提示词自定义」，八模块 summary 去序号去 AI 前缀。⑧**使用说明书浮窗**：GET /api/help_doc（交付目录 README.md，frozen=exe 旁）+ web/help.js（独立文件，cache-bust ?v=2）：miniMd 极简渲染（h1-h4/ul/table/bold/code/link）+左侧目录锚点跳转；#helpBg floatwin 类**非模态**可拖动（fw-title mousedown）/CSS resize:both/最大化还原/最小化到 dockpill，边看说明边实操；入口=设置页标题旁「📖 使用说明」。**事故与修复**：help_doc 插入时 heredoc 损坏把文件头部复制 2100 行进中段（app=FastAPI 重绑定致前半路由全部失效）——**AST top-level 语句级去重**（保留首次 def/class/Assign/imports，装饰器行起始）恢复 2181 行干净文件；helpContent 被 loading 提示覆盖（写错容器）→修正；/help.js 需显式路由（server 只有 / 静态）+index.html ?v=2 破 IAB 缓存。**新军规：patch python 字符串含 \n 一律 chr(92)+n 拼接，写完立刻 ast.parse+node --check** || v2.13.4 | 2026-11-05 | 6 项（附截图反馈）：①**补料重编提速与可知性**：查证用户任务实为 7 分 06 秒**成功完成**（task_runs aa01478：14:53→16:00、成稿《日本银行…会议纪要》，rid19 转草稿）——感知“无法完成”=生成慢+进度单调；修=原线索截 4000 字/补充材料单条截 6000 字（此前整段 2.6 万字输入）、**max_tokens 文本档 20000→12000**（视觉档保留 20000，与 compile_item 同源口径）、卡进度文案改“通常需要 2-5 分钟可打断”、浮窗/任务 toast 加时限提示。②**代理横幅整体移除**（多处境外站点失败不再弹窗，用户改外置代理软件）：删 HTML/#proxyBanner、showBannerOnce/dismissBanner/bannerDay 及两处触发调用（grep 残留=0；免 localStorage 残键）。③**线索面板「✦ AI 检索」**（检索框旁新按钮 apps/app/ai/local_search.py 新模块 ai_search_local）：输入关键词/主体 → 时间窗内线索（窗口沿用 filterDays、0=全部；最多 250 条池）→ model_screen 一次 JSON 语义匹配 → 返回命中 ids+**同级结构 items**（_slim_by_ids 复用 /api/items 字段）。前端 AI_SEARCH_MODE 状态机：ITEMS 快照备份→applyAiSearchItems（同 renderFeeds 渲染路径+顶栏 #aiSearchBar 显示口径与命中数）→loadItems/任何筛选切换即 exitAiSearch 恢复；热点卡 AI 模式跳绘；按钮 finally 复位。**实测**:「日本银行」7 天内 3 秒命中 7 条、idle、退出正常。③b**数据库一键 AI 智能分类**：page-archive 底部新增 #aiClassifyCard（🧠 一键 AI 分类按钮+结果区）——后端 /api/archive/ai_classify run_in_background("ai_classify"→组 ai)：按当前月份/关键词筛选取线索（≤500）→ **分批 60 条/批** ai_classify 主题收敛 ≤12 → 结果落 settings key archive_classify_result（created_at/total/topics[count,titles≤40 条含 url]）；GET /api/archive/ai_classify_result 读取；前端任务 done 回调刷新、details 折叠列表渲染、空态占位。**实测**：q="AI"23 条→6 主题（保险养老 11/科技与人工智能 5/…）1 批 40s；空 q 无匹配如实提示。④**信源排序双 bug**：a) sort 逻辑挂在已废弃的第三页签分支（v2.13.1 并签后 common/custom 永不排序）→ 统一收敛到 filter 后独立块全域应用；b) **置顶 pinned 永远压最先**，主导感知“选了排序模式没反应”→比较器重写：**primeRank(选中维度)=主键**、pinned 降序仅作同分次键（default 模式维持置顶优先）。⑤**设置页网络代理卡取消**（外置代理软件接管；后端 proxy_enabled/proxy_url 键保留兼容可手改 settings.json）：删 HTML 卡+**loadSettings SET_BOOLS 循环判空**（set_proxy_enabled 元素已删，未判空会 null.checked 整个设置页崩溃）；/ui 保存路径 FIELDS_PROXY 无调用方安全。⑥**RSSHub 自动下载更新**：新端点 POST /api/rsshub/upgrade（_job_rsshub_upgrade 后台任务、组 net）：npmmirror→npm 双镜像查 version+**下载 tkg tarball**（≤180s）→tarfile 流式**仅提取 package/dist/**（zip-slip 防护）→备份旧 dist 为 .bak→替换+写 _ver.json→重启内置服务轮询 20×3s alive；**启动失败自动回滚旧 dist 重启**（依赖差异兜底）、成功删 .bak；前端「⤓ 自动下载并更新 RSSHub」按钮+check_update toast 文案改引导一键升级；STAGE_LABEL=rsshub_upgrade/RSSHub 升级，done 后 syncRsshubCard+loadSources。**dev 守卫实测**：未捆绑环境 started 后立即如实失败，bundle 零副作用。**大坑二枚**：patch python f-string 写 \n 会落成 HTML/JS 真实换行炸语法（btnRsshubUpgrade confirm 跨行炸 boot 减摆→在其中已加**node --check 全 script 联调惯例**）；IAB 25s evaluate 上限内不等 async 渲染——先 save 后 sleep 再读 UI（排序验证被旧 DOM 幻骗过一轮） || v2.13.3 | 2026-11-05 | 4 项（附截图反馈）：①**专报管理表头「藏」改「收藏」**（原 width:36px 单字截头易误读为显示故障→56px nowrap 完整列名+title）。②**线索批量条分流与自动清选**：updateSel——选中线索已全部有筛选判定（is_match 非 null）时隐藏「AI 筛选选中」只留「一键编译」（混合选中恢复显示）；「一键编译」启动成功后 clearSel() 自动取消全部勾选并收起批量条。③**弃编译线索补材料重编（大功能）**：单篇专报「已放弃」行操作列新增「＋ 补充材料」→ #suppBg 浮窗（链接/上传 1-6 份，复用 /api/custom/upload 与 #customBg 材料模型结构）；**后端**：server._intake_materials 抽为公共 helper（_job_custom 同改调用；链接抓正文/upload 解译→insert_custom_item 合成线索）；app/ai/custom.py 新增 **compile_supplement**（材料1=原线索基础信息 content/summary、材料2..N=补充材料；system=单篇 custom_compile_prompt 设置优先+SYSTEM_PROMPT 兜底+compile_extra；缺字段自动补齐重试一轮；[[citeK]] 顺序重编号；citations/evidence 逐材料 ref——材料1 ret=机构《题》日期、补充=人工补充《名》当日）；**db.update_report allowed 放开 citations/evidence/model**；成功后 status abandoned→draft、issue_no 保持原行不动；新端点 POST /api/reports/{rid}/supplement（SupplementReq note+materials；假 rid 守卫 started:false 实测）；tasks GROUPS 补 "supplement":"ai"；前端 STAGE_LABEL 补「补充材料重编」、done→loadReports。**验证**：浮窗打开（原线索+放弃原因上下文呈现）、abandoned 行按钮、材料行模型渲染、批量条分流函数级断言、路由删区 0 残留 0 boot 错。④**RSSHub 路由库预置参考路由整体取消**（用户已有官网 docs 检索→粘贴添加链路）：删 #btnRsshubQuick/#btnRsshubTestAll/路由表格/#rsshubRows/尾说明；JS 删 renderRsshubRows/rsshubTest/rsshubAdd 两按钮与检索框绑定（残留 grep=0）；syncRsshubCard 只取实例 base 不拉 routes（RSSHUB 收敛 {base,baseInit}）；粘贴成功提示“下方表格”改“下方信源列表”。窄窗下表格按钮竖排被用户感知为“样式跳回”，删表后该呈现消失（回复中已同步解释窗口宽度机制） |
| v2.13.2 | 2026-11-05 | 5 项（附截图反馈）：①**自定义页签右侧空白根治**：浏览器实测根因=两半卡 inline-block 百分比宽 `.half-card`（50%-10px+各 margin-right）在限宽容器内几像素溢出即换行掉折→左半卡单挂、右侧大片空白（用户截图与 dev 1:1 复现）；改两卡包进 `<div class="src-cards">` **flex 容器**（gap:14px、flex:1 1 470px、允许窄屏 wrap），任意容器宽度稳定并排（实测 x=250/805 各 541px 同行等高）。②**专报收藏表右侧空白**：非表宽问题——`🗑 一键清空专报收藏` 的 `<span style="float:right">` 浮出 h3 悬在右侧，后续 auto 宽的 `.tbl-wrap`（block）按 CSS 规则收缩绕行避开浮块（925px vs 卡 1112px）留下右侧空白；h3 改 `display:flex` 不再 float，表即拉满（实测两个 .tbl-wrap 均 1063=卡内宽一致）。③**编辑浮窗最大化改全屏**：`.floatwin.max` 由 `calc(100vw-20px)` 改 `100vw×100vh + left/top:0 + border-radius:0`（实测 1920×1000 全铺、四角贴边）。④**中文摘录不再翻译不重复展示**：新 `quoteIsZh()`（汉字数≥拉丁字母数且≥4）判定摘录语言；中文时取证卡不渲染 #cpQuoteZh 翻译条、说明文案改"摘录本身已是中文，无需附翻译"、自动翻译调用整段跳过（含 zhBox 判空防白调 /api/translate_text）；英文摘录行为不变。⑤**信源管理右键菜单**：顶部「恢复预置配置」「AI 检查并修复失效信源」两按钮取消；信源行 contextmenu 弹 #srcCtx 浮层菜单（🩹 AI 检查并修复失效信源 / ℹ️ 查看该信源详细信息）——**AI 修复单源**走新端点 `POST /api/sources/{key}/ai_fix`（server `_job_ai_check_one` 复用 ai_check 任务组；source_analyzer.check_and_fix_sources 增 `only_key` 参数，单源走 get_source 不受 only_enabled 限制；假 key 守卫 200 started:false 实测）；**详情浮窗 #srcDetailBg**：名称/英文名/key/分组·是否入常用/分类/采集方式/rss_url·list_url·sitemap·cfg.url（可点击链接）/selectors 解析配置 JSON/备注/最近连通/最近错误 14 项字段；菜单外点击/滚动/失焦自动关闭；详情渲染 28 td 实测。**版式教训**：h3 内 float:right 按钮会让后续 auto 宽 block 兄弟收缩绕行避免浮块留下空白——本轮只修用户指认处不过度扩散 |
| v2.13.1 | 2026-11-05 | 6 项（附截图反馈）：①**AIError JSON 解析失败根治**：deepseek 系输出超限截断后 chat_json 全批崩——**_extract_json 新增截断恢复**（按最后一次合法闭合位置截断、丢弃尾段、在截断文本上重算未闭合栈反向补齐后整体解析；四例单测全过）+ **chat_json 解析失败自动加倍 max_tokens 重试一次（≤32000）**；后续筛选批/自定义编译的"模型输出无法解析为 JSON"部分自愈。②**专报管理三页签**（单篇/综合/自定义平行）＋右上「＋ 新建自定义专报」仅自定义页签显示；**综合/自定义行全新重构**：checkbox 可勾选、藏列支持**收藏星**（`type=multi` 收藏链路后端支持）、脚注处·融合行信息行改善、导出列与操作列不挤（**CSS .sm nowrap 全按钮防竖排**）；**批量删除走 POST /api/multireports/delete**；**空态引导**文案；**修改时间**沿用。③**信源管理**：**异常信源页签取消**（异常行保留在"全部信源"，●异常标识）；**全行🗑删除钮**（预置信源走 excluded_preset_keys；ai_search 虚拟源不可删）；**自定义页签内两半卡并排**（.half-card 50%）：左=✦ RSSHub 路由库（含粘贴/检索/目录/更新管理）、右=AI 添加信源（含 AI 探测站点+AI 搜索引擎抓取）；cntBad 引用同步清理。④**toast 下移 top:76px**，与顶部进行中任务进度条分离不遮挡。⑤UI 全链路联调：三页签流转、综合勾选可删（selN3 计数）、星钮 9 枚、源 54 行全删除钮、并排卡各 488px、**页面 console 零错**。教训：**修 bootstrap 全停类 bug 时必须先删以旧 id 绑定的顶层 onclick 引用（此前 btnCustomReport 移除后 binding 空内引用再次全停）** |
| v2.13.0 | 2026-11-05 | 2 项（附截图反馈）：①**新增「自定义专报」子模块（专报管理标题右按钮）**：浮窗 #customBg 内可**粘贴网页链接（自动抓正文）**、**上传 Word(docx)/PDF/图片**（json base64 ≤25MB，服务员留档 data\custom_uploads\_index.json），材料行可改名/删除，附**主题指向**与**附加要求**（喂给 multi system prompt extra）；**材料处理**：docx/python-docx 全文（段落+表格）｜文本版 pdf→PyMuPDF get_text（≥200 字走文本路径）｜**扫描 pdf→前 8 页渲染 JPEG base64 走多模态**｜图片→dataURL；每材料登记为**合成线索**（db.insert_custom_item：专属源 custom_docs 自动建、url=custom://upload/<up_id>、**items.kind=2 不出现在线索面板/数据库/统计**——list_items/list_archive 双过滤 COALESCE(i.kind,0)=0，重复 upload 同 id 覆写 content）；**编译走 multi 同构**（app/ai/custom.py：材料【材料k】块+[[citeK]] 契约+sections 结构+fix_quotes+citations ref=自定义材料《名》日期；材料不足阈值放宽 600 字；**视觉调用 max_tokens 20000**——14000 被长正文截断致模型两次缺 conclusion；**模型不支持图片时错误包装**「当前编译模型不支持图片输入…请改换 GLM-4V/Qwen-VL/GPT-4o 或改用 Word」）；落库 multi_reports（citations/item_ids=合成条目 id、sources_info 自动）——**编辑/取证/导出/对比/清聊天与综合专报全复用**。**实测**：上传 docx+图片混编→任务 done、成稿 mid=9 1556 字、[[cite1]] 落点、材料 2（图）正常入 sources_info；单一图片材料模型按"材料不足"如实放弃（mid=7，弃条 mid=3/4/5/8 留在 dev 库验调试废弃行）；浮窗开/加/删/关闭、task done→loadReports 自动刷新、材料行面板 0 泄漏。**顺带修复 multi.py 致命提取缺失（成稿后引用 title/editor_note/sections/conclusion 未定义）——该 bug 属于上轮改动泄漏（未上线已是坏态），本轮一并修**。**venv 新增 pip/PyMuPDF 1.28.6**（wheel 打包由 PyInstaller 自动收集；fitz deprecation 警告无碍）。②**RSSHub AP News 添加失败**（用户截图案例）：官方文档仓确认 **/apnews/rss/:category?** 是合法路由（fixtures：rss.ts path='/rss/:category?'），链接路径本身正确；实测 rssforever/rss.tips 503、ktachibana 429、本机内置 503（exe 直接 AP 上游 fetch failed，PROXY_URI 走 7897 openssl TLS 断——**用户当前 Clash 节点连 AP 也出不去**；curl 直连 AP 301 也拦）→ 结论：AP 属防护强站，例遇公共镜像全拦属上游风控常态（与 Folo 能用机制无冲突：Folo 走官方计费池/自有抓取）。**用户体验升级**：粘贴失败明细按 429/503/403 语义分别注解（429=限流、503=上游拒绝或路由不存在、403=官方限制），并附「防护强站多数实例都失败；可自建或换源」说明 |
| v2.12.1 | 2026-11-05 | 5 项（附截图反馈）：①**线索面板数据空白修复（高危回归）**：根因=v2.11.1 撤检索行时遗留的 `$("#rsshubQ").addEventListener`（#rsshubQ 已删→null）在 **boot 期统一脚本段首错**→ 整段尾部的 loadStats/loadItems/loadReports/pollTask 全部未执行（页面骨架在、内容全空）——**教训：单文件 index.html 的 <script> 整段同步执行，任何一处运行期 null 访问报废全部 boot**；修=元素判空后再绑定（`const el=$(…); if(el) el.addEventListener`）。**Playwright pageerror 抓到 boot 错误栈才定位（browser SDK 的 evaluate 钩子装得太晚抓不到 boot 错）**。②**node.exe 黑窗隐藏修复**：根因=把 `CREATE_NO_WINDOW` 写成 `os.CREATE_NO_WINDOW`（实际在 **subprocess** 模块）→ hasattr(os,…) 恒 false → flags=0 → node.exe 以默认方式启动弹控制台；修=creationflags=subprocess.CREATE_NO_WINDOW（STARTUPINFO SW_HIDE 与 DETACHED_PROCESS 两备选实测亦可，取最简）。③**RSSHub 信源自动中文名**：add_url 成功即用 `_fallback_name()`（内置 HOST_ZH 31 站名+RSSSEG_ZH 33 段词表）重命名（bloomberg/business→**彭博社-商业**）；AI 配置可用时前端随后的 /api/rsshub/ai_name 再覆盖一次更贴切翻译；「🔤 中文名补齐」按钮批量把仍英文名的 RSSHub 行改中；**行内点击机构名可改名**（prompt→POST /api/sources/{key}/rename 新端点+db.rename_source）。④**信源批量启停/排序**：原逐源串行 POST 54 次（无反馈十几秒，用户误读"按钮与实际不同步"）→ 新端点 POST /api/sources/toggle_all {tab,enabled} 一次完成+toast「✓ 已停用 N 个信源」（实测 28→0→54 全程<1s）；排序用 localStorage.srcSort 持久。⑤**路由库卡布局**：粘贴订阅链接行**上移到实例行上方**（用户主操作流）；「去哪里找路由」改折叠行（点击展开，含 ⚡全部试连）；状态行下新增三按钮：**⟳ 重启内置服务**（POST /api/rsshub/restart——kill 子进程再 start_rsshub_async）/ **⬆ 检查 RSSHub 更新**（GET commits GitHub 对比 rsshub\_ver.json 的 built_at，更新随程序新版分发）/ **🔤 中文名补齐**；状态行 12s setInterval 实时刷新 |
| v2.12.0 | 2026-11-04 | **RSSHub 方案 B"全自动内置"（用户拍板：安装包一样、分发外地人一键即得）**：①**组包链路（全部实测）**：官方仓 master 源码（DIYgod/RSSHub tarball codeload 直连）→ **pnpm 12.16.1**（corepack+COREPACK_HOME 绕 Program Files 权限；npmmirror 源）全量 install → `pnpm run build` 产生 dist（routes 各 chunk+index.mjs，31s）→ **hoisted node-linker 重装 prod 依赖**（`pnpm install --prod --frozen-lockfile --node-linker=hoisted`——deploy 模式仍留 65 junction 绝对路径异地必断，hoisted 纯物理树零链接）→ + node.exe（**npmmirror Node 24.19.0**，engines ^24.15）= rsshub_bundle\rsshub_final（411MB；构建底稿留 rsshub_bundle 供后续换版本重组）。②**关键坑**：dist/index.mjs 按 **VERCEL_ENV**（不是 NODE_ENV！）选择路由注册——production 才加载打包 routes-BLspYSaV.mjs；不带 VERCEL_ENV 走 dev registry 扫 dist/routes（不存在）→ ENOENT 子进程秒退（本机手动跑成功过是环境差异，冒烟前必须 VERCEL_ENV=production；实测 cnbc 957KB/200）。③**main.py 集成**：rsshub_bundle_dir() 探 exe 旁 rsshub\node.exe+dist\index.mjs；start_rsshub_async 后台线程 Popen（CREATE_NO_WINDOW、stdout/err DEVNULL、env=VERCEL_ENV=production+**PROXY_URI=程序代理设置（proxy_enabled=1 时）**）→ wait_port 60s 就绪 → _set_rsshub_base_if_needed：**settings.json 的 rsshub_base 为空或仍默认 rssforever 时自动切 127.0.0.1:1200**（尊重用户已配置其他实例）；atexit on_exit kill 子进程——**实测正常关窗（WM_CLOSE）回收成功；taskkill /F 强杀属冒烟行为会遗留孤儿，下次启动 alive 检测自动复用不冲突**；复用检测（1200 已 listen 就直接用）。④**server /api/rsshub/status**（bundled/alive/running_child）+ **add_feed_url 候选插入 LOCAL_BASE**（就绪时排原实例与当前实例之后、公共镜像之前）+ 前端路由库卡状态行（✓ 内置已启动/启动中提示）+ 实例下拉动态加「✓ 本机内置 RSSHub」置顶项。⑤**交付规模**：程序目录 250MB→511MB（+411 内置）；启动序列不变（RSSHub 后台异步拉起 6-12 秒就绪，不阻塞主界面）；**朋友体验=拷贝文件夹双击 exe 即全功能 RSSHub**，官方文档搜路由→粘贴链接→默认本机命中（本机少数上游被拒的路由自动回退公共镜像——bloomberg 实测本机 IP 直连超时、走 PROXY_URI 亦被 Clash 当前节点拒，回退链路的必要性实证）。冒烟：状态端点 alive:true、实例自动切换（settings.json 网络_采集. rsshub_base=127.0.0.1:1200）、add_url 空参+bad url 正常、关窗回收 ✓；**已知瞬时**：首启极早窗口 add_url 偶发一次 500（疑似 settings 写入并发竞态，重试即好，未再复现） |
| v2.11.1 | 2026-11-04 | 2 项（RSSHub 路由库二次返工，用户澄清：内置检索不要了、粘贴 rsshub.app 链接识别失败）：①**撤内置官方路由检索**（v2.11.0 的 2081 站索引 assets/rsshub_routes.json、ZH_ALIASES、load_index/search_ns/ns_detail、GET /api/rsshub/search、GET /api/rsshub/ns/{ns}、#rsshubQ 检索行与 #rsshubNsBox 浮层全部移除，build.spec 与源码 assets 同步清掉，交付目录 _internal/assets/rsshub_routes.json 手动删除——cp 覆盖不清多余文件）；改为信息行**外跳官方文档**：`<a class="link" href="https://docs.rsshub.app/" target="_blank">`（vs 现有教程外链同款，pywebview 跳系统浏览器），引导"官方文档搜站名→复制 Example 订阅链接→回程序粘贴"；「⚡ 全部试连」按钮迁入该行（对目录书 19 条防抖过滤 input 恢复 v2.9.9 原渲染）。②**粘贴链接多实例并行试连回退（识别失败根因=官方 rsshub.app 只对 Folo 等合作阅读器放行，程序侧 403；同一路由 path 在任意实例输出同一订阅）**：probe_site 加可选 timeout 参数（默认行为不变）；add_feed_url 重写——解析 path 后对 [原实例, 当前保存实例, 预设镜像 ktachibana/rss.tips/rssforever] **ThreadPoolExecutor 并行试连（每实例 9s 短超时）**，**首个成功者入库**（url=胜出实例+path，默认停用），全败返回 tried 逐实例明细（host+错误摘要）不写库；前端结果行：成功=「✓ 已添加《name》——N 条/x.xs（实例 host）」、失败=每实例试连明细列表+说明"官方 rsshub.app 只放行 Folo 等合作阅读器，建议自建实例后粘贴"。**实测（用户案例）**：POST add_url https://rsshub.app/bloomberg/business → 官方超时/rssforever 503 跳过、**ktachibana 命中 20 条/2.8s 添加成功**（url 自动落到 ktachibana 实例）；实测 /bloomberg/business 在 rss.tips 亦 200。UI 联调：检索行/nsBox 已撤、外跳链接与目录书 19 条渲染正常、粘贴占位文案更新 |
| v2.11.0 | 2026-11-04 | 2 项（RSSHub 模块返工，用户澄清真实诉求=「粘贴订阅链接一键测试添加」+「像官方文档一样按网站名检索全部路由」）：①**官方全量路由索引内置**：实测公共实例均不透传路由查询 API（镜像 /api/routes 503=该接口仅 RSSHub DEBUG 模式注册、官方 rsshub.app 403 官方限流声明）→ 改从**官方文档仓 DIYgod/rsshub-docs 的 src/public/routes.json**（2081 命名空间/3986 路由，含中文显示名/官网/热度 heat/每条 path+名称+**官方 example 示例+参数说明**）生成精简索引 **assets/rsshub_routes.json（773KB，build.spec 收 assets）**+ **ZH_ALIASES 中文别名表**（彭博社/经济学人/财新/华尔街见闻等 40 条）；**app/rsshub.py 新增 load_index/search_ns/ns_detail/add_feed_url**（search_ns：q 匹配 ns 键+官方名+域名，别名命中置顶，热度排序 limit40）+ server 3 端点 GET /api/rsshub/search、GET /api/rsshub/ns/{ns}、POST /api/rsshub/add_url（**先试连成功才入库**，失败返回真实原因不写库；add_url 支持**任意实例完整 URL**或纯路径=当前实例拼接）。②**前端路由库卡两入口**：「粘贴订阅链接 [输入框] 🔧 一键测试并添加信源」（结果行 ✓ 已添加《名称》——正常 N 条/x.xs，默认停用提示启用 / ✗ 试连失败原因+换实例提示）；「官方路由检索」输入网站名 → 防抖 280ms 离线搜索（不联网）→ **浮层 #rsshubNsBox 站点列表**（名称+ns+官网+热度+分类标签）点选 → 主表渲染**该站全部路由**（表头动态"站点/id 全部路由(N)"；含参数模板路由用官方 example 路径试连/添加；无示例参数路由显示"需参照参数说明"）+ 「全部试连」照旧；目录书 19 条保留为默认"推荐路由"。实测：彭博社→bloomberg 置顶、点选 yahoo→10 路由渲染、试连如实显示 503 上游风控、粘贴 https://rsshub.rssforever.com/cnbc/rss → ✓ 添加（29 条/9.2s）、行内添加确认消息带示例路径，全过 |
| v2.10.0 | 2026-11-04 | 5 项：①**摘录短句默认带中文翻译**：取证卡摘录黄条下自动新增译文条（#cpQuoteZh 同色黄条）——缓存有 quote_zh 秒显；无则即时调翻译并**写回 evidence JSON 持久化**（新端点 POST /api/evidence_zh + db.update_evidence_zh，只改对应 no 的 quote_zh 字段；下次打开不再重翻）。②**译文与原文该段同色标出（补实现）**：**翻译锚点机制**——withAnchors() 在送翻原文的各摘录 quote 位置插入【K】锚点 → 流式翻译系统提示词明确要求译文对应语义位置原样保留【数字】标记（不译不增删）→ 前端 renderTrans() 把译文中的【K】转成黄色 qflag 编号徽标，与摘录黄条同色定位（**坑：缓存译文分支原先用 esc() 直显，【K】不渲染——已改走 renderTrans，实测 anchorInTrans:true**）；python 客户端实测流式翻译回文锚点保留。③**任务完成黑框置顶居中**：#toast 改 top:18px、left:50% 平移居中、z-index:1500（原底部易被浮窗/任务面板遮挡漏看）。④**AI 欢迎语显隐**：欢迎气泡带 welcome 标记——未对话前展示使用提示，发出首条用户消息即随 intro 一起移除；replayChat 有历史记录时不再显示欢迎语。⑤**RSSHub 路由库迁入自定义信源页签并易用化**：卡片随 srcTab==="custom" 显隐（syncRsshubCard，不再占独立位置）；**实例改下拉预设**（rssforever 默认/rss.tips/ktachibana/官方 rsshub.app/自定义，选中自动填输入框可改，保存即生效）；**⚡ 全部试连**批量按钮（并行 4 路 setTimeout 错峰，结果原地更新，按钮带 data-path）；**⚡ 一键添加推荐信源**（目录中 name_cn 含"推荐"的实测路由，confirm 后逐条 /api/rsshub/add，默认停用）；标题改初学者三步引导（①挑路由 ②点试连 ③正常就添加），下注 403/503=上游风控的解读与《信源扩展：RSSHub 接入方案.md》指引。实测：toast 顶置/欢迎语首条消息后消失/quote_zh 自动翻译显示（"2023年已悄然过半…"）/缓存译文黄标 anchorInTrans:true/RSSHub 搜索 cnbc 过滤、试连「✓ 正常 29 条/4s」、添加为信源（key=rsshub_rsshub.rssforever.com_cnbc_rss），全过 |
| v2.9.9 | 2026-11-04 | 5 项：①**字数统计根因终修**：v2.9.7 的 body reduce 箭头漏了累加器（ 少 ），每轮只返回当前节值、末值 467 覆盖累计——修复后实测 954→2398 字（与 Word/AI 口径一致）；wordCountStyle 另改逐字符确定论扫描（CJK 每字+英数按词）。②**取证窗**：默认停靠改**编辑窗右上**（右缘-24，标题下 50px，实测 pop.right 1192 vs 编辑窗 1236）；**打开即自动读取原文/译文缓存**——有留存译文直接显示（不用先点原文全文）；翻译改**流式 SSE**（新端点 POST /api/translate_text_stream：progress 事件实时回传已翻译字数、done 落缓存；max_tokens 32768→失败自动退 8192），UI 显示「AI 正在翻译全文… N 字」；摘录提示文案改「仅在原文全文中同色条标出」；③**单篇无逐条取证摘录的不再在正文打编号**（renderPreview hasEvi 过滤 [[citeK]]→无 quote 直接不渲染；预览底部「当页脚注预览」块**仅综合专报显示**——单篇导出不插脚注，重复脚注条不再出现）；④任务历史「概况」列改**「任务详情」**：全量显示 progress/失败原因（不再截 90），并新增 taskResLine() 成功明细行（成功 X/Y/新增线索 N 条/热点方向等，从 result JSON 提取）；⑤**RSSHub 方案 A 落地**：**实测**——官方 rsshub.app 被 Cloudflare 拦（48 条候选全部 403），镜像 rssforever/rss.tips 可达但多数路由 503（**可用的实证：/cnbc/rss 29 条**）；改为从官方仓库 git tree API 抓 lib/routes 路由签名**生成路由目录书 19 条**（bloomberg/ft/ftchinese/economist/wsj/reuters/theguardian/nytimes/cnbc/imf/treasury 等）；**新后端模块 app/rsshub.py**（get/set 实例地址、probe_path 走通用 probe、add_source 造 key 别名）+ 4 端点 + **信源管理新增「✦ RSSHub 路由库」卡**（实例配置保存/恢复默认、搜索过滤、逐条试连、试连成功一键加为自定义信源默认停用，添加后走通用启用开关采集）；rsshub_base 入「网络与采集」设置组随 settings.json 分区。实测：字数 2398/取证自动载译文/无摘录不标/RSSHub 搜-cnbc-试连 29 条-添加入列，全过 |
| v2.9.8 | 2026-11-04 | 5 项：①**白线 bug 修复**：根因=#citePop 规则末尾的 display:flex 覆盖了同规则前面的 display:none（v2.9.7 引入）→ 取证空窗常显（宽 540、高≈0）成一条白色横线——基座恢复 display:none、.on 改 display:flex（flex-column 布局保留）；②「编辑」按钮 nowrap（.solidp white-space)防两字竖排；③**字数位置**：subbar 弹性占位删除，全文合计移到原文链接右侧 14px（不再置最右）；④**取证窗四改**：**默认停靠预览右上**（eviPlaceNearPreview：取 #ed_preview 右缘对齐、top=编辑窗+50，即图示位；不遮 AI 对话拦），可拖动改位；**关闭修复**：✕ 移入头部条（拖拽 onmousedown 的 target 判定原用 ===close 与 DOM 重组不匹——改 closest(.cp-close)，renderEvidence/材料对照面板两处），并支持 **Esc** 关闭；**翻译按钮文案**：有缓存/翻后即「🌐 查看译文」，不再出现「再次翻译（覆盖缓存）」（重翻入口撤除）；⑤**RSSHub 方案补第七节**：分发/多机环境要求（exe 自包含、B1 本地部署仅部署机可用、推荐内网共享部署 0.0.0.0、不用 RSSHub 的机器其余功能无恙）。实测：citePop 隐藏/按钮高 27px/subbar 紧邻/取证窗 top 对齐+右缘贴预览/close+Esc 关闭/缓存译文按钮文案，全过 |
| v2.9.7 | 2026-11-04 | 7 项：①列表「编辑修改」按钮改名「编辑」；②布局调整：原文链接+字数条改**底部按钮栏上方一行**（fw-subbar 移至 fw-actions 前），「🔍 引用校对」移到「编辑完成，导出 Word」右侧（标题栏只留 A−/A＋/默认/—□✕），**AI 发送按钮改 46px 圆形主题色 ↑ 箭头**；③**已放弃专报可勾选删除**（checkbox 移除 disabled、全选包含；批量导出前过滤已放弃并提示）；④**引用校对 V4**：**单篇编译编号递增**（compiler 提示词 9/10 条：[[citeK]] K 递增+顶层 cites:[{no,quote}] 摘录契约；compile_item re.sub 重编+落 evidence/reports 列迁移）——**真实编译实测 31 处 1..31 严格递增、quote 摘录 31 条**；save_report 增 evidence 列（**坑：占位符 13/12 错位先报 OperationalError，已修**）；**单篇导出不再插脚注**（export_report 传 None，标记由 _add_text_runs 清场；综合专报脚注不变）；**AI 翻译不限长**（translate_text 不截断，max_tokens 32768，400 参数错自动退 8192 重试）；**译文缓存落库**（items.translated_full 列迁移，带 item_id 时先查缓存直接返回、翻译成功即落库——实测二次点开秒显示）；**取证摘录回插**（取证卡显示该号 AI 摘录黄条；📄 原文全文内按 quote 前缀匹配高亮+挂 [K] 徽标——31 条中 17 条命中高亮，未命中者仍有摘录条可对照）；**取证窗可拖动**（头部 evi-head 拖，默认停靠屏幕左侧不盖 AI 对话栏），正文/译文**黑字**（cp-body）不再灰；⑤字数改 **Word 口径**（wordCountStyle：中文按字+英文/数字按词，标点与 [[citeK]] 不计；实测样例 9 字与预期一致；输入防抖 300ms+blur 兜底）；⑥**信源页签合并**：常用/参考合并为「全部信源」（预置+参考+已加入的自定义），自定义源独立页签集中显示（AI 添加卡仍在）；⑦**RSSHub 方案 v2（实测后）**：本机代理=Clash 7897（非 7890），rsshub.app 连通但**官方声明仅供测试**；npm 包实测 22.4MB/4482 文件（不含 Chromium ~230MB）；**FinBrief 程序包体积不受影响**（RSSHub 独立进程）——更新方案文档为「A 试水+B1 本地 Node 推荐」，实施与路线待用户拍板。实测：直接编辑/编译递增编号/取证卡-摘录-高亮-缓存译文/拖动/列表勾选/发送圆钮，全过 |
| v2.9.6 | 2026-11-04 | 10 项：①**启动默认最大化**（create_window maximized=True）；②**「人工编辑」=左栏直接改写**（点击开关 contenteditable，直接改预览文字；再次点击自动拆集+落库+计版本；AI 发送/关闭/导出前自动收口保存——**坑：closeEditor 原不接管 #fwClose（浮窗拖拽 IIFE 内另绑）导致静默保存失效，已统一 **；分小节/段落表单窗弃用（数据流改 collectFromPreview：DOM 反解 sup→[[citeK]]、<br>→换行）；「AI 修改」chip 删除（默认即 AI 预览态）；A−/A＋ 旁新增「默认」一键恢复 100% 字号；③专报表「藏/期号/字数/状态/修改时间」th nowrap 防 checkout 竖排；**「原文」列删除（点击标题即开原文链接，无 URL 标题纯文本）**；操作列宽度自适应；④编辑窗顶部公共行改**固定 subbar**（原文核对直接展示 URL 文字，全文字数固定可见不需下滑；综合专报显示「材料对照」链开取证面板）；⑤**引用校对 V3（核心重构）**：按钮移到编辑窗标题栏（🔍 引用）——单篇正式接入引用体系（compile 提示词要求正文关键句尾 [[cite1]]、save_report 落 citations、build_docx 单篇导出传 citations 注脚注——**实测 AI 真编译输出 31 处 cite1 且 docx footnotes.xml 注入成功**）；取证卡=材料机构/中文标题/日期/**导出脚注全文**+「打开原文网页」+「📄 原文全文」（GET /api/item/{id} 读 content）+「🌐 AI 翻译」（新端点 POST /api/translate_text≤6000 字符，样本实测译文流畅）；预览引用上标常驻可点击；综合专报「🔍」=材料对照列表；⑥「每次修改自动保存…」灰字删除；AI 输入框加高 64px 且发送按钮与输入框等高垂直居中；⑦测试连接「模型回复为空」修复：max_tokens 20→64 且 content 空时用 reasoning_content 兜底（deepseek-flash 实测回复收到，连接正常.）；⑧**专报右键删除**（行 contextmenu→confirm→DELETE /api/report/{id} 新端点）+**批量删除**（「🗑 删除选中」按钮+POST /api/reports/delete 批端点；综合行右键删除沿用 deleteMultiReport）；⑨快照 repFromSnap/AI done newRep 保留 citations（撤销/AI 修改不丢脚注映射）；⑩RSSHub 信源扩展**方案文档**交付（未动代码，待用户拍板：部署形态 ABC+候选路由+接入步骤）。实测：直接编辑开关/静默保存/取证卡单篇+综合（content 11855 字加载/上标直点）/翻译/导出脚注/右键删/批量删/test 回复，全过 |
| v2.9.5 | 2026-11-03 | 12 项：①字数统计**只留全文合计**并移至「原文核对」行右侧（公共底行，两模式共用，分项统计取消）；②「🗑 清空聊天」移到 ⇄ 对比右侧平行排布，**↩ 撤销可恢复最近一次清空/删除单轮**（前端 pendingChatRestore + lastChatSnap 快照，新端点 `POST /api/report|multireport/{id}/chat_restore` 全量回写 db.set_chat_log；**坑：清空后 verPtr=0 会使撤销按钮 disabled 而 click 无效**——updateVerBtns 条件放宽为 `verPtr<=0 && !pendingChatRestore`；新指令发出即作废旧恢复点）；③热点方向 meta 去掉「· 模型名」，只留"基于 N 条符合线索"；④检索完成提示含新增条数（fetch 分支原有"新增 X 条线索"，result 缺失时的兜底文案"检索采集 完成"为 STAGE_LABEL+" 完成"）；⑤**DeepSeek 教程模型名更正**：deepseek-flash 为官方现役主推（DeepSeek-V4.1-Flash-1017 服务，官方文档实测），deepseek-chat/reasoner 已不在官方模型列表——v2.9.3 曾误判该模型名无效，已改教程推荐 deepseek-flash / deepseek-v4-pro；⑥专报列表**删除底部综合专报说明行**、**按处理时间排序**（list_reports/list_multi_reports ORDER BY COALESCE(NULLIF(updated_at,''),created_at) DESC）、最右**新增「修改时间」列**（updated_at≠created_at 显示 MM-DD HH:MM，从未编辑显示 —）；⑦人工编辑/AI 修改**合并为带底色「编辑修改」按钮**（button.solidp 主题色，直接进入原 AI 修改界面，浮窗顶部「人工编辑」chip 保留可切换表单直改，openEditor 默认 mode 改 'ai'）；⑧导航「收藏夹」移到「线索数据库」前；⑨**引用校对**：手动 pane [[citeK]] 说明行加「🔍 引用校对」按钮——开启后自动切预览模式、正文引用上标高亮可点击，弹 #citePop 浮层（z=1400，点外关闭）显示材料机构/中文标题优先/发布日期/**导出脚注全文**/英文原文摘要；数据：综合用 sources_info（get_multi_report 补 url/summary 字段），单篇用 get_report 新增 item_title_zh/item_summary 连查（单篇无 cititations 提示"没有对应脚注文本"）；⑩「AI 对话式修改」使用提示移入对话栏顶部（#aiIntro），第一条用户消息发出即隐藏；⑪编辑窗标题栏新增 **A−/A＋ 字体缩放**（fw-cols style.zoom 0.7-1.5，localStorage.edZoom 持久，双击 A− 恢复 100%）；⑫线索面板副标题删除。实测：合并按钮/缩放/清空撤销闭环（清空→↩恢复，气泡 1→0→1 server 同步）/右键删轮恢复/校对浮层（综合 21 上标逐条核对+单篇空引用兜底)/flash 教程文案，全部通过 |
| v2.9.4 | 2026-11-03 | 6 项：①目录按钮移至浮窗左缘**垂直居中**（.fw-tocbtn top:50% 反位移，误差实测 0px）+**点击目录/按钮之外任何位置自动收起**（document 级 mousedown 判 closest）；②**左栏实时字数条落位两模式公共区**（#edCharCounts 移至 fw-cols 末尾，updateCharCounts 按 editorMode 取 edReport 或表单值，输入事件与 renderPreview 双触发，人工/AI 模式实测一致）；③**AI 修改后左栏预览不更新的排查与修复**：done 事件 report 为四字段投影经 chainAfterEdit→edReport→renderPreview 链路实测已联动（pv-title 同步新标题）；补 done 事件附带最新 chat_log（服务端读回+前端 newRep 保留），修正 AI 发送后本地 edReport.chat_log 失联导致的显示不一致；④**聊天记录管理**：user 气泡**右键删除单轮**（`POST /api/report|multireport/{id}/chat_delete` {index}→db.remove_chat_log_entry 服务端裁剪，前端 replayChat 以服务端返回重建气泡+版本链）；「🗑 清空聊天」一键清空（chat_clear，正文与版本不变、撤销/恢复由当前状态重新起算，replayChat([])）；⑤**补全日期失败原因可见**（§5 日期待核机制更新：逐条 warn 日志 + 完成日志附失败 N + 返回 fail_reasons；实测不可达线索 0/1，日志落地）；⑥**连通性测试后「最近连通情况」列同步刷新**（testSrc 测完 await loadSources() 重绘后再恢复按钮结论，实测 FSB 未检测→● 正常 11-03 · 10 条与 ✓ 正常 10 条/2.3s 同步）。实测：AI 修改预览联动/右键删轮/清空/目录居中+点外收起/字数双模式/连通列/补日期原因日志， |
| v2.9.3 | 2026-11-03 | 4 项：①筛选模型 label 删去「（同时用于热点方向分析）」注；Token 统计表新增「累计 tokens」列（输入+输出，今日/近 30 天行均含，loadTokenStats 计算合计）；②**界面外观新增 5 套主题配色**（data-theme：teal 默认青玉/forest 青柠森林绿/ocean 海雾深海蓝/violet 星夜紫丁香/sunset 暖阳落日橙；accent 与页面底色各自适配，body.dark[data-theme] 暗色分别调色——主题属性需同时落在 html 与 body（applyUI/init 同步 dataset），否则暗色选择器匹配不到 body）；硬编码主题色（button.dark #0d7d72、progress-pill #134e4a 等）改 var(--teal/…)；**ui_theme 设置键**（默认 teal，FIELDS_UI/SET_FIELDS/内置归档「界面外观」组），一键恢复默认外观还原青玉主题；③任务状态最终语义化：**ok=0 或 False → error**（概况=失败：原因，补全日期 0/16 全败不再显示「完成」），有 total 且无 fail 字段且 0<ok<total → 概况=完成（成功 X/Y）——fetch/compile 自报 fail 的不受影响；④用户端 API 连不上（getaddrinfo failed）为当时断网/DNS，用户已自行恢复，无需程序侧处理 |
| v2.9.2 | 2026-11-03 | 10 项：①人工编辑窗表单宽窄不全的根因=表单样式全挂 .modal 选择器，浮窗无该容器——补 .floatwin .ed 全量宽度/高亮（title/textarea 1131px 实测）；②主题控件上移（margin 2px 14px 14px）；③对比/任务详情浮层 z-index 1300 > 浮窗 1200（此前被遮住）；④**AI 修改会话持久化**：reports/multi_reports 新增 chat_log 列（ALTER 迁移），每轮修改追加 {q,a,before,after,ts}（db.append_chat_log，上限 30 轮），GET 返回——前端 openEditor 重放气泡+**由快照重建跨会话版本链**（撤销/恢复/最初跨关闭依然可用）；**AI 记得上一步修改**：edit.build_ai_edit_messages(history=) 把历史当作 user/assistant 对拼接（仅最近 8 轮），system+历史前缀 append-only——同时改善服务端 prompt 缓存命中率；⑤收藏夹按钮改 .cardtools 工具条放表格框右上方；⑥任务中心执行日志卡标题删括号注；**业务失败不再显示「完成」**（tasks.py wrapper：result.ok False→status=error、progress=失败：原因）；⑦编辑窗左栏底**实时字数条**（标题/编者按/正文/结论/合计）+**左缘章节目录**（fw-tocbtn 收起式，点击条目 scrollIntoView 跳小节，人工/AI 双模式通用 id）；⑧settings.json 提示词组附「_内置提示词参考」（八大提示词当前内置全文，_ 前缀键 apply 自动忽略，可整段拷贝替换 custom_*）；⑨任务中心导航移入「系统」组（设置上方）；⑩两列模式右列溢出根因=grid 未限 min-width——columns 改 repeat(2,minmax(0,1fr)) + 卡片 overflow-wrap:anywhere（实测无横向溢出） |
| v2.9.1 | 2026-11-03 | 7 项：①代理横幅**当日只提示一次**（按日期落键 bannerShownDay，忽略/已显示后当天不再弹）；②**运行日志整合进任务中心**（导航去独立项；新增「执行日志」卡+阶段筛选；任务历史/进行行「详情」弹出该任务时间窗完整日志浮层）；③线索面板**两列展示**切换（默认单列、记忆偏好）；④专报表再拆「导出」独立列（含综合行导出+删除并列）；⑤**编辑器浮窗大修**：高度 min(88vh pulled >660px 基准)+右/下/右下三向手工拉伸+**最小化=真隐藏窗口、右下角 dock 标签恢复**+最大化前清内联几何（修此前失效）+双栏 stretch 等高、右栏 460px+流式完成后气泡保留「本轮思考过程」折叠区+**AI 思考段协议**（edit 流式提示词要求模型按【思考】…【JSON】输出，chat_stream 关 json_mode，server 按标记把思考路由为 reasoning 帧，收尾只补发未路由到的思考防重复；agnes-2.5-flash 实测输出思考段）；⑥试用用量数字/百分比 rAF 滚动动画+进度条过渡；⑦已复核按钮**双击改回草稿**；**导出 FATAL 终修**：引用标记识别放宽（[[cite 2]] 等变体）、**标题同样转脚注**（此前标题为普通 run，AI 修改把标记写进标题即触发「正文残留 [[citeK]]」FATAL）、非法形态出口清场+未识别标记一律删除；现场数据纪律明确：**升级交付不删除运行目录 data\**（覆盖式 cp，不先删）。实测：resize/最小化恢复/最大化（1260×700）、详情浮层、日志筛选、流式思考 6 帧且不重复，全部通过 |
| v2.9 | 2026-11-03 | 7 项：①API Key 教程改版「试用额度用尽时如何获取」（DeepSeek/通义千问/Agnes/智谱/MiniMax 五平台，网址可点击跳浏览器，推荐模型按各平台当前主推更新并注明以控制台为准）；②顶部进行中任务小窗可点击，直接跳转任务中心；③专报表新增独立「原文」列（综合行不适用显示 —）；④**编辑器浮窗化**（可拖动/最大化/最小化、无遮罩不阻塞主界面）+ AI 对话栏加宽（440px）+ 欢迎语重复修复 + **版本链**（↩撤销 / ↪恢复 / ⟲恢复最初，均同步落库）+ **⇄修改前后对比**（句级 LCS diff 红删绿改浮层）+ **AI 修改流式化**（SSE 逐帧显示思考过程与已产出字数，新端点 *_ai_edit_stream，client.chat_stream）；⑤settings.json 升级为**分区结构**（_目录 + 八大分区，含全部 AI 提示词；递归读取兼容旧平铺，§9 更新）；⑥**综合专报失败可诊断**：逐材料进度（"读取材料 k/N"）、材料字数明细、模型输出残缺时自动补齐一轮、失败一律落库"已放弃（原因）"并留运行日志（根因复盘：v2.8 版本在"模型输出结构不完整"时直接 return，既不落库也不留档）；⑦**现场数据纪律：后续升级交付不再删除运行目录 data\**（用户测试数据全程保留）。实测：流式采样 5 帧思考/生成、版本链 diff/撤销/恢复/最初、浮窗拖动最大化最小化、教程五链接，全部通过 |
| v2.8 | 2026-11-02 | 7 项：①页面滚动时左侧栏同步可见（双独立滚动列）；②「编辑复核」改名「人工编辑」+ **AI 对话式修改专报**（双栏编辑器：左结构化预览右 AI 对话；撤销栈逐步回滚；导出排版恒按模板，排版型指令被拒并说明）；③开发文档大幅详细化（新增 §9 settings.json 参数键表、§4.7 综合专报数据流、§8 脚注规范）；④**多线索综合专报**（2-5 条：线索面板批量条/热点卡勾选/收藏夹复选三入口；格式参考第 58 期模板、**当页引用按模板插脚注**——脚注文本程序生成防编造，复引按"同注X。"惯例拆独立注号（实测 Word 不允许同一脚注 id 多处引用）；导出与单篇共用期号计数）；⑤**AI 重试机制**（可配 0-6 次、指数退避 3/6/12s、429 较长等待、4xx 不重试、重试写运行日志）；⑥**试用体验**（未配 Key 一键启用内置测试 Key【用户授权】，总量 100 万 tokens，剩余用量与百分比进度条；设置页新增主流平台 API Key 获取教程五则）；⑦**设置参数文件结构化**（data\settings.json 双存储：启动外部编辑优先、界面保存即同步、内部键排除，键表见 §9）。实测：真实 AI 综合编译/AI 修改/撤销/试用启用与用量统计/双引号脚注自检，以及 Word+WPS 双引擎导出 PDF 脚注渲染全部通过 |
| v2.7.1 | 2026-10-27 | 修复"自定义/参考信源启用后不在常用组"：v2.7 的"启用即加入常用"只改了 in_common（采集已生效），但常用页签按 src_group=common 过滤导致不显示。现常用页签=所有 in_common=1 的源（行内标"来自参考/自定义组"），参考/自定义页签只显示未加入常用的源（移出常用即退回原池）——与 v2.3 用户定义的"添加到常用/移除"语义一致 |

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0 | 2026-10-24 | 首发：26 信源采集、AI 筛选/编译、模板导出+自检、Web 前端、exe 打包 |
| v1.1 | 2026-10-24 | 图标；浅路径交付；加载页先行修空白页；aihot.news 风格前端重构 |
| v2.0 | 2026-10-24 | 12 项：设置页 API 直连测试；渐进式检索；任务中心；toast 可关；设置页帮助；筛选/编译提示词自定义；标题 AI 翻译（自动+手动）；导出两端对齐+标题缩进；自动检索双模式；一键筛选未筛；信源管理独立页+AI 增源/查源 |
| v2.1 | 2026-10-25 | 6 项：开发文档（本文件）；代理横幅可忽略；热点一键编译；发布日期补全（自动+手动+编译时）；分隔线按修订模板改为★前后各 12 横杠对称；品牌改"信息编译" |
| v2.2 | 2026-10-25 | 7 项：①设置分模块保存+一键保存全部；②符合要求线索 AI 四维评分排名（贴合/时效/权威/完整，权重与细则可配；展示前 6 条可展开，旧数据可补评分）；③线索面板"只看未读"；④"一键筛选未筛"改名"一键筛选"；⑤新增热点方向模块（AI 归纳排名+数据佐证，保留 10 次历史）；⑥收藏夹（线索/专报收藏）；⑦白天/黑夜主题切换（左下角，记忆偏好）。另：点击线索标题自动标已读 |
| v2.3 | 2026-10-25 | 10 项：①主题切换三段式（自动跟随系统/白天/夜间）；②一键筛选升主色按钮（与开始检索一致）、深色主按钮弃用黑色；③收藏夹取消收藏改 ★ 星标（与收藏方式一致）+ 一键清空；④热点方向"数据佐证"改"相关线索"可展开，逐条编译专报/收藏；⑤热点卡行内"编译专报/复核专报"单独挑编；⑥标题自动翻译默认开；⑦AI 提示词四模块化（筛选/编译/评分与热点/翻译，整体替换+附加+一键恢复默认）；⑧网络代理独立设置卡；⑨信源三分组管理（常用/参考/自定义），参考信源 10 个经书签整理逐站验证写入；⑩收藏夹线索可 AI 筛选、编译/编辑专报 |
| v2.4 | 2026-10-26 | 13 项：①主题分段控件缩小连体；②参考信源扩至 17 个（RSS/sitemap/AI 三路验证）；③参考组说明精简；④任务并行化（fetch/net/ai 三并发组，同组排队可取消）；⑤Token 用量统计（每日输入输出+累计，client 自动记账）；⑥信源连通标注（最近获取时间/成败/错误）+ 置顶（多选）+ 连通性排序；⑦线索数据库页（全量历史分页，月份/筛选状态/关键词管理）；⑧热点卡改勾选多选+顶部"编译选中"，去逐行按钮；⑨任务中心重写（进行中多行/排队可移除/历史可删可清空）；⑩评分内置提示词只读查看修复；⑪线索 AI 摘要（点击生成缓存复用）；⑫翻译标题默认加粗可关；⑬界面大小缩放/字体设置+一键恢复默认外观 |
| v2.5 | 2026-10-26 | 6 项：①连通性自检移至信源管理（批测按钮+每源"▶ 测试"独立小模块，结果原地显示；线索面板移除该按钮）；②置顶按钮改低调 SVG 图钉（浅灰/置顶淡金，无明显底框）；③参考信源全局代理环境复核（结论：防护站依旧 403，无新增收录）；④主题三段控件移至左下灰色文字下方；⑤Token 统计改分行表格（今日/近30天 × 输入/输出/调用次数，精确个位）；⑥AI 摘要提示词可定制（提示词卡第五模块：替换+附加+查看内置+恢复默认），摘要块去图标（改"摘要："前缀），按钮"重新摘要"改"重新生成摘要"，摘要失败在任务中心留 error 记录 |
| v2.6 | 2026-10-26 | 11 项：①信源管理新增异常信源页签（跨组汇集 + 永久删除，预置信源删除后不随 yaml 复活）；②并发机制（采集 fetch_concurrency 线程池 + AI 任务组 ai_concurrency 上限，设置可调，按 API 限流配置）；③AI 并发上限下热点卡/统计刷新修复；④任务"⏹ 打断"（协作取消点 + 到单元级回滚：采集删本轮入库、筛选删判定、翻译补日期复位、编译删草稿）；⑤新增 11 个国外权威金融媒体参考信源（WSJ/MarketWatch/经济学人×2/财富/半岛/BBC/卫报/FT中文/Google News 聚合×2，共 28 个参考源）；⑥热点卡行内 AI 摘要按钮（生成/查看）；⑦搜索支持中文译名；⑧AI 摘要与筛选完成后面板自动刷新（修复"面板未更新"）；⑨时间选项加"今日"，统计卡按所选时间窗计数；⑩统计突然清零根因=统计仅启动渲染一次，已随任务完成与时间窗联动修复；⑪AI 搜索引擎抓取信源（DDG/必应/百度三引擎自动切换 + AI 确认 + 提示词⑥模块，作 AI 添加信源失败后的兜底） |
| v2.7 | 2026-10-27 | 6 项：①窗口文本可复制（pywebview create_window 加 text_select=True——此前默认禁用选择，界面文字无法复制）；②信源排序新增"启用在前"；③任务中心排版收紧；常用页签语义修正（显示所有 in_common=1 的源，含参考/自定义加入的，行内标"来自X组"；来源池页签只显示未加入常用的）（卡宽 ≤1020px、空态变矮、历史状态改标签、概况列省略号）；④修复"每次检索都提示补全日期"（自动翻译/补日期只处理**本轮新增**线索，run_fetch 返回 added_ids；全库补齐走面板按钮）；⑤修复"自定义信源采不到线索"（根因：启用≠加入常用，in_common=0 不参与采集。现**启用即自动加入常用**，AI 添加验证通过亦然；存量库一次性迁移 v27_enabled_common_applied）；虚拟信源 ai_search 禁用开关与测试（防 UnsupportedProtocol 报错）；⑥打断提示更准确（当前 AI 调用/单站抓取完成后立即回滚）；"重新生成摘要"真正重新生成（/api/summarize 加 force，此前返回缓存）；重复编译已编线索增加覆盖确认；时间选项默认恢复"最近7天"（v2.6 把"今日"排首位导致默认窗变小） |
| v2.7.1 | 2026-10-27 | 修复"自定义/参考信源启用后不在常用组"：v2.7 的"启用即加入常用"只改了 in_common（采集已生效），但常用页签按 src_group=common 过滤导致不显示。现常用页签=所有 in_common=1 的源（行内标"来自参考/自定义组"），参考/自定义页签只显示未加入常用的源（移出常用即退回原池）——与 v2.3 用户定义的"添加到常用/移除"语义一致 |

## 15. 项目迁移与后续开发指引（核心军规）

为了确保后续开发能够平滑过渡，并在不同机器或环境中顺利运行与维护，必须严格遵守以下原则：

### 15.1 目录结构与数据资产隔离
- **数据目录绝对保留**：`data\` 目录包含 SQLite 数据库（`finbrief.db`）、业务配置文件（`settings.json`）和启动日志，是用户最核心的资产。在做任何代码覆盖、迁移或更新部署时，**严禁清空或删除 `data\` 目录**。
- **环境隔离**：不要直接拷贝 `.venv` 虚拟环境。在目标机器上重新安装 Python 3.11，通过 `pip install -r requirements.txt` 重建虚拟环境，以避免底层 C 依赖或路径硬编码失效。
- **静态资源与模板**：`web\` 目录为单文件前端，`app\fetcher\sources.yaml` 为预置信源配置，`assets\icon.ico` 为程序图标。在打包（PyInstaller）时必须通过 `build.spec` 明确收集。

### 15.2 Word/WPS 排版防御性军规
- **ECMA-376 规范要求**：在 WordprocessingML 的 `footnotes.xml` 中，开头必须包含 `separator` (id=-1) 和 `continuationSeparator` (id=0) 系统元素，否则 Word 会拒绝渲染脚注引用。
- **全角引号与字体 Hint**：中文文档中的双引号必须使用标准弯引号 `“` 与 `”`，单引号使用 `‘` 与 `’`。在生成或修改 docx 时，所有文字 run 的 `w:rFonts` 必须补充 `w:hint="eastAsia"`，否则在部分系统或 Word 版本下中文标点会回退为西文窄字形渲染。
- **避免重复注号导致文件损坏**：Word 不允许正文中多个 `<w:footnoteReference>` 标签指向相同的脚注 ID（会导致 Word COM 报错 `-2147352567` 报文件损坏）。多次引用同一材料时，应生成独立递增的注号，正文角标位置严格排在句子结尾标点符号（如 `，。；：`）之前。
- **句柄及时释放**：使用 Win32 COM（Word.Application / KWPS.Application）或 PyMuPDF 校验导出的文件时，对象必须在 `finally` 块中立即显式调用 `.Quit()` 或 `.close()`，否则会导致文件被常驻进程独占，下一次导出时提示“文件正被另一应用使用”。

### 15.3 前端生命周期与单页安全性
- **DOM 解析时序（避免空指针阻断 Boot）**：`web/index.html` 为无构建链的单文件应用，所有 `<script>` 逻辑均在同步执行。因此，**所有被 JavaScript 事件监听绑定的 DOM 节点必须在物理位置上先于 `<script>` 渲染**，或者在获取节点时做好 `null` 守卫（例如 `const el = $('#btn'); if (el) el.onclick = ...`）。如果顶层脚本发生未捕获的 TypeError，会导致后续所有页面初始化和数据轮询中断。
- **表格表头与滚动体验**：长列表页面（如信源管理、专报管理）必须使用 `position: sticky; top: 0; z-index: 3;` 保持表头吸顶，避免向下滑动时被行元素遮挡。

### 15.4 AI 调用与接口容错
- **OpenCode 会话头管理**：针对 `opencode.ai` 的 Base URL，客户端底层已实现自动生成并持久化 `x-opencode-session` 稳定会话头，后续开发无需在前端暴露该配置。
- **流式输出与思考模型适配**：使用带有推理能力的模型（如 DeepSeek-Flash/Reasoner 等）时，需确保系统能从 `reasoning_content` 中提取思考过程，且当模型在输出中耗尽 max_tokens 时，需具备指数退避或自动加倍 token 重试的容错能力。
- **格式严格校验**：AI 编译输出的 JSON 数据必须严格包含 `sufficient`、`title`、`editor_note`、`sections`、`conclusion` 字段。若缺失，需通过补问重试一次，避免直接中断。

### 15.5 部署与发布检查清单
1. 检查 `requirements.txt` 中是否包含新增的三方库。
2. 运行 `pyinstaller build.spec --noconfirm` 进行构建。
3. 验证构建后的 `dist\金融热点编译系统\` 能否独立启动并正常调起本地端口。
4. 将编译产物覆盖至浅路径目标目录时，**跳过 `data\` 和 `rsshub\`**，只覆盖代码和静态资源。
5. 启动测试，确认无控制台错误，各项核心流程（采集、筛选、编译、导出、自检）全部通过。
