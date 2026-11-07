# -*- coding: utf-8 -*-
"""自定义专报编译（v2.13.0）。

用户在专报管理自行上传线索信息（网页链接 / Word(docx) / PDF / 图片），
材料逐一提取后按与综合专报一致的编译规则（multi 的 SYSTEM_PROMPT：
主题式融合、[[citeK]] 脚注纪律、2200-3200 字、sections 结构）编译。

- docx / 文本型 pdf：提取全部文本，纯文本路径（快，零视觉依赖）；
- 扫描型 pdf / 图片：PyMuPDF 渲染页 JPEG（≤8 页、宽≤1100px）→ 多模态模型
  识别图片；模型不支持图像输入时把接口报错如实回传并提示更换多模态模型；
- 落库进 multi_reports（同一专报流水线），materials 以"合成线索"条目登记
  （source = custom 自定义材料源；批量记录 kind=2 在 items.kind 上，线索
  面板/统计不展示）——编辑/取证/导出/删除等全部复用综合专报能力。"""
import base64
import json
import os
import re
import time

import fitz  # PyMuPDF

from app import db
from app.textutils import fix_quotes as _fixq
from app.ai import client
from app.ai.multi import SYSTEM_PROMPT, _footnote_text
from app.tasks import TaskCancelled

UPLOAD_DIR = os.path.join(db.DB_PATH and os.path.dirname(db.DB_PATH),
                          "custom_uploads")
MAX_MATS = 6                      # 单次最多 6 份材料
MAX_PDF_PAGES_VISION = 8          # 扫描 PDF 最多送前 8 页
MAX_TEXT_PER_MAT = 9000           # 文本材料进入提示词的截断（与 multi 一致）
HOST_DETERMINE = ("api.openai.com",)


def _system_prompt(extra: str) -> str:
    prompt = SYSTEM_PROMPT.replace("{extra}", "")
    if extra.strip():
        prompt += ("\n\n单位附加编译要求（必须遵守，输出 JSON 契约不变）：" +
                   extra.strip())
    return prompt


def _upload_dir() -> str:
    d = os.path.dirname(db.DB_PATH)
    os.makedirs(os.path.join(d, "custom_uploads"), exist_ok=True)
    return os.path.join(d, "custom_uploads")


def _ensure_custom_source() -> int:
    """每份材料登记为 items 表合成线索，源固定挂在这一专属信源行下。"""
    db.upsert_source({"key": "custom_docs", "name_cn": "自定义材料",
                      "name_en": "Custom material", "category": "media",
                      "mode": "html", "config": {}, "enabled": 0,
                      "note": "自定义专报材料（AI 编译用，不出现在线索面板）",
                      "src_group": "custom", "in_common": 0})
    src = db.get_source("custom_docs")
    if not src or not src.get("id"):
        return 0
    return src["id"]


def _decode_save(name: str, b64: str) -> str:
    """保存上传文件（按内容哈希命名稳判），返回磁盘绝对路径。"""
    raw = base64.b64decode(b64 or "")
    import hashlib
    stem = hashlib.sha1(raw).hexdigest()[:12]
    ext = os.path.splitext(name)[1].lower() or ".bin"
    d = _upload_dir()
    path = os.path.join(d, f"{stem}{ext}")
    with open(path, "wb") as f:
        f.write(raw)
    return path


