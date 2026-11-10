# -*- coding: utf-8 -*-
"""RSSHub 路由库子模块（v2.9.9，方案 A：公共实例；v2.11.1 粘贴链接多实例并行试连）。

ROUTE_BOOK 为从 RSSHub 官方仓库 lib/routes 抓取解析的路由签名（path 模板），
公共实例实测多数路由会因上游风控偶发 403/503——子模块提供"试连"如实反馈，
成功后一键添加为自定义信源（启停/删除走通用信源管理）。
v2.11.1：官方路由检索改为跳转官方文档站 docs.rsshub.app（外链，用户自行检索）；
add_feed_url 解析路由 path 后在原实例/当前保存实例/预设镜像并行试连（短超时），
首个成功者入库（默认停用），全部失败返回逐实例明细不写库——官方 rsshub.app
对程序侧 403（官方限流，仅对 Folo 等合作阅读器放行），同一路由 path 在任意
实例都能输出同一订阅。"""
import re

from app import db
from app.fetcher import runner

BASE_KEY = "rsshub_base"
DEFAULT_BASE = "https://rsshub.rssforever.com"   # 公共实例之一，直连可达（rsshub.app 被官方限为仅测试）

ROUTE_BOOK = [
    {"cat": "财经媒体", "name_cn": "CNBC（官方 RSS，推荐，实测 29 条）", "path": "/cnbc/rss", "note": "无需参数，最稳"},
    {"cat": "财经媒体", "name_cn": "彭博 Bloomberg", "path": "/bloomberg", "note": "可选 site 参数"},
    {"cat": "财经媒体", "name_cn": "彭博 作者专栏", "path": "/bloomberg/authors/:id/:slug/:source?", "note": "需作者 id"},
    {"cat": "财经媒体", "name_cn": "FT 中文网（频道）", "path": "/ftchinese/channel/:language/:channel?", "note": "language=chinese/english"},
    {"cat": "财经媒体", "name_cn": "FT myFT 个性化", "path": "/ft/myft/:key", "note": "需 myFT key"},
    {"cat": "财经媒体", "name_cn": "华尔街日报 WSJ", "path": "/wsj/:lang/:category?", "note": "lang=en-us 等"},
    {"cat": "财经媒体", "name_cn": "路透调查报告", "path": "/reuters/investigates", "note": "特殊报道合集"},
    {"cat": "财经媒体", "name_cn": "经济学人（栏目全文）", "path": "/economist/:endpoint", "note": "endpoint=latest 等"},
    {"cat": "财经媒体", "name_cn": "经济学人 Espresso", "path": "/economist/espresso", "note": "晨报"},
    {"cat": "财经媒体", "name_cn": "经济学人 全球商业评论", "path": "/economist/global-business-review/:language?", "note": "可带语言"},
    {"cat": "财经媒体", "name_cn": "巴伦中文网", "path": "/barronschina/:id?", "note": "栏目可选"},
    {"cat": "媒体综合", "name_cn": "纽约时报", "path": "/nytimes", "note": "可选 lang"},
    {"cat": "媒体综合", "name_cn": "纽约时报 RSS 分类", "path": "/nytimes/rss/:cat?", "note": "cat=front-page 等"},
    {"cat": "媒体综合", "name_cn": "卫报中国", "path": "/theguardian/china", "note": ""},
    {"cat": "媒体综合", "name_cn": "卫报社论", "path": "/theguardian/editorial", "note": ""},
    {"cat": "监管与机构", "name_cn": "IMF 国家报告", "path": "/imf/country-reports/:country?", "note": "可按国家过滤"},
    {"cat": "监管与机构", "name_cn": "美财政部新闻稿", "path": "/treasury/press-releases/:category?", "note": "分类可选"},
    {"cat": "个股与投资", "name_cn": "投资者（聚合）", "path": "/investor/:id{.+}?", "note": "id 形如 site/新闻源"},
    {"cat": "个股与投资", "name_cn": "FX Markets（频道）", "path": "/fx-markets/:channel", "note": "付费尝鲜"},
]


def get_base():
    v = (db.get_setting(BASE_KEY) or "").strip()
    return v or DEFAULT_BASE


def set_base(v):
    v = (v or "").strip() or DEFAULT_BASE
    if not v.startswith("http"):
        return False
    db.set_setting(BASE_KEY, v.rstrip("/"))
    return True


