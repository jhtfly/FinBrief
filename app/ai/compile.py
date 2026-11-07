"""AI 编译：依据英文原文编译成结构化中文专报。"""
import re

from app import db
from app.textutils import fix_quotes as _fixq
from app.ai import client
from app.fetcher import html_sites, runner

SYSTEM_PROMPT = """你是中国人民银行某分行"金融稳定专报"的资深编译专家。你的任务是把一篇境外机构发布的英文（或外文）材料，忠实、准确地编译为一篇中文专报。

编译原则（必须严格遵守）：
1. 忠实原文，不得虚构任何事实、数据、机构表态或政策措施；不得加入无依据的判断。
2. 文风必须为公文体，使用第三人称，不用"我们""笔者"等表述。
3. 采用"概括 + 分点"写法，逻辑清楚、重点突出。
4. 数据、机构名称、时间、制度名称、规则名称必须与原文一致，准确无误。
5. 英文专业术语译法须规范，首次出现时在括号中附原文，例如：宏观审慎（macroprudential）。
6. 正文总字数 2000-3000 字；编者按 300 字以内。
7. 中文标点一律使用全角中文标点；引号必须用中文弯引号""，不得使用英文直引号。
8. 若原文信息量不足以支撑一篇完整专报，请在 "sufficient" 字段返回 false 并说明放弃原因，不得勉强成文。
9. **引用标记（重要）**：本专报来源于同一篇原文材料。在正文中**转述原文关键事实、数据、机构表态的句子末尾**添加引用标记 [[citeK]]，编号 K 按全文出现次序**递增**（第一处 [[cite1]]、第二处 [[cite2]]……），一段大约 1-2 处；标记位置在句号之前（如"……提高了 15%[[cite2]]。"）；**标记不得出现在标题、编者按或结论中**，不要改写标记格式。
10. **取证摘录（与编号一一对应）**：在输出 JSON 顶层增加 `"cites"` 数组：每处引用一条 {"no": K, "quote": "该编号所依据的原文短句（英文原文，忠实摘录、不得改写，控制在 25 词以内）"}，no 从 1 起与正文 [[citeK]] 一一对应。这用于专报发布前人工逐条核对原文，务必真实。

专报结构要求：
- title：正标题，格式为"机构中文名：关于《或"》报告/演讲主题的报告"，正式、准确、简洁。
- editor_note：编者按，一整段。逻辑顺序：[机构]发布《报告名》→核心事件→背景→结论→"现将相关情况呈上，供参阅。"开头必须有"编者按："三个字（冒号用中文全角）。
- sections：正文分节，2-3 个一级标题（格式"一、二、三、"），每节下设 2-3 个二级标题（格式"（一）（二）（三）"），每个小节正文不少于 180 字。内容围绕：政策/报告发布背景、主要观点或制度安排、风险影响或市场影响、国际经验及对我国启示。必须突出与金融稳定、宏观审慎、资本市场稳定、保险风险、影子银行、监管规则、流动性安排、风险处置机制的关联。全部小节正文合计不得少于 2000 字，不足时须依据原文扩充实质内容（不得注水）。
- conclusion：结论段。概括该线索的核心价值、对我国或地方金融稳定工作的借鉴意义、后续值得持续关注的方向。

输出 JSON 格式：
{"sufficient": true或false, "abandon_reason": "放弃原因（sufficient为false时填写）",
 "title": "正标题", "editor_note": "编者按全文",
 "sections": [{"h1": "一、……", "blocks": [{"h2": "（一）……", "text": "该小节正文"}]}],
 "conclusion": "结论全文",
 "cites": [{"no": 1, "quote": "对应原文短句"}]}"""


