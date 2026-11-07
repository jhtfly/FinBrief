# -*- coding: utf-8 -*-
"""v2.13.4 本地线索库 AI 检索与智能分类。
- ai_search_local：输入关键词/主体，对时间窗内线索做 AI 语义匹配，返回命中 id 列表；
- ai_classify：对一批线索按主题归类（数据库页「AI 智能分类」用），返回主题桶。"""
import json
import time

from app import db
from app.ai import client
from app.db import get_conn


def _window_items(days: int, limit: int) -> list:
    """时间窗内线索(id/标题/摘要/日期/机构)，供 AI 检索或分类。"""
    with get_conn() as conn:
        day_sql = (f"WHERE i.published_at >= date('now', '-{int(days)} days')"
                   if days and days > 0 else "")
        rows = conn.execute(
            "SELECT i.id, i.title, i.title_zh, i.url, i.summary, i.published_at, "
            "COALESCE(s.name_cn,'') AS source, sc.is_match, sc.score "
            "FROM items i JOIN sources s ON s.id=i.source_id "
            "LEFT JOIN screenings sc ON sc.item_id=i.id " + day_sql +
            " ORDER BY i.published_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _slim_by_ids(ids: list) -> list:
    """v2.13.4 命中条目回查为 /api/items 同结构 dict（含机构/筛选/摘要等渲染字段）。"""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT i.id, i.title, i.title_zh, i.url, i.summary, i.published_at, "
            "i.fetched_at, i.read, i.ai_summary, sc.score AS score, "
            "COALESCE(s.name_cn,'') AS source, sc.is_match, sc.criteria "
            f"FROM items i LEFT JOIN sources s ON s.id=i.source_id "
            f"LEFT JOIN screenings sc ON sc.item_id=i.id "
            f"WHERE i.id IN ({marks}) ORDER BY i.published_at DESC", ids).fetchall()
    try:
        favs = set(db.favorite_ids("item"))
    except Exception:
        favs = set()
    out = []
    for r in rows:
        d = dict(r)
        # criteria 为 JSON 文本
        try:
            d["criteria"] = (json.loads(d["criteria"]) if d.get("criteria") else []) or []
        except (ValueError, TypeError):
            d["criteria"] = []
        d["read"] = int(d.get("read") or 0)
        d["fav"] = d["id"] in favs
        d["published_at"] = d.get("published_at") or ""
        out.append(d)
    return out


def expand_keywords(kw: str, model: str) -> list:
    """v2.13.6 AI 关键词扩展：一次小调用（输出仅 ~100 token），思考占用低、失败即退化原词。"""
    try:
        data = client.chat_json(
            [{"role": "system",
              "content": "你是检索词扩展器。给用户输入的中文/英文检索意图，输出 4-8 个"
                         "同义、英文原词、相关联想词（如：人工智能 → AI, artificial intelligence, "
                         "算力, 大模型, 生成式AI）。只输出 JSON：{kws: [若干检索词]}（kws 为字符串数组）。"},
             {"role": "user", "content": kw}],
            model, temperature=0.2, max_tokens=800)
        kws = []
        for x in (data.get("kws") or []):
            x = str(x).strip()[:24]
            if x and x.lower() != kw.lower() and x not in kws:
                kws.append(x)
        return [kw] + kws[:8]
    except Exception as e:
        try:
            db.log("ai", f"检索词扩展失败（退化原词检索）: {type(e).__name__}: {str(e)[:80]}")
        except Exception:
            pass
        return [kw]


def _like_search(kws: list, days: int, limit: int = 300) -> list:
    """本地多词 LIKE 命中（标题/中文译名/摘要/机构名任一包含即算）。"""
    with get_conn() as conn:
        where = []
        params = []
        for k in kws:
            like = f"%{k}%"
            where.append("(i.title LIKE ? OR i.title_zh LIKE ? OR i.summary LIKE ? "
                         "OR s.name_cn LIKE ? OR s.name_en LIKE ?)")
            params += [like] * 5
        day_sql = (f" AND i.published_at >= date('now', '-{int(days)} days')"
                   if days and days > 0 else "")
        rows = conn.execute(
            "SELECT i.id, i.title, i.title_zh, i.url, i.summary, i.published_at "
            "FROM items i JOIN sources s ON s.id=i.source_id WHERE COALESCE(i.kind,0)=0 AND "
            + "(" + " OR ".join(where) + ")" + day_sql +
            " ORDER BY i.published_at DESC LIMIT ?",
            params + [int(limit)]).fetchall()
    return [dict(r) for r in rows]


