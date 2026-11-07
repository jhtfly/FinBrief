"""专报 Word 导出：python-docx 严格按模板排版生成 docx。

排版规范（源自"编译助手"与参考模板）：
- A4，上下边距 2.54cm，左右边距 3.18cm
- 刊头（设置 masthead，默认 XXXX专报）：华文新魏 50pt 居中（红）
- 期号"第X期"：楷体_GB2312 15pt 居中
- 单位与日期行：楷体_GB2312 14pt 两端分列（制表位）
- 分隔线"———————————— ★————————————"居中，红色
- 正标题：黑体 18pt 居中；编者按："编者按："黑体+仿宋正文 16pt
- 一级标题：黑体 16pt；二级标题：楷体_GB2312 16pt
- 正文：仿宋_GB2312 16pt，首行缩进 2 字符，行距固定值 28 磅
- 所有 run 的 rFonts 补 w:hint="eastAsia"（中文标点全角渲染关键）

v2.8 综合专报脚注（格式严格跟随第 58 期参考模板实测）：
- 正文文本中的 [[citeK]] 标记 → 拆 run 插入上标 footnoteReference（同字号同字体）；
- footnotes.xml 程序化构造：脚注段 line=240 auto 两端对齐，首 run 上标 footnoteRef，
  次 run Times New Roman/宋体 9pt 正文"  机构《材料名》，日期。"（文本由程序生成防编造）；
- 通过 OPC 新建 /word/footnotes.xml part + document.xml.rels 关系挂载。
"""
import os
import re
import time
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.opc.part import Part
from docx.opc.packuri import PackURI
from docx.shared import Cm, Pt, RGBColor

from app import db

_FOOT_NOTES_CT = ("application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.footnotes+xml")
_FOOT_NOTES_RT = ("http://schemas.openxmlformats.org/officeDocument/2006/"
                  "relationships/footnotes")
_CITE_RE = re.compile(r"\[\[\s*cite\s*(\d+)\s*\]\]", re.I)
# 任何 [[..]] 形态的标记（含不合法变体）——转脚注用前者，孤残标记清理用后者
_MARKER_ANY = re.compile(r"\[\[[^[\]]{0,24}\]\]")
# 非法形态引用标记（【cite1】、[cite1]、事前清场兜底）
_DIRTY_MARK = re.compile(r"(?<!\[)[【\[]\s*cite\s*[0-9]{1,3}\s*[】\]](?!\])", re.I)  # v2.16.0 防止把 [[cite1]] 的内层当脏标记误清
# v2.13.5 空/半空脚注占位（[]、[[ ]]、[[cite]] 等）——AI 改写残留，导出时清掉不留正文
_EMPTY_BRACKET = re.compile(r"\[\s*\]\s*|\[\[\s*\]\]\s*|\[\[\s*cite\s*\]\]\s*")


def _clean_dirty_markers(t: str) -> str:
    """击杀非法形态引用标记（合法 [[citeN]] 保留，交 _add_text_runs 转脚注）。"""
    return _DIRTY_MARK.sub("", t or "")


def _fix_zh_quotes(t: str) -> str:
    """ASCII 双直引号 → 中文弯引号（交替左右），其余字符不动。"""
    if '"' not in (t or ""):
        return t or ""
    out, open_q = [], True
    for ch in t:
        if ch == '"':
            out.append("\u201c" if open_q else "\u201d")
            open_q = not open_q
        else:
            out.append(ch)
    return "".join(out)


def _set_font(run, cn_font, size_pt, bold=False, color=None):
    run.font.name = cn_font
    run.font.size = Pt(size_pt)
    run.font.bold = bold
    if color:
        run.font.color.rgb = color
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = rPr.makeelement(qn("w:rFonts"), {})
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:ascii"), cn_font)
    rFonts.set(qn("w:hAnsi"), cn_font)
    rFonts.set(qn("w:eastAsia"), cn_font)
    rFonts.set(qn("w:hint"), "eastAsia")  # 中文标点按中文字形渲染


