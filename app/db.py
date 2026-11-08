"""SQLite 数据库层：建表、连接与基础 DAO。"""
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager

from app.paths import DB_PATH, ensure_dirs

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,          -- 站点标识，如 federalreserve
    name_cn TEXT NOT NULL,             -- 机构中文名
    name_en TEXT NOT NULL,             -- 机构英文名/域名
    category TEXT NOT NULL,            -- 分类：cb_reg / insurance / deposit / consult / bank
    mode TEXT NOT NULL,                -- rss | html
    config TEXT NOT NULL,              -- JSON：rss_url 或 html 选择器配置
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_no INTEGER NOT NULL DEFAULT 0,
    note TEXT DEFAULT '',
    src_group TEXT NOT NULL DEFAULT 'common',  -- common 常用 / reference 参考 / custom 自定义
    in_common INTEGER NOT NULL DEFAULT 1       -- 是否加入常用（采集只跑常用组）
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    published_at TEXT,                 -- ISO8601，站点发布时间（无则 NULL）
    fetched_at TEXT NOT NULL,          -- ISO8601
    summary TEXT DEFAULT '',           -- RSS 摘要或列表页摘要
    content TEXT DEFAULT '',           -- 抓取的原文正文（编译用）
    content_fetched INTEGER NOT NULL DEFAULT 0,
    lang TEXT DEFAULT 'en',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_pub ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_src ON items(source_id);

CREATE TABLE IF NOT EXISTS screenings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER UNIQUE NOT NULL REFERENCES items(id),
    is_match INTEGER NOT NULL,         -- 1 符合 / 0 不符合
    criteria TEXT NOT NULL DEFAULT '[]',   -- JSON 数组，对应六项要求序号
    match_points TEXT DEFAULT '',      -- 匹配点说明
    reason TEXT DEFAULT '',            -- 判定理由 / 不符合原因
    model TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER UNIQUE NOT NULL REFERENCES items(id),
    issue_no INTEGER,                  -- 导出时分配；草稿阶段 NULL
    title TEXT NOT NULL,               -- 正标题：机构：关于……的报告
    editor_note TEXT NOT NULL,
    body TEXT NOT NULL,                -- JSON：[{h1, blocks:[{h2?, text}]}]
    conclusion TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft / reviewed / exported / abandoned
    abandon_reason TEXT DEFAULT '',
    model TEXT DEFAULT '',
    docx_path TEXT DEFAULT '',
    chat_log TEXT DEFAULT '',          -- v2.9.2 AI 修改会话：[{q,a,before,after,ts}]
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    exported_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok_sites INTEGER DEFAULT 0,        -- 成功站点数
    fail_sites INTEGER DEFAULT 0,
    new_items INTEGER DEFAULT 0,       -- 新入库线索数
    window_days INTEGER DEFAULT 7,
    failures TEXT DEFAULT '[]'         -- JSON：[{source, error}]
);

CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,                -- item / report
    target_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(type, target_id)
);