def _ai_relevance_pick(kw: str, items: list, model: str, note_cb=None) -> set:
    """v2.16.0 语义主判：把候选标题清单交给 AI，强相关才返回 id。"""
    import re as _re
    def _batch_pick(part, bi, n):
        lines = "\n".join(
            f"{it['id']}|{(it.get('title_zh') or it.get('title') or '')[:70]}"
            for it in part)
        data = client.chat_json(
            [{"role": "system",
              "content": "你是金融线索相关性判定助手。用户给出检索词和一批候选线索（每行 id|标题）。"
                         "判断每个候选标题与检索词是否强相关（主题/主体直接相关才算；"
                         "弱相关、仅词面重叠、或无关的一律不给）。"
                         "只输出 JSON：{\"ids\": [相关线索id...]}（id 整数数组，最多 60 条；"
                         "没有强相关就输出空数组）。"},
             {"role": "user",
              "content": f"检索词：{kw}\n\n候选线索（每行 id|标题，共 {len(part)} 条）：\n{lines}"}],
            model, temperature=0.1, max_tokens=12000)
        out = set()
        for x in (data.get("ids") or []):
            s = str(x).strip()
            if s.isdigit():
                out.add(int(s))
        return out
    picked = set()
    BATCH = 250
    for bi in range(0, len(items), BATCH):
        part = items[bi:bi + BATCH]
        if note_cb:
            note_cb(f"AI 相关性判定 {bi // BATCH + 1}/{(len(items) + BATCH - 1) // BATCH} 批（{len(part)} 条）…")
        try:
            picked |= _batch_pick(part, bi, len(items) // BATCH + 1)
        except Exception as e:
            db.log("ai", f"语义判定批次失败（退化该批关键词匹配）: {type(e).__name__}: {str(e)[:80]}")
            raise
    return picked


def ai_search_local(kw: str, days: int = 7, note_cb=None) -> dict:
    """v2.16.0 语义检索（用户主诉求）：窗口内线索标题清单交给 AI 判强相关即返回。

    流程：窗口内线索（≤400）→ AI 相关性判定（分批 250/批；max_tokens 12000，
    reasoning 模型思考占满会自动加倍重试）→ 命中回查渲染；AI 整体失败时退化
    关键词扩展+多词模糊匹配（v2.13.6 原链）。"""
    kw = (kw or "").strip()
    if not kw:
        return {"ok": False, "error": "请先输入关键词或检索主体"}
    model = db.get_setting("model_screen") or db.get_setting("model_compile")
    t0 = time.time()
    with get_conn() as conn:
        day_sql = ""
        if days and days > 0:
            day_sql = f" AND i.published_at >= date('now', '-{int(days)} days')"
        rows = conn.execute(
            "SELECT i.id, i.title, i.title_zh, i.url, i.summary, i.published_at, "
            "COALESCE(s.name_cn,'') AS source "
            "FROM items i JOIN sources s ON s.id=i.source_id "
            "WHERE COALESCE(i.kind,0)=0" + day_sql +
            " ORDER BY i.published_at DESC LIMIT 400").fetchall()
    items = [dict(r) for r in rows]
    if not items:
        return {"ok": True, "matched": [], "items": [], "kws": [],
                "note": "时间窗内没有线索", "scanned": 0}
    picked = set()
    ai_ok = False
    try:
        picked = _ai_relevance_pick(kw, items, model, note_cb=note_cb)
        ai_ok = bool(picked)          # AI 返回空=没有强相关（不是失败）
    except Exception as e:
        db.log("ai", f"AI 相关性判定失败（退化关键词匹配）: {type(e).__name__}: {str(e)[:90]}")
    if ai_ok:
        slim = _slim_by_ids([it["id"] for it in items if it["id"] in picked])
        slim = [x for x in slim if x["id"] in picked]
        matched = [x["id"] for x in slim]
        db.log("ai", f"AI 语义检索「{kw[:24]}」命中 {len(matched)}/{len(items)} 条"
                     f"（{time.time()-t0:.1f}s）")
        return {"ok": True, "matched": matched, "items": slim, "kws": [],
                "note": f"AI 判定强相关 {len(matched)} 条（扫描 {len(items)} 条）",
                "scanned": len(items)}
    # —— 兜底：关键词扩展 + 多词模糊 LIKE（原两段式） ——
    kws = expand_keywords(kw, model)
    like_items = _like_search(kws, days, limit=300)
    by_id = {it["id"]: it for it in items}
    like_ids = [i for i in [it["id"] for it in like_items] if i in by_id]
    slim = _slim_by_ids(like_ids)
    db.log("ai", f"AI 检索退化模式「{kw[:24]}」扩展词 {len(kws)} → 命中 {len(like_ids)} 条"
                 f"（{time.time()-t0:.1f}s）")
    return {"ok": True, "matched": like_ids, "items": slim, "kws": kws,
            "note": "扩展词：" + " / ".join(kws[:6]) if len(kws) > 1 else "",
            "scanned": len(like_ids), "truncated": False}


_TOPICS_HINT = ("监管机构与政策执法 / 货币与利率政策 / 银行业务与风险管理 / "
                "证券与资本市场 / 保险与养老金融 / 存款保险与金融稳定 / "
                "宏观经济与市场动态 / 地缘政治与能源 / 科技与人工智能 / 其他")


def ai_classify(items: list, note_cb=None) -> dict:
    """对给定 items（窗口内线索 dict）做主题分类，返回 {topics:[{topic,count,titles}]}。
    大批量分批（每批 60 条）多次调用后合并；titles 每主题最多保留 40 条标题。"""
    agg = {}
    BATCH = 30   # v2.13.5 reasoning 模型思考占配额，缩小批次换速度
    n_batches = (len(items) + BATCH - 1) // BATCH
    for bi in range(n_batches):
        part = items[bi * BATCH:(bi + 1) * BATCH]
        if note_cb:
            note_cb(f"AI 主题分类第 {bi+1}/{n_batches} 批（{len(part)} 条）…")
        lines = "\n".join(
            f"{it['id']}|{(it.get('title_zh') or it.get('title') or '')[:60]}"
            f"｜{(it.get('summary') or '')[:70]}" for it in part)
        system = ("你是金融情报线索分类助手。把给定的线索按主题归类。主题在以下范畴中选取"
                  "（可按需合并或新增更贴切的主题，但尽量收敛到 12 个以内）：\n"
                  + _TOPICS_HINT + "\n"
                  "只输出 JSON：{\"topics\":[{\"topic\":\"主题名\",\"ids\":[线索id...]}]}；"
                  "每条线索归入最贴切的一个主题，不得遗漏。")
        user = f"线索清单（每行 id|标题｜摘要，共 {len(part)} 条）：\n{lines}\n\n请输出 JSON。"
        model = db.get_setting("model_screen") or db.get_setting("model_compile")
        data = client.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model, temperature=0.2, max_tokens=9000)
        by_id = {it["id"]: it for it in part}
        for t in (data.get("topics") or []):
            topic = (t.get("topic") or "其他").strip()[:24] or "其他"
            bucket = agg.setdefault(topic, {"topic": topic, "count": 0, "titles": []})
            seen = set(bucket["titles"] and [x["id"] for x in bucket["titles"]] or [])
            for i in (t.get("ids") or []):
                i = int(i) if str(i).strip().isdigit() else None
                if i is None or i not in by_id:
                    continue
                it = by_id[i]
                bucket["count"] += 1
                if len(bucket["titles"]) < 40:
                    im = it.get("is_match")
                    bucket["titles"].append({
                        "id": it["id"], "url": it.get("url") or "",
                        "title": (it.get("title_zh") or it.get("title") or "")[:64],
                        "date": (it.get("published_at") or "")[:10],
                        "source": (it.get("source") or "")[:20],
                        "match": (None if im is None else int(im)),
                        "score": it.get("score")})
    out = sorted(agg.values(), key=lambda b: -b["count"])
    return {"topics": out, "total": sum(b["count"] for b in out)}
