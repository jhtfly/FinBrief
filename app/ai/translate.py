"""英文标题批量翻译（通过大模型 API，可在设置中开关/换模型）。"""
import re

from app import db
from app.ai import client
from app.tasks import TaskCancelled

SYSTEM_PROMPT = """你是金融领域标题翻译器。把给定的外文标题逐条翻译成简洁、准确的中文：
- 机构名使用规范译名（如 FSB=金融稳定理事会、BIS=国际清算银行、SEC=美国证交会）。
- 缩写保留原文，如 DLT、CBDC。
- 只输出标题本身，不带书名号或引号。
输出 JSON：{"translations":[{"id":序号,"zh":"中文标题"}]}"""


def _system_prompt() -> str:
    """翻译系统提示词：整体替换 + 附加要求（输出 JSON 契约恒定保留）。"""
    prompt = (db.get_setting("custom_translate_prompt") or "").strip() or SYSTEM_PROMPT
    extra = (db.get_setting("translate_extra") or "").strip()
    if extra:
        prompt += f"\n\n单位附加翻译要求（必须遵守）：\n{extra}"
    if '"translations"' not in prompt:  # 保证自定义替换时不破坏输出契约
        prompt += '\n输出 JSON：{"translations":[{"id":序号,"zh":"中文标题"}]}'
    return prompt


def _is_mostly_chinese(text: str) -> bool:
    if not text:
        return True
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk / max(len(text), 1) > 0.3


def translate_items(item_ids: list | None = None, batch_size: int = 15,
                    progress_cb=None, should_cancel=None) -> dict:
    """翻译线索标题。item_ids 为空时处理全部未翻译条目。
    v2.6 支持协作打断：批次间检查标志，被打断时复位本轮已译标题。"""
    if item_ids:
        with db.get_conn() as conn:
            marks = ",".join("?" * len(item_ids))
            rows = conn.execute(
                f"SELECT id, title FROM items WHERE id IN ({marks}) "
                "AND (title_zh IS NULL OR title_zh='')", item_ids).fetchall()
        todo = [dict(r) for r in rows]
    else:
        todo = db.untranslated_items()
    todo = [t for t in todo if not _is_mostly_chinese(t["title"])]
    if not todo:
        return {"ok": 0, "total": 0, "error": None}

    model = db.get_setting("model_screen") or db.get_setting("model_compile")
    done = 0
    error = None
    done_ids = []   # 本轮已译（打断回滚用）
    try:
        for i in range(0, len(todo), batch_size):
            if should_cancel and should_cancel():
                raise TaskCancelled("任务已被用户打断")
            batch = todo[i:i + batch_size]
            lines = "\n".join(f"{n}. {b['title'][:200]}"
                              for n, b in enumerate(batch, 1))
            messages = [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": f"待翻译标题：\n{lines}"},
            ]
            try:
                data = client.chat_json(messages, model, temperature=0.1,
                                        max_tokens=3000)
                for tr in data.get("translations", []):
                    try:
                        idx = int(tr.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if 1 <= idx <= len(batch):
                        zh = (tr.get("zh") or "").strip()
                        if zh:
                            db.update_item_title_zh(batch[idx - 1]["id"], zh)
                            done_ids.append(batch[idx - 1]["id"])
                            done += 1
            except TaskCancelled:
                raise
            except client.AIError as e:
                error = str(e)
                db.log("translate", f"标题翻译批次失败: {e}", "warn")
            if progress_cb:
                progress_cb(min(i + batch_size, len(todo)), len(todo), done)
    except TaskCancelled:
        db.reset_items_field(done_ids, "title_zh")
        db.log("translate", f"翻译任务被打断：已回滚本轮 {len(done_ids)} 条标题", "warn")
        raise
    db.log("translate", f"标题翻译完成 {done}/{len(todo)}"
                        + (f"，末批错误: {error}" if error else ""))
    return {"ok": done, "total": len(todo), "error": error}