CREATE TABLE IF NOT EXISTS hotspots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    model TEXT DEFAULT '',
    matched_count INTEGER DEFAULT 0,
    analysis TEXT NOT NULL             -- JSON：directions 数组
);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,               -- info / warn / error
    stage TEXT NOT NULL,               -- fetch / screen / compile / export / system
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (          -- v2.4 任务中心记录（排队/执行/历史）
    id TEXT PRIMARY KEY,                        -- tasks.py 生成的 uuid
    stage TEXT NOT NULL,
    label TEXT DEFAULT '',
    status TEXT NOT NULL,                       -- queued / running / done / error / cancelled
    progress TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS token_usage (        -- v2.4 每日 token 用量
    day TEXT PRIMARY KEY,                       -- YYYY-MM-DD
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS multi_reports (      -- v2.8 综合专报（多线索合成，含脚注）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,                        -- 主题式标题（无"机构："前缀）
    editor_note TEXT NOT NULL,
    body TEXT NOT NULL,                         -- JSON [{h1, blocks:[{h2,text(text 内含 [[citeN]] 标记)}]}]
    conclusion TEXT NOT NULL,
    citations TEXT NOT NULL DEFAULT '[]',       -- JSON [{no,item_id,ref}]
    item_ids TEXT NOT NULL DEFAULT '[]',        -- JSON 参与线索
    status TEXT NOT NULL DEFAULT 'draft',       -- draft/reviewed/exported/abandoned
    abandon_reason TEXT DEFAULT '',
    model TEXT DEFAULT '',
    docx_path TEXT DEFAULT '',
    issue_no INTEGER,                           -- 导出时分配（与单篇共用期号计数）
    chat_log TEXT DEFAULT '',                   -- v2.9.2 AI 修改会话（与单篇同构）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    exported_at TEXT
);
"""


def routine_cleanup():
    """v2.13.6 启动例行清理：run_logs/task_runs/token_usage 保留 90 天；
    custom_uploads 目录按登记引用回收（无合成线索引用且超 7 天即删）；
    startup.log 超过 2MB 截断保留尾部。全部失败仅落日志不中断。"""
    try:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - 90 * 86400))
        with get_conn() as conn:
            for tb, col in (("run_logs", "ts"), ("task_runs", "started_at"),
                            ("fetch_runs", "started_at")):
                try:
                    conn.execute(f"DELETE FROM {tb} WHERE {col} < ?", (cutoff,))
                except sqlite3.Error:
                    pass
            try:
                conn.execute("DELETE FROM token_usage WHERE day < ?",
                             (time.strftime("%Y-%m-%d",
                                            time.localtime(time.time() - 90 * 86400)),))
            except sqlite3.Error:
                pass
    except Exception as e:
        try:
            db_log_warn("db", f"启动清理部分失败: {type(e).__name__}: {e}")
        except Exception:
            pass
    # custom_uploads GC：_index.json 条目若 url=custom://upload/<up_id> 在 items 中不存在
    # 且登记时间超过 7 天 → 删除文件与索引
    try:
        import json as _json
        d = os.path.join(os.path.dirname(DB_PATH), "custom_uploads")
        idxp = os.path.join(d, "_index.json")
        if os.path.isfile(idxp):
            with get_conn() as conn:
                used = {r[0] for r in conn.execute(
                    "SELECT url FROM items WHERE url LIKE 'custom://upload/%'")}
            idx = _json.load(open(idxp, encoding="utf-8"))
            stale = []
            for up_id, meta in list(idx.items()):
                url = "custom://upload/" + up_id
                if url not in used and (meta.get("created") or "") <                         time.strftime("%Y-%m-%d",
                                      time.localtime(time.time() - 7 * 86400)):
                    fp = meta.get("disk") or ""
                    if fp and os.path.isfile(fp):
                        try:
                            os.remove(fp)
                        except OSError:
                            pass
                    stale.append(up_id)
            if stale:
                for up_id in stale:
                    idx.pop(up_id, None)
                _json.dump(idx, open(idxp, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception as e:
        try:
            db_log_warn("db", f"上传材料回收失败: {type(e).__name__}: {e}")
        except Exception:
            pass
    # startup.log 截断（保留尾部 400 行）
    try:
        logp = os.path.join(os.path.dirname(DB_PATH), "startup.log")
        if os.path.isfile(logp) and os.path.getsize(logp) > 2 * 1024 * 1024:
            with open(logp, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-400:]
            open(logp, "w", encoding="utf-8").writelines(tail)
    except Exception:
        pass


def db_log_warn(stage: str, message: str):
    """v2.13.6 轻量告警日志（吞异常点统一落痕）。"""
    try:
        log(stage, message)
    except Exception:
        pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


@contextmanager
def get_conn():
    ensure_dirs()
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        items_cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
        if "title_zh" not in items_cols:
            conn.execute("ALTER TABLE items ADD COLUMN title_zh TEXT DEFAULT ''")
        if "read" not in items_cols:
            conn.execute("ALTER TABLE items ADD COLUMN read INTEGER DEFAULT 0")
        sc_cols = [r["name"] for r in conn.execute("PRAGMA table_info(screenings)").fetchall()]
        if "score" not in sc_cols:
            conn.execute("ALTER TABLE screenings ADD COLUMN score INTEGER")
        if "score_detail" not in sc_cols:
            conn.execute("ALTER TABLE screenings ADD COLUMN score_detail TEXT DEFAULT ''")
        src_cols = [r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
        if "src_group" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN src_group TEXT NOT NULL DEFAULT 'common'")
        if "in_common" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN in_common INTEGER NOT NULL DEFAULT 1")
            # v2.3 一次性迁移：常用归属沿用现有启停状态（在用的即常用）
            conn.execute("UPDATE sources SET in_common = enabled")
        # v2.4：信源连通状态与置顶
        for col, ddl in [("last_fetch_at", "TEXT DEFAULT ''"),
                         ("last_fetch_ok", "INTEGER"),
                         ("last_fetch_items", "INTEGER DEFAULT 0"),
                         ("last_error", "TEXT DEFAULT ''"),
                         ("pinned", "INTEGER NOT NULL DEFAULT 0")]:
            if col not in src_cols:
                conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {ddl}")
        if "ai_summary" not in items_cols:
            conn.execute("ALTER TABLE items ADD COLUMN ai_summary TEXT DEFAULT ''")
        rep_cols = [r["name"] for r in conn.execute("PRAGMA table_info(reports)").fetchall()]
        if "kind" not in rep_cols:
            conn.execute("ALTER TABLE reports ADD COLUMN kind TEXT NOT NULL DEFAULT 'single'")
        if "citations" not in rep_cols:
            conn.execute("ALTER TABLE reports ADD COLUMN citations TEXT DEFAULT ''")
        # v2.8：综合专报表补列（早期创建的库缺 issue_no 时自动补齐）
        mcols = [r["name"] for r in
                 conn.execute("PRAGMA table_info(multi_reports)").fetchall()]
        if mcols and "issue_no" not in mcols:
            conn.execute("ALTER TABLE multi_reports ADD COLUMN issue_no INTEGER")
        if mcols and "chat_log" not in mcols:
            conn.execute("ALTER TABLE multi_reports ADD COLUMN chat_log TEXT DEFAULT ''")
        if "chat_log" not in rep_cols:
            conn.execute("ALTER TABLE reports ADD COLUMN chat_log TEXT DEFAULT ''")
        # v2.9.7：单篇引用取证摘录列（[{no,quote}]）＋ 原文全文 AI 译文缓存（items）
        if "evidence" not in rep_cols:
            conn.execute("ALTER TABLE reports ADD COLUMN evidence TEXT DEFAULT ''")
        it_cols2 = [r["name"] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
        if "translated_full" not in it_cols2:
            conn.execute("ALTER TABLE items ADD COLUMN translated_full TEXT DEFAULT ''")
        if "kind" not in it_cols2:
            conn.execute("ALTER TABLE items ADD COLUMN kind INTEGER DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind)")
        # v2.3：标题翻译默认打开（仅对未做过该迁移的库执行一次，之后用户可自行关闭）
        flag = conn.execute("SELECT value FROM settings WHERE key='v23_translate_default_applied'").fetchone()
        if not flag:
            conn.execute("UPDATE settings SET value='1' WHERE key='auto_translate'")
            conn.execute("INSERT INTO settings(key, value) VALUES('v23_translate_default_applied','1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'")
        # v2.7：一次性迁移——已启用的信源一律视为常用（启用即采集的新心智），
        # 此后 toggle 开=加入常用、关=仅停用
        flag27 = conn.execute("SELECT value FROM settings WHERE key='v27_enabled_common_applied'").fetchone()
        if not flag27:
            conn.execute("UPDATE sources SET in_common=1 WHERE enabled=1")
            conn.execute("INSERT INTO settings(key, value) VALUES('v27_enabled_common_applied','1') "
                         "ON CONFLICT(key) DO UPDATE SET value='1'")
        # v2.8：结构化设置镜像——启动时先应用 data\settings.json（开发人员
        # 手改文件优先生效），再无条件镜像回写（保证文件与库一致且字段齐全，
        # 首次启动即生成该文件）
        try:
            apply_settings_file()
        except Exception:
            pass
        # v2.19：一次性迁移——补全日期默认开（v2.17 起）。旧版设置页曾把
        # backfill_auto=0 存进库/镜像文件。两点时序要求：
        # a) 必须在 apply_settings_file() 之后判断（文件优先会覆盖库值）；
        # b) 实际写入必须在 with get_conn() 大事务提交之后用独立连接执行——
        #    否则 sync 镜像读不到未提交的 1，把 0 写回文件，下次启动又灌回库。
        try:
            need219 = not conn.execute(
                "SELECT value FROM settings WHERE key='v219_backfill_default_applied'").fetchone()
        except Exception:
            need219 = False
    if need219:
        try:
            set_setting("backfill_auto", "1")   # 独立连接提交 + 同步镜像文件
            with get_conn() as conn:
                conn.execute("INSERT INTO settings(key, value) "
                             "VALUES('v219_backfill_default_applied','1') "
                             "ON CONFLICT(key) DO UPDATE SET value='1'")
            log("system", "v2.19 迁移：检索后自动补全缺失日期已重置为默认开")
        except Exception:
            pass
    # v2.20：密钥加密迁移——a) 试用体系残留在自定义 Key 里的上游密钥直接清除
    #（试用模式不再依赖 api_key 设置；apply_settings_file 可能已把它加密入库，
    #  故须用 get_secret 解开后的明文比对）；b) 存量明文 api_key 就地 DPAPI 加密。
    try:
        from app.trial_config import upstream_key as _upstream_key
        stored = get_setting("api_key", "")
        if stored:
            if stored.startswith(_SECRET_PREFIX) or stored.startswith("plain:"):
                try:
                    plain = get_secret("api_key")
                except Exception:
                    plain = ""
                if plain == _upstream_key():
                    with get_conn() as conn:
                        conn.execute("DELETE FROM settings WHERE key='api_key'")
                    sync_settings_file()
                    log("system", "v2.20 迁移：已清除试用体系残留在自定义 Key 位上的上游密钥"
                                  "（试用模式现在独立于自定义 API，激活即用）")
            elif stored == _upstream_key():
                with get_conn() as conn:
                    conn.execute("DELETE FROM settings WHERE key='api_key'")
                sync_settings_file()
                log("system", "v2.20 迁移：已清除试用体系残留在自定义 Key 位上的上游密钥"
                              "（试用模式现在独立于自定义 API，激活即用）")
            else:
                set_secret("api_key", stored)
                log("system", "v2.20 迁移：自定义 API Key 已改为加密存储（不再出现在 settings.json）")
    except Exception:
        pass
    # v2.20：v2.16 试用备份残留 trial_backup_key 早已废弃（还原机制已移除），
    # 库里和镜像里都不能留明文密钥。
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM settings WHERE key='trial_backup_key'")
        sync_settings_file()   # 清出旧镜像文件中残留的明文备份密钥
    except Exception:
        pass


# ---------------- settings ----------------
DEFAULT_SETTINGS = {
    "api_base_url": "",                  # v2.20.2 不预填服务商地址，用户自定义（OpenAI 兼容）
    "api_key": "",                       # 用户在设置页输入，绝不打包
    "model_screen": "",                  # v2.20.2 不预填模型名，用户自定义
    "model_compile": "",                 # v2.20.2 不预填模型名，用户自定义
    "model_translate": "",               # 标题翻译模型，空=用筛选模型
    "trial_model": "deepseek-v4.1-flash",  # v2.19 试用模型（与 trial_config.TRIAL_DEFAULT_MODEL 一致）
    "auto_translate": "1",               # 采集后自动翻译英文标题（v2.3 起默认开）
    "backfill_auto": "1",                # 采集后自动补全缺失发布日期（v2.17 起默认开）
    "auto_screen_after_fetch": "1",      # v2.19 采集后自动对新增线索执行 AI 筛选（默认开）
    "proxy_enabled": "0",
    "proxy_url": "",                     # 如 http://127.0.0.1:7890
    "export_dir": "",                    # v2.20 默认空=程序目录下"编译报告"文件夹（自动创建）
    "issue_counter": "0",                # 当前已用期号，导出成功后 +1
    "window_days": "7",                  # 只采集最近 N 天
    "request_timeout": "20",             # 单站超时秒
    "auto_fetch_mode": "off",            # off | daily | interval
    "auto_fetch_time": "08:00",
    "auto_fetch_interval_min": "30",
    "sign_name": "XXX",                  # v2.20 默认署名（通用占位，用户自改）
    "org_line": "XXXX",                  # v2.20 默认单位（刊头单位行）
    "masthead": "XXXX专报",              # v2.20 默认刊头（通用占位，用户自改）
    # ---- AI 提示词自定义（v2.3：每类支持"整体替换"+"附加"；空=使用内置） ----
    "custom_screen_criteria": "",        # 筛选提示词：整体替换（含六项要求与判定逻辑）
    "screen_extra": "",                  # 筛选提示词：附加要求（拼在内置/替换之后）
    "custom_compile_prompt": "",         # 编译系统提示词：整体替换
    "compile_extra": "",                 # 编译系统提示词：附加要求
    "custom_translate_prompt": "",       # 翻译提示词：整体替换
    "translate_extra": "",               # 翻译提示词：附加要求
    "score_custom_rules": "",            # 评分细则（附加到评分提示词）
    "hotspot_extra": "",                 # 热点方向提示词：附加要求
    "custom_summarize_prompt": "",       # 摘要提示词：整体替换（v2.5）
    "summarize_extra": "",               # 摘要提示词：附加要求（v2.5）
    "custom_search_confirm_prompt": "",  # AI 搜索信源：确认提示词整体替换（v2.6）
    "search_confirm_extra": "",          # AI 搜索信源：附加要求（v2.6）
    # ---- 并发（v2.6） ----
    "fetch_concurrency": "3",
    "rsshub_base": "",        # v2.9.9 RSSHub 实例地址（空=内置默认公共镜像）
    "ai_concurrency": "2",               # AI 类任务同时运行数（1-8；按 API 限流调低）
    "ai_retry_max": "3",                 # v2.8 AI 调用重试次数（0-6，指数退避）
    # ---- 界面外观（v2.4） ----
    "translate_bold": "1",               # 翻译标题加粗（0=常规字重）
    "ui_zoom": "1",                      # 界面缩放（0.85 / 0.9 / 1 / 1.1 / 1.2）
    "ui_theme": "teal",                  # v2.9.3 主题配色（teal/forest/ocean/violet/sunset）
    "ui_font": "",                       # 界面字体（空=默认系统字体）
    # AI 评分四维度权重（合计可为任意正数，程序按占比归一化）
    "score_weight_relevance": "40",      # 主题贴合性
    "score_weight_timeliness": "25",     # 时效性（系统按发布时间计算）
    "score_weight_authority": "20",      # 机构权威性（AI 评）
    "score_weight_completeness": "15",   # 内容完整性（AI 评）
}


def get_setting(key: str, default=None) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            return row["value"]
    return str(DEFAULT_SETTINGS.get(key, "")) if default is None else str(default)


# ---------------- 密钥加密存储（v2.20） ----------------
# api_key 一律经 Windows DPAPI（当前用户作用域）加密后入库，且绝不写入
# data\settings.json 镜像；库中格式 "dpapi1:<base64>"。密文绑定本机当前
# Windows 用户——把 data 目录拷到别的机器无法解密（程序会提示重新填写），
# 这是有意为之的防泄露设计。
_SECRET_PREFIX = "dpapi1:"


def _dpapi_protect(text: str) -> str:
    """Windows DPAPI CryptProtectData（用户作用域）→ "dpapi1:<base64>"；
    调用失败（非 Windows 等）退回 "plain:" 前缀明文（不理想但不阻断使用）。"""
    try:
        import base64
        import ctypes
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        data = text.encode("utf-8")
        buf = ctypes.create_string_buffer(data, len(data))
        inp = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
                ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            return "plain:" + text
        try:
            raw = ctypes.string_at(out.pbData, out.cbData)
            return _SECRET_PREFIX + base64.b64encode(raw).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return "plain:" + text


def _dpapi_unprotect(stored: str) -> str:
    """解 "dpapi1:<base64>"；本机用户不匹配/损坏返回 ""（提示重填）。"""
    try:
        import base64
        import ctypes
        from ctypes import wintypes

        class _BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        raw = base64.b64decode(stored[len(_SECRET_PREFIX):])
        buf = ctypes.create_string_buffer(raw, len(raw))
        inp = _BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
            return ""
        try:
            return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return ""


def set_secret(key: str, value: str):
    """密钥写入：DPAPI 加密后入库（幂等：已是密文则原样存）。"""
    v = value or ""
    if v.startswith(_SECRET_PREFIX) or v.startswith("plain:"):
        stored = v
    else:
        stored = _dpapi_protect(v) if v else ""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, stored))
    sync_settings_file()


def get_secret(key: str) -> str:
    """密钥读取：解密返回明文；不存在/解密失败返回 ""。兼容旧明文值（原样返回）。"""
    v = get_setting(key, "")
    if not v:
        return ""
    if v.startswith(_SECRET_PREFIX):
        return _dpapi_unprotect(v)
    if v.startswith("plain:"):
        return v[len("plain:"):]
    return v   # 旧版明文残留（init 迁移会就地加密）


def secret_is_encrypted(key: str) -> bool:
    return get_setting(key, "").startswith(_SECRET_PREFIX)


def export_dir_setting() -> str:
    """v2.20 导出目录：设置值为空 → 程序运行目录下"编译报告"文件夹（自动创建）。"""
    d = (get_setting("export_dir") or "").strip()
    if not d:
        d = os.path.join(os.path.dirname(os.path.dirname(DB_PATH)), "编译报告")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


# v2.8：结构化设置镜像（exe 旁 data\settings.json，开发人员可直接编辑）
_SETTINGS_FILE_INTERNAL = ("v23_translate_default_applied", "v27_enabled_common_applied",
                           "v219_backfill_default_applied", "trial_active_hash",
                           "last_auto_day", "trial_used_in", "trial_used_out",
                           "api_key",          # v2.20 密钥绝不写入 settings.json
                           "trial_backup_key")  # v2.20 废弃残留，同样不得出现在镜像

# v2.9 settings.json 分区（结构化目录）：键 → 分组（未列入的进"其他设置"）
_SETTINGS_GROUPS = {
    "API与模型": ["api_base_url", "api_key", "model_screen", "model_compile",
                 "model_translate", "trial_model", "ai_retry_max",
                 "api_extra_headers", "opencode_session_id"],
    "网络与采集": ["proxy_enabled", "proxy_url", "request_timeout", "rsshub_base",
                 "fetch_concurrency", "ai_concurrency", "window_days"],
    "自动检索与翻译": ["auto_fetch_mode", "auto_fetch_time",
                    "auto_fetch_interval_min", "auto_translate", "translate_bold",
                    "backfill_auto", "auto_screen_after_fetch"],
    "评分权重": ["score_weight_relevance", "score_weight_timeliness",
               "score_weight_authority", "score_weight_completeness",
               "score_custom_rules"],
    "提示词": ["custom_screen_criteria", "screen_extra",
             "custom_compile_prompt", "compile_extra",
             "custom_translate_prompt", "translate_extra",
             "custom_summarize_prompt", "summarize_extra",
             "custom_search_confirm_prompt", "search_confirm_extra",
             "custom_edit_prompt", "edit_extra",
             "custom_multi_prompt", "multi_extra", "hotspot_extra"],
    "导出排版": ["export_dir", "masthead", "issue_counter", "org_line", "sign_name"],
    "界面外观": ["ui_zoom", "ui_theme", "ui_font", "translate_bold"],
}
_GROUP_HINTS = {
    "API与模型": "接口地址 / 密钥 / 各模型名 / AI 失败自动重试次数（0-6）",
    "网络与采集": "代理 / 超时 / 采集并发 / AI 并发 / 采集时间窗（天）",
    "自动检索与翻译": "每日定时或按间隔自动检索；标题自动翻译与译名加粗",
    "评分权重": "四维评分权重与细则附加",
    "提示词": "八个模块（筛选/编译/翻译/摘要/搜索/修改/综合/热点），整体替换+附加",
    "导出排版": "Word 目录、刊头、期号计数（导出中勿手改）、单位行、署名",
    "界面外观": "界面缩放与字体",
    "其他设置": "其余开发用键",
}


def _settings_group_of(key: str) -> str:
    for grp, keys in _SETTINGS_GROUPS.items():
        if key in keys:
            return grp
    return "其他设置"


def _settings_file_path() -> str:
    return os.path.join(os.path.dirname(DB_PATH), "settings.json")


def sync_settings_file():
    """把当前生效的完整设置（内置默认+已存值）结构化镜像到 data\settings.json。

    v2.9 分区格式：{"_目录": {组: [程序内注释]}, "组名": {键: 值, ...}, ...}
    平铺与嵌套两种写法读取均可（见 apply_settings_file）；
    开发人员手改叶子值保存即可，重启程序或回设置页后生效。"""
    import json as _json
    try:
        rows = dict(all_settings())          # 默认值 + stored 合并视图
        for k in _SETTINGS_FILE_INTERNAL:
            rows.pop(k, None)
        groups = {}
        for k, v in rows.items():
            groups.setdefault(_settings_group_of(k), {})[k] = v
        # v2.9.2：提示词组附「_内置提示词参考」——当前内置各提示词全文
        # （键以 _ 开头仅作展示/复制参考，应用设置时自动忽略；可整段拷进对应
        # custom_* 键替换或直接修改 custom_* 生效）
        try:
            from app.ai.screen import (CRITERIA_TEXT as SC, JUDGE_PROMPT,
                                       OUTPUT_SPEC)
            from app.ai.compile import SYSTEM_PROMPT as CP
            from app.ai.translate import SYSTEM_PROMPT as TP
            from app.ai.summarize import SYSTEM_PROMPT as MP
            from app.ai.hotspots import ANALYZE_PROMPT as HP
            from app.ai.multi import SYSTEM_PROMPT as MUP
            from app.ai.edit import SYSTEM_PROMPT as EP
            from app.ai_search import SYSTEM_PROMPT as SP
            groups.setdefault("提示词", {})["_内置提示词参考"] = {
                "筛选六项要求（复制到 custom_screen_criteria 可替换）": SC,
                "筛选判定与评分契约（供参考；评分细则用 score_custom_rules 追加）":
                    JUDGE_PROMPT.replace("{criteria}", SC).strip(),
                "编译提示词（复制到 custom_compile_prompt）": CP,
                "翻译提示词（复制到 custom_translate_prompt）": TP,
                "摘要提示词（复制到 custom_summarize_prompt）": MP,
                "热点方向提示词（hotspot_extra 附加）": HP,
                "综合专报编译提示词（复制到 custom_multi_prompt）":
                    MUP.replace("{extra}", ""),
                "AI 编辑提示词（复制到 custom_edit_prompt）": EP,
                "搜索确认提示词（复制到 custom_search_confirm_prompt）": SP,
                "评分输出契约（-score 输出规则，仅供参考）": OUTPUT_SPEC.strip(),
            }
        except Exception:
            pass
        out = {"_目录": {g: _GROUP_HINTS.get(g, "") for g in groups}}
        out.update(groups)
        with open(_settings_file_path(), "w", encoding="utf-8") as f:
            _json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        pass


def all_settings_rows():
    with get_conn() as conn:
        return conn.execute("SELECT key, value FROM settings").fetchall()


def apply_settings_file() -> int:
    """启动时读取 data\settings.json 并覆盖 settings 表（外部编辑优先生效）。

    兼容两种写法：平铺 {键:值} 与 v2.9 分区 {"_目录":…, "组名": {键:值}}——
    递归遍历所有层级，叶子标量即写库；返回应用的键数。"""
    import json as _json

    def _walk(d, sink):
        n = 0
        for k, v in (d or {}).items():
            if not isinstance(k, str) or k.startswith("_"):
                continue          # _目录/_说明 等元数据键
            if isinstance(v, dict):
                n += _walk(v, sink)          # 分区块递归
            elif isinstance(v, (str, int, float, bool)) or v is None:
                sink[k] = v
                n += 1
        return n

    path = _settings_file_path()
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            return 0
        kv = {}
        _walk(data, kv)
        n = 0
        file_key = None
        with get_conn() as conn:
            for k, v in kv.items():
                if k == "api_key":
                    # v2.20 密钥不直接进库：文件里的历史明文改走加密通道
                    #（老版本 settings.json 残留的明文 Key 在启动时被收编加密，
                    #  随后由下方 sync 清出镜像文件，避免明文永久留在磁盘）
                    if isinstance(v, str) and v.strip():
                        file_key = v
                    continue
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)))
                n += 1
        if file_key:
            set_secret("api_key", file_key)   # 文件优先语义不变：收编为加密存储
            sync_settings_file()              # 并把明文 Key 清出镜像文件
        return n
    except Exception:
        log("system", "settings.json 解析失败，已忽略（保留数据库设置）", "warn")
        return 0


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    if key not in _SETTINGS_FILE_INTERNAL:
        sync_settings_file()   # v2.8: 修改即同步镜像文件


def all_settings() -> dict:
    """默认值+已存值的合并视图。**不含 api_key**（v2.20 起密钥走 get_secret/
    set_secret 加密通道，绝不进入本视图与 settings.json 镜像）。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    stored.pop("api_key", None)
    merged = dict(DEFAULT_SETTINGS)
    merged.pop("api_key", None)
    merged.update(stored)
    return merged


# ---------------- logging ----------------
def log(stage: str, message: str, level: str = "info"):
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO run_logs(ts, level, stage, message) VALUES(?,?,?,?)",
                (now_iso(), level, stage, message),
            )
    except sqlite3.Error:
        pass  # 日志失败不影响主流程


def recent_logs(limit=200, stage=None):
    with get_conn() as conn:
        if stage:
            rows = conn.execute(
                "SELECT * FROM run_logs WHERE stage=? ORDER BY id DESC LIMIT ?",
                (stage, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------- task runs（v2.4 任务中心） ----------------
def list_task_runs(limit=100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("status") in ("running", "queued"):
            continue  # 活跃任务由 tasks.snapshot 单独提供，避免重复
        out.append(d)
    return out


def delete_task_run(tid: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM task_runs WHERE id=? AND status NOT IN ('running','queued')",
            (tid,))
        return cur.rowcount


def clear_task_runs() -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM task_runs WHERE status NOT IN ('running','queued')")
        return cur.rowcount


# ---------------- token usage（v2.4） ----------------
def add_token_usage(tokens_in: int, tokens_out: int):
    day = time.strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO token_usage(day, tokens_in, tokens_out, calls)
               VALUES(?,?,?,1)
               ON CONFLICT(day) DO UPDATE SET
                 tokens_in=tokens_in+excluded.tokens_in,
                 tokens_out=tokens_out+excluded.tokens_out,
                 calls=calls+1""",
            (day, int(tokens_in or 0), int(tokens_out or 0)))


def token_usage_stats():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM token_usage ORDER BY day DESC LIMIT 30").fetchall()
    days = [dict(r) for r in rows]
    return {"days": days,
            "total_in": sum(d["tokens_in"] for d in days),
            "total_out": sum(d["tokens_out"] for d in days),
            "total_calls": sum(d["calls"] for d in days)}


def reset_token_usage():
    with get_conn() as conn:
        conn.execute("DELETE FROM token_usage")


def reset_all_content() -> dict:
    """v2.13.8 一键清空所有数据（设置模块入口）：
    删 内容模块（线索+筛选+合成线索材料）与 数据模块（单篇/综合专报、热点方向、
    收藏、任务近况/运行日志/采集记录、token 用量）全部数据；
    settings 全清回归出厂默认（信源管理子模块 sources 表保留）；
    custom_uploads 目录一并清空。返回计数 dict 供前端展示。"""
    tables = ["screenings", "favorites", "hotspots", "items", "multi_reports",
              "reports", "fetch_runs", "run_logs", "task_runs", "token_usage"]
    counts = {}
    with get_conn() as conn:
        for t in tables:
            try:
                cur = conn.execute(f"DELETE FROM {t}")
                counts[t] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except sqlite3.Error as e:
                counts[t] = -1
                db_log_warn("db", f"清空 {t} 失败: {type(e).__name__}: {e}")
        try:
            cur = conn.execute("DELETE FROM settings")
            counts["settings"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error as e:
            counts["settings"] = -1
            db_log_warn("db", f"清空 settings 失败: {type(e).__name__}: {e}")
        # v2.13.8 结构化镜像 data\settings.json 一并删除——否则重启后外部
        # 文件会把旧设置全部回填（外部编辑优先生效），"回归出厂"就被破坏。
        try:
            sf = os.path.join(os.path.dirname(DB_PATH), "settings.json")
            if os.path.isfile(sf):
                os.remove(sf)
                counts["settings_json_removed"] = 1
        except OSError as e:
            db_log_warn("db", f"删除 settings.json 失败: {type(e).__name__}: {e}")
    # custom_uploads 目录整体清空
    cleared = 0
    try:
        d = os.path.join(os.path.dirname(DB_PATH), "custom_uploads")
        if os.path.isdir(d):
            for fn_ in os.listdir(d):
                fp = os.path.join(d, fn_)
                try:
                    os.remove(fp) if os.path.isfile(fp) else shutil.rmtree(fp)
                    cleared += 1
                except OSError:
                    pass
    except Exception as e:
        db_log_warn("db", f"清空 custom_uploads 失败: {type(e).__name__}: {e}")
    counts["custom_uploads_files"] = cleared
    try:
        log("system", f"一键清空所有数据执行完毕：线索/专报/任务/设置 已重置" +
            (f"，清除材料文件 {cleared} 个" if cleared else "") + "（信源保留）")
    except Exception:
        pass
    return counts


# ---------------- sources ----------------
def upsert_source(src: dict):
    grp = src.get("src_group") or src.get("group") or "common"
    in_common = int(src.get("in_common", 1 if grp == "common" else 0))
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sources(key, name_cn, name_en, category, mode, config, enabled,
                                   sort_no, note, src_group, in_common)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 name_cn=excluded.name_cn, name_en=excluded.name_en,
                 category=excluded.category, mode=excluded.mode,
                 config=excluded.config, enabled=excluded.enabled,
                 sort_no=excluded.sort_no, note=excluded.note""",
            (
                src["key"], src["name_cn"], src["name_en"], src.get("category", "cb_reg"),
                src["mode"], json.dumps(src["config"], ensure_ascii=False),
                int(src.get("enabled", 1)), int(src.get("sort_no", 0)),
                src.get("note", ""), grp, in_common,
            ),
        )


def rename_source(key: str, name: str) -> bool:
    """v2.12.1 修改信源显示名（AI 自动命名或行内人工改名共用）。"""
    name = (name or "").strip()
    if not name:
        return False
    with get_conn() as conn:
        r = conn.execute("UPDATE sources SET name_cn=? WHERE key=?", (name, key))
        return r.rowcount > 0


def update_source_flags(key: str, **fields):
    """更新信源启停/常用归属。允许字段：enabled, in_common。"""
    allowed = {"enabled", "in_common"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            params.append(int(v))
    if not sets:
        return
    params.append(key)
    with get_conn() as conn:
        conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE key=?", params)


def update_source_fetch_status(key: str, ok: bool, items: int, error: str):
    """采集/探测后更新信源连通状态（v2.4）。"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE sources SET last_fetch_at=?, last_fetch_ok=?,
               last_fetch_items=?, last_error=? WHERE key=?""",
            (now_iso(), 1 if ok else 0, int(items), (error or "")[:200], key))


def set_source_pinned(key: str, pinned: bool):
    with get_conn() as conn:
        conn.execute("UPDATE sources SET pinned=? WHERE key=?",
                     (1 if pinned else 0, key))


def delete_source(key: str) -> bool:
    """v2.6 永久删除信源（连带其线索保留：线索与信源解绑需保留显示，故只删信源行）。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM sources WHERE key=?", (key,))
        return cur.rowcount > 0


def rollback_delete_items(ids: list) -> int:
    """v2.6 打断回滚：删除本轮采集入库的线索（含筛选/收藏/专报挂链）。

    v2.18.0 由 delete_items 改名——v2.13.6 新增的同名"线索数据库删除"版
    （返回 {deleted, blocked}、拒绝已编译线索）在模块后段把本函数整体遮蔽，
    打断回滚因此拿到 dict、日志显示原始字典。回滚专用语义（删本轮新入库
    线索，必无专报引用）与数据库页删除契约不同，必须分开。"""
    ids = [int(i) for i in ids if i]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with get_conn() as conn:
        # 先解除引用，避免外键约束
        conn.execute(f"DELETE FROM favorites WHERE type='item' AND target_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM screenings WHERE item_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM reports WHERE item_id IN ({marks})", ids)
        cur = conn.execute(f"DELETE FROM items WHERE id IN ({marks})", ids)
        return cur.rowcount


def delete_screenings(item_ids: list) -> int:
    """v2.6 打断回滚：删除本轮筛选写入的判定。"""
    ids = [int(i) for i in item_ids if i]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with get_conn() as conn:
        cur = conn.execute(f"DELETE FROM screenings WHERE item_id IN ({marks})", ids)
        return cur.rowcount


def reset_items_field(ids: list, field: str):
    """v2.6 打断回滚：将本轮写入的字段复位（title_zh / published_at / ai_summary）。"""
    assert field in ("title_zh", "published_at", "ai_summary")
    ids = [int(i) for i in ids if i]
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    with get_conn() as conn:
        conn.execute(f"UPDATE items SET {field}='' WHERE id IN ({marks})", ids)


def delete_reports(ids: list) -> int:
    """v2.6 打断回滚：删除本轮编译生成的专报草稿。"""
    ids = [int(i) for i in ids if i]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with get_conn() as conn:
        conn.execute(f"DELETE FROM favorites WHERE type='report' AND target_id IN ({marks})", ids)
        cur = conn.execute(f"DELETE FROM reports WHERE id IN ({marks})", ids)
        return cur.rowcount


def list_sources(only_enabled=False, group=None, in_common=None):
    q = "SELECT * FROM sources WHERE 1=1"
    params = []
    if only_enabled:
        q += " AND enabled=1"
    if group:
        q += " AND src_group=?"
        params.append(group)
    if in_common is not None:
        q += " AND in_common=?"
        params.append(1 if in_common else 0)
    q += " ORDER BY sort_no, id"
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["config"] = json.loads(d["config"])
        out.append(d)
    return out


def get_source(key: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["config"] = json.loads(d["config"])
    return d


# ---------------- items ----------------
def insert_item(source_id, title, url, published_at, summary=""):
    """插入线索；URL 已存在则补填空日期/摘要，未新增。
    v2.6 返回新插入条目 id（未新增返回 None，供打断回滚）。"""
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT id FROM items WHERE url=?", (url.strip(),)).fetchone()
            if row:  # 已存在：仅补填缺失字段，不算新增
                conn.execute(
                    """UPDATE items SET
                         published_at=COALESCE(published_at, ?),
                         summary=CASE WHEN summary='' THEN ? ELSE summary END
                       WHERE id=?""",
                    (published_at, summary or "", row["id"]))
                return None
            cur = conn.execute(
                """INSERT INTO items
                   (source_id, title, url, published_at, fetched_at, summary, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (source_id, title.strip(), url.strip(), published_at, now_iso(),
                 summary or "", now_iso()),
            )
            return cur.lastrowid
    except sqlite3.Error:
        return None


def update_item_content(item_id: int, content: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET content=?, content_fetched=1 WHERE id=?",
            (content, item_id),
        )


def update_item_title_zh(item_id: int, title_zh: str):
    with get_conn() as conn:
        conn.execute("UPDATE items SET title_zh=? WHERE id=?", (title_zh, item_id))


def update_item_published_at(item_id: int, published_at: str):
    with get_conn() as conn:
        conn.execute("UPDATE items SET published_at=? WHERE id=?",
                     (published_at, item_id))


def null_date_items(limit=20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, url, title FROM items WHERE published_at IS NULL "
            "OR published_at='' LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def untranslated_items(limit=500):
    # v2.13.6 LIMIT 真正生效（原 SQL 无 LIMIT，手动翻译一次可能全库拉取）
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title FROM items WHERE (title_zh IS NULL OR title_zh='') "
            "ORDER BY id DESC LIMIT ?", (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def insert_custom_item(title: str, url: str, summary: str = "",
                       content: str = "") -> int | None:
    """v2.13.0 自定义专报材料登记为合成线索（items.kind=2；列表/统计不显示）。
    专属源 custom_docs 缺失时先自动登记（任何时刻调用均可用）。"""
    src = get_source("custom_docs")
    if not src:
        upsert_source({"key": "custom_docs", "name_cn": "自定义材料",
                       "name_en": "Custom material", "category": "media",
                       "mode": "html", "config": {}, "enabled": 0,
                       "note": "自定义专报材料（AI 编译用，不出现在线索面板）",
                       "src_group": "custom", "in_common": 0})
        src = get_source("custom_docs")
    src_id = src["id"] if src else None
    if src_id is None:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM items WHERE url=?", (url.strip(),)).fetchone()
        if row:   # 同一材料重复使用：返回原 id，并用本次提取结果覆写内容/摘要
            if content or summary:
                conn.execute("UPDATE items SET content=?, summary=CASE WHEN ?<>'' THEN ? ELSE summary END WHERE id=?",
                             (content, summary, summary, row["id"]))
            return row["id"]
    iid = insert_item(src_id, title, url, now_iso()[:10], summary)
    if iid:
        with get_conn() as conn:
            conn.execute("UPDATE items SET kind=2 WHERE id=?", (iid,))
        if content:
            update_item_content(iid, content)
    return iid


def list_items(days=None, only_window=False, matched=None, limit=1000,
               offset: int = 0, q: str = ""):
    """线索列表；可联表筛选结果。
    v2.6：days 过滤（1=自然日今日，N=最近 N 天；无日期条目一并显示），
    offset 支持分页。v2.13.6：q 关键词下推 SQL（原标题/中文译名/机构名 LIKE），
    避免先分页后过滤导致靠后命中被截断。"""
    base = """SELECT i.*, s.name_cn AS source_name_cn, s.name_en AS source_name_en,
                  sc.is_match, sc.criteria AS sc_criteria, sc.match_points,
                  sc.reason AS sc_reason, sc.score, sc.score_detail
           FROM items i
           JOIN sources s ON s.id = i.source_id
           LEFT JOIN screenings sc ON sc.item_id = i.id
           WHERE COALESCE(i.kind,0)=0"""
    qsql = base
    params = []
    if q and q.strip():
        qq = f"%{q.strip()}%"
        qsql += " AND (i.title LIKE ? OR i.title_zh LIKE ? OR s.name_cn LIKE ?)"
        params += [qq, qq, qq]
    order_col = "COALESCE(i.published_at, i.fetched_at)"
    if days is not None and days > 0:
        order_col = "COALESCE(i.published_at, i.fetched_at)"
        if days == 1:
            qsql += f" AND substr({order_col},1,10)=?"
            params.append(time.strftime("%Y-%m-%d"))
        else:
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - int(days) * 86400))
            qsql += f" AND {order_col} >= ?"
            params.append(cutoff)
    elif only_window and days:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - int(days) * 86400)
        )
        qsql += " AND (i.published_at IS NULL OR i.published_at >= ?)"
        params.append(cutoff)
    if matched is True:
        qsql += " AND sc.is_match = 1"
    elif matched is False:
        qsql += " AND sc.is_match = 0"
    qsql += f" ORDER BY {order_col} DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(max(0, offset))]
    with get_conn() as conn:
        rows = conn.execute(qsql, params).fetchall()
    return [dict(r) for r in rows]


def get_item(item_id: int):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT i.*, s.name_cn AS source_name_cn, s.name_en AS source_name_en
               FROM items i JOIN sources s ON s.id=i.source_id WHERE i.id=?""",
            (item_id,),
        ).fetchone()
    return dict(row) if row else None


def list_archive(q: str = "", matched=None, month: str = "",
                 page: int = 1, per_page: int = 100):
    """线索数据库：全量历史分页（v2.4）。返回 (rows, total, months)。"""
    base = """FROM items i
             JOIN sources s ON s.id = i.source_id
             LEFT JOIN screenings sc ON sc.item_id = i.id
             WHERE COALESCE(i.kind,0)=0"""
    params: list = []
    if q:
        base += " AND (i.title LIKE ? OR i.title_zh LIKE ? OR s.name_cn LIKE ?)"
        params += [f"%{q}%"] * 3
    if matched is True:
        base += " AND sc.is_match = 1"
    elif matched is False:
        base += " AND sc.is_match = 0"
    if month:
        base += " AND substr(COALESCE(i.published_at, i.fetched_at),1,7)=?"
        params.append(month)
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        months = [r[0] for r in conn.execute(
            """SELECT DISTINCT substr(COALESCE(published_at, fetched_at),1,7) m
               FROM items ORDER BY m DESC LIMIT 36""").fetchall()]
        rows = conn.execute(
            f"""SELECT i.id, i.title, i.title_zh, i.url, i.published_at, i.fetched_at,
                       i.read, i.ai_summary,
                       s.name_cn AS source_name_cn,
                       sc.is_match, sc.criteria AS sc_criteria, sc.score
                {base} ORDER BY COALESCE(i.published_at, i.fetched_at) DESC
                LIMIT ? OFFSET ?""",
            params + [per_page, (max(1, page) - 1) * per_page]).fetchall()
    return [dict(r) for r in rows], total, months


def get_items_by_ids(ids: list):
    """按 id 批量取线索（联筛选分与专报状态，供热点相关线索/收藏夹操作）。"""
    ids = [int(i) for i in ids if i]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    q = f"""SELECT i.*, s.name_cn AS source_name_cn, s.name_en AS source_name_en,
                   sc.is_match, sc.score,
                   rp.id AS report_id, rp.status AS report_status
            FROM items i
            JOIN sources s ON s.id = i.source_id
            LEFT JOIN screenings sc ON sc.item_id = i.id
            LEFT JOIN reports rp ON rp.item_id = i.id
            WHERE i.id IN ({marks})"""
    with get_conn() as conn:
        rows = conn.execute(q, ids).fetchall()
    return [dict(r) for r in rows]


def mark_item_read(item_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE items SET read=1 WHERE id=?", (item_id,))


# ---------------- favorites ----------------
def toggle_favorite(ftype: str, target_id: int) -> bool:
    """切换收藏。返回 True=已收藏 / False=已取消。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM favorites WHERE type=? AND target_id=?",
            (ftype, target_id)).fetchone()
        if row:
            conn.execute("DELETE FROM favorites WHERE id=?", (row["id"],))
            return False
        conn.execute(
            "INSERT INTO favorites(type, target_id, created_at) VALUES(?,?,?)",
            (ftype, target_id, now_iso()))
        return True


