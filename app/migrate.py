# -*- coding: utf-8 -*-
"""v2.21.7 数据迁移：从旧版本程序目录一键并入本机数据库。

策略（附加式，不动本机已有数据）：
- settings：逐键覆盖写（旧值真实存在时），随后同步 settings.json 镜像
- sources：按 key upsert（同名 key 保留本机配置，仅补缺失的源）
- 其余业务表（items/screenings/reports/fetch_runs/favorites/hotspots/
  run_logs/task_runs/token_usage/multi_reports）：旧库独有行整行附加，
  AUTOINCREMENT 主键重编（按本机该表 max(id)+1 起偏移，避免主键冲突）；
  行内按列名对齐（新旧版本列差异自动取交集，缺列用默认值）
- api_key 等 DPAPI 密文换机不可解：原样搬运 + 返回提示（用户需重填）
- 老库路径下 finbrief.db 不存在时支持 data\\finbrief.db 子目录自动定位
"""
import json
import os
import sqlite3

from app import db
from app import paths as app_paths

# 附加式迁移的表清单（settings/sources 单独处理）
_APPEND_TABLES = [
    "items", "screenings", "reports", "fetch_runs", "favorites",
    "hotspots", "run_logs", "task_runs", "token_usage", "multi_reports",
]


def _locate_db(root: str) -> str:
    """在给定程序目录中定位 finbrief.db（根目录或 data\\ 下）。"""
    cands = [
        os.path.join(root, "finbrief.db"),
        os.path.join(root, "data", "finbrief.db"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def _tables(cur) -> set:
    return {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _cols(cur, table: str) -> list:
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate_from(old_root: str, progress_cb=None) -> dict:
    """把 old_root 程序目录的数据并入本机库。返回统计与提示。"""
    old_root = (old_root or "").strip().strip('"')
    if not old_root or not os.path.isdir(old_root):
        return {"ok": False, "error": "目录不存在，请填写旧版本程序的根目录（exe 所在目录）"}
    old_db = _locate_db(old_root)
    if not old_db:
        return {"ok": False,
                "error": "该目录下未找到 finbrief.db（旧版本数据文件）——请确认是程序根目录"}
    try:
        src = sqlite3.connect(f"file:{old_db.replace('?', '_').replace('#', '_')}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        return {"ok": False, "error": f"旧数据库无法打开：{e}"}

    stats = {}
    notes = []
    moved_keys = []

    with db.get_conn() as dst:
        src_tables = _tables(src.cursor())
        dst_tables = _tables(dst.cursor())

        # ---------- settings：旧值覆盖（真实存在的键） ----------
        n_set = 0
        if "settings" in src_tables:
            for row in src.execute("SELECT key, value FROM settings").fetchall():
                k, v = row["key"], row["value"]
                if v is None or v == "":
                    continue
                dst.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
                n_set += 1
                moved_keys.append(k)
        stats["settings"] = n_set

        # ---------- sources：按 key upsert（本机已有同 key 仅补缺） ----------
        n_src_add = n_src_keep = 0
        if "sources" in src_tables:
            scols = _cols(src.cursor(), "sources")
            dcols = _cols(dst.cursor(), "sources")
            cols = [c for c in dcols if c in scols and c != "id"]   # 新旧列交集（版本差异安全）
            # 新库 NOT NULL 列的缺省策略：旧库没有的列→默认值；有但值为 NULL→也用默认值顶替
            dflts = {}
            for (cid, name, ctype, notnull, dflt, pk) in dst.execute(
                    "PRAGMA table_info(sources)").fetchall():
                if not notnull:
                    continue
                if dflt is not None:
                    dflts[name] = dflt
                elif "INT" in (ctype or "").upper():
                    dflts[name] = 0
                else:
                    dflts[name] = ""
            fills = {k: v for k, v in dflts.items() if k not in cols}
            all_cols = cols + list(fills)
            have = {r[0] for r in dst.execute("SELECT key FROM sources").fetchall()}
            for row in src.execute(f"SELECT {','.join(cols)} FROM sources").fetchall():
                if row["key"] in have:
                    n_src_keep += 1
                    continue
                vals = [row[c] if row[c] is not None else dflts.get(c, row[c])
                        for c in cols] + list(fills.values())
                dst.execute(
                    f"INSERT INTO sources({','.join(all_cols)}) VALUES({','.join('?' * len(all_cols))})",
                    vals)
                n_src_add += 1
        stats["sources_new"] = n_src_add
        stats["sources_kept"] = n_src_keep

        # ---------- 业务表：附加式（主键重编） ----------
        for t in _APPEND_TABLES:
            if t not in src_tables or t not in dst_tables:
                continue
            scols = _cols(src.cursor(), t)
            dcols = _cols(dst.cursor(), t)
            common = [c for c in dcols if c in scols and c != "id"]
            if not common:
                continue
            # NOT NULL 列缺省（同 sources 策略：旧值 NULL/缺列 → 默认值顶替）
            dflts = {}
            for (cid, name, ctype, notnull, dflt, pk) in dst.execute(
                    f"PRAGMA table_info({t})").fetchall():
                if not notnull:
                    continue
                if dflt is not None:
                    dflts[name] = dflt
                elif "INT" in (ctype or "").upper():
                    dflts[name] = 0
                else:
                    dflts[name] = ""
            fills = {k: v for k, v in dflts.items() if k not in common}
            all_cols = common + list(fills)
            # 本机已有行的内容指纹（防重复并入：同表同内容跳过）
            try:
                fp = {tuple(str(r[c]) for c in common)
                      for r in dst.execute(f"SELECT {','.join(common)} FROM {t}").fetchall()}
            except sqlite3.Error:
                fp = set()
            order = "id" if "id" in scols else common[0]
            n = 0
            for row in src.execute(f"SELECT {','.join(scols)} FROM {t} ORDER BY {order}").fetchall():
                # 老库行内容指纹（用交集列）
                key = tuple(str(row[c]) for c in common if c in row.keys())
                if key in fp:
                    continue
                vals = [row[c] if row[c] is not None else dflts.get(c, row[c])
                        for c in common] + list(fills.values())
                dst.execute(
                    f"INSERT INTO {t}({','.join(all_cols)}) VALUES({','.join('?' * len(all_cols))})",
                    vals)
                n += 1
            if n:
                stats[t] = n

        # favorites 特殊：按 (type,target_id) 去重保留（上面指纹含 created_at 可能重复并入）
        # —— 已由内容指纹覆盖（同 target 同时间即跳过）。

    src.close()

    # settings.json 镜像同步（含迁移来的设置）
    try:
        db.sync_settings_file()
    except Exception:
        pass

    # DPAPI 密文换机不可解 → 原样搬运，提示重填
    secret_keys = [k for k in moved_keys
                   if k in ("api_key", "apihz_id", "apihz_key")]
    if secret_keys:
        notes.append("加密项（API Key / 共享 ID、KEY）已按原样搬运；"
                     "Windows 加密绑定机器，换机后请在本机重新填写一次即可生效。")

    total = sum(v for v in stats.values() if isinstance(v, int))
    return {"ok": True, "stats": stats, "total": total, "notes": notes,
            "old_db": old_db}
