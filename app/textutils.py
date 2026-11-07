"""v2.13.6 公共文本工具：中文弯引号规范化（原 6 处逐字重复收拢一处）。"""


def fix_quotes(t: str) -> str:
    """把可能残留的 ASCII 直引号成对替换为中文弯引号（“ 左 / ” 右）。"""
    out, open_q = [], True
    for ch in t or "":
        if ch == '"':
            out.append("\u201c" if open_q else "\u201d")
            open_q = not open_q
        else:
            out.append(ch)
    return "".join(out)
