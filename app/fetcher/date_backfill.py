"""日期补全：对列表页拿不到发布日期的线索，访问详情页提取日期。

v2.21 并发提速：逐条详情页访问改为线程池并发（并发数=设置 fetch_concurrency）；
打断语义不变——已补的日期整体回滚。"""
import threading
from concurrent.futures import ThreadPoolExecutor

from app import db
from app.fetcher import html_sites
from app.tasks import TaskCancelled


def backfill_dates(item_ids: list | None = None, limit: int = 15,
                   progress_cb=None, should_cancel=None) -> dict:
    """补全线索发布日期。item_ids 为空时自动取最近的无日期线索（上限 limit）。
    v2.6 支持协作打断：被打断时复位本轮已补的日期。
    v2.21 并发：详情页抓取在 fetch_concurrency 个线程上并行，进度按提交序推进。"""
    if item_ids:
        items = []
        for iid in item_ids:
            it = db.get_item(iid)
            if it and not it.get("published_at"):
                items.append(it)
    else:
        items = db.null_date_items(limit)
    proxy = None
    s = db.all_settings()
    if s.get("proxy_enabled") == "1" and s.get("proxy_url"):
        proxy = s["proxy_url"]
    done_ids = []   # 本轮已补（打断回滚用）
    err_reasons = []   # v2.9.4 失败原因（运行日志 + 返回值）
    lock = threading.Lock()
    done = [0]

    try:
        try:
            workers = max(1, min(10, int(s.get("fetch_concurrency", "3"))))
        except (TypeError, ValueError):
            workers = 3
        if items:
            workers = min(workers, len(items))

        def _one(it):
            """单条详情页抓取（线程内）；返回 (item, pub|None, reason|None)。"""
            try:
                pub = html_sites.extract_date_from_page(it["url"], proxy=proxy)
                return it, pub, None
            except Exception as e:
                return it, None, f"{type(e).__name__}: {e}"

        cancel_hit = [False]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = []
            for idx, it in enumerate(items, 1):
                if should_cancel and should_cancel():
                    cancel_hit[0] = True
                    break
                if progress_cb:
                    try:
                        progress_cb(idx, len(items), done[0])
                    except Exception:
                        pass
                futs.append(pool.submit(_one, it))
            # v2.21 打断检查放到「提交结果前」：一旦请求打断，剩余结果不受理，
            # 全部已写日期由尾部统一回滚（与 v2.6 语义一致）
            for fut in futs:
                if should_cancel and should_cancel():
                    for f2 in futs:
                        f2.cancel()
                    break
                it, pub, reason = fut.result()
                if pub:
                    db.update_item_published_at(it["id"], pub)
                    with lock:
                        done_ids.append(it["id"])
                        done[0] += 1
                    db.log("backfill", f"[{it['title'][:40]}] 补全日期 {pub[:10]}")
                else:
                    reason = reason or "页面未识别到发布日期（或访问失败）"
                    with lock:
                        err_reasons.append(f"{(it.get('title') or '')[:28]}：{reason[:110]}")
                    db.log("backfill", f"[{(it.get('title') or '')[:40]}] 补全失败：{reason}"[:200],
                           "warn")
        if cancel_hit[0]:
            raise TaskCancelled("任务已被用户打断")
    except TaskCancelled:
        db.reset_items_field(done_ids, "published_at")
        db.log("backfill", f"日期补全被打断：已回滚本轮 {len(done_ids)} 条日期", "warn")
        raise
    db.log("backfill", f"日期补全完成：{done[0]}/{len(items)} 条"
                       + (f"，失败 {len(err_reasons)}" if err_reasons else ""))
    return {"ok": done[0], "total": len(items), "fail_reasons": err_reasons}