def _cite_run(p, note_no: int, size_pt: float, repeat: bool = False):
    """追加一个上标引用 run（Times New Roman 三号，v2.19.0 用户规范）。

    v2.20 同源去重：首次引用该线索 → repeat=False，插入真实 footnoteReference
    （页面底部出现完整脚注）；同一线索再次引用 → repeat=True，只插入静态上标
    序号文本（= 首次脚注的注号），**不再新建页面底部脚注**。之所以不用第二次
    footnoteReference：Word 不允许同一 w:id 被 footnoteReference 多处引用
    （实测报"文件可能已经损坏"），而"同注"新注会重复整段引用文本，用户要求
    去掉。导出是一次性产物，静态序号与首次脚注的渲染编号恒一致。"""
    run = p.add_run()
    _set_font(run, "Times New Roman", size_pt)
    try:
        run.font.superscript = True
    except Exception:
        rPr = run._element.get_or_add_rPr()
        va = rPr.makeelement(qn("w:vertAlign"), {})
        va.set(qn("w:val"), "superscript")
        rPr.append(va)
    if repeat:
        run.text = str(note_no)
        return
    fref = run._element.makeelement(qn("w:footnoteReference"), {})
    fref.set(qn("w:id"), str(note_no))
    run._element.append(fref)


def _cite_canon(c: dict):
    """同源线索归一键：URL（去尾斜杠、小写）优先，退回标题。
    v2.20.1：url 与 title 都为空时按材料号独立——multi/单篇编译落库的
    citations 只有 {no, item_id, ref}（无 url/title），此前两键同归
    ("", "") 会把不同材料错并为一条脚注（用户报"引用多篇只剩一个脚注"
    即此根因）。仅在确有可归一信息（同 URL 或同标题）时才合并。"""
    url = (c.get("url") or "").strip().rstrip("/").lower()
    if url:
        return ("u", url)
    title = (c.get("title") or "").strip()
    if title:
        return ("t", title)
    return ("n", str(c.get("no")))


def _new_note_state(citations):
    """v2.8 脚注状态；v2.20 同源去重：
    - map=材料号→完整脚注文本；notes=按首次出现次序累积的脚注表；
    - alias=材料号归一（citations 里同一线索被重复登记——同 URL/同标题——
      全部别名到首次出现的材料号）；
    - issued=已分配注号的材料号→注号，用于"后续引用只插序号不重复建脚注"。"""
    if not citations:
        return None
    canon, alias = {}, {}
    for c in citations:
        key = _cite_canon(c)
        if key in canon:
            alias[c["no"]] = canon[key]
        else:
            canon[key] = c["no"]
    return {"map": {c["no"]: c.get("ref") or "" for c in citations},
            "alias": alias, "issued": {}, "notes": []}


def _note_no(state, material_no: int):
    """返回 (注号, 是否重复引用)。首次引用登记完整脚注；同源材料（含别名）
    再次引用不建新注，正文只落静态序号（见 _cite_run）。"""
    k = state["alias"].get(material_no, material_no)
    if state["map"].get(k) is None:            # 材料号未知：占位避免残缺引用
        k = material_no
    if k in state["issued"]:
        return state["issued"][k], True
    ref = state["map"].get(k)
    if ref is None:
        ref = "  引用缺失（材料编号超出范围）。"
    no = len(state["notes"]) + 1
    state["notes"].append({"no": no, "ref": ref})
    state["issued"][k] = no
    return no, False


