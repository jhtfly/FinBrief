"""多线索综合专报编译（v2.8）。

将多条符合要求的线索（2-5 条）融合为一篇综合专报：
- 结构/文风沿用专报模板（主题式标题、编者按、一、（一）、结论）；
- 与单篇的区别：正文按引用点插入脚注——AI 在行文中以 `[[citeK]]` 标记引用
  送审清单第 K 条原文材料，程序把编号映射为脚注文本（机构＋材料名＋日期），
  导出 Word 时按第 58 期模板的脚注格式落到当页。
"""
import json
import time

from app import db
from app.textutils import fix_quotes as _fixq
from app.ai import client
from app.tasks import TaskCancelled

SYSTEM_PROMPT = """你是中国人民银行某分行"金融稳定专报"的资深编译专家。请把给定的多条境外机构材料融合，编译为一篇综合主题的中文专报。

编译原则（严格遵守）：
1. 忠实原文，不得虚构事实、数据、机构表态；不得加入无依据判断。
2. 公文体、第三人称；"概括＋分点"写法；全角中文标点；中文弯引号。
3. 采用主题式正标题（不写"机构：关于"前缀），突出多篇材料的共同主题。
4. 正文总字数 2200-3200 字；编者按 300 字以内（一整段、以"编者按："开头）。
5. 结构：sections 2-3 个一级标题（"一、二、三、"），每节 2-3 个二级标题（"（一）（二）"），每小节≥180 字；最后 conclusion 收束（核心价值＋对我国/地方金融稳定工作的借鉴＋后续关注方向）。
6. **脚注纪律（关键）**：凡是引用某条材料的具体观点、数据、表述处，在该句末（引号或句号后）插入标记 `[[citeK]]`（K=该材料的编号 1..N，如 `……时间成为最稀缺的资源"[[cite1]]`）。每条材料至少被引用一次；全篇标记总数 3-10 个；标记不得放在标题中。程序会把 `[[citeK]]` 替换为当页脚注（机构＋材料名＋日期），你只需保证落点准确，不要自写脚注文本。

输出 JSON：
{"sufficient": true或false, "abandon_reason": "（信息不足时）",
 "title": "主题式正标题",
 "editor_note": "编者按全文",
 "sections": [{"h1": "一、……", "blocks": [{"h2": "（一）……或空字符串", "text": "小节正文（可含 [[citeK]] 标记）"}]}],
 "conclusion": "结论全文"}
等标题保持公文体、术语首次出现附原文括注，与单篇一致。{extra}"""


def _footnote_text(item: dict) -> str:
    """按模板格式程序化生成脚注文本：`  机构《材料名》，日期。`"""
    org = item.get("source_name_cn") or "原作者"
    name = (item.get("title_zh") or item.get("title") or "").strip()
    pub = (item.get("published_at") or "")[:10] or "日期待核"
    return f"  {org}《{name}》，{pub}。"


def _system_prompt() -> str:
    prompt = SYSTEM_PROMPT.replace("{extra}", "")
    extra = (db.get_setting("multi_extra") or "").strip()
    if extra:
        prompt += f"\n\n单位附加编译要求（必须遵守，输出 JSON 契约不变）：\n{extra}"
    return prompt