def pdf_extract(path: str) -> dict:
    """pdf 提取：文本型→文本；扫描型→页图（JPEG dataURL 列表）。"""
    doc = fitz.open(path)
    pages = doc.page_count
    text = "\n".join(p.get_text() for p in doc)[:26000]
    doc.close()
    if len(text.strip()) >= 200:
        return {"kind": "pdf_text", "text": text, "pages": pages, "images": []}
    doc = fitz.open(path)
    images = []
    for i, page in enumerate(doc):
        if i >= MAX_PDF_PAGES_VISION:
            break
        zoom = min(1.9, 1100.0 / max(page.rect.width, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        b = pix.tobytes("jpeg")  # jpg 压缩，控制体积
        pix = None
        images.append("data:image/jpeg;base64," +
                      base64.b64encode(b).decode())
    doc.close()
    return {"kind": "pdf_scan", "text": "", "pages": len(images), "images": images}


def image_extract(path: str) -> str:
    with open(path, "rb") as f:
        b = f.read()
    ext = os.path.splitext(path)[1].lower()
    mime = {".png": "png", ".jpeg": "jpeg", ".jpg": "jpeg", ".webp": "webp",
            ".gif": "gif"}.get(ext, "jpeg")   # v2.13.6 键补前导点
    return "data:image/%s;base64,%s" % (mime, base64.b64encode(b).decode())


def docx_extract(path: str) -> str:
    from docx import Document
    doc = Document(path)
    out = [p.text for p in doc.paragraphs if (p.text or "").strip()]
    for tb in doc.tables:
        for row in tb.rows:
            out.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(out)[:26000]


def build_material(path_or_url: str, title: str = "") -> dict:
    """上传文件材料：磁盘文件 → kind/text/images 元数据与合成条目。"""
    ext = os.path.splitext(path_or_url)[1].lower()
    if ext == ".docx":
        return {"kind": "docx", "text": docx_extract(path_or_url), "images": [], "pages": 0}
    if ext == ".pdf":
        return pdf_extract(path_or_url)
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return {"kind": "image", "text": "", "images": [image_extract(path_or_url)], "pages": 1}
    raise ValueError("暂不支持的文件类型：" + ext)


def _blocks_from_materials(mats: list) -> list:
    out = []
    for k, m in enumerate(mats, 1):
        title = m.get("title") or m.get("name") or "未命名材料"
        url = m.get("url") or ""
        tag = m.get("kind_display") or m.get("kind")
        body = (m.get("text") or "")[:MAX_TEXT_PER_MAT]
        if m.get("images"):  # 视觉材料：正文由随消息图片提供
            body = (body + "\n【此材料为图片/扫描件，原图随本次消息附送；请识别图片中的文字与表格后引用】").strip()
        out.append(
            f"【材料{k}】{title} {'｜' + tag if tag else ''}{('  URL：' + url) if url else ''}\n"
            f"===== 正文开始 =====\n{body or '（无可读文本）'}\n===== 正文结束 =====")
    return out


def compile_custom(materials: list, topic: str = "", note: str = "",
                   progress_cb=None, should_cancel=None) -> dict:
    """材料编制（与 multi 同构）：materials = 合成线索 dict 列表
    （已插入 items 表，取 db.get_item 字段），其中图片/扫描件材料带 images（dataURL 列表）。
    返回 {ok, mid|reason}，供任务 wrapper 语义化。"""
    def _prog(msg):
        if progress_cb:
            progress_cb(msg)
    mats = []
    for k, m in enumerate(materials, 1):
        it = db.get_item(m.get("item_id")) if m.get("item_id") else None
        if not it:
            continue
        _prog(f"读取材料 {k}/{len(materials)}：{it.get('title_zh') or it.get('title') or '材料'}…")
        body_text = (it.get("content") or "")
        mats.append({"item_id": it.get("id"),
                     "name": it.get("title_zh") or it.get("title") or "材料",
                     "title": it.get("title_zh") or it.get("title") or "材料",
                     "url": it.get("url") or "",
                     "text": body_text,
                     "images": m.get("images") or [],
                     "kind": (m.get("kind") or "text"),
                     "kind_display": m.get("kind_display") or ""})
    if not mats:
        return {"ok": False, "reason": "没有可编译的材料"}
    sizes = [(m.get("name") or "材料") + " "
             + (str(len(m.get("text") or "")) + " 字") if not m.get("images")
             else (m.get("name") or "材料") + f"（图 {len(m.get('images'))} 张）"
             for m in mats]
    total_text = sum(len(m.get("text") or "") for m in mats)
    total_imgs = sum(len(m.get("images") or []) for m in mats)
    # 自定义专报不再硬性套用 multi 的 1600 字符门槛（用户可能只编剪报/短文）
    if total_text < 600 and not total_imgs:
        reason = (f"材料合计仅 {total_text} 字符（<600），不足以支撑专报："
                  + "；".join(sizes))
        mid = db.save_multi_report("", "", [], "", [], [], "", status="abandoned",
                                   abandon_reason=reason)
        return {"ok": False, "reason": reason, "mid": mid}

    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")

    _prog(f"AI 编译 {len(mats)} 份材料（文本 {total_text} 字"
          + (f"，图片 {total_imgs} 张" if total_imgs else "") + "）…")

    blocks = _blocks_from_materials(mats)
    topic_line = f"专报主题指向（可据此提炼标题与切分）：{topic}\n" if (topic or '').strip() else ""
    user_text = (topic_line
                 + "以下为参与编译的原文材料：\n\n"
                 + "\n\n".join(blocks)
                 + "\n\n请按系统指令编译（输出 JSON，行文中的引用处用 [[citeK]] 标记，K=材料编号）。"
                 + (("补充要求：" + note.strip()) if (note or '').strip() else ""))
    vision = [m for m in mats if m.get("images")]
    if vision:
        parts = [{"type": "text", "text": user_text}]
        for m in vision:
            for img in m["images"]:
                parts.append({"type": "image_url", "image_url": {"url": img}})
        user = parts
    else:
        user = user_text
    model = db.get_setting("model_compile") or db.get_setting("model_screen")
    messages = [{"role": "system", "content": _system_prompt(note)},
                {"role": "user", "content": user}]

    def _validate(d):
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

    def _chat_call(msgs, mdl):
        try:
            # v2.13.0：视觉路径输出大（含图片正文+长文），上限提至 20000
            return client.chat_json(msgs, mdl, temperature=0.3, max_tokens=20000)
        except Exception as e:
            msg = str(e)
            if any(k in msg.lower() for k in ("image", "vision", "multimodal", "图片")):
                raise RuntimeError(
                    "当前编译模型不支持图片输入。请到「设置→大模型 API」把编译模型改换为"
                    "支持图像的模型（如 GLM-4V/Qwen-VL/GPT-4o 等），或把 PDF/图片改用 "
                    "Word 文本材料后重试。（模型原始报错：" + msg[:130] + "）") from e
            raise
    data = _chat_call(messages, model)
    missing = _validate(data)
    if missing:
        raw1 = json.dumps(data, ensure_ascii=False)[:600]
        db.log("ai", f"自定义编译首次输出缺字段 {missing}，片段：{raw1}")
        _prog(f"模型输出缺 {','.join(missing)}，自动补齐重试…")
        messages = messages + [
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
            {"role": "user", "content":
                "上一次输出缺少字段 " + ",".join(missing) + "。请重新输出**完整** JSON，"
                "不得缺少 title/editor_note/sections/conclusion 任一字段；只输出 JSON。"}]
        data = _chat_call(messages, model)
        missing = _validate(data)

    if not data.get("sufficient", True):
        reason = data.get("abandon_reason") or "模型判断材料不足以支撑专报"
        mid = db.save_multi_report("", "", [], "", [], [r for r in _mat_ids(mats)],
                                   model, status="abandoned", abandon_reason=reason)
        return {"ok": False, "reason": reason, "mid": mid}
    if missing:
        raw = json.dumps(data, ensure_ascii=False)[:600]
        db.log("compile", f"自定义编译失败：补齐后仍缺 {missing}，输出片段：{raw}", "error")
        reason = f"模型两次输出均不完整（缺 {','.join(missing)}），已放弃；细节见运行日志"
        mid = db.save_multi_report("", "", [], "", [], _mat_ids(mats), model,
                                   status="abandoned", abandon_reason=reason)
        return {"ok": False, "reason": reason, "mid": mid}

    title = (data.get("title") or "").strip()
    editor_note = (data.get("editor_note") or "").strip()
    sections = data.get("sections") or []
    conclusion = (data.get("conclusion") or "").strip()

    sections = [{**sec, "h1": _fixq(sec.get("h1") or ""),
                 "blocks": [{"h2": _fixq((b or {}).get("h2") or ""),
                             "text": _fixq((b or {}).get("text") or "")}
                            for b in (sec.get("blocks") or [])]}
                for sec in sections]

    used = {}

    def _collect(t):
        for num in re.findall(r"\[\[cite(\d+)\]\]", t or ""):
            k = int(num)
            if 1 <= k <= len(mats):
                used[k] = mats[k - 1]
            else:
                pass
    _collect(title)
    _collect(editor_note)
    for sec in sections:
        _collect(sec.get("h1"))
        for b in sec.get("blocks") or []:
            _collect(b.get("text"))
    _collect(conclusion)
    if not used:
        for k, m in enumerate(mats, 1):
            used[k] = m
    # v2.16.0 自定义材料没有逐条取证摘录（材料整体理解），引用无法逐条核对——
    # 不再生成脚注：正文 [[citeK]] 标记统一清除，citations 置空
    import re as _re
    def _strip_marks(obj):
        if isinstance(obj, str):
            return _re.sub(r"\[\[cite\s*\d+\]\]", "", obj, flags=_re.I)
        if isinstance(obj, dict):
            return {k: _strip_marks(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_strip_marks(x) for x in obj]
        return obj
    sections = _strip_marks(sections)
    conclusion = _strip_marks(conclusion)
    citations = []

    ids = _mat_ids(mats)
    mid = db.save_multi_report(title, editor_note, sections, conclusion,
                               citations, ids, model)
    db.log("compile", f"自定义专报编译完成：{title[:40]}（{len(mats)} 份材料、"
                      f"脚注 {len(citations)} 个）")
    return {"ok": True, "mid": mid, "title": title, "footnotes": len(citations)}


def _mat_ids(mats: list) -> list:
    return [m.get("item_id") for m in mats if m.get("item_id")]


# ------------------------------------------------ v2.13.3 弃编译线索补材料重编
def compile_supplement(report_id: int, mats_meta: list, extra_note: str = "",
                       progress_cb=None, should_cancel=None) -> dict:
    """人工补材料后重新编译已放弃的单篇专报（沿用原线索主题，落回原 reports 行）。

    mats_meta：与 compile_custom 同形的已登记补充材料 [{item_id,kind,kind_display,images}]。
    材料 1 = 原线索（基础信息，概要或既有材料）；材料 2..N = 人工补充材料（主要信息源）。
    成功后 update 原 reports 行（title/editor_note/body/conclusion/citations/evidence，
    status 由 abandoned → draft，issue_no 保持原值不动）。"""
    def _prog(msg):
        if progress_cb:
            progress_cb(msg)

    row = db.get_report(report_id)
    if not row:
        return {"ok": False, "reason": f"报告 #{report_id} 不存在"}
    item = db.get_item(row["item_id"])
    if not item:
        return {"ok": False, "reason": "原线索不存在（可删除该放弃行重新编译）"}

    mats = [{"item_id": item["id"],
             "name": "原始线索（基础信息）",
             "title": item.get("title_zh") or item.get("title") or "原始线索",
             "url": item.get("url") or "",
             # v2.13.4 原线索多为概要/失效抓取，截 4000 字控制整体输入规模
             "text": (((item.get("content") or "").strip()
                       or (item.get("summary") or "").strip()))[:4000],
             "images": [], "kind": "context",
             "kind_display": ""}]
    for k, m in enumerate(mats_meta, 2):
        it = db.get_item(m.get("item_id")) if m.get("item_id") else None
        if not it:
            continue
        _prog(f"读取补充材料 {k}：{it.get('title_zh') or it.get('title') or '材料'}…")
        mats.append({"item_id": it.get("id"),
                     "name": it.get("title_zh") or it.get("title") or "材料",
                     "title": it.get("title_zh") or it.get("title") or "材料",
                     "url": it.get("url") or "",
                     # v2.13.4 补充材料单条截 6000 字（此前整段输入最长 2.6 万字拖慢生成）
                     "text": (it.get("content") or "")[:6000],
                     "images": m.get("images") or [],
                     "kind": (m.get("kind") or "text"),
                     "kind_display": m.get("kind_display") or ""})
    if len(mats) < 2:
        return {"ok": False, "reason": "补充材料全部不可用（链接抓取失败或上传记录失效）"}

    total_text = sum(len(m.get("text") or "") for m in mats[1:])
    total_imgs = sum(len(m.get("images") or []) for m in mats[1:])
    if total_text < 400 and not total_imgs:
        reason = (f"补充材料合计仅 {total_text} 字符（<400），不足以支撑专报编译；"
                  "请补充正式材料（机构原文、新闻全文等）后再编")
        db.update_report(report_id, {"status": "abandoned", "abandon_reason": reason})
        return {"ok": False, "reason": reason}

    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")

    _prog(f"AI 编译中（原始线索 1 份 + 补充材料 {len(mats)-1} 份；生成通常需要 2-5 分钟，"
          "可在任务中心打断）…")

    blocks = _blocks_from_materials(mats)
    user_text = ("【材料1】为原线索基础信息（多为概要，信息有限）；"
                 f"【材料2】起的 {len(mats)-1} 份为人工补充的完整材料，是主要信息来源。"
                 "请以原线索为主题，主要依据补充材料编译一篇单篇专报"
                 "（标题按“来源机构：关于×××的报告”口径），"
                 "事实细节与引用一律取自补充材料：\n\n"
                 + "\n\n".join(blocks)
                 + "\n\n请按系统指令编译（输出 JSON，行文引用处用 [[citeK]] 标记，K=材料编号）。"
                 + (("补充要求：" + extra_note.strip()) if (extra_note or '').strip() else ""))

    vision = [m for m in mats if m.get("images")]
    if vision:
        parts = [{"type": "text", "text": user_text}]
        for m in vision:
            for img in m["images"]:
                parts.append({"type": "image_url", "image_url": {"url": img}})
        user = parts
    else:
        user = user_text
    model = db.get_setting("model_compile") or db.get_setting("model_screen")
    # 与单篇 compile_item 同源：用户自定义的“AI 编译系统”提示词优先（local import 避免加载环）
    from app.ai.compile import SYSTEM_PROMPT as _CPROMPT
    system_prompt = (db.get_setting("custom_compile_prompt") or "").strip() or _CPROMPT
    extra = (db.get_setting("compile_extra") or "").strip()
    if extra:
        system_prompt += (f"\n\n单位附加编译要求（必须遵守，不得违背上述输出 JSON 格式）：\n{extra}")
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user}]

    # v2.13.4 纯文本重编降为 12000（与 compile_item 同源），仅图片/扫描件材料才用 20000
    _mt = 20000 if any(m.get("images") for m in mats) else 12000
    data = client.chat_json(messages, model, temperature=0.3, max_tokens=_mt)

    def _validate(d):
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

    user_ids = [m.get("item_id") for m in mats[1:]]

    if not data.get("sufficient", True):
        reason = data.get("abandon_reason") or "模型判断补充材料仍不足以支撑专报"
        db.update_report(report_id, {"status": "abandoned", "abandon_reason": reason})
        return {"ok": False, "reason": reason, "item_ids": user_ids}

    missing = _validate(data)
    if missing:
        _prog(f"模型输出缺 {','.join(missing)}，自动补齐重试…")
        messages2 = messages + [
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
            {"role": "user", "content":
                "上一次输出缺少字段 " + ",".join(missing) + "。请重新输出**完整** JSON，"
                "不得缺少 title/editor_note/sections/conclusion 任一字段；只输出 JSON。"}]
        data = client.chat_json(messages2, model, temperature=0.3, max_tokens=_mt)
        missing = _validate(data)
    if missing:
        reason = f"模型两次输出均不完整（缺 {','.join(missing)}）；可再点「补充材料」重试"
        db.update_report(report_id, {"status": "abandoned", "abandon_reason": reason})
        return {"ok": False, "reason": reason, "item_ids": user_ids}

    title = (data.get("title") or "").strip()
    editor_note = (data.get("editor_note") or "").strip()
    sections = data.get("sections") or []
    conclusion = (data.get("conclusion") or "").strip()

    def _fixq(t: str) -> str:
        out, open_q = [], True
        for ch in t:
            if ch == '"':
                out.append("\u201c" if open_q else "\u201d")
                open_q = not open_q
            else:
                out.append(ch)
        return "".join(out)

    sections = [{**sec, "h1": _fixq(sec.get("h1") or ""),
                 "blocks": [{"h2": _fixq((b or {}).get("h2") or ""),
                             "text": _fixq((b or {}).get("text") or "")}
                            for b in (sec.get("blocks") or [])]}
                for sec in sections]
    editor_note = _fixq(editor_note)
    conclusion = _fixq(conclusion)

    idx = 0
    for sec in sections:
        for blk in sec.get("blocks") or []:
            def bump(mm):
                nonlocal idx
                idx += 1
                return f"[[cite{idx}]]"
            blk["text"] = re.sub(r"\[\[\s*cite\s*\d+\s*\]\]", bump, blk.get("text") or "")

    quotes = {int(r.get("no") or 0): (r.get("quote") or "").strip()
              for r in (data.get("cites") or []) if str(r.get("no") or "").strip().isdigit()}
    cits, evid = [], []
    for k, m in enumerate(mats, 1):
        u = db.get_item(m["item_id"]) or {}
        if m["kind"] == "context":
            foot = (f"  {item.get('source_name_cn')}《{(item.get('title_zh') or item.get('title') or '')[:50]}》，"
                    f"{(item.get('published_at') or '')[:10]}。")
        else:
            foot = (f"  人工补充《{(m.get('title') or '')[:40]}》，{db.now_iso()[:10]}。")
        cits.append({"no": k, "item_id": m["item_id"], "ref": foot})
        evid.append({"no": k, "quote": quotes.get(k, "")})

    db.update_report(report_id, {
        "title": title, "editor_note": editor_note, "body": sections,
        "conclusion": conclusion, "status": "draft", "model": model,
        "abandon_reason": "", "citations": cits, "evidence": evid})
    db.log("compile", f"[{item.get('source_name_cn')}] 补材料重编完成：{title[:50]}"
                      f"（补充材料 {len(mats)-1} 份）")
    return {"ok": True, "rid": report_id, "title": title,
            "footnotes": len(cits), "item_ids": user_ids}