def _add_text_runs(p, text: str, cn_font: str, size_pt: float, state=None):
    """按 [[citeK]] 标记拆分文本写 run：普通片段→文本 run，合法标记→上标脚注引用。

    v2.9.1：识别放宽（[[cite 2]] / [[CITE1]] 等变体）；任何未识别的 [[..]] 残标
    直接丢弃且**绝不留正文**（AI 修改可能改写标记格式，落点由脚注约束保证）；
    state=None（单篇）时所有标记一并删除。"""
    text = text or ""
    # v2.13.5 空/半空占位与脏标记先清场
    text = _EMPTY_BRACKET.sub("", _DIRTY_MARK.sub("", text))
    if not _MARKER_ANY.search(text):
        _set_font(p.add_run(text), cn_font, size_pt)
        return
    if not state:          # 单篇：无脚注体系，凡标记一律删除不留正文
        _set_font(p.add_run(_MARKER_ANY.sub("", text)), cn_font, size_pt)
        return
    # v2.13.5 同一材料的邻接重复引用（如 [[cite1]][[cite1]] 或 [[cite1]][[cite2]]
    # 都指向同一材料且中间只隔空白）只插一个脚注——每个落点一条即可，不重复插
    matches = list(_MARKER_ANY.finditer(text))
    cite_no = lambda m: (lambda mm: int(mm.group(1)) if mm else None)(
        _CITE_RE.match(m.group(0)))
    groups = []
    i = 0
    while i < len(matches):
        m = matches[i]
        k = cite_no(m)
        j = i
        while j + 1 < len(matches):
            nxt = matches[j + 1]
            nk = cite_no(nxt)
            gap = text[matches[j].end():nxt.start()]
            if k is not None and nk == k and gap.strip() == "":
                j += 1
            else:
                break
        groups.append((m, k))
        i = j + 1
    pos = 0
    # v2.17.0 脚注序号放在紧跟其后的标点（，。；：、）之前；v2.19.0 补齐反向
    # 情形——AI/人工把标记写在标点之后（"…线。[[cite1]]"）时，引用同样移到
    # 该标点之前，保证角标永远紧贴句子末尾标点的前面。
    _PUNCT = "，。；：、)"
    for m, k in groups:
        start, end = m.start(), m.end()
        has_ref = k is not None and k in state["map"]
        if end < len(text) and text[end] in _PUNCT:
            # 情形 A：标记后紧跟标点（…线[[cite1]]。）→ 文本、引用、标点
            if start > pos:
                _set_font(p.add_run(text[pos:start]), cn_font, size_pt)
            if has_ref:
                _no, _rep = _note_no(state, k)
                _cite_run(p, _no, size_pt, repeat=_rep)
            _set_font(p.add_run(text[end]), cn_font, size_pt)
            pos = end + 1
        elif start > pos and text[start - 1] in _PUNCT:
            # 情形 B：标记前紧贴标点（…线。[[cite1]]）→ 引用插到该标点前
            if start - 1 > pos:
                _set_font(p.add_run(text[pos:start - 1]), cn_font, size_pt)
            if has_ref:
                _no, _rep = _note_no(state, k)
                _cite_run(p, _no, size_pt, repeat=_rep)
            _set_font(p.add_run(text[start - 1]), cn_font, size_pt)
            pos = end
        else:
            # 无相邻标点：引用原位插入
            if start > pos:
                _set_font(p.add_run(text[pos:start]), cn_font, size_pt)
            if has_ref:
                _no, _rep = _note_no(state, k)
                _cite_run(p, _no, size_pt, repeat=_rep)
            pos = end
    if pos < len(text):
        _set_font(p.add_run(text[pos:]), cn_font, size_pt)


def _para(doc, align=None, line_exact_pt=None, first_line_chars=None,
          space_before=0, space_after=0):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    if align is not None:
        pf.alignment = align
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)
    pPr = p._p.get_or_add_pPr()
    if line_exact_pt:  # 固定行距（exact），28 磅 = 560 twips
        spacing = pPr.find(qn("w:spacing"))
        if spacing is None:
            spacing = pPr.makeelement(qn("w:spacing"), {})
            pPr.append(spacing)
        spacing.set(qn("w:line"), str(int(line_exact_pt * 20)))
        spacing.set(qn("w:lineRule"), "exact")
    if first_line_chars:  # 首行缩进按字符数（200 = 2 字符）
        ind = pPr.find(qn("w:ind"))
        if ind is None:
            ind = pPr.makeelement(qn("w:ind"), {})
            pPr.append(ind)
        ind.set(qn("w:firstLineChars"), str(first_line_chars * 100))
        ind.set(qn("w:firstLine"), str(int(first_line_chars * 16 * 20)))
    return p


def sanitize_filename(name: str) -> str:
    """替换 Windows 文件名非法字符（仅文件名层面，文档内保持正常标点）。"""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "－")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:120]