def favorite_ids(ftype: str) -> set:
    with get_conn() as conn:
        rows = conn.execute("SELECT target_id FROM favorites WHERE type=?",
                            (ftype,)).fetchall()
    return {r["target_id"] for r in rows}


def clear_favorites(ftype: str) -> int:
    """清空某类收藏，返回删除条数。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM favorites WHERE type=?", (ftype,))
        return cur.rowcount


def list_favorites(ftype: str):
    if ftype == "item":
        q = """SELECT f.id AS fav_id, f.created_at AS fav_at,
                      i.*, s.name_cn AS source_name_cn, s.name_en AS source_name_en,
                      sc.is_match, sc.criteria AS sc_criteria, sc.score,
                      rp.id AS report_id, rp.status AS report_status
               FROM favorites f
               JOIN items i ON i.id = f.target_id
               JOIN sources s ON s.id = i.source_id
               LEFT JOIN screenings sc ON sc.item_id = i.id
               LEFT JOIN reports rp ON rp.item_id = i.id
               WHERE f.type='item' ORDER BY f.id DESC"""
    elif ftype == "multi":
        # v2.13.6 综合专报收藏独立分支（原 else 硬编码 type='report'，multi 收藏永远查不出）
        q = """SELECT f.id AS fav_id, f.created_at AS fav_at, m.*,
                      '' AS item_url, '' AS item_title
               FROM favorites f
               JOIN multi_reports m ON m.id = f.target_id
               WHERE f.type='multi' ORDER BY f.id DESC"""
    else:
        q = """SELECT f.id AS fav_id, f.created_at AS fav_at, r.*,
                      i.url AS item_url, i.title AS item_title
               FROM favorites f
               JOIN reports r ON r.id = f.target_id
               JOIN items i ON i.id = r.item_id
               WHERE f.type='report' ORDER BY f.id DESC"""
    with get_conn() as conn:
        rows = conn.execute(q).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if ftype in ("report", "multi") and isinstance(d.get("body"), str):
            try:
                d["body"] = json.loads(d["body"])
            except (ValueError, TypeError):
                d["body"] = []
        out.append(d)
    return out


# ---------------- hotspots ----------------
def save_hotspots(model: str, matched_count: int, analysis: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO hotspots(created_at, model, matched_count, analysis) VALUES(?,?,?,?)",
            (now_iso(), model, matched_count, analysis))
        # 只保留最近 10 次分析
        conn.execute(
            "DELETE FROM hotspots WHERE id NOT IN "
            "(SELECT id FROM hotspots ORDER BY id DESC LIMIT 10)")
        return cur.lastrowid


def list_hotspots(limit=10):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM hotspots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["analysis"] = json.loads(d["analysis"])
        except (ValueError, TypeError):
            d["analysis"] = []
        out.append(d)
    return out


# ---------------- screenings ----------------
def upsert_screening(item_id, is_match, criteria, match_points, reason, model,
                     score=None, score_detail=None):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO screenings(item_id, is_match, criteria, match_points, reason, model,
                                      score, score_detail, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                 is_match=excluded.is_match, criteria=excluded.criteria,
                 match_points=excluded.match_points, reason=excluded.reason,
                 model=excluded.model, score=excluded.score,
                 score_detail=excluded.score_detail, created_at=excluded.created_at""",
            (item_id, int(is_match), json.dumps(criteria, ensure_ascii=False),
             match_points or "", reason or "", model,
             score, json.dumps(score_detail, ensure_ascii=False) if score_detail else "",
             now_iso()),
        )