def probe_path(path, base=None):
    """试连：按 实例地址+路由 拉一次 RSS，走通用 probe。"""
    path = (path or "").strip().lstrip("/")
    if not path:
        return {"ok": False, "items": 0, "seconds": 0, "error": "路由路径为空", "url": "", "sample": []}
    if not path.startswith("/"):
        path = "/" + path
    base = ((base or "").strip() or get_base()).rstrip("/")
    url = base + path
    src = {"key": "rsshub_probe", "name_cn": "RSSHub 试连", "mode": "rss",
           "config": {"rss_url": url}}
    r = runner.probe_site(src)
    # v2.13.9 试连带最近样本（右键路由测试浮层展示标题样本）
    return {"ok": r.get("ok", False), "items": r.get("items", 0),
            "seconds": r.get("seconds"), "error": r.get("error") or "", "url": url,
            "sample": r.get("sample") or []}


def add_source(path, name_cn, base=None):
    path = (path or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    base = ((base or "").strip() or get_base()).rstrip("/")
    url = base + path
    key = "rsshub_" + url.split("://", 1)[-1]
    key = key.split("?")[0].rstrip("/")
    key = "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in key).strip("_")[:60]
    if not key or len(key) < 8:
        key = "rsshub_custom_" + key
    src = {"key": key, "name_cn": name_cn or "RSSHub 路由", "name_en": "RSSHub",
           "category": "media", "mode": "rss", "config": {"rss_url": url},
           "enabled": 0, "note": "RSSHub", "src_group": "custom", "in_common": 0}
    db.upsert_source(src)
    return {"ok": True, "key": key, "url": url}


# ---------------- v2.11.1 粘贴订阅链接：多实例并行试连回退 ----------------
# 官方 rsshub.app 对程序侧 403（官方限流，仅对 Folo 等合作阅读器放行）；
# 同一路由 path 在任意 RSSHub 实例都能输出同一订阅——粘贴链接失败即换预设镜像。
FALLBACK_BASES = [
    "https://rsshub.ktachibana.party",
    "https://rsshub.rss.tips",
    "https://rsshub.rssforever.com",
]
OFFICIAL_BASE = "https://rsshub.app"
# v2.12.0 内置本机实例：程序自动拉起 127.0.0.1:1200（main.py），就绪后排在镜像前
LOCAL_BASE = f"http://127.0.0.1:1200"


def local_base_ready() -> bool:
    """内置本机 RSSHub 是否可连（socket 探测，开销极小）。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 1200), timeout=1.2):
            return True
    except OSError:
        return False


def probe_url(url, timeout=9):
    src = {"key": "rsshub_probe", "name_cn": "RSSHub 试连", "mode": "rss",
           "config": {"rss_url": url}}
    r = runner.probe_site(src, timeout=timeout)
    return {"ok": r.get("ok", False), "items": r.get("items", 0),
            "seconds": r.get("seconds"), "error": r.get("error") or ""}


def add_feed_url(raw, name_cn=None):
    """粘贴订阅链接一键测试添加（v2.11.1 多实例并行回退）：
    解析路由 path 后，在 [原实例, 当前保存实例, 预设镜像…] 并行试连（每实例 9s 短超时），
    首个成功者入库（默认停用）；全部失败返回逐实例明细，不写库。"""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return {"ok": False, "error": "请先粘贴 RSSHub 订阅链接或路由路径", "tried": []}
    m = re.match(r"^(https?://[^/]+)(/.+)$", raw, re.I)
    if m:
        orig_base = m.group(1).rstrip("/")
        path = m.group(2).rstrip("/")
    elif raw.startswith("/"):
        orig_base = ""
        path = raw.rstrip("/")
    else:
        return {"ok": False, "error": "无法识别：请粘贴完整订阅链接（https://…）或以 / 开头的路由路径", "tried": []}
    uniq = []
    ordered = [orig_base, get_base()]
    ordered += [LOCAL_BASE] if local_base_ready() else []
    ordered += FALLBACK_BASES
    for b in ordered:
        b = (b or "").strip().rstrip("/")
        if b and b not in uniq:
            uniq.append(b)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(5, len(uniq))) as ex:
        results = list(ex.map(lambda b: (b, probe_url(b + path, 9)), uniq))
    win = next(((b, r) for b, r in results if r["ok"]), None)
    if win is None:
        tried = [{"base": b, "ok": False, "error": (r["error"] or "")[:90],
                  "seconds": r["seconds"]} for b, r in results]
        err = next((r["error"] for _, r in results if r["error"]), "") or "全部实例试连失败"
        return {"ok": False, "error": err, "tried": tried, "path": path}
    b, r = win
    url = b + path
    key = "rsshub_" + url.split("://", 1)[-1]
    key = key.split("?")[0].rstrip("/")
    key = "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in key).strip("_")[:60]
    if not key or len(key) < 8:
        key = "rsshub_custom_" + key
    if not (name_cn or "").strip():
        name_cn = ("RSSHub " + (path or url))[:46]
    out_src = {"key": key, "name_cn": name_cn.strip(), "name_en": "RSSHub",
               "category": "media", "mode": "rss", "config": {"rss_url": url},
               "enabled": 0, "note": "RSSHub", "src_group": "custom", "in_common": 0}
    db.upsert_source(out_src)
    return {"ok": True, "key": key, "url": url, "items": r["items"],
            "seconds": r["seconds"], "name_cn": name_cn.strip(), "base_used": b,
            "path": path,
            "tried": [{"base": bb, "ok": rr["ok"], "items": rr["items"],
                       "seconds": rr["seconds"], "error": (rr["error"] or "")[:80]}
                      for bb, rr in results]}


# ---------------- v2.21 路由规则体检：失效路由自动迁移可用实例 ----------------
# 用户真实需求：上游网站格式变化后，本地内置（或某实例）路由代码过期——
# 把该路由的"订阅地址"迁移到仍在正常输出该路由的实例上，等于完成一次
# 无需整包部署的"路由规则更新"。

_RSSHUB_HOST_RE = re.compile(
    r"^(https?://)?(127\.0\.0\.1|localhost|rsshub\.app|rsshub\.rssforever\.com"
    r"|rsshub\.rss\.tips|rsshub\.ktachibana\.party)(:\d+)?(/|$)", re.I)


def is_rsshub_route_url(url: str) -> bool:
    """识别 RSSHub 路由订阅地址（主机在已知实例名单里）。"""
    return bool(_RSSHUB_HOST_RE.match((url or "").strip()))


def _candidate_bases(orig_base: str) -> list:
    ordered = [b for b in (orig_base, get_base()) if b]
    ordered += [LOCAL_BASE] if local_base_ready() else []
    ordered += list(FALLBACK_BASES)
    uniq = []
    for b in ordered:
        b = (b or "").strip().rstrip("/")
        if b and b not in uniq:
            uniq.append(b)
    return uniq


def route_heal(progress_cb=None, should_cancel=None, timeout=9) -> dict:
    _re = re
    """路由规则体检：逐源探测 RSSHub 路由；失败者在多实例并行试连，
    用仍能输出同一路由的实例替换其订阅地址（写库 + 备注迁移痕迹）。
    返回 {total, ok, migrated:[{key,name,old,new}], failed:[{name,error}]}。"""
    import os as _os
    srcs = [s for s in db.list_sources()
            if s.get("mode") == "rss" and (s.get("config") or {}).get("rss_url")
            and is_rsshub_route_url(s["config"]["rss_url"])]
    migrated, failed = [], []
    total = len(srcs)
    for i, s_ in enumerate(srcs, 1):
        if should_cancel and should_cancel():
            raise RuntimeError("路由体检已打断")
        if progress_cb:
            try:
                progress_cb(i, total, s_["name_cn"])
            except Exception:
                pass
        url = s_["config"]["rss_url"]
        m = _re.match(r"^(https?://[^/]+)(/.+)$", url, _re.I)
        orig_base, path = (m.group(1), m.group(2)) if m else ("", "/")
        first = probe_url(url, timeout=timeout)
        if first["ok"]:
            continue
        # 本址失效：逐候选并行试连
        cands = [b for b in _candidate_bases(orig_base) if b != orig_base]
        results = []
        if cands:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(5, len(cands))) as ex:
                results = list(ex.map(lambda b: (b, probe_url(b + path, timeout)), cands))
        win = next(((b, r) for b, r in results if r["ok"]), None)
        if not win:
            err = next(((r.get("error") or "试连失败") for _, r in results), "无可用实例")
            failed.append({"name": s_["name_cn"], "key": s_["key"],
                           "error": str(err)[:110]})
            db.log("rsshub", f"[路由体检] {s_['name_cn']} 各实例试连均失败：{str(err)[:90]}", "warn")
            continue
        b, r = win
        new_url = b + path
        note = (s_.get("note") or "")
        tag = f"v2.21 路由迁移 {db.now_iso()[:10]}（原实例 {orig_base.replace('http://', '').replace('https://', '')} 失效）"
        if tag[:24] not in note:
            note = (note + "；" + tag).strip("；")
        db.update_source_config(s_["key"], {"rss_url": new_url}, note=note[:300])
        migrated.append({"key": s_["key"], "name": s_["name_cn"],
                         "old": url, "new": new_url, "items": r["items"]})
        db.log("rsshub", f"[路由体检] {s_['name_cn']} 已迁移：{url} → {new_url}"
                         f"（正常 {r['items']} 条）")
    return {"total": total, "migrated": migrated, "failed": failed}