def compile_item(item_id: int) -> dict:
    """编译单条线索。返回 {ok, reason, report} 。
    先抓原文全文，再调用模型生成结构化专报。
    """
    settings = db.all_settings()
    model = settings.get("model_compile", "")
    item = db.get_item(item_id)
    if not item:
        return {"ok": False, "reason": "线索不存在"}

    # 抓取原文全文（已抓过则复用）
    content = item.get("content") or ""
    if not item.get("content_fetched"):
        proxy = None
        if settings.get("proxy_enabled") == "1" and settings.get("proxy_url"):
            proxy = settings["proxy_url"]
        text, err = html_sites.fetch_page_text(item["url"], proxy=proxy,
                                               max_chars=24000)
        if text and len(text) > 800:
            content = text
            db.update_item_content(item_id, text)
        else:
            content = (item.get("summary") or "")
    if len(content) < 400:
        reason = f"原文信息不足（正文仅 {len(content)} 字符），不足以支撑完整专报，放弃编译"
        db.save_report(item_id, "", "", [], "", model, status="abandoned",
                       abandon_reason=reason)
        db.log("compile", f"[{item['source_name_cn']}] {reason}")
        return {"ok": False, "reason": reason}

    # 编译前顺手补全缺失的发布日期（详情页通常有 meta/time 声明）
    if not item.get("published_at"):
        try:
            proxy = None
            if settings.get("proxy_enabled") == "1" and settings.get("proxy_url"):
                proxy = settings["proxy_url"]
            pub = html_sites.extract_date_from_page(item["url"], proxy=proxy)
            if pub:
                db.update_item_published_at(item_id, pub)
                db.log("compile", f"[{item['source_name_cn']}] 编译时补全日期 {pub[:10]}")
        except Exception:
            pass
        item = db.get_item(item_id) or item

    pub = (item.get("published_at") or "日期未知")[:10]
    user_content = (
        f"来源机构：{item['source_name_en']}（{item['source_name_cn']}）\n"
        f"发布时间：{pub}\n"
        f"原文标题：{item['title']}\n"
        f"原文链接：{item['url']}\n\n"
        f"===== 原文材料开始 =====\n{content}\n===== 原文材料结束 =====\n\n"
        f"请依据上述原文材料，按系统指令编译中文专报，输出 JSON。")

    system_prompt = (db.get_setting("custom_compile_prompt") or "").strip() \
        or SYSTEM_PROMPT
    extra = (db.get_setting("compile_extra") or "").strip()
    if extra:
        system_prompt += f"\n\n单位附加编译要求（必须遵守，不得违背上述输出 JSON 格式）：\n{extra}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    data = client.chat_json(messages, model, temperature=0.3, max_tokens=12000)

    if not data.get("sufficient", True):
        reason = data.get("abandon_reason") or "模型判断原文信息不足"
        db.save_report(item_id, "", "", [], "", model, status="abandoned",
                       abandon_reason=reason)
        db.log("compile", f"[{item['source_name_cn']}] 模型放弃编译: {reason[:100]}")
        return {"ok": False, "reason": reason}

    title = (data.get("title") or "").strip()
    editor_note = (data.get("editor_note") or "").strip()
    sections = data.get("sections") or []
    conclusion = (data.get("conclusion") or "").strip()
    # 基本完整性校验：不足则视为失败
    if not title or not editor_note or not sections or not conclusion:
        reason = "模型输出结构不完整（标题/编者按/正文/结论缺失）"
        return {"ok": False, "reason": reason}

    # 中文弯引号规范化：把可能残留的 ASCII 直引号替换为中文弯引号

    editor_note = _fixq(editor_note)
    conclusion = _fixq(conclusion)
    for sec in sections:
        sec["h1"] = _fixq(sec.get("h1") or "")
        for blk in sec.get("blocks") or []:
            blk["h2"] = _fixq(blk.get("h2") or "")
            blk["text"] = _fixq(blk.get("text") or "")

    # v2.9.7 单篇引用校对体系：K 递增持证据摘录；材料信息逐号可核；导出时不插脚注（仅编辑内核对）
    idx = 0
    for sec in sections:
        for blk in sec.get("blocks") or []:
            def bump(m):
                nonlocal idx
                idx += 1
                return f"[[cite{idx}]]"
            blk["text"] = re.sub(r"\[\[\s*cite\s*\d+\s*\]\]", bump, blk.get("text") or "")
    rows = data.get("cites") or []
    quotes = {int(r.get("no") or 0): (r.get("quote") or "").strip()
              for r in rows if str(r.get("no") or "").strip().isdigit()}
    mname = (item.get("title_zh") or item.get("title") or "")[:50]
    cits = [{"no": k, "item_id": item_id,
             "ref": f"  {item['source_name_cn']}《{mname}》，{(item.get('published_at') or '')[:10]}。"}
            for k in range(1, max(idx, 1) + 1)]
    evidence = [{"no": k, "quote": quotes.get(k, "")} for k in range(1, max(idx, 1) + 1)]
    db.save_report(item_id, title, editor_note, sections, conclusion, model,
                   citations=cits, evidence=evidence)
    db.log("compile", f"[{item['source_name_cn']}] 编译完成：{title[:50]}")
    return {"ok": True, "report": db.get_report_by_item(item_id)}
