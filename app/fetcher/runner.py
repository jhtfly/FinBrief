"""采集调度：线程池并发抓取（v2.6）+ 逐站执行失败记录继续 + 协作打断回滚。"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from app import db
from app.fetcher import html_sites, rss
from app.paths import SOURCES_YAML
from app.tasks import TaskCancelled


def load_preset_sources():
    """把 sources.yaml 预置信源写入数据库（已有则更新配置，保留启停/常用归属）。

    v2.6：被用户"永久删除"的预置信源（记录在 settings.excluded_preset_keys）不再装载。
    """
    with open(SOURCES_YAML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    excluded = {k.strip() for k in
                (db.get_setting("excluded_preset_keys") or "").split(",") if k.strip()}
    for i, src in enumerate(data.get("sources", [])):
        key = src["key"]
        if key in excluded:
            continue
        cur = db.get_source(key)
        if cur:  # 保留用户设置的 enabled / 常用归属
            src["enabled"] = cur["enabled"]
            src["in_common"] = cur["in_common"]
            src["src_group"] = cur["src_group"]
        src["sort_no"] = i
        db.upsert_source(src)
    return len(data.get("sources", []))


def _proxy() -> str | None:
    s = db.all_settings()
    if s.get("proxy_enabled") == "1" and s.get("proxy_url"):
        return s["proxy_url"]
    return None


def _fetch_one(source: dict, timeout: int, window_days: int, proxy):
    cfg = source["config"]
    if source["mode"] == "rss":
        return rss.fetch_rss(cfg["rss_url"], timeout, window_days, proxy)
    if source["mode"] == "sitemap":
        return html_sites.fetch_sitemap(cfg, timeout, window_days, proxy)
    return html_sites.fetch_html_list(cfg, timeout, window_days, proxy)


def run_fetch(source_keys: list | None = None, progress_cb=None,
              should_cancel=None) -> dict:
    """采集一轮。返回统计 dict。逐站执行，单站失败记录后继续，不中断。

    v2.6：站点抓取在 fetch_concurrency 个线程上并行（网络 IO 并发），
    入库仍在主线程串行进行（保证渐进展示与打断回滚的一致性）。
    打断：主循环在每站入库前检查标志，被打破时删除本轮已入库的线索并抛
    TaskCancelled——呈现为"恢复到未执行该任务前的状态"。

    progress_cb(done, total, site_name, ok_n, fail_n, new_n)
    """
    settings = db.all_settings()
    timeout = int(settings.get("request_timeout", 20))
    window_days = int(settings.get("window_days", 7))
    proxy = _proxy()
    try:
        workers = max(1, min(10, int(settings.get("fetch_concurrency", "3"))))
    except (TypeError, ValueError):
        workers = 3

    sources = db.list_sources(only_enabled=True, in_common=True)  # 只采集常用组
    if source_keys:
        wanted = set(source_keys)
        sources = [s for s in sources if s["key"] in wanted]
    if not sources:
        return {"ok": 0, "fail": 0, "new": 0, "failures": [], "message": "无启用的信源"}

    run_id = db.start_fetch_run(window_days)
    total = len(sources)
    db.log("fetch", f"开始采集 {total} 个站点（窗口 {window_days} 天，"
                    f"代理: {'开' if proxy else '直连'}，抓取并发 {workers}）")
    ok = fail = new_items = 0
    inserted_ids = []   # 本轮新入库线索 id（打断回滚用）
    failures = []

    src_index = {s["key"]: i + 1 for i, s in enumerate(sources, 1)}

    # 并发抓取 + 完成即串行入库（保留渐进展示；打断可回滚已入库部分）
    def _site_fetch(src):
        try:
            items, err = _fetch_one(src, timeout, window_days, proxy)
        except Exception as e:  # 任何未预期异常都不能中断整轮
            items, err = [], f"异常: {type(e).__name__}: {e}"
        return src, items, err

    inserted_ids = []   # 本轮新入库线索 id（打断回滚用）
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_site_fetch, s): s["key"] for s in sources}
            for fut in as_completed(futures):
                if should_cancel and should_cancel():
                    for f2 in futures:
                        f2.cancel()
                    raise TaskCancelled("任务已被用户打断")
                src, items, err = fut.result()
                done = len([f3 for f3 in futures if f3.done()])
                if err is not None:
                    fail += 1
                    failures.append({"source": src["name_cn"], "key": src["key"],
                                     "error": err})
                    db.update_source_fetch_status(src["key"], False, 0, err)
                    db.log("fetch", f"[{src['name_cn']}] {err}", "warn")
                else:
                    ok += 1
                    added = 0
                    for it in items:
                        iid = db.insert_item(src["id"], it["title"], it["url"],
                                             it["published_at"], it["summary"])
                        if iid:
                            inserted_ids.append(iid)
                            added += 1
                    new_items += added
                    db.update_source_fetch_status(src["key"], True, len(items), "")
                    db.log("fetch", f"[{src['name_cn']}] 获取 {len(items)} 条，新入库 {added} 条")
                if progress_cb:
                    try:
                        progress_cb(done, total, src["name_cn"], ok, fail, new_items)
                    except Exception:
                        pass
        if new_items == 0 and ok > 0 and ok < len(sources) and fail >= max(3, len(sources) // 3):
            db.log("fetch", f"本轮 {fail} 个站点访问失败、无新增线索，若需覆盖境外站点请开启代理后重试", "warn")
        db.finish_fetch_run(run_id, ok, fail, new_items, failures)
        db.log("fetch", f"采集完成：成功 {ok}，失败 {fail}，新增 {new_items}")
        return {"ok": ok, "fail": fail, "new": new_items, "failures": failures,
                "run_id": run_id, "added_ids": inserted_ids}
    except TaskCancelled:
        # 打断回滚：删除本轮已入库线索，恢复到任务执行前状态
        # （v2.18.0 改调 rollback_delete_items——delete_items 已是数据库页的
        #   {deleted, blocked} 契约版，且会拒绝删除有专报引用的线索）
        n = db.rollback_delete_items(inserted_ids)
        db.finish_fetch_run(run_id, ok, fail, 0, failures)
        db.log("fetch", f"采集任务被打断：已回滚本轮 {n} 条新入库线索", "warn")
        raise


def probe_site(source: dict, timeout: int | None = None):
    """连通性自检：单站抓一次（窗口放宽到 30 天，仅看是否可达）。
    v2.11.1 支持调用方指定 timeout（RSSHub 粘贴链接多实例并行试连用短超时）。"""
    settings = db.all_settings()
    timeout = int(timeout or min(int(settings.get("request_timeout", 20)), 15))
    proxy = _proxy()
    t0 = time.time()
    items, err = _fetch_one(source, timeout, 30, proxy)
    # v2.13.5 附带采集样本（单源测试右键查看用；仅标题/链接/日期，最多 6 条）
    sample = [{"title": (it.get("title") or "")[:80],
               "url": it.get("url") or "",
               "published_at": (it.get("published_at") or "")[:16]}
              for it in items[:6]]
    return {"ok": err is None, "error": err, "items": len(items),
            "seconds": round(time.time() - t0, 1), "sample": sample}
