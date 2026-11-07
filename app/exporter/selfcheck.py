"""docx 生成后自动自检（源自用户版式校验纪律）：
- FATAL：全文不得残留 ASCII 直引号；所有 rFonts 必须带 w:hint
- WARN：其他可疑项（字体名、空正文等）
- v2.8：综合专报需附 footnotes.xml——
  脚注文本无 ASCII 引号、rFonts 带 hint、文内引用与脚注一一对应、正文无残留标记
"""
import re
import zipfile


def check_docx(path: str, concise: bool = False) -> list:
    """返回问题列表；前缀 FATAL 表示必须修复，WARN 为提示。空列表即通过。

    v2.18.0 concise=True（精简格式）时跳过刊头/期号/编者按结构检查——
    精简格式本就不含这些元素，此前每次导出都带两条无意义 WARN。"""
    problems = []
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8")
            try:
                fxml = z.read("word/footnotes.xml").decode("utf-8")
            except KeyError:
                fxml = ""
    except (OSError, KeyError) as e:
        return [f"FATAL 无法读取docx: {e}"]

    # 1) ASCII 直引号检查（仅文本，不校验 XML 属性引号；正文与脚注分别检）
    def quote_check(scope: str, body_xml: str):
        texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", body_xml)
        full = "".join(texts)
        n_dq = full.count('"')
        n_sq = full.count("'")
        if n_dq:
            problems.append(f"FATAL {scope}存在 {n_dq} 个 ASCII 双直引号，须替换为中文弯引号")
        if n_sq:
            problems.append(f"WARN {scope}存在 {n_sq} 个 ASCII 单直引号，请人工核对")

    quote_check("正文", xml)

    # 2) rFonts 必须带 w:hint="eastAsia"
    rfonts_tags = re.findall(r"<w:rFonts [^>]*/>", xml)
    no_hint = [t for t in rfonts_tags if "w:hint" not in t]
    if no_hint:
        problems.append(
            f"FATAL 有 {len(no_hint)} 个 rFonts 缺少 w:hint（中文标点会按西文窄字形渲染）")

    # 3) 行距固定值检查：正文段（仿宋 16pt）应有 line=560 + lineRule=exact
    exact_count = xml.count('w:lineRule="exact"')
    if exact_count < 3:
        problems.append(f"WARN 固定行距段落数仅 {exact_count}，请核实行距设置")

    # 4) 基本结构（精简格式无刊头/期号/编者按，整体跳过）
    texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)
    full_text = "".join(texts)
    if not concise:
        if "华文新魏" not in xml:
            problems.append("WARN 未检出刊头字体 华文新魏")
        if "编者按" not in full_text:
            problems.append("WARN 未检出编者按")
        if not re.search(r"第\d+期", full_text):
            problems.append("WARN 未检出期号")

    # 5) 脚注（v2.8 综合专报；无脚注则跳过）
    if fxml:
        quote_check("脚注", fxml)
        frf = re.findall(r"<w:rFonts [^>]*/>", fxml)
        f_no_hint = [t for t in frf if "w:hint" not in t]
        if f_no_hint:
            problems.append(f"FATAL 脚注有 {len(f_no_hint)} 个 rFonts 缺少 w:hint")
        ref_ids = set(re.findall(r'<w:footnoteReference w:id="(\d+)"', xml))
        def_ids = set(re.findall(r'<w:footnote w:id="(\d+)"', fxml))
        # 同一脚注可被多处引用（集合匹配）；引用了不存在的脚注才是致命问题
        missing = ref_ids - def_ids
        if missing:
            problems.append(f"FATAL 正文引用了 footnotes.xml 中不存在的脚注 id: "
                            f"{sorted(missing)}")
        orphan = def_ids - ref_ids
        if orphan:
            problems.append(f"WARN footnotes.xml 存在未被正文引用的脚注 id: {sorted(orphan)}")
        if 'w:eastAsia="宋体"' not in fxml:
            problems.append("WARN 脚注文本未检出宋体字体")
    if "[[cite" in full_text:
        problems.append("FATAL 正文残留 [[citeK]] 引用标记未替换")

    return problems