def compile_multi_items(item_ids: list, progress_cb=None,
                        should_cancel=None) -> dict:
    """编译综合专报。item_ids 必须为 2-5 条；返回 {ok, mid/..., reason}。

    v2.9：逐材料预审均有进度回调；放弃与结构残缺一律落库（abandoned）可查；
    模型输出字段缺失时自动补齐重试一次，仍缺则落库并附原始输出片段日志。"""
    def _prog(msg):
        if progress_cb:
            progress_cb(msg)

    if not item_ids or len(item_ids) < 2:
        return {"ok": False, "reason": "请选择 2 条及以上线索再生成综合专报"}
    if len(item_ids) > 5:
        return {"ok": False, "reason": "综合专报一次最多融合 5 条线索（更多请分篇）"}
    items = []
    for iid in item_ids:
        it = db.get_item(iid)
        if it:
            items.append(it)
    if len(items) < 2:
        return {"ok": False, "reason": "有效线索不足 2 条"}

    model = db.get_setting("model_compile") or db.get_setting("model_screen")
    blocks = []
    sizes = []          # v2.9 每条材料的正文规模（供放弃明细）
    total_chars = 0
    for k, it in enumerate(items, 1):
        name = it.get("source_name_cn") or "材料"
        _prog(f"读取材料 {k}/{len(items)}：{name}…")
        content = it.get("content") or ""
        if not it.get("content_fetched"):
            s = db.all_settings()
            proxy = s.get("proxy_url") if s.get("proxy_enabled") == "1" else None
            from app.fetcher import html_sites
            try:
                text, err = html_sites.fetch_page_text(it["url"], proxy=proxy,
                                                       max_chars=14000)
            except Exception:
                text = ""
            if text and len(text) > 500:
                content = text
                db.update_item_content(it["id"], text)
            if not content:
                _prog(f"材料 {k}/{len(items)}：{name} 原文获取失败，仅用列表摘要")
        body = (content or it.get("summary") or "")[:9000]
        sizes.append(f"{name} {len(body)} 字")
        total_chars += len(body)
        pub = (it.get("published_at") or "日期未知")[:10]
        blocks.append(
            f"【材料{k}】{it['source_name_en']}（{it['source_name_cn']}）｜{pub}\n"
            f"标题：{it['title']}\n"
            f"URL：{it['url']}\n"
            f"===== 正文开始 =====\n{body}\n===== 正文结束 =====")
    if total_chars < 1600:
        reason = (f"所选 {len(items)} 条线索正文合计仅 {total_chars} 字符"
                  f"（<1600 字），不足以支撑综合专报，放弃编译。明细："
                  + "；".join(sizes))
        mid = db.save_multi_report("", "", [], "", [], item_ids, model,
                                   status="abandoned", abandon_reason=reason)
        return {"ok": False, "reason": reason, "mid": mid}

    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")
    _prog(f"AI 融合编译 {len(items)} 条线索（共约 {total_chars} 字）…")
    user = "以下为参与融合的原文材料：\n\n" + "\n\n".join(blocks) + \
        "\n\n请按系统指令融合编译，输出 JSON（正文引用处用 [[citeK]] 标记）。"

    def _validate(d):
        """返回缺失字段名列表；空列表即结构完整。"""
        missing = []
        if not (d.get("title") or "").strip():
            missing.append("title")
        if not (d.get("editor_note") or "").strip():
            missing.append("editor_note")
        secs = d.get("sections")
        if not isinstance(secs, list) or not secs:
            missing.append("sections")
        if not (d.get("conclusion") or "").strip():
            missing.append("conclusion")
        return missing

    messages = [{"role": "system", "content": _system_prompt()},
                {"role": "user", "content": user}]
    data = client.chat_json(messages, model, temperature=0.3, max_tokens=14000)

    # v2.9 结构残缺自动补齐：把缺失字段告知模型再要一次完整 JSON（最多补一轮）
    missing = _validate(data)
    if missing:
        raw1 = json.dumps(data, ensure_ascii=False)[:600]
        db.log("ai", f"综合编译首次输出缺字段 {missing}，原样片段：{raw1}")
        _prog(f"模型输出缺 {','.join(missing)}，自动补齐重试…")
        messages = messages + [
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
            {"role": "user", "content":
                f"上一次输出缺少字段 {missing}（或类型不对）。请重新输出**完整** JSON，"
                "不得缺少 title/editor_note/sections/conclusion 任一字段，"
                "sections 为 [{h1, blocks:[{h2, text}]}] 结构；只输出 JSON。"}]
        data = client.chat_json(messages, model, temperature=0.3,
                                max_tokens=14000)
        missing = _validate(data)

    if not data.get("sufficient", True):
        reason = data.get("abandon_reason") or "模型判断材料不足以支撑综合专报"
        mid = db.save_multi_report("", "", [], "", [], item_ids, model,
                                   status="abandoned", abandon_reason=reason)
        return {"ok": False, "reason": reason, "mid": mid}

    if missing:
        # 补齐后仍缺：落库可查，附缺口与模型原始输出片段
        raw = json.dumps(data, ensure_ascii=False)[:600]
        db.log("compile", f"综合编译失败：补齐后仍缺 {missing}，模型输出片段：{raw}",
               "error")
        reason = (f"模型两次输出均不完整（缺：{','.join(missing)}），"
                  "已放弃编译；失败细节见运行日志")
        mid = db.save_multi_report("", "", [], "", [], item_ids, model,
                                   status="abandoned", abandon_reason=reason)
        return {"ok": False, "reason": reason, "mid": mid}

    # v2.13.0 关键修复（提取缺失会让后续修引号/引脚注引用未定义变量 NameError）
    title = (data.get("title") or "").strip()
    editor_note = (data.get("editor_note") or "").strip()
    sections = data.get("sections") or []
    conclusion = (data.get("conclusion") or "").strip()


    sections = [{**sec, "h1": _fixq(sec.get("h1") or ""),
                 "blocks": [{"h2": _fixq((b or {}).get("h2") or ""),
                             "text": _fixq((b or {}).get("text") or "")}
                            for b in (sec.get("blocks") or [])]}
                for sec in sections]

    # 脚注映射：解析 [[citeK]] → item + ref 文本；程序生成 ref（防 AI 编造）
    import re
    used = {}

    def _collect(text):
        for num in re.findall(r"\[\[cite(\d+)\]\]", text or ""):
            k = int(num)
            if 1 <= k <= len(items):
                used[k] = items[k - 1]
            else:  # 越界标记删除
                pass
    _collect(editor_note)
    for sec in sections:
        _collect(sec["h1"])
        for b in sec["blocks"]:
            _collect(b["text"])
    _collect(conclusion)
    if not used:
        for k, it in enumerate(items, 1):  # 兜底：至少各引一条
            used[k] = it
    # citations.no 与文中标记一致；item_id 用原始 id（v2.13.6 删除首个死代码赋值）
    citations = [{"no": k, "item_id": it["id"], "ref": _footnote_text(it)}
                 for k, it in sorted(used.items())]

    mid = db.save_multi_report(title, editor_note, sections, conclusion,
                               citations, item_ids, model)
    db.log("compile", f"综合专报编译完成：{title[:40]}（融合 {len(items)} 条，"
                      f"脚注 {len(citations)} 个）")
    return {"ok": True, "mid": mid, "title": title, "footnotes": len(citations)}