# ---------------- reports ----------------
def save_report(item_id, title, editor_note, body, conclusion, model,
                status="draft", abandon_reason="", citations=None, evidence=None):
    ts = now_iso()
    body_json = json.dumps(body, ensure_ascii=False)
    cites_json = json.dumps(citations or [], ensure_ascii=False)
    evid_json = json.dumps(evidence or [], ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reports(item_id, title, editor_note, body, conclusion,
                                   citations, evidence,
                                   status, model, abandon_reason, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                 title=excluded.title, editor_note=excluded.editor_note,
                 body=excluded.body, conclusion=excluded.conclusion,
                 citations=excluded.citations, evidence=excluded.evidence,
                 model=excluded.model, status=excluded.status,
                 abandon_reason=excluded.abandon_reason, updated_at=excluded.updated_at""",
            (item_id, title, editor_note, body_json, conclusion, cites_json,
             evid_json, status, model, abandon_reason, ts, ts),
        )


def list_reports(status=None):
    q = """SELECT r.*, i.url AS item_url, i.title AS item_title, i.published_at,
                  s.name_cn AS source_name_cn
           FROM reports r
           JOIN items i ON i.id = r.item_id
           JOIN sources s ON s.id = i.source_id"""
    params = []
    if status:
        q += " WHERE r.status IN (" + ",".join("?" * len(status)) + ")"
        params.extend(status)
    # v2.9.5 按处理时间排（最近编辑/导出的在最前）
    q += " ORDER BY COALESCE(NULLIF(r.updated_at, ''), r.created_at) DESC"
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["body"] = json.loads(d["body"])
        d["citations"] = json.loads(d["citations"]) if d.get("citations") else []
        d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else []
        out.append(d)
    return out


def get_report(report_id: int):
    with get_conn() as conn:
            row = conn.execute(
                """SELECT r.*, i.url AS item_url, i.title AS item_title,
                          i.title_zh AS item_title_zh, i.summary AS item_summary,
                          i.published_at AS item_published_at,
                          s.name_cn AS source_name_cn
                   FROM reports r
                   JOIN items i ON i.id=r.item_id
                   JOIN sources s ON s.id=i.source_id
                   WHERE r.id=?""",
            (report_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["body"] = json.loads(d["body"])
    d["citations"] = json.loads(d["citations"]) if d.get("citations") else []
    d["evidence"] = json.loads(d["evidence"]) if d.get("evidence") else []
    return _parse_chat_log(d)


def get_report_by_item(item_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM reports WHERE item_id=?",
                           (item_id,)).fetchone()
    return get_report(row["id"]) if row else None


def update_report(report_id, fields: dict):
    # v2.13.3 放开 citations/evidence/model（弃编译线索补材料重编时整行更新）
    allowed = {"title", "editor_note", "body", "conclusion", "status",
               "issue_no", "docx_path", "exported_at", "abandon_reason",
               "citations", "evidence", "model"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("body", "citations", "evidence"):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(now_iso())
    params.append(report_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id=?", params)


# ---------------- chat log（v2.9.2 AI 修改会话持久化） ----------------
def append_chat_log(kind: str, rid: int, entry: dict, cap: int = 30):
    """追加一轮 AI 修改记录（含修改前/后快照，供会话恢复与版本链重建）。"""
    table = "multi_reports" if kind == "multi" else "reports"
    with get_conn() as conn:
        row = conn.execute(f"SELECT chat_log FROM {table} WHERE id=?",
                           (rid,)).fetchone()
        if not row:
            return
        try:
            log = json.loads(row["chat_log"]) if row["chat_log"] else []
        except (ValueError, TypeError):
            log = []
        log.append(entry)
        if len(log) > cap:
            log = log[-cap:]
        conn.execute(f"UPDATE {table} SET chat_log=? WHERE id=?",
                     (json.dumps(log, ensure_ascii=False), rid))


def update_evidence_zh(kind: str, rid: int, no: int, zh: str):
    """v2.10.0 把某编号取证摘录的中文译文写回 evidence（quote_zh），下次秒开。"""
    table = "multi_reports" if kind == "multi" else "reports"
    with get_conn() as conn:
        row = conn.execute(f"SELECT evidence FROM {table} WHERE id=?", (rid,)).fetchone()
        if not row:
            return False
        try:
            ev = json.loads(row["evidence"]) if row["evidence"] else []
        except (ValueError, TypeError):
            ev = []
        for e in ev:
            if int(e.get("no", -1)) == int(no):
                e["quote_zh"] = (zh or "").strip()
        conn.execute(f"UPDATE {table} SET evidence=? WHERE id=?",
                     (json.dumps(ev, ensure_ascii=False), rid))
        return True


def set_chat_log(kind: str, rid: int, chat: list, cap: int = 30):
    """v2.9.5 全量恢复聊天记录（撤销「清空/删除单轮」用，列表由前端回传）。"""
    table = "multi_reports" if kind == "multi" else "reports"
    with get_conn() as conn:
        row = conn.execute(f"SELECT id FROM {table} WHERE id=?", (rid,)).fetchone()
        if not row:
            return False
        conn.execute(f"UPDATE {table} SET chat_log=? WHERE id=?",
                     (json.dumps((chat or [])[-cap:], ensure_ascii=False), rid))
        return True


def _parse_chat_log(d: dict):
    raw = d.get("chat_log")
    if raw:
        try:
            d["chat_log"] = json.loads(raw)
        except (ValueError, TypeError):
            d["chat_log"] = []
    else:
        d["chat_log"] = []
    return d


def remove_chat_log_entry(kind: str, rid: int, index: int):
    """删除第 index 轮 AI 修改记录（0 基；越界静默）。返回剩余轮数。"""
    table = "multi_reports" if kind == "multi" else "reports"
    with get_conn() as conn:
        row = conn.execute(f"SELECT chat_log FROM {table} WHERE id=?",
                           (rid,)).fetchone()
        if not row:
            return 0
        try:
            log = json.loads(row["chat_log"]) if row["chat_log"] else []
        except (ValueError, TypeError):
            log = []
        if 0 <= index < len(log):
            log.pop(index)
        conn.execute(f"UPDATE {table} SET chat_log=? WHERE id=?",
                     (json.dumps(log, ensure_ascii=False), rid))
        return len(log)


def clear_chat_log(kind: str, rid: int):
    table = "multi_reports" if kind == "multi" else "reports"
    with get_conn() as conn:
        conn.execute(f"UPDATE {table} SET chat_log='' WHERE id=?", (rid,))


# ---------------- multi reports（v2.8 综合专报） ----------------
def save_multi_report(title, editor_note, body, conclusion, citations,
                      item_ids, model, status="draft", abandon_reason=""):
    ts = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO multi_reports(title, editor_note, body, conclusion,
                                         citations, item_ids, status, model,
                                         abandon_reason, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (title, editor_note, json.dumps(body, ensure_ascii=False),
             conclusion, json.dumps(citations, ensure_ascii=False),
             json.dumps(item_ids), status, model, abandon_reason, ts, ts))
        return cur.lastrowid


def update_multi_report(mid: int, fields: dict):
    allowed = {"title", "editor_note", "body", "conclusion", "citations",
               "status", "docx_path", "exported_at", "abandon_reason",
               "model", "issue_no"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("body", "citations",):
            v = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(now_iso())
    params.append(mid)
    with get_conn() as conn:
        conn.execute(f"UPDATE multi_reports SET {', '.join(sets)} WHERE id=?", params)


def list_multi_reports():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM multi_reports
               ORDER BY COALESCE(NULLIF(updated_at, ''), created_at) DESC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["body"] = json.loads(d["body"]) if d["body"] else []
        d["citations"] = json.loads(d["citations"]) if d["citations"] else []
        d["item_ids"] = json.loads(d["item_ids"]) if d["item_ids"] else []
        out.append(d)
    return out


def get_multi_report(mid: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM multi_reports WHERE id=?", (mid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["body"] = json.loads(d["body"]) if d["body"] else []
    d["citations"] = json.loads(d["citations"]) if d["citations"] else []
    d["item_ids"] = json.loads(d["item_ids"]) if d["item_ids"] else []
    # 附线索摘要（预览/人工编辑对照用）
    d["sources_info"] = []
    for iid in d["item_ids"]:
        it = get_item(iid)
        d["sources_info"].append(
            {"item_id": iid,
             **({"source": it.get("source_name_cn"), "title": it.get("title"),
                 "title_zh": it.get("title_zh") or "",
                 "url": it.get("url") or "", "summary": it.get("summary") or "",
                 "published_at": it.get("published_at")} if it else {})})
    return _parse_chat_log(d)




def delete_items(ids: list) -> dict:
    """v2.13.6 线索数据库删除（单/批）：连带 screenings 与收藏；
    已编译成专报的线索拒绝删除（先删专报），返回 {deleted, blocked}。"""
    ids = [int(x) for x in (ids or []) if str(x).strip().lstrip("-").isdigit()]
    if not ids:
        return {"deleted": 0, "blocked": []}
    marks = ",".join("?" * len(ids))
    deleted, blocked = 0, []
    with get_conn() as conn:
        rep = dict((r["item_id"], r["rid"]) for r in conn.execute(
            f"SELECT item_id, id AS rid FROM reports WHERE item_id IN ({marks})", ids))
        # multi_reports 用 item_ids JSON 数组（无 item_id 列），逐条 JSON 匹配
        mul = {}
        for r in conn.execute(
                f"SELECT id AS mid, item_ids FROM multi_reports WHERE item_ids IS NOT NULL"):
            try:
                arr = json.loads(r["item_ids"] or "[]")
            except (ValueError, TypeError):
                continue
            for iid in ids:
                if iid in arr:
                    mul[iid] = r["mid"]
        for iid in ids:
            if iid in rep or iid in mul:
                blocked.append({"id": iid,
                                "reason": ("已有单篇专报 #%s" % rep[iid]) if iid in rep
                                else ("被综合专报 #%s 引用" % mul[iid])})
                continue
            conn.execute(f"DELETE FROM screenings WHERE item_id=?", (iid,))
            conn.execute(f"DELETE FROM favorites WHERE type='item' AND target_id=?", (iid,))
            conn.execute(f"DELETE FROM items WHERE id=?", (iid,))
            deleted += 1
    return {"deleted": deleted, "blocked": blocked}


def delete_multi_report(mid: int):
    # v2.13.6 连带清理收藏（type='multi'），避免悬挂收藏行
    with get_conn() as conn:
        conn.execute("DELETE FROM multi_reports WHERE id=?", (mid,))
        conn.execute("DELETE FROM favorites WHERE type='multi' AND target_id=?", (mid,))


# ---------------- fetch runs ----------------
def start_fetch_run(window_days):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO fetch_runs(started_at, window_days) VALUES(?,?)",
            (now_iso(), window_days),
        )
        return cur.lastrowid


def finish_fetch_run(run_id, ok, fail, new_items, failures):
    with get_conn() as conn:
        conn.execute(
            """UPDATE fetch_runs SET finished_at=?, ok_sites=?, fail_sites=?,
               new_items=?, failures=? WHERE id=?""",
            (now_iso(), ok, fail, new_items,
             json.dumps(failures, ensure_ascii=False), run_id),
        )


def last_fetch_run():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    d = dict(row) if row else None
    if d and d.get("failures"):
        try:
            d["failures"] = json.loads(d["failures"])
        except (ValueError, TypeError):
            pass
    return d


def list_fetch_runs(limit=15):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM fetch_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["failures"] = json.loads(d["failures"]) if d["failures"] else []
        except (ValueError, TypeError):
            d["failures"] = []
        out.append(d)
    return out