def _unique_path(path: str) -> str:
    """v2.18.0 重名防覆盖：目标文件已存在时追加"副本"（再撞则副本2/副本3…）。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    cand = stem + "副本" + ext
    n = 2
    while os.path.exists(cand):
        cand = f"{stem}副本{n}{ext}"
        n += 1
    return cand


# ---------------- 脚注 part 注入（v2.8） ----------------
def _footnote_p_xml(ref: str) -> str:
    """单个脚注正文段 XML，格式严格跟随第 58 期模板实测：
    中文宋体 + 西文 Times New Roman、9pt（sz=18）。v2.18.0 修复 v2.17.0
    误改为 Times New Roman 三号（sz=32）导致脚注字号与正文同大的回归。"""
    ref = _fix_zh_quotes(ref or "")
    ref = escape(ref)
    return (
        '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="240" '
        'w:lineRule="auto"/><w:jc w:val="both"/></w:pPr>'
        '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteRef/></w:r>'
        '<w:r><w:rPr><w:rFonts w:hint="eastAsia" w:ascii="Times New Roman" '
        'w:hAnsi="Times New Roman" w:eastAsia="宋体"/>'
        '<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
        f'<w:t xml:space="preserve">{ref}</w:t></w:r></w:p>')


def _footnotes_xml(citations: list) -> str:
    """整个 footnotes.xml 文档（真实脚注 id 与文中 footnoteReference 对应）。"""
    items = []
    # v2.16.0 ECMA-376：footnotes.xml 必须先带 separator(-1)/continuationSeparator(0)
    # 两个系统条目，否则 Word 打开时引用与脚注全部不渲染（"导出 Word 没有脚注"根因）
    items.append('<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr>'
                 '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
                 '</w:pPr><w:r><w:separator/></w:r></w:p></w:footnote>')
    items.append('<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr>'
                 '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
                 '</w:pPr><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>')
    for c in sorted(citations, key=lambda x: x.get("no", 0)):
        items.append(f'<w:footnote w:id="{c["no"]}">' +
                     _footnote_p_xml(c.get("ref") or "") + "</w:footnote>")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main">' + "".join(items) + "</w:footnotes>")


def _inject_footnotes(doc, citations: list):
    """把 footnotes.xml 作为新 OPC part 挂到文档（含 document.xml.rels 关系）。"""
    if not citations:
        return
    blob = _footnotes_xml(citations).encode("utf-8")
    part = Part(PackURI("/word/footnotes.xml"), _FOOT_NOTES_CT,
                blob, doc.part.package)
    doc.part.relate_to(part, _FOOT_NOTES_RT)



# ---------------- 版面构建 ----------------
def _new_document() -> Document:
    """A4 + 边距 + Normal 样式。"""
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin = sec.bottom_margin = Cm(2.54)
    sec.left_margin = sec.right_margin = Cm(3.18)
    normal = doc.styles["Normal"]
    normal.font.name = "仿宋_GB2312"
    normal.font.size = Pt(16)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "仿宋_GB2312")
    return doc


def _write_head(doc, masthead: str, issue_no: int, org_line: str, today: str,
                concise: bool = False):
    """刊头 → 期号 → 单位日期行 → 红色分隔线。
    v2.13.5 精简格式：取消刊头名称/期号/单位行/日期；v2.18.0 起精简格式
    连红色分隔线（★ 线与纯红线）也一并取消，正文直接从标题开始。"""
    if not concise:
        p = _para(doc, WD_ALIGN_PARAGRAPH.CENTER)
        _set_font(p.add_run(masthead), "华文新魏", 50, color=RGBColor(0xFF, 0, 0))
        p = _para(doc, WD_ALIGN_PARAGRAPH.CENTER)
        _set_font(p.add_run(f"第{issue_no}期"), "楷体_GB2312", 15)
        doc.add_paragraph()  # 空行
        p = _para(doc, WD_ALIGN_PARAGRAPH.LEFT)
        p.paragraph_format.tab_stops.add_tab_stop(Cm(14.64), WD_TAB_ALIGNMENT.RIGHT)
        _set_font(p.add_run(org_line), "楷体_GB2312", 14)
        _set_font(p.add_run("	" + today), "楷体_GB2312", 14)
        p = _para(doc, WD_ALIGN_PARAGRAPH.CENTER)
        _set_font(p.add_run("———————————— ★————————————"), "宋体", 15,
                  color=RGBColor(0xFF, 0, 0))
    doc.add_paragraph()

def _write_content(doc, title, editor_note, body, conclusion, sign_text: str,
                   state=None):
    """正标题 → 编者按 → 正文小节 → 结论 → 署名（单篇与综合公共部分）。

    v2.9.1：标题中的引用标记同样转换（此前标题是普通 run，AI 修改把标记写进
    标题时会在 document.xml 残留 [[citeK]] 触发导出 FATAL 自检）。"""
    # 正标题
    p = _para(doc, WD_ALIGN_PARAGRAPH.CENTER, space_before=6, space_after=12)
    _add_text_runs(p, title, "黑体", 18, state)
    # 编者按（两端对齐）
    p = _para(doc, WD_ALIGN_PARAGRAPH.JUSTIFY, first_line_chars=2, line_exact_pt=28)
    note = editor_note or ""
    m = re.match(r"^编者按[:：]\s*", note)
    _set_font(p.add_run("编者按："), "黑体", 16)
    _add_text_runs(p, note[m.end():] if m else note, "仿宋_GB2312", 16, state)
    # 正文（标题与正文均首行缩进2字符，段落两端对齐）
    for sec_ in body or []:
        p = _para(doc, WD_ALIGN_PARAGRAPH.JUSTIFY, first_line_chars=2,
                  line_exact_pt=28, space_before=6)
        _add_text_runs(p, sec_.get("h1", ""), "黑体", 16, state)
        for blk in sec_.get("blocks") or []:
            if blk.get("h2"):
                p = _para(doc, WD_ALIGN_PARAGRAPH.JUSTIFY, first_line_chars=2,
                          line_exact_pt=28)
                _add_text_runs(p, blk["h2"], "楷体_GB2312", 16, state)
            p = _para(doc, WD_ALIGN_PARAGRAPH.JUSTIFY, first_line_chars=2,
                      line_exact_pt=28)
            _add_text_runs(p, blk.get("text", ""), "仿宋_GB2312", 16, state)
    # 结论（两端对齐）
    p = _para(doc, WD_ALIGN_PARAGRAPH.JUSTIFY, first_line_chars=2, line_exact_pt=28)
    _add_text_runs(p, conclusion or "", "仿宋_GB2312", 16, state)
    # 署名
    doc.add_paragraph()
    p = _para(doc, WD_ALIGN_PARAGRAPH.RIGHT)
    _set_font(p.add_run(sign_text), "仿宋_GB2312", 16)


def build_docx(report: dict, issue_no: int, export_dir: str,
               citations=None) -> str:
    """按模板生成单篇专报 docx，返回文件路径。"""
    settings = db.all_settings()
    masthead = settings.get("masthead") or "XXXX专报"
    org_line = settings.get("org_line") or "XXXX"
    sign_name = settings.get("sign_name") or "XXX"
    today = time.strftime("%Y年%m月%d日")

    concise = (settings.get("export_style") or "full") == "concise"
    doc = _new_document()
    _write_head(doc, masthead, issue_no, org_line, today, concise=concise)
    state = _new_note_state(citations) if citations else None
    _write_content(doc, report["title"], report["editor_note"],
                   report["body"], report["conclusion"],
                   "" if concise else f"（{sign_name}）", state)
    if state:
        _inject_footnotes(doc, state["notes"])
    os.makedirs(export_dir, exist_ok=True)
    # v2.18.0 精简格式文件名＝导出日期（YYYYMMDD）+标题（不再带"精简报告："前缀，
    # 也不占期号）；标准模式保持期号命名。重名追加"副本"防覆盖。
    if concise:
        fname = sanitize_filename(time.strftime("%Y%m%d") + report["title"]) + ".docx"
    else:
        # v2.20.1 文件名跟随刊头名称（此前硬编码"永州稳定专报"——开源用户不适用
        # 且暴露单位名；刊头空时用默认 XXXX专报）
        fname = sanitize_filename(f"{masthead}第{issue_no}期：{report['title']}") + ".docx"
    path = _unique_path(os.path.join(export_dir, fname))
    doc.save(path)
    return path


def build_docx_multi(multi: dict, issue_no: int, export_dir: str) -> str:
    """按模板生成综合专报 docx（含 [[citeK]] 脚注），返回文件路径。"""
    settings = db.all_settings()
    masthead = settings.get("masthead") or "XXXX专报"
    org_line = settings.get("org_line") or "XXXX"
    sign_name = settings.get("sign_name") or "XXX"
    today = time.strftime("%Y年%m月%d日")
    citations = multi.get("citations") or []

    # v2.9.1 出口清场：非法形态标记（【cite1】/[cite1] 等）一律清除，
    # 合法 [[citeN]] 交给 _add_text_runs 转 unique 注号
    multi = {**multi,
             "title": _clean_dirty_markers(multi.get("title") or ""),
             "editor_note": _clean_dirty_markers(multi.get("editor_note") or ""),
             "conclusion": _clean_dirty_markers(multi.get("conclusion") or ""),
             "body": [{"h1": _clean_dirty_markers(s.get("h1") or ""),
                       "blocks": [{"h2": _clean_dirty_markers(b.get("h2") or ""),
                                   "text": _clean_dirty_markers(b.get("text") or "")}
                                  for b in (s.get("blocks") or [])]}
                      for s in (multi.get("body") or [])]}

    doc = _new_document()
    # v2.13.5 精简格式 v2.16.0 起对综合/自定义同样生效（用户反馈只有单篇生效）
    concise = (settings.get("export_style") or "full") == "concise"
    _write_head(doc, masthead, issue_no, org_line, today, concise=concise)
    state = _new_note_state(citations)
    _write_content(doc, multi["title"], multi["editor_note"],
                   multi["body"], multi["conclusion"],
                   f"（投稿：{sign_name}）", state)   # v2.20 通用署名（去除单位前缀）
    if state:   # v2.20.1 防御：citations 为空时 state 为 None（此前直接下标会崩）
        _inject_footnotes(doc, state["notes"])
    os.makedirs(export_dir, exist_ok=True)
    # v2.18.0 精简格式文件名＝导出日期（YYYYMMDD）+标题（单篇/综合同规则，
    # 不再带"精简报告："前缀）；标准模式保持期号命名。重名追加"副本"防覆盖。
    if concise:
        fname = sanitize_filename(time.strftime("%Y%m%d") + multi["title"]) + ".docx"
    else:
        # v2.20.1 文件名跟随刊头名称（此前硬编码"永州金融稳定专报"）
        fname = sanitize_filename(
            f"{masthead}第{issue_no}期：{multi['title']}") + ".docx"
    path = _unique_path(os.path.join(export_dir, fname))
    doc.save(path)
    return path


def export_report(report_id: int) -> dict:
    """导出专报：分配期号、生成 docx、自检、更新状态与期号计数。"""
    report = db.get_report(report_id)
    if not report:
        return {"ok": False, "error": "专报不存在"}
    if not report.get("title"):
        return {"ok": False, "error": "专报内容为空，无法导出"}
    settings = db.all_settings()
    export_dir = db.export_dir_setting()   # v2.20 默认=程序目录下"编译报告"（自动创建）
    concise = (settings.get("export_style") or "full") == "concise"
    if concise:
        issue_no = None    # v2.13.5 精简模式不占期号
    else:
        issue_no = int(settings.get("issue_counter", "0")) + 1

    # v2.9.7：单篇不插脚注（脚注体系仅供编辑器内与原文逐条核对；
    # [[citeK]] 由 _add_text_runs 的清场机制在导出正文删除）
    path = build_docx(report, issue_no, export_dir)

    # 自检（引号 / 字体提示）；失败则报错但不删除文件，便于排查
    from app.exporter.selfcheck import check_docx
    problems = check_docx(path, concise=concise)
    fatal = [p for p in problems if p.startswith("FATAL")]
    if fatal:
        return {"ok": False, "error": "自检失败: " + "; ".join(fatal),
                "path": path, "problems": problems}

    db.update_report(report_id, {
        "status": "exported", "issue_no": issue_no, "docx_path": path,
        "exported_at": db.now_iso(),
    })
    if issue_no is not None:
        db.set_setting("issue_counter", str(issue_no))
    db.log("export", f"专报导出成功：{'精简' if issue_no is None else '第%d期' % issue_no}"
                      f" {report['title'][:40]} -> {path}")
    return {"ok": True, "path": path, "issue_no": issue_no, "problems": problems}


def export_multi_report(mid: int) -> dict:
    """导出综合专报：期号与单篇共用计数，自检通过后落库。"""
    multi = db.get_multi_report(mid)
    if not multi:
        return {"ok": False, "error": "综合专报不存在"}
    if not multi.get("title"):
        return {"ok": False, "error": "综合专报内容为空，无法导出"}
    settings = db.all_settings()
    export_dir = db.export_dir_setting()   # v2.20 默认=程序目录下"编译报告"（自动创建）
    concise = (settings.get("export_style") or "full") == "concise"
    issue_no = None if concise else int(settings.get("issue_counter", "0")) + 1

    path = build_docx_multi(multi, issue_no, export_dir)

    from app.exporter.selfcheck import check_docx
    problems = check_docx(path, concise=concise)
    fatal = [p for p in problems if p.startswith("FATAL")]
    if fatal:
        return {"ok": False, "error": "自检失败: " + "; ".join(fatal),
                "path": path, "problems": problems}

    db.update_multi_report(mid, {
        "status": "exported", "issue_no": issue_no, "docx_path": path,
        "exported_at": db.now_iso(),
    })
    if issue_no is not None:
        db.set_setting("issue_counter", str(issue_no))
    db.log("export", f"综合专报导出成功：{'精简' if issue_no is None else '第%d期' % issue_no}"
                      f" {multi['title'][:40]} -> {path}")
    return {"ok": True, "path": path, "issue_no": issue_no, "problems": problems}
