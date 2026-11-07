"""热点方向分析：对符合要求的线索做主题归并、排名与佐证。"""
import json

from app import db
from app.ai import client
from app.tasks import TaskCancelled

ANALYZE_PROMPT = """你是金融稳定领域的热点分析专家。以下是最近{days}天内经筛选符合要求的线索清单（每条含编号、标题、机构、发布时间、命中要求、匹配点）。

请分析当前的热点方向并排名：
1. 归纳 3-8 个热点方向，按热度从高到低排名；
2. 热度判断依据：涉及线索数量、机构的权威性多家共振（多家机构关注同一主题加分）、与筛选要求核心方向的贴合深度、时效性（越新越高）；
3. 每个方向输出：
   - name：方向名（10-20 字，概括主题）
   - description：80 字内，说明该方向的核心动态与关注价值
   - evidence_ids：2-4 条佐证线索的编号数组（只填清单中的编号，如 [1,3]）
   - count：该方向相关线索数量（整数）
   - trend："上升" | "新出现" | "持续" | "新高"
   - score：该方向热度分（0-100 整数，供排序参考，须与排名一致）
{extra}
线索清单：
{items}

输出 JSON：{{"directions":[{{"rank":1,"name":"...","description":"...","evidence_ids":[1,3],"count":3,"trend":"上升","score":90}}]}}"""


def analyze_hotspots(progress_cb=None, should_cancel=None, days=None) -> dict:
    settings = db.all_settings()
    days = int(settings.get("window_days", 7)) if days is None else int(days)
    items = db.list_items(days=(days if days and days > 0 else None),
                          matched=True, limit=200)
    if len(items) < 3:
        return {"ok": 0, "error": f"该时间窗内符合要求的线索仅 {len(items)} 条（不足 3 条），请调整时间范围或先检索筛选"}
    lines = []
    for n, it in enumerate(items, 1):
        title = it.get("title_zh") or it["title"]
        pub = (it.get("published_at") or "日期待核")[:10]
        crit = ",".join(str(c) for c in json.loads(it["sc_criteria"] or "[]")) \
            if it.get("sc_criteria") else ""
        lines.append(f"【编号{n}】{title}｜{it['source_name_en']}｜{pub}"
                     f"｜命中要求{crit or '-'}｜{it.get('match_points') or ''}")
    model = settings.get("model_screen", "")
    extra = (settings.get("hotspot_extra") or "").strip()
    extra_block = f"\n单位附加分析要求（必须遵守）：\n{extra}\n" if extra else ""
    messages = [
        {"role": "system", "content": "你是金融稳定领域热点分析专家，只输出 JSON。"},
        {"role": "user", "content": ANALYZE_PROMPT.format(
            days=days, items="\n".join(lines), extra=extra_block)},
    ]
    if progress_cb:
        progress_cb(f"分析 {len(items)} 条符合线索…")
    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")
    data = client.chat_json(messages, model, temperature=0.2, max_tokens=6000)
    directions = data.get("directions") or []
    directions = [d for d in directions if isinstance(d, dict) and d.get("name")]
    # 编号 → 线索 id 映射（存真实 id，前端可展开操作）
    id_by_no = {n: it["id"] for n, it in enumerate(items, 1)}
    for i, d in enumerate(directions, 1):
        d.setdefault("rank", i)
        ids = []
        for x in d.get("evidence_ids") or []:
            try:
                ids.append(id_by_no[int(x)])
            except (TypeError, ValueError, KeyError):
                continue
        if ids:
            d["evidence_item_ids"] = ids
    if not directions:
        return {"ok": 0, "error": "模型未返回有效热点方向"}
    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")
    db.save_hotspots(model, len(items), json.dumps(directions, ensure_ascii=False))
    db.log("hotspots", f"热点方向分析完成：{len(directions)} 个方向（基于 {len(items)} 条符合线索）")
    return {"ok": 1, "count": len(items), "directions": len(directions)}
