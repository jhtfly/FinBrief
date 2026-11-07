"""AI 线索摘要：单条线索点击触发，简要总结 120-200 字中文摘要。"""
from app import db
from app.ai import client
from app.tasks import TaskCancelled

SYSTEM_PROMPT = """你是金融资讯摘要助手。根据给定的境外机构信息（标题、摘要、正文节选），用中文写一段简明摘要：
- 120-200 字，一整段；
- 概括核心事件/观点/数据，不虚构、不加评论；
- 机构名用规范中文译名，首次出现的术语可括注原文。
输出 JSON：{"summary": "摘要全文"}"""


def _system_prompt() -> str:
    """摘要提示词：整体替换 + 附加要求（v2.5，输出契约自动保留）。"""
    prompt = (db.get_setting("custom_summarize_prompt") or "").strip() or SYSTEM_PROMPT
    extra = (db.get_setting("summarize_extra") or "").strip()
    if extra:
        prompt += f"\n\n单位附加摘要要求（必须遵守）：\n{extra}"
    if '"summary"' not in prompt:
        prompt += '\n输出 JSON：{"summary": "摘要全文"}'
    return prompt


def summarize_item(item_id: int, progress_cb=None, should_cancel=None,
                   force: bool = False) -> dict:
    item = db.get_item(item_id)
    if not item:
        return {"ok": 0, "error": "线索不存在"}
    if (item.get("ai_summary") or "").strip() and not force:
        return {"ok": 1, "summary": item["ai_summary"], "cached": True}
    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")
    content = item.get("content") or ""
    if not content:
        content = item.get("summary") or ""
    if not content:
        # 现抓详情页正文
        from app.fetcher import html_sites
        s = db.all_settings()
        proxy = s.get("proxy_url") if s.get("proxy_enabled") == "1" else None
        text, err = html_sites.fetch_page_text(item["url"], proxy=proxy, max_chars=8000)
        if text:
            content = text
    title = item.get("title_zh") or item["title"]
    user = (f"机构：{item['source_name_cn']}（{item['source_name_en']}）\n"
            f"发布时间：{(item.get('published_at') or '未知')[:10]}\n"
            f"标题：{item['title']}\n"
            f"摘要：{(item.get('summary') or '')[:600]}\n"
            f"正文节选：{content[:6000]}\n\n请输出 JSON。")
    model = db.get_setting("model_screen") or db.get_setting("model_compile")
    data = client.chat_json(
        [{"role": "system", "content": _system_prompt()},
         {"role": "user", "content": user}],
        model, temperature=0.2, max_tokens=1000)
    summary = (data.get("summary") or "").strip()
    if not summary:
        return {"ok": 0, "error": "模型未返回摘要"}
    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET ai_summary=? WHERE id=?", (summary, item_id))
    db.log("summarize", f"[{item['source_name_cn']}] 生成 AI 摘要")
    return {"ok": 1, "summary": summary}
