"""日期补全：对列表页拿不到发布日期的线索，访问详情页提取日期。"""
from app import db
from app.fetcher import html_sites
from app.tasks import TaskCancelled


def backfill_dates(item_ids: list | None = None, limit: int = 15,
                   progress_cb=None, should_cancel=None) -> dict:
    """补全线索发布日期。item_ids 为空时自动取最近的无日期线索（上限 limit）。
    v2.6 支持协作打断：条目间检查标志，被打断时复位本轮已补的日期。"""
    if item_ids:
        items = []
        for iid in item_ids:
            it = db.get_item(iid)
            if it and not it.get("published_at"):
                items.append(it)
    else:
        items = db.null_date_items(limit)
    done = 0
    proxy = None
    s = db.all_settings()
    if s.get("proxy_enabled") == "1" and s.get("proxy_url"):
        proxy = s["proxy_url"]
    done_ids = []   # 本轮已补（打断回滚用）
    err_reasons = []   # v2.9.4 失败原因（运行日志 + 返回值）
    try:
        for i, it in enumerate(items, 1):
            if should_cancel and should_cancel():
                raise TaskCancelled("任务已被用户打断")
            if progress_cb:
                progress_cb(i, len(items), done)
            # v2.9.4：失败落原因（运行日志 warn + result.fail_reasons，
            # 任务详情/概况可见，不再"0/16"全败却查不到原因）
            try:
                pub = html_sites.extract_date_from_page(it["url"], proxy=proxy)
            except Exception as e:
                pub = None
                reason = f"{type(e).__name__}: {e}"
                err_reasons.append(f"{(it.get('title') or '')[:28]}：{reason[:80]}")
                db.log("backfill", f"[{(it.get('title') or '')[:40]}] 补全失败：{reason}"[:200],
                       "warn")
            if pub:
                db.update_item_published_at(it["id"], pub)
                done_ids.append(it["id"])
                db.log("backfill", f"[{it['title'][:40]}] 补全日期 {pub[:10]}")
                done += 1
            else:
                reason = "页面未识别到发布日期（或访问失败）"
                err_reasons.append(f"{(it.get('title') or '')[:28]}：{reason}")
                db.log("backfill", f"[{(it.get('title') or '')[:40]}] 补全失败：{reason}", "warn")
    except TaskCancelled:
        db.reset_items_field(done_ids, "published_at")
        db.log("backfill", f"日期补全被打断：已回滚本轮 {len(done_ids)} 条日期", "warn")
        raise
    db.log("backfill", f"日期补全完成：{done}/{len(items)} 条"
                       + (f"，失败 {len(err_reasons)}" if err_reasons else ""))
    return {"ok": done, "total": len(items), "fail_reasons": err_reasons}
