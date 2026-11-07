"""专报 AI 对话式修改（v2.8）。

复核编辑器右侧的 AI 对话：把当前专报 JSON 与用户的自然语言指令交给模型，
返回**修改后的完整专报 JSON**。前台把新 JSON 渲染到左侧结构化预览，
用户可"撤销本次 AI 修改"或点保存落库。
排版约束：本接口只改内容（title/editor_note/sections/conclusion），
导出排版始终由 docx_builder 按模板落版，与对话内容无关。
排版指令（如"改字号"）会被提示词拒绝并说明。
"""
import json

from app import db
from app.textutils import fix_quotes as _fixq
from app.ai import client

SYSTEM_PROMPT = """你是"金融稳定专报"的修改助手。用户会给出当前专报全文（JSON）与一条自然语言修改指令。
修改要求（严格遵守）：
1. 只按指令修改内容（标题/编者按/正文小节/结论的措辞、增删、结构调整、数据更正、语气统一）；
   未涉及的部分原样保留；输出**修改后的完整 JSON**（字段：title, editor_note,
   sections[{h1, blocks[{h2, text}]}], conclusion），不得增删顶层字段。
2. 保持公文体、第三人称、全角中文标点与中文弯引号；术语首次出现附原文括注。
3. 保持"一、""（一）"的层级编号体系合理（删除小节后必要时重排编号）。
4. 不虚构事实、数据、机构表态；用户要求补充的内容若超出原文，改写为中性概括且不添加具体数字。
5. 排版类指令（字号、字体、行距、页边距、脚注样式等）一律不改文本，可在
   "reply" 中说明"导出时将按模板排版，内容未变"。
6. 若指令与金融稳定工作无关或意图不明确，不做修改，reply 说明原因并原样返回。
7. 单条 text 内 `[[citeN]]` 标记（如存在）必须原样保留位置语义，不得删除或改动。
输出 JSON：{"title": ..., "editor_note": ..., "sections": ..., "conclusion": ...,
            "reply": "对本轮修改的 30-80 字中文说明（改了什么/为何未改）"}"""

BUILT_IN_FALLBACK = SYSTEM_PROMPT


def _system_prompt() -> str:
    prompt = (db.get_setting("custom_edit_prompt") or "").strip() or SYSTEM_PROMPT
    extra = (db.get_setting("edit_extra") or "").strip()
    if extra:
        prompt += f"\n单位附加修改要求（必须遵守，输出 JSON 契约不变）：\n{extra}"
    return prompt


def _dump(report: dict) -> str:
    return json.dumps({
        "title": report.get("title") or "",
        "editor_note": report.get("editor_note") or "",
        "sections": report.get("body") or [],
        "conclusion": report.get("conclusion") or "",
    }, ensure_ascii=False, indent=1)


# 流式模式的思考段协议：模型先输出思考文本，再以【JSON】标记引出正文 JSON
_STREAM_THINK_NOTE = """

流式输出格式（必须严格遵守）：
1. 先写【思考】标记，随后用不超过 150 字的中文简述你的修改思路（自然语言，不输出 JSON 字段值）；
2. 然后另起一行写【JSON】标记，其后**只输出完整 JSON 对象**（字段 title/editor_note/sections/conclusion/reply）；
3. 【JSON】标记之后不得再出现任何普通文字或第二个 JSON。"""


def split_stream_text(raw: str):
    """把流式全文拆为 (思考文本, JSON 文本)；无【JSON】标记时 JSON=原文。"""
    raw = raw or ""
    j = raw.find("【JSON】")
    if j < 0:
        think = raw.strip()
        k = think.find("【思考】")
        if k >= 0:
            think = think[k + len("【思考】"):]
        return think, raw
    think = raw[:j]
    k = think.find("【思考】")
    if k >= 0:
        think = think[k + len("【思考】"):]
    return think.strip(), raw[j + len("【JSON】"):].strip()


def build_ai_edit_messages(kind: str, report: dict, instruction: str,
                           streaming: bool = False, history=None):
    """构造 AI 修改的 messages（流式/同步共用；streaming 追加思考段协议）。

    v2.9.2：history=持久化会话记录 [{q,a,…}]——以 user/assistant 对插入
    （append-only、排在最后一条指令之前）：
    - 模型能"记住"此前改过什么，后续指令可基于前文继续；
    - system 与历史都在前缀且内容稳定，服务端按前缀缓存可显著提高命中率。"""
    msgs = [{"role": "system", "content": _system_prompt()}]
    for it in (history or [])[-8:]:
        q = (it.get("q") or "").strip()
        a = (it.get("a") or "").strip()
        if q:
            msgs.append({"role": "user", "content": q})
        if a:
            msgs.append({"role": "assistant", "content": a})
    cites_note = ""
    if kind == "multi" and report.get("citations"):
        cites_note = ("\n本篇为综合专报，脚注文本由系统生成无需修改；"
                      "正文中 [[citeN]] 标记的位置语义必须保留。")
    user = (f"当前专报 JSON：\n{_dump(report)}\n\n"
            f"用户修改指令：{instruction}{cites_note}\n\n"
            "请输出修改后的完整 JSON。")
    sys_prompt = msgs[0]["content"]
    if streaming:
        sys_prompt += _STREAM_THINK_NOTE
    msgs[0]["content"] = sys_prompt
    msgs.append({"role": "user", "content": user})
    return msgs


def apply_ai_edit_data(data: dict) -> dict:
    """校验+清洗模型修改输出 → {ok, report, reply}（同步/流式共用）。"""
    title = (data.get("title") or "").strip()
    sections = data.get("sections")
    if not title or not isinstance(sections, list):
        return {"ok": False, "reply": "模型输出结构不完整，本次未应用修改"}
    sections = [{"h1": _fixq((s or {}).get("h1") or ""),
                 "blocks": [{"h2": _fixq((b or {}).get("h2") or ""),
                             "text": _fixq((b or {}).get("text") or "")}
                            for b in ((s or {}).get("blocks") or [])]}
                for s in sections]
    new_report = {"title": _fixq(title),
                  "editor_note": _fixq((data.get("editor_note") or "").strip()),
                  "body": sections,
                  "conclusion": _fixq((data.get("conclusion") or "").strip())}
    return {"ok": True, "report": new_report, "reply": (data.get("reply") or "")[:160]}


def ai_edit_report(kind: str, report: dict, instruction: str,
                   should_cancel=None, history=None) -> dict:
    """kind="single"（单篇）或 "multi"（综合专报）。返回 {ok, report_or_none, reply}。"""
    instruction = (instruction or "").strip()
    if not instruction:
        return {"ok": False, "reply": "请输入修改指令"}
    model = db.get_setting("model_compile") or db.get_setting("model_screen")
    if should_cancel and should_cancel():
        from app.tasks import TaskCancelled
        raise TaskCancelled("任务已被用户打断")
    data = client.chat_json(
        build_ai_edit_messages(kind, report, instruction, history=history),
        model, temperature=0.2, max_tokens=14000)
    return apply_ai_edit_data(data)
