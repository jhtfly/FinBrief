"""FastAPI 后端：采集/筛选/编译/导出/设置 全部路由。"""


import os


import sys
import threading


from typing import Optional


from fastapi import FastAPI


from fastapi.responses import FileResponse, JSONResponse, StreamingResponse


import json


from pydantic import BaseModel


from app import db


from app.exporter.docx_builder import export_report


from app.fetcher import runner


from app.paths import WEB_DIR


from app.tasks import run_in_background, task_snapshot, cancel_task, interrupt_task, clear_queue


app = FastAPI(title="金融热点编译系统")


@app.get("/")
def index():
    # v2.21.1 根修：禁用 HTTP 缓存——WebView2 对无 Cache-Control 的静态响应按
    # Last-Modified 启发式缓存，升级程序后可能继续提供旧版界面（用户截图实证）。
    return FileResponse(f"{WEB_DIR}/index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/help.js")
def help_js():
    """v2.13.5 使用说明浮窗脚本（README 渲染 + 目录）。"""
    return FileResponse(f"{WEB_DIR}/help.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/route_catalog.json")
def route_catalog():
    """v2.13.8 内置路由目录（2979+ 命名空间分类清单，从 RSSHub 路由库扫描生成）。"""
    return FileResponse(f"{WEB_DIR}/route_catalog.json", media_type="application/json",
                        headers={"Cache-Control": "no-store"})


def _job_fetch(state, source_keys=None):
    sources = db.list_sources(only_enabled=True)
    if source_keys:
        wanted = set(source_keys)
        sources = [s for s in sources if s["key"] in wanted]
    total = len(sources)
    # v2.13.8 失败信源重试机制：fetch_failed_retry 设置 0=关 / 1-3=失败站自动重跑轮次（默认关）
    try:
        retry_rounds = max(0, min(3, int(db.get_setting("fetch_failed_retry", "0") or 0)))
    except (TypeError, ValueError):
        retry_rounds = 0

    def cb(done, total_, name, ok_n, fail_n, new_n):
        state.update(f"{done}/{total_}：{name}" + (f" · 新增{new_n}" if new_n else ""))

    state.update(f"0/{total}：开始…")
    result = runner.run_fetch(source_keys, progress_cb=cb,
                              should_cancel=lambda: state.cancelled)
    fails = result.get("failures") or []
    added_ids = result.get("added_ids") or []
    # v2.13.8 失败信源检索重试（serve 面向被墙/瞬时错误的站点再做 N 轮）
    for rr in range(1, retry_rounds + 1):
        if not fails:
            break
        if state.cancelled:
            break
        keys = [f["key"] for f in fails if f.get("key")]
        if not keys:
            break
        state.update(f"失败站重试 第{rr}/{retry_rounds} 轮（{len(keys)} 站）…")
        sub = runner.run_fetch(keys, progress_cb=cb,
                               should_cancel=lambda: state.cancelled)
        added_ids += sub.get("added_ids") or []
        result["ok"] += sub.get("ok", 0)
        result["new"] += sub.get("new", 0)
        if sub.get("run_id"):
            result["run_id"] = sub["run_id"]
        if sub.get("added_ids"):
            seen_ids = set(result.get("added_ids") or []) | set(sub["added_ids"])
            result["added_ids"] = list(seen_ids)
        fails = sub.get("failures") or []
        result["failures"] = fails
        result["fail"] = len(fails)
        if not fails:
            state.update(f"失败站重试成功：第 {rr} 轮全部补齐")
            break
    fails = result.get("failures") or []
    result["hint_proxy"] = bool(fails) and len(fails) >= 3
    # 采集后自动翻译/补日期——v2.7 起只处理【本轮新增】线索，
    # 不再触碰历史遗留（修复"每次检索都提示补全日期"）；
    # 全库补齐仍可经线索面板「翻译英文标题」「补全日期」按钮手动触发。
    if db.get_setting("auto_translate") == "1" and added_ids:
        from app.ai.translate import translate_items
        state.update(f"翻译英文标题 0/{len(added_ids)}…")
        tr = translate_items(
            added_ids,
            progress_cb=lambda d, t, ok: state.update(f"翻译标题 {d}/{t}"),
            should_cancel=lambda: state.cancelled)
        result["translated"] = tr.get("ok", 0)
    # v2.15.0 面板「补全日期」按钮取消、检索后默认自动补全；backfill_auto=0（设置里可关）
    new_no_date = [iid for iid in added_ids
                   if db.get_setting("backfill_auto", "1") != "0"
                   and not (db.get_item(iid) or {}).get("published_at")]
    if new_no_date:
        from app.fetcher.date_backfill import backfill_dates
        state.update(f"补全发布日期 0/{len(new_no_date)}…")
        bf = backfill_dates(new_no_date, progress_cb=lambda d, t, ok: state.update(
            f"补全发布日期 {d}/{t}…"), should_cancel=lambda: state.cancelled)
        result["backfilled"] = bf.get("ok", 0)
    # v2.19 检索后自动 AI 筛选（默认开，设置里可关）：仅对本轮新增线索执行，
    # 与面板「⚡ 一键筛选」同一判定管线，不重复消耗历史线索额度
    if added_ids and db.get_setting("auto_screen_after_fetch", "1") != "0":
        from app.ai.screen import screen_items
        state.update(f"AI 筛选新增线索 0/{len(added_ids)}…")
        sc = screen_items(added_ids, progress_cb=lambda d, t, m: state.update(
            f"AI 筛选新增线索 {d}/{t}（符合 {m} 条）"),
            should_cancel=lambda: state.cancelled)
        result["screened"] = sc.get("ok", 0)
        result["screen_matched"] = sc.get("match", 0)
    state.update(f"采集完成：成功 {result['ok']}，失败 {result['fail']}，新增 {result['new']}"
                 + (f"，自动筛选符合 {result.get('screen_matched', 0)} 条"
                    if "screened" in result else ""))
    return result


def _job_screen(state, item_ids):
    from app.ai.screen import screen_items
    total = len(item_ids)
    state.update(f"0/{total}")

    def cb(done, total_, matched):
        state.update(f"{done}/{total_}（符合 {matched} 条）")

    result = screen_items(item_ids, progress_cb=cb,
                          should_cancel=lambda: state.cancelled)
    return result


def _job_compile(state, item_ids):
    """编译（v2.6 支持打断回滚）：逐条检查取消标志，被打断删除本轮生成的草稿。"""
    from app.ai.compile import compile_item
    from app.tasks import TaskCancelled
    ok, fail = 0, []
    created_ids = []   # 本轮新生成的专报（打断回滚用）
    try:
        for i, iid in enumerate(item_ids, 1):
            state.check()  # 协作检查点
            item = db.get_item(iid)
            name = item["source_name_cn"] if item else str(iid)
            state.update(f"{i}/{len(item_ids)}：{name}")
            try:
                before = db.get_report_by_item(iid)
                r = compile_item(iid)
                after = db.get_report_by_item(iid)
                if after and (before is None or before["id"] != after["id"]):
                    created_ids.append(after["id"])
                if r["ok"]:
                    ok += 1
                else:
                    fail.append({"item_id": iid, "source": name,
                                 "reason": r.get("reason", "")})
            except TaskCancelled:
                raise
            except Exception as e:
                fail.append({"item_id": iid, "source": name,
                             "reason": f"{type(e).__name__}: {e}"})
        state.update(f"编译完成：成功 {ok}，失败 {len(fail)}")
        return {"ok": ok, "fail": fail}
    except TaskCancelled:
        n = db.delete_reports(created_ids)
        db.log("compile", f"编译任务被打断：已回滚本轮 {n} 篇草稿", "warn")
        raise


def _job_probe(state):
    from app.tasks import TaskCancelled
    sources = db.list_sources(only_enabled=True)
    out = []
    try:
        for i, s in enumerate(sources, 1):
            state.check()
            state.update(f"{i}/{len(sources)}：{s['name_cn']}")
            r = runner.probe_site(s)
            db.update_source_fetch_status(s["key"], r["ok"], r.get("items", 0),
                                          r.get("error") or "")  # v2.4 连通状态
            out.append({"source": s["name_cn"], "key": s["key"], **r})
    except TaskCancelled:
        out.append({"interrupted": True, "tested": len(out), "total": len(sources)})
    return out


def _job_translate(state, item_ids):
    from app.ai.translate import translate_items
    state.update("统计待翻译…")

    def cb(done, total, ok):
        state.update(f"{done}/{total}")

    return translate_items(item_ids or None, progress_cb=cb,
                           should_cancel=lambda: state.cancelled)


def _job_ai_add(state, url):
    from app.ai.source_analyzer import add_source_from_url, SourceError
    state.update(f"分析 {url} …")
    try:
        return add_source_from_url(url)
    except SourceError as e:
        return {"ok": False, "error": str(e)}


def _job_ai_check(state):
    from app.ai.source_analyzer import check_and_fix_sources

    def cb(done, total, name):
        state.update(f"{done}/{total}：{name}")

    return check_and_fix_sources(progress_cb=cb)


class FetchReq(BaseModel):
    source_keys: Optional[list] = None


@app.post("/api/fetch")
def api_fetch(req: FetchReq):
    ok, msg = run_in_background("fetch", _job_fetch, req.source_keys)
    return {"started": ok, "message": msg}


class ScreenReq(BaseModel):
    item_ids: list


@app.post("/api/screen")
def api_screen(req: ScreenReq):
    ok, msg = run_in_background("screen", _job_screen, req.item_ids)
    return {"started": ok, "message": msg}


class CompileReq(BaseModel):
    item_ids: list


@app.post("/api/compile")
def api_compile(req: CompileReq):
    ok, msg = run_in_background("compile", _job_compile, req.item_ids)
    return {"started": ok, "message": msg}


@app.post("/api/probe")
def api_probe():
    ok, msg = run_in_background("probe", _job_probe)
    return {"started": ok, "message": msg}


class TranslateReq(BaseModel):
    item_ids: list = []


@app.post("/api/translate")
def api_translate(req: TranslateReq):
    ok, msg = run_in_background("translate", _job_translate, req.item_ids)
    return {"started": ok, "message": msg}


class BackfillReq(BaseModel):
    item_ids: list = []


def _job_backfill(state, item_ids):
    from app.fetcher.date_backfill import backfill_dates
    state.update("统计待补全…")
    return backfill_dates(item_ids or None, progress_cb=lambda d, t, ok:
                          state.update(f"{d}/{t}（成功 {ok}）"),
                          should_cancel=lambda: state.cancelled)


@app.post("/api/backfill")
def api_backfill(req: BackfillReq):
    ok, msg = run_in_background("backfill", _job_backfill, req.item_ids)
    return {"started": ok, "message": msg}


class AIAddReq(BaseModel):
    url: str


@app.post("/api/sources/ai_add")
def api_sources_ai_add(req: AIAddReq):
    url = (req.url or "").strip()
    if not url:
        return {"started": False, "message": "请填写网址"}
    ok, msg = run_in_background("ai_add", _job_ai_add, url)
    return {"started": ok, "message": msg}


@app.post("/api/sources/ai_check")
def api_sources_ai_check():
    ok, msg = run_in_background("ai_check", _job_ai_check)
    return {"started": ok, "message": msg}


def _job_ai_check_one(state, key):
    from app.ai.source_analyzer import check_and_fix_sources
    state.update(f"检查 {key} …")

    def cb(done, total, name):
        state.update(f"{done}/{total}：{name}")

    return check_and_fix_sources(progress_cb=cb, only_key=key)


@app.post("/api/sources/{key}/ai_fix")
def api_source_ai_fix(key: str):
    if not db.get_source(key):
        return {"started": False, "message": "信源不存在"}
    ok, msg = run_in_background("ai_check", _job_ai_check_one, key)
    return {"started": ok, "message": msg}


def _job_hotspots(state, days=None):
    from app.ai.hotspots import analyze_hotspots
    state.update("统计符合线索…")
    return analyze_hotspots(progress_cb=lambda m: state.update(m),
                            should_cancel=lambda: state.cancelled, days=days)


class HotspotsReq(BaseModel):
    days: int = 7          # v2.13.7 时间窗（0=全部时间）


@app.post("/api/hotspots")
def api_hotspots(req: HotspotsReq):
    days = req.days if req and req.days is not None else 7
    ok, msg = run_in_background("hotspots", _job_hotspots, days)
    return {"started": ok, "message": msg}


@app.get("/api/hotspots")
def api_hotspots_list():
    analyses = db.list_hotspots(10)
    for a in analyses:
        ref_ids = set()
        for d in a["analysis"]:
            ref_ids.update(d.get("evidence_item_ids") or [])
        refs = {}
        for it in db.get_items_by_ids(list(ref_ids)):
            refs[it["id"]] = {
                "id": it["id"], "title": it["title"],
                "title_zh": it.get("title_zh") or "",
                "source": it.get("source_name_cn") or "",
                "published_at": it.get("published_at") or "",
                "url": it["url"], "score": it.get("score"),
                "is_match": it.get("is_match"),
                "has_draft": bool(it.get("report_id")) and it.get("report_status") != "abandoned",
            }
        for d in a["analysis"]:
            d["ref_items"] = [refs[i] for i in (d.get("evidence_item_ids") or []) if i in refs]
    return {"analyses": analyses}


class FavReq(BaseModel):
    type: str          # item / report
    target_id: int


@app.post("/api/favorites")
def api_favorites_toggle(req: FavReq):
    if req.type not in ("item", "report", "multi"):
        return JSONResponse({"error": "type 必须是 item / report / multi"}, status_code=400)
    added = db.toggle_favorite(req.type, req.target_id)
    return {"ok": True, "favorited": added}


class FavClearReq(BaseModel):
    type: str          # item / report


@app.post("/api/favorites/clear")
def api_favorites_clear(req: FavClearReq):
    if req.type not in ("item", "report", "multi"):
        return JSONResponse({"error": "type 必须是 item / report / multi"}, status_code=400)
    removed = db.clear_favorites(req.type)
    db.log("favorites", f"清空{'线索' if req.type == 'item' else '专报'}收藏 {removed} 条")
    return {"ok": True, "removed": removed}


@app.get("/api/favorites")
def api_favorites_list():
    items = db.list_favorites("item")
    reports = db.list_favorites("report")
    for r in reports:
        r["word_count"] = sum(len(b.get("text", ""))
                              for s in (r.get("body") or [])
                              for b in (s.get("blocks") or []))
    return {"items": items, "reports": reports,
            "multi": db.list_favorites("multi")}


@app.post("/api/item/{item_id}/read")
def api_item_read(item_id: int):
    db.mark_item_read(item_id)
    return {"ok": True}


class TestAPIReq(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@app.post("/api/test_api")
def api_test_api(req: TestAPIReq):
    """设置页直连测试：用提交的值（空则用已存值），不落库。
    v2.20.1：请求未填任何自定义值时按模式测试——已激活试用 → 测试用上游
    （统一试用模型）；未激活但自定义没配全 → 也退测试用上游（用户要求：
    未配置自定义 API 也能测试用连接）。手动填了 base/key/model 则测自定义。"""
    import time as _t

    import httpx
    s = db.all_settings()
    base = (req.base_url or s.get("api_base_url") or "").rstrip("/")
    key = req.api_key or db.get_secret("api_key")
    model = req.model or s.get("model_screen") or ""
    channel = "custom"
    hand_filled = bool(req.base_url or req.api_key or req.model)
    if not hand_filled and db.get_setting("trial_active_hash", ""):
        from app.trial_config import TRIAL_BASE_URL, TRIAL_DEFAULT_MODEL, upstream_key
        base, key = TRIAL_BASE_URL, upstream_key()
        model = db.get_setting("trial_model", "") or TRIAL_DEFAULT_MODEL
        channel = "trial"
    if not base or not key:
        # 未配置自定义 API：退测试用上游（试用期外也允许先验证通道可用性）
        from app.trial_config import TRIAL_BASE_URL, TRIAL_DEFAULT_MODEL, upstream_key
        base, key = TRIAL_BASE_URL, upstream_key()
        model = db.get_setting("trial_model", "") or TRIAL_DEFAULT_MODEL
        channel = "trial"
    if not base or not key or not model:
        return {"ok": False, "error": "API 地址 / Key / 模型均需填写"}
    t0 = _t.time()
    try:
        with httpx.Client(timeout=45, verify=False) as client:
            resp = client.post(f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "max_tokens": 64,
                      "messages": [{"role": "user",
                                    "content": "回复：连接成功"}]})
        latency = round(_t.time() - t0, 1)
        if resp.status_code == 200:
            data = resp.json()
            msg = (data["choices"][0].get("message") or {})
            # v2.9.6：思考型模型 max_tokens 可能只产生思考段（content 为空），用 reasoning_content 兜底
            reply = (msg.get("content") or msg.get("reasoning_content") or "").strip()
            if not reply:
                reply = "（已连通，但该模型未返回文本内容；连接可用）"
            return {"ok": True, "latency": latency, "reply": reply[:80],
                    "channel": channel}
        detail = ""
        try:
            detail = resp.json().get("error", {}).get("message", "")[:150]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {resp.status_code}: {detail}",
                "channel": channel}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:150]}"}


class TranslateTextReq(BaseModel):
    text: str = ""
    item_id: int = 0


@app.post("/api/translate_text_stream")
def api_translate_text_stream(req: TranslateTextReq):
    """v2.9.9 引用校对翻译改流式：progress 事件实时回传已翻译字数；完成后落缓存。"""
    from app.ai import client as ai_client

    def _emit(obj: dict):
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def gen():
        try:
            text = (req.text or "").strip()
            if not text:
                yield _emit({"type": "error", "message": "无内容"}); return
            if req.item_id:
                it = db.get_item(req.item_id)
                cached = (it or {}).get("translated_full") or ""
                if cached.strip():
                    yield _emit({"type": "done", "text": cached.strip(), "cached": True}); return
            s = db.all_settings()
            model = (s.get("model_translate") or s.get("model_compile") or "").strip()
            if not model and not db.get_setting("trial_active_hash", ""):
                yield _emit({"type": "error", "message": "未配置模型"}); return
        except Exception as e:   # v2.20.1：取数异常也走 error 事件（此前生成器直接
            # 死掉，前端只见流结束、报不出原因，显示笼统的"未返回内容"）
            yield _emit({"type": "error",
                         "message": (type(e).__name__ + ": " + str(e))[:200]}); return
        # 试用模式下 model 传空：client._route 统一走试用模型（v2.20 双模式）
        messages = [
            {"role": "system", "content":
                "你是专业金融外文编译员。把用户提供的境外机构原文完整翻译为通顺中文。"
                "在忠实原意的前提下可按语义重新排版：原文一段过长或要点混杂时，按语义把译文拆成多个"
                "自然段（每段 2-5 句为宜，段落间空行分隔），要点顺序保持在原意中的先后；"
                "专业术语首次出现附英文原文。只输出译文，不要任何解释。"
                "原文中形如【数字】的标记是引用锚点：请在译文的对应语义位置原样保留这些标记"
                "（不要翻译、不要增删、不要移动到段首段尾以外的位置）。"},
            {"role": "user", "content": text},
        ]
        tok = {"n": 0}

        def on_delta(kind, content):
            if kind == "content":
                tok["n"] += len(content or "")

        try:
            out = ai_client.chat_stream(messages, model, temperature=0.2,
                                        json_mode=False, max_tokens=32768,
                                        on_delta=on_delta)
        except Exception as e:
            msg = str(type(e).__name__) + ": " + str(e)
            if "max_tokens" in msg or "invalid" in msg.lower() or "400" in msg:
                try:
                    tok = {"n": 0}
                    out = ai_client.chat_stream(messages, model, temperature=0.2,
                                                json_mode=False, max_tokens=8192,
                                                on_delta=on_delta)
                except Exception as e2:
                    yield _emit({"type": "error",
                                 "message": (str(type(e2).__name__) + ": " + str(e2))[:200]})
                    return
            else:
                yield _emit({"type": "error", "message": msg[:200]}); return
        out = (out or "").strip()
        if not out:
            yield _emit({"type": "error", "message": "模型未返回译文"}); return
        if req.item_id and len(out) > 50:
            try:
                conn = db.get_conn()
                conn2 = None
                with db.get_conn() as c2:
                    c2.execute("UPDATE items SET translated_full=? WHERE id=?",
                               (out, req.item_id))
                    c2.commit()
            except Exception:
                pass
        yield _emit({"type": "done", "text": out, "cached": False})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


class EvidenceZhReq(BaseModel):
    kind: str = "single"
    rid: int = 0
    no: int = 0
    zh: str = ""


@app.post("/api/evidence_zh")
def api_evidence_zh(req: EvidenceZhReq):
    """v2.10.0 取证摘录译文持久化（quote_zh 写回 evidence，打开取证卡不再重翻）。"""
    if req.kind not in ("single", "multi") or not req.rid:
        return {"ok": False, "message": "参数不完整"}
    ok = db.update_evidence_zh(req.kind, req.rid, req.no, req.zh)
    return {"ok": bool(ok)}


@app.post("/api/translate_text")
def api_translate_text(req: TranslateTextReq):
    """v2.9.7 引用校对：英文原文→中文全译。不截断；带 item_id 时缓存进 items.translated_full
    （已译过直接返回缓存）。"""
    text = (req.text or "").strip()
    if not text:
        return {"ok": False, "error": "无内容"}
    if req.item_id:
        it = db.get_item(req.item_id)
        if it and (it.get("translated_full") or "").strip():
            return {"ok": True, "text": it["translated_full"].strip(),
                    "cached": True}
    from app.ai import client as ai_client

    def _call(limit_tokens):
        s = db.all_settings()
        model = (s.get("model_translate") or s.get("model_compile") or "").strip()
        if not model and not db.get_setting("trial_active_hash", ""):
            raise RuntimeError("未配置模型")
        # 试用模式下 model 传空：client._route 统一走试用模型（v2.20 双模式）
        messages = [
            {"role": "system", "content":
                "你是专业金融外文编译员。把用户提供的境外机构原文完整翻译为通顺中文。"
                "在忠实原意的前提下可按语义重新排版：原文一段过长或要点混杂时，按语义把译文拆成多个"
                "自然段（每段 2-5 句为宜，段落间空行分隔），要点顺序保持在原意中的先后；"
                "专业术语首次出现附英文原文。只输出译文，不要任何解释。"
                "原文中形如【数字】的标记是引用锚点：请在译文的对应语义位置原样保留这些标记"
                "（不要翻译、不要增删、不要移动到段首段尾以外的位置）。"},
            {"role": "user", "content": text},
        ]
        return ai_client.chat(messages, model, temperature=0.2,
                              json_mode=False, max_tokens=limit_tokens)

    try:
        out = _call(32768)   # v2.9.7 取消 6000 字截断（您反馈模型上限足够用）
    except Exception as e:
        # 部分平台按 max_tokens 参数拒绝大值 → 退回保守值重试一次
        msg = f"{type(e).__name__}: {e}"
        if "max_tokens" in msg or "invalid" in msg.lower() or "400" in msg:
            out = _call(8192)
        else:
            return {"ok": False, "error": msg[:200]}
    out = (out or "").strip()
    if not out:
        return {"ok": False, "error": "模型未返回译文"}
    if req.item_id and len(out) > 50:
        try:
            with db.get_conn() as conn:
                conn.execute("UPDATE items SET translated_full=? WHERE id=?",
                             (out, req.item_id))
                conn.commit()
        except Exception:
            pass
    return {"ok": True, "text": out}


@app.get("/api/prompt_defaults")
def api_prompt_defaults():
    from app.ai.compile import SYSTEM_PROMPT as CP
    from app.ai.edit import SYSTEM_PROMPT as EP
    from app.ai.hotspots import ANALYZE_PROMPT as HP
    from app.ai.screen import CRITERIA_TEXT as SC
    from app.ai.screen import JUDGE_PROMPT, OUTPUT_SPEC
    from app.ai.multi import SYSTEM_PROMPT as MUP
    from app.ai.summarize import SYSTEM_PROMPT as MP
    from app.ai.translate import SYSTEM_PROMPT as TP
    from app.ai_search import SYSTEM_PROMPT as SP
    return {"screen_criteria": SC, "compile_prompt": CP,
            "screen_judge": JUDGE_PROMPT.replace("{criteria}", SC),
            "translate_prompt": TP, "hotspot_prompt": HP,
            "score_prompt": OUTPUT_SPEC.strip(),       # v2.4 内置评分规则（只读参考）
            "summarize_prompt": MP,                    # v2.5 内置摘要提示词
            "search_prompt": SP,                       # v2.6 内置搜索确认提示词
            "multi_prompt": MUP.replace("{extra}", ""),  # v2.8 内置综合专报编译提示词
            "edit_prompt": EP}                         # v2.8 内置 AI 修改提示词


@app.get("/api/fetch_runs")
def api_fetch_runs(limit: int = 15):
    return {"runs": db.list_fetch_runs(limit)}


@app.get("/api/task")
def api_task():
    return task_snapshot()


@app.post("/api/task/interrupt/{tid}")
def api_task_interrupt(tid: str):
    """v2.6 打断运行中的任务（协作式回滚到任务前状态）。
    v2.7：说明真实语义——当前处理单元（AI 单次调用/单站抓取）完成后立即回滚。"""
    ok = interrupt_task(tid)
    if ok:
        return {"ok": True,
                "message": "已发送打断请求：当前步骤（一次 AI 调用或单站抓取）完成后立即回滚"}
    return {"ok": False, "message": "任务已结束或不在运行中（无需打断）"}


@app.delete("/api/task/queue/{tid}")
def api_task_cancel(tid: str):
    ok = cancel_task(tid)
    return {"ok": ok, "message": "已取消排队" if ok else "任务不在排队中（可能已开始）"}


@app.post("/api/task/queue/clear")
def api_task_queue_clear(req: dict = None):
    """v2.21 一键清空排队（间隔检索风暴/误攒任务时用）。body 可选 {group:"fetch"}。"""
    grp = (req or {}).get("group")
    n = clear_queue(grp)
    return {"ok": True, "cleared": n,
            "message": f"已清空排队 {n} 个" + (f"（{grp} 组）" if grp else "")}


@app.delete("/api/task/{tid}")
def api_task_delete(tid: str):
    n = db.delete_task_run(tid)
    return {"ok": n > 0}


@app.post("/api/task/clear_history")
def api_task_clear():
    n = db.clear_task_runs()
    db.log("system", f"清空任务历史 {n} 条")
    return {"ok": True, "removed": n}


@app.get("/api/token_stats")
def api_token_stats():
    return db.token_usage_stats()


@app.post("/api/token_stats/reset")
def api_token_reset():
    db.reset_token_usage()
    return {"ok": True}


@app.get("/api/trial/status")
def api_trial_status():
    from app.trial_config import (TRIAL_DEFAULT_MODEL, TRIAL_LIMIT_TOKENS,
                                  TRIAL_MODELS)
    used = (int(db.get_setting("trial_used_in", "0") or 0)
            + int(db.get_setting("trial_used_out", "0") or 0))
    # v2.20 试用模式 = 已激活试用密匙（trial_active_hash），与自定义 API 设置完全独立
    is_trial = bool(db.get_setting("trial_active_hash", ""))
    return {"used": used, "limit": TRIAL_LIMIT_TOKENS,
            "remaining": max(0, TRIAL_LIMIT_TOKENS - used),
            "percent": round(used * 100.0 / TRIAL_LIMIT_TOKENS, 1),
            "active": is_trial,
            "model": db.get_setting("trial_model", "") or TRIAL_DEFAULT_MODEL,
            "models": list(TRIAL_MODELS)}


class TrialActivateReq(BaseModel):
    key: str


@app.post("/api/trial/activate")
def api_trial_activate(req: TrialActivateReq):
    """v2.20 试用密匙激活：校验指纹 →（换新密匙时）重置本机额度 → 进入试用模式。
    不再读写 api_base_url/api_key/筛选/编译模型（试用与自定义 API 完全独立，
    所有任务统一走试用模型）。"""
    from app.trial_config import TRIAL_DEFAULT_MODEL, is_valid_key, key_hash
    k = (req.key or "").strip()
    if not k:
        return {"ok": False, "message": "请先粘贴管理员分发的试用密匙"}
    if not is_valid_key(k):
        db.log("system", "试用密匙激活失败：密匙无效", "warn")
        return {"ok": False,
                "message": "密匙无效：请核对是否为管理员分发的 FB-XXXX-XXXX-XXXX-XXXX 格式密匙（不含多余空格）"}
    h = key_hash(k)
    fresh = db.get_setting("trial_active_hash", "") != h
    if fresh:
        db.set_setting("trial_used_in", "0")
        db.set_setting("trial_used_out", "0")
        db.set_setting("trial_active_hash", h)
        db.log("system", "试用密匙已激活：进入试用模式，额度重置为 100 万 tokens"
                         "（自定义 API 配置保持原样不受影响）")
        model = db.get_setting("trial_model", "") or TRIAL_DEFAULT_MODEL
        msg = ("✅ 试用已激活：可用额度 100 万 tokens（本机累计），全部任务统一使用模型 "
               + model + "；你的自定义 API 配置保持不变")
    else:
        db.log("system", "试用密匙重新激活（同一把，额度沿用本机累计）")
        msg = "✅ 试用已重新激活（同一把密匙）：额度沿用本机累计"
    return {"ok": True, "message": msg}


@app.post("/api/trial/deactivate")
def api_trial_deactivate():
    """v2.20 停用试用：清除激活标记，回到自定义 API 模式（配置原样保留）。"""
    if not db.get_setting("trial_active_hash", ""):
        return {"ok": False, "message": "当前未在试用模式"}
    with db.get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key='trial_active_hash'")
    db.log("system", "已停用试用额度，切换回自定义 API 模式")
    return {"ok": True, "message": "✅ 已停用试用，AI 调用改用你的自定义 API 配置"
                                   "（试用密匙可随时重新激活，额度沿用）"}


class TrialModelReq(BaseModel):
    model: str


@app.post("/api/trial/model")
def api_trial_model(req: TrialModelReq):
    """v2.20 试用模型切换：trial_model 记住选择；试用模式下所有任务统一使用该模型。"""
    from app.trial_config import TRIAL_MODELS
    m = (req.model or "").strip()
    if m not in TRIAL_MODELS:
        return {"ok": False, "message": "不支持的试用模型：" + m}
    db.set_setting("trial_model", m)
    if db.get_setting("trial_active_hash", ""):
        db.log("system", "试用模型已切换为 " + m + "（筛选/编译/翻译统一生效）")
        return {"ok": True, "message": "✅ 试用模型已切换为 " + m
                                       + "（筛选/编译/翻译统一生效）"}
    return {"ok": True, "message": "✅ 试用模型已记住为 " + m + "（下次激活试用时生效）"}


@app.post("/api/custom_api/clear")
def api_custom_api_clear():
    """v2.20 一键清除自定义 API 配置（地址/密钥/筛选/编译/翻译模型）。
    不影响试用模式（试用走独立通道）。"""
    with db.get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key IN "
                     "('api_base_url','api_key','model_screen','model_compile','model_translate')")
    db.sync_settings_file()
    db.log("system", "已一键清除自定义 API 配置（地址/密钥/模型），可在设置重新填写")
    return {"ok": True, "message": "✅ 已清除自定义 API 配置（API 地址、密钥与各模型名）"}


class SummarizeReq(BaseModel):
    item_id: int


def _job_summarize(state, item_id):
    from app.ai.summarize import summarize_item
    return summarize_item(item_id, progress_cb=lambda m: state.update(m),
                          should_cancel=lambda: state.cancelled)


class SearchClueReq(BaseModel):
    keywords: list          # 机构名 / 关键词列表
    engine: str = "auto"    # auto / ddg / bing / baidu


def _job_search(state, req):
    from app.ai_search import search_clues

    def cb(m):
        state.update(m)

    return search_clues(req["keywords"], req.get("engine", "auto"),
                        progress_cb=cb, should_cancel=lambda: state.cancelled)


@app.post("/api/search_clue")
def api_search_clue(req: SearchClueReq):
    if not req.keywords:
        return {"started": False, "message": "请填写机构 / 关键词"}
    ok, msg = run_in_background(
        "search", _job_search,
        {"keywords": req.keywords, "engine": req.engine},
        label="/".join(req.keywords)[:40])
    return {"started": ok, "message": msg}


@app.post("/api/summarize")
def api_summarize(req: SummarizeReq):
    ok, msg = run_in_background("summarize", _job_summarize, req.item_id)
    return {"started": ok, "message": msg}


@app.get("/api/items")
def api_items(days: int = 7, matched: Optional[str] = None, q: str = "",
              offset: int = 0, limit: int = 2000):
    """v2.6：days 按时间窗过滤（0=全部；1=今日；N=最近N天；无日期条目视为"待核"一并显示），
    q 同时检索原标题 / 中文译名 / 机构名，支持 offset 分页。"""
    m = None
    if matched == "1":
        m = True
    elif matched == "0":
        m = False
    items = db.list_items(days=days, only_window=False, matched=m,
                          limit=int(limit), offset=int(offset),
                          q=q)   # v2.13.6 q 下推 SQL（避免分页后过滤截断）
    # 精简字段
    favs = db.favorite_ids("item")
    slim = []
    for it in items:
        slim.append({
            "id": it["id"], "title": it["title"], "title_zh": it.get("title_zh") or "",
            "url": it["url"],
            "published_at": it["published_at"], "fetched_at": it["fetched_at"],
            "summary": (it.get("summary") or "")[:200],
            "ai_summary": (it.get("ai_summary") or "")[:400],
            "source": it["source_name_cn"], "source_key": it["source_name_en"],
            "is_match": it.get("is_match"), "criteria": _json(it.get("sc_criteria")),
            "match_points": it.get("match_points"),
            "score": it.get("score"), "read": bool(it.get("read")),
            "fav": it["id"] in favs,
        })
    return {"items": slim, "total": len(slim)}


@app.get("/api/archive")
def api_archive(q: str = "", matched: Optional[str] = None,
                month: str = "", page: int = 1, per_page: int = 100):
    """线索数据库（v2.4）：全量历史线索分页浏览。month=YYYY-MM。"""
    m = None
    if matched == "1":
        m = True
    elif matched == "0":
        m = False
    favs = db.favorite_ids("item")
    rows, total, months = db.list_archive(q=q, matched=m, month=month,
                                          page=page, per_page=per_page)
    slim = []
    for it in rows:
        slim.append({
            "id": it["id"], "title": it["title"], "title_zh": it.get("title_zh") or "",
            "url": it["url"], "published_at": it["published_at"],
            "fetched_at": it["fetched_at"],
            "source": it["source_name_cn"],
            "is_match": it.get("is_match"), "criteria": _json(it.get("sc_criteria")),
            "score": it.get("score"), "read": bool(it.get("read")),
            "ai_summary": (it.get("ai_summary") or "")[:400],
            "fav": it["id"] in favs,
        })
    return {"items": slim, "total": total, "page": page,
            "per_page": per_page, "months": months}


def _json(v):
    import json as _j
    if v is None or v == "":
        return []
    try:
        return _j.loads(v)
    except (ValueError, TypeError):
        return []


@app.get("/api/item/{item_id}")
def api_item(item_id: int):
    it = db.get_item(item_id)
    if not it:
        return JSONResponse({"error": "not found"}, status_code=404)
    return it


@app.get("/api/sources")
def api_sources():
    return {"sources": db.list_sources()}


class SourceToggle(BaseModel):
    enabled: bool


@app.post("/api/sources/{key}/toggle")
def api_source_toggle(key: str, req: SourceToggle):
    src = db.get_source(key)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    if key == "ai_search" and req.enabled:
        return {"ok": False, "message": "「AI 搜索引擎抓取」是虚拟归集信源，不能启用采集"}
    enabled = 1 if req.enabled else 0
    # v2.7：启用即加入常用（用户心智：打开开关=参与采集），停用仅停用
    if enabled and not src["in_common"]:
        db.update_source_flags(key, enabled=1, in_common=1)
        db.log("sources", f"[{src['name_cn']}] 已启用并自动加入常用信源")
        return {"ok": True, "joined_common": True}
    db.update_source_flags(key, enabled=enabled)
    return {"ok": True}


class SourceToggleAll(BaseModel):
    tab: str = "common"          # common（含参考+常用中的自定义）/ custom / bad
    enabled: bool


@app.post("/api/sources/toggle_all")
def api_sources_toggle_all(req: SourceToggleAll):
    """v2.12.1 批量启停（原前端逐源串行十几秒无反馈，被用户误认为『按钮与实际不同步』）。
    分组口径与前端页签一致：common=非 custom 源 + 已加入常用的 custom 源；custom=自定义组。"""
    srcs = db.list_sources()
    if req.tab == "custom":
        keys = [s["key"] for s in srcs if (s["src_group"] or "custom") == "custom"]
    elif req.tab == "bad":
        keys = [s["key"] for s in srcs
                if s.get("last_fetch_at") and s.get("last_fetch_ok") == 0]
    else:  # common
        keys = [s["key"] for s in srcs
                if (s["src_group"] or "custom") != "custom"
                or s.get("in_common") in (1, True)]
    url_key = [k for k in keys if k == "ai_search" and req.enabled]
    changed = 0
    for k in keys:
        if k in url_key:
            continue
        db.update_source_flags(k, enabled=1 if req.enabled else 0)
        changed += 1
    db.log("sources", f"批量{'启用' if req.enabled else '停用'} {changed} 个信源（页签 {req.tab}）")
    return {"ok": True, "changed": changed}


class SourceRename(BaseModel):
    name: str


@app.post("/api/sources/{key}/rename")
def api_source_rename(key: str, req: SourceRename):
    """v2.12.1 行内/自动命名共用改名。"""
    from app import rsshub as rh
    ok = db.rename_source(key, req.name)
    if ok:
        db.log("sources", f"[{key}] 名称已改为《{req.name.strip()}》")
    return {"ok": bool(ok)}


class SourceAiNameReq(BaseModel):
    key: str = ""


RSSHOST_ZH = {
    "bloomberg": "彭博社", "economist": "经济学人", "nytimes": "纽约时报",
    "reuters": "路透社", "cnbc": "CNBC", "wsj": "华尔街日报", "ft": "金融时报",
    "ftchinese": "FT中文网", "theguardian": "卫报", "guardian": "卫报",
    "yahoo": "雅虎", "msn": "MSN", "finance": "财经", "marketwatch": "MarketWatch",
    "apnews": "美联社", "dwnews": "德国之声", "dw": "DW", "treasury": "美财政部",
    "imf": "IMF", "worldbank": "世界银行", "ebc": "欧央行", "ecb": "欧央行",
    "bis": "国际清算银行", "sec": "美SEC", "fdic": "FDIC", "fsb": "FSB",
    "whitehouse": "白宫", "goldman": "高盛", "gs": "高盛", "caixin": "财新网",
    "wallstreetcn": "华尔街见闻", "cls": "财联社", "yicai": "第一财经",
    "eastmoney": "东方财富", "jin10": "金十数据", "nikkei": "日经", "huxiu": "虎嗅",
}


RSSSEG_ZH = {
    "business": "商业", "news": "新闻", "rss": "RSS", "editorial": "社论",
    "china": "中国", "espresso": "晨报", "latest": "最新", "authors": "作者专栏",
    "press-releases": "新闻稿", "pressrelease": "新闻稿", "country-reports": "国别报告",
    "markets": "市场", "opinion": "观点", "world": "国际",
    "technology": "科技", "economy": "经济", "finance": "财经", "top": "头条",
    "economy_china": "中国经济", "my-news": "我的新闻", "investigates": "调查",
    "daily-briefing-chinese": "中文简报", "global-business-review": "全球商业评论",
    "banking": "银行业", "bank": "银行", "channel": "频道",
    "columns": "专栏", "daily": "日报", "media": "媒体", "schedules": "日程",
    "country": "国家", "data": "数据", "investor": "投资者",
    "author": "作者", "articles": "文章", "front-page": "首页要闻",
}


def _fallback_name(rss_url: str) -> str:
    from urllib.parse import urlparse, unquote
    try:
        parts = [unquote(x) for x in urlparse(rss_url).path.split("/") if x and x.strip(":p?")]
    except ValueError:
        parts = [rss_url.split("/")[-1] or rss_url]
    host_map_key = (urlparse(rss_url).path or "").strip("/").split("/")[0].lower()
    r = None
    for k in (host_map_key, host_map_key.split("-")[0]):
        if k in RSSHOST_ZH:
            r = RSSHOST_ZH[k]
            break
    if not r:
        r = host_map_key[:20] or "RSSHub"
    segs = []
    for seg in parts[1:]:
        t = seg.replace("?", "").split("-")[0] or seg
        z = RSSSEG_ZH.get(seg) or RSSSEG_ZH.get(t) or RSSSEG_ZH.get(seg.split("-")[0])
        segs.append(z or seg)
    return (r + ("-" + "-".join(segs) if segs else ""))[:46]


class SourceCommon(BaseModel):
    in_common: bool
    category: str = ""   # v2.21 加入精选时可指定分类（拼接 SemanticClick 语义与分组展示）


@app.post("/api/sources/{key}/common")
def api_source_common(key: str, req: SourceCommon):
    """加入/移出精选信源。加入时自动启用，可选指定分类（v2.21 分组展示）。"""
    src = db.get_source(key)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    if req.in_common:
        fields = {"in_common": 1, "enabled": 1}
        cat = (req.category or "").strip()
        if cat:
            fields["category"] = cat[:20]
        db.update_source_flags(key, **fields)
        db.log("sources", f"[{src['name_cn']}] 已加入精选信源"
                          + (f"（分类：{cat}）" if cat else ""))
    else:
        db.update_source_flags(key, in_common=0, enabled=0)
        db.log("sources", f"[{src['name_cn']}] 已移出精选信源")
    return {"ok": True}


class SourceCategory(BaseModel):
    category: str


@app.post("/api/sources/{key}/category")
def api_source_category(key: str, req: SourceCategory):
    """v2.21 修改精选信源分类（分组展示/检索均可按分类）。"""
    cat = (req.category or "").strip()
    if not cat:
        return JSONResponse({"error": "分类为空"}, status_code=400)
    src = db.get_source(key)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.update_source_flags(key, category=cat[:20])
    db.log("sources", f"[{src['name_cn']}] 分类改为：{cat}")
    return {"ok": True}


class SourcePin(BaseModel):
    pinned: bool


@app.post("/api/sources/{key}/pin")
def api_source_pin(key: str, req: SourcePin):
    src = db.get_source(key)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.set_source_pinned(key, req.pinned)
    return {"ok": True}


@app.delete("/api/sources/{key}")
def api_source_delete(key: str):
    """v2.6 永久删除信源：从数据库移除，不再出现在任何分组/异常页；
    预置信源记录到 excluded_preset_keys，重启装载时跳过。"""
    src = db.get_source(key)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    db.delete_source(key)
    excluded = {k.strip() for k in (db.get_setting("excluded_preset_keys") or "").split(",") if k.strip()}
    # 预置信源的 key 手动加白：无法直接知道来源，用 yaml 键集合
    import yaml as _y
    from app.paths import SOURCES_YAML
    try:
        with open(SOURCES_YAML, "r", encoding="utf-8") as f:
            data = _y.safe_load(f) or {}
        preset_keys = {s["key"] for s in (data.get("sources") or [])}
    except Exception:
        preset_keys = set()
    if src["key"] in preset_keys:
        excluded.add(src["key"])
        db.set_setting("excluded_preset_keys", ",".join(sorted(excluded)))
    db.log("sources", f"信源[{src['name_cn']}] 已永久删除"
                      + ("（预置信源，重启不再装载）" if src["key"] in preset_keys else ""))
    return {"ok": True}


@app.post("/api/sources/{key}/test")
def api_source_test(key: str):
    """v2.5 单个信源连通性测试：同步抓一次并更新连通状态。"""
    src = db.get_source(key)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    # v2.7 虚拟信源（AI 搜索抓取归集）不参与连通测试
    if key == "ai_search" or (src["mode"] == "rss" and not (src["config"] or {}).get("rss_url")):
        return {"ok": False, "virtual": True, "items": 0, "seconds": 0,
                "error": "虚拟信源（搜索引擎抓取归集），无需连通测试"}
    r = runner.probe_site(src)
    db.update_source_fetch_status(key, r["ok"], r.get("items", 0),
                                  r.get("error") or "")
    db.log("sources", f"[{src['name_cn']}] 连通性测试："
                      f"{'正常' if r['ok'] else '异常'}，{(r.get('items', 0))} 条"
                      + (f"，{r.get('seconds')}s" if r["ok"] else f"，{(r.get('error') or '')[:80]}"))
    return {"ok": r["ok"], "items": r.get("items", 0),
            "seconds": r.get("seconds"), "error": r.get("error"),
            "sample": r.get("sample") or []}


@app.post("/api/sources/reload")
def api_sources_reload():
    n = runner.load_preset_sources()
    return {"ok": True, "count": n}


class RsshubTestReq(BaseModel):
    path: str = ""
    base: str = ""


@app.get("/api/rsshub/book")
def api_rsshub_book():
    from app import rsshub as rh
    return {"base": rh.get_base(), "routes": rh.ROUTE_BOOK}


class RsshubBaseReq(BaseModel):
    base: str = ""


@app.post("/api/rsshub/base")
def api_rsshub_base(req: RsshubBaseReq):
    from app import rsshub as rh
    ok = rh.set_base(req.base)
    return {"ok": bool(ok), "base": rh.get_base()}


class RsshubAddReq(BaseModel):
    path: str = ""
    name_cn: str = ""
    base: str = ""


@app.post("/api/rsshub/test")
def api_rsshub_test(req: RsshubTestReq):
    from app import rsshub as rh
    r = rh.probe_path(req.path, req.base)
    if r.get("ok"):
        db.log("rsshub", f"[RSSHub] 试连成功 {r.get('items')} 条/{r.get('seconds')}s：{r.get('url')}")
    else:
        db.log("rsshub", f"[RSSHub] 试连失败：{str(r.get('error') or '')[:120]}（{r.get('url')}）", "warn")
    return r


@app.post("/api/rsshub/add")
def api_rsshub_add(req: RsshubAddReq):
    from app import rsshub as rh
    out = rh.add_source(req.path, req.name_cn, req.base)
    if out.get("ok"):
        db.log("rsshub", f"[RSSHub] 已添加自定义信源《{req.name_cn}》{out.get('key')}")
    return out


class RsshubAddUrlReq(BaseModel):
    url: str = ""
    name_cn: str = ""




class RssDirectReq(BaseModel):
    url: str


@app.post("/api/sources/add_rss")
def api_sources_add_rss(req: RssDirectReq):
    """v2.21 直连 RSS 订阅源一键添加：拉取实测（feedparser 解析成功且有条目才入库）。
    与 RSSHub 路由无关的普通订阅源（如 WordPress 站点 /feed/）用此通道。"""
    import feedparser as _fp
    import httpx as _h
    url = (req.url or "").strip()
    if not url.startswith("http"):
        return {"ok": False, "error": "请粘贴完整的 RSS 订阅地址（https://…/feed 等）"}
    ssett = db.all_settings()
    proxy = None
    if ssett.get("proxy_enabled") == "1" and ssett.get("proxy_url"):
        proxy = ssett["proxy_url"]
    try:
        with _h.Client(timeout=15, verify=False, follow_redirects=True,
                       proxy=proxy if proxy else None,
                       headers={"User-Agent": "Mozilla/5.0 (compatible; FinBrief/1.0)"}) as cli:
            resp = cli.get(url)
            resp.raise_for_status()
        raw = resp.content
    except Exception as e:
        return {"ok": False, "error": f"下载失败：{type(e).__name__}: {str(e)[:110]}"}
    head_l = raw[:600].lstrip().lower()
    if not (head_l.startswith(b"<?xml") or b"<feed" in head_l or b"<rss" in head_l):
        return {"ok": False, "error": "该地址返回的不是 RSS/Atom 订阅内容（可先在浏览器确认）"}
    fp = _fp.parse(raw)
    if fp.bozo and not fp.entries:
        return {"ok": False, "error": f"解析失败：{getattr(fp, 'bozo_exception', '未知')}"}
    if not fp.entries:
        return {"ok": False, "error": "订阅可访问但暂无条目（可在添加后观察更新）"}
    title = ""
    if getattr(fp, "feed", None):
        title = (getattr(fp.feed, "title", "") or "")
    title = title.strip() or url.split("//", 1)[-1][:40]
    sample = [{"title": (getattr(e, "title", "") or "")[:60]} for e in fp.entries[:5]]
    key = "rss_" + url.split("://", 1)[-1]
    key = key.split("?")[0].rstrip("/")
    key = "".join(ch if (ch.isalnum() or ch in "_.-") else "_" for ch in key).strip("_")[:52]
    if not key or len(key) < 6:
        key = "rss_custom_" + key
    src = {"key": key,
           "name_cn": title[:32],
           "name_en": "(RSS)",
           "category": "media",
           "mode": "rss",
           "config": {"rss_url": url, "direct": 1},
           "enabled": 0,
           "note": "RSS 订阅直连",
           "src_group": "custom",
           "in_common": 0}
    db.upsert_source(src)
    db.log("sources", f"直连 RSS 添加信源《{title[:32]}》：{url[:90]}")
    return {"ok": True, "key": key, "name_cn": title[:32], "items": len(fp.entries),
            "sample": sample}


@app.post("/api/rsshub/add_url")
def api_rsshub_add_url(req: RsshubAddUrlReq):
    """粘贴订阅链接一键测试添加：并行试连多实例，成功才入库（默认停用）。"""
    from app import rsshub as rh
    r = rh.add_feed_url(req.url, req.name_cn)
    if r.get("ok"):
        # v2.12.1 添加即自动中文名（词典必成；AI 可用时前端随后用 ai_name 覆盖更贴切）
        zh = _fallback_name(r.get("url") or req.url)
        db.rename_source(r.get("key"), zh)
        r["name_cn"] = zh
        db.log("rsshub", f"[RSSHub] 粘贴添加成功 {r.get('items')} 条/{r.get('seconds')}s："
                         f"《{zh}》{r.get('key')}（实例 {r.get('base_used')}）")
    else:
        db.log("rsshub", f"[RSSHub] 粘贴添加失败：{str(r.get('error') or '')[:120]}（{req.url}）", "warn")
    return r


class RsshubAiNameReq(BaseModel):
    key: str = ""


@app.post("/api/rsshub/ai_name")
def api_rsshub_ai_name(req: RsshubAiNameReq):
    """v2.12.1 自动中文名：先用词典兜底（零成本必成），AI 可用时再覆盖一次。"""
    from app import rsshub as rh
    src = db.get_source(req.key)
    if not src:
        return {"ok": False, "message": "信源不存在"}
    rss_url = (src.get("config") or {}).get("rss_url") or ""
    zh = _fallback_name(rss_url)
    db.rename_source(req.key, zh)
    final_name = zh
    ai_used = False
    s = db.all_settings()
    # v2.20 试用模式（已激活密匙）下 AI 命名直接可用；自定义模式需地址+密钥
    if db.get_setting("trial_active_hash", "") or \
            (db.get_secret("api_key") and s.get("api_base_url")):
        try:
            from app.ai.client import chat
            prompt = ("把下面这行 RSS 订阅的英文站点名/栏目路径翻译成简体中文媒体名，"
                      "格式：站点名-栏目。站点名用国内通用媒体译名（如 Bloomberg→彭博社），"
                      "无对应译名的保留英文。只输出结果本身。\n"
                      + rss_url.split("//")[-1])
            ai = chat(prompt, model=s.get("model_translate") or s.get("model_screen") or "deepseek-v4.1-flash",
                      system="你只输出翻译结果本身，不要任何解释。", max_tokens=64)
            if ai and ai.strip():
                ai = ai.strip().strip('"“”。 ～')[:40]
                if ai:
                    db.rename_source(req.key, ai)
                    final_name = ai
                    ai_used = True
        except Exception:
            pass
    return {"ok": True, "name": final_name, "ai_used": ai_used}


class RsshubRestartReq(BaseModel):
    action: str = ""


@app.post("/api/rsshub/restart")
def api_rsshub_restart(req: RsshubRestartReq):
    """v2.12.1 重启内置 RSSHub 本地服务（用户点「⟳ 重启服务」）。"""
    import main
    st = main.rsshub_service_status()
    if not st.get("bundled"):
        return {"ok": False, "message": "未捆绑内置 RSSHub"}
    try:
        if st.get("running_child"):
            import app.rsshub as _rh
            main._RSSHUB_PROC.kill()
            main._RSSHUB_PROC = None
            import time as _t
            _t.sleep(1.2)
    except OSError:
        pass
    main.start_rsshub_async()
    db.log("rsshub", "[RSSHub] 内置服务已手动重启")
    return {"ok": True, "message": "正在重启内置 RSSHub…（约 6-12 秒后自动就绪）"}


@app.post("/api/rsshub/check_update")
def api_rsshub_check_update():
    """v2.12.1 检查 RSSHub 官方上游版本：对比本地构建日期。更新随程序新版分发。"""
    import main
    st = main.rsshub_service_status()
    if not st.get("bundled"):
        return {"ok": False, "message": "未捆绑内置 RSSHub"}
    ver_path = os.path.join(st["dir"], "_ver.json")
    local = {}
    try:
        with open(ver_path, encoding="utf-8") as f:
            local = json.load(f) or {}
    except (OSError, ValueError):
        local = {}
    latest = {}
    try:
        import httpx as _h
        r = _h.get("https://api.github.com/repos/DIYgod/RSSHub/commits/master",
                   timeout=8, headers={"User-Agent": "finbrief"})
        if r.status_code == 200:
            j = r.json()
            latest = {"sha": (j.get("sha") or "")[:12],
                      "date": (j.get("commit", {}).get("committer", {}).get("date") or "")[:10]}
    except (httpx.HTTPError, ValueError):
        latest = {}
    return {"ok": True, "local": local, "latest": latest, "bundled": True}


def _job_rsshub_upgrade(state):
    """v2.21 RSSHub 整包升级：从仓库 Releases 下载 rsshub_bundle_*.zip 运行包，
    整目录替换本机内置 rsshub 并重启服务（失败自动回滚）。

    背景：npm dist 通道已失效（新版官方包不再附带可运行 dist），而用户的真实
    需求是“上游网站格式变化后，本地路由规则也要随之更新”——整包换新即得到
    最新路由集合；单条路由临时失效则用「路由规则体检」迁移到可用实例兜底。"""
    import httpx as _h
    import time as _time
    import json as _js
    import os as _os
    import shutil as _sh
    import tempfile as _tmpd
    import zipfile as _zf
    import main as _main
    st = _main.rsshub_service_status()
    if not st.get("bundled"):
        db.log("rsshub", "[RSSHub] 整包升级失败：未捆绑内置 RSSHub", "warn")
        return {"ok": False, "reason": "未捆绑内置 RSSHub"}
    rsshub_dir = st["dir"]

    ssett = db.all_settings()
    proxy = None
    if ssett.get("proxy_enabled") == "1" and ssett.get("proxy_url"):
        proxy = ssett["proxy_url"]
    client_kw = {"timeout": 30, "verify": False, "follow_redirects": True,
                 "headers": {"User-Agent": "FinBrief"}}
    if proxy:
        client_kw["proxy"] = proxy

    state.update("查询仓库最新 Release 及 rsshub_bundle 资产…")
    asset = None
    try:
        with _h.Client(**client_kw) as cli:
            r = cli.get("https://api.github.com/repos/jhtfly/FinBrief/releases/latest")
            if r.status_code == 200:
                rel = r.json()
                for a in rel.get("assets") or []:
                    nm = (a.get("name") or "").lower()
                    if nm.startswith("rsshub_bundle") and nm.endswith(".zip"):
                        asset = a
                        break
    except Exception as e:
        return {"ok": False,
                "reason": f"查询 Releases 失败（网络/代理原因）：{type(e).__name__}: {str(e)[:90]}"}
    if asset is None:
        msg = ("当前 Releases 尚未提供 rsshub_bundle_*.zip 整包升级资产"
               "（整包升级资产将随下次发版提供）。个别路由暂时失效时，"
               "可先用「路由规则体检」把该源迁移到可用实例。")
        db.log("rsshub", "[RSSHub] 整包升级中止：" + msg, "warn")
        return {"ok": False, "reason": msg}

    name = asset.get("name")
    size = int(asset.get("size") or 0)
    url = asset.get("browser_download_url") or ""
    if size < 50 * 1024 * 1024:
        return {"ok": False, "reason": f"资产 {name} 体积异常（{size} 字节），中止"}
    state.update(f"下载整包 {name}（约 {size // 1048576}MB，可能需数分钟）…")
    tmp_zip = _os.path.join(_tmpd.gettempdir(), "finbrief_rsshub_upgrade.zip")
    try:
        with _h.Client(**{**client_kw, "timeout": 1800}) as cli:
            with cli.stream("GET", url) as resp:
                resp.raise_for_status()
                got = 0
                with open(tmp_zip, "wb") as f:
                    for chunk in resp.iter_bytes(1024 * 256):
                        f.write(chunk)
                        got += len(chunk)
                        if got % (8 * 1024 * 1024) < 262144:
                            state.update(f"下载中 {got // 1048576}/{size // 1048576} MB…")
    except Exception as e:
        try:
            _os.remove(tmp_zip)
        except OSError:
            pass
        return {"ok": False, "reason": f"整包下载失败：{type(e).__name__}: {str(e)[:110]}"}
    if _os.path.getsize(tmp_zip) != size:
        try:
            _os.remove(tmp_zip)
        except OSError:
            pass
        return {"ok": False, "reason": f"下载不完整（{name}），已中止（未做改动）"}

    state.update("校验并解包整包…")
    tmp_dir = _tmpd.mkdtemp(prefix="finbrief_bundle_")
    try:
        with _zf.ZipFile(tmp_zip) as zf:
            for m in zf.namelist():
                norm = _os.path.normpath(m).replace(os.sep, "/")
                if norm.startswith("..") or norm.startswith("/"):
                    raise ValueError("安装包路径异常")
            zf.extractall(tmp_dir)
    except Exception as e:
        _sh.rmtree(tmp_dir, ignore_errors=True)
        try:
            _os.remove(tmp_zip)
        except OSError:
            pass
        return {"ok": False, "reason": f"解包失败：{e}"}
    # 定位包内 rsshub 根（同时具备 node.exe 与 dist/index.mjs 的目录）
    new_root = None
    for dp, dns, fns in _os.walk(tmp_dir):
        if "node.exe" in fns and _os.path.isfile(_os.path.join(dp, "dist", "index.mjs")):
            new_root = dp
            break
    if not new_root:
        _sh.rmtree(tmp_dir, ignore_errors=True)
        try:
            _os.remove(tmp_zip)
        except OSError:
            pass
        return {"ok": False, "reason": "整包内未找到可运行 RSSHub（node.exe + dist/index.mjs），中止"}
    state.update("停用内置服务并替换整包…")
    try:
        if _main._RSSHUB_PROC:
            _main._RSSHUB_PROC.kill()
            _main._RSSHUB_PROC = None
    except (OSError, AttributeError):
        pass
    bak = rsshub_dir + ".bak"
    if _os.path.exists(bak):
        _sh.rmtree(bak, ignore_errors=True)
    _os.rename(rsshub_dir, bak)
    try:
        _sh.copytree(new_root, rsshub_dir)
    except Exception as e:
        if _os.path.exists(rsshub_dir):
            _sh.rmtree(rsshub_dir, ignore_errors=True)
        _os.rename(bak, rsshub_dir)
        return {"ok": False, "reason": f"替换整包失败（已还原旧版）：{e}"}
    try:
        with open(_os.path.join(rsshub_dir, "_ver.json"), "w", encoding="utf-8") as f:
            _js.dump({"version": name, "source": "release_bundle",
                      "date": db.now_iso()[:10]}, f)
    except OSError:
        pass
    state.update("整包已替换，重启内置服务验证中（失败自动回滚）…")
    _main.start_rsshub_async()
    alive = False
    for i in range(30):
        _time.sleep(3)
        try:
            if _main.rsshub_service_status().get("alive"):
                alive = True
                break
        except Exception:
            pass
        state.update(f"内置 RSSHub 重启中…（{i + 1}/30）")
    if not alive:
        db.log("rsshub", "[RSSHub] 整包升级后启动失败，自动回滚旧版")
        try:
            if _os.path.exists(rsshub_dir):
                _sh.rmtree(rsshub_dir, ignore_errors=True)
            _os.rename(bak, rsshub_dir)
            _main.start_rsshub_async()
        except Exception:
            pass
        return {"ok": False, "reason": "新版启动失败，已自动回滚旧版并重启"}
    _sh.rmtree(bak, ignore_errors=True)
    _sh.rmtree(tmp_dir, ignore_errors=True)
    try:
        _os.remove(tmp_zip)
    except OSError:
        pass
    db.log("rsshub", f"[RSSHub] 整包升级成功：{name}（Releases 运行包替换）")
    return {"ok": True, "version": name}


def _job_rsshub_heal(state):
    """v2.21 路由规则体检：逐源探测 RSSHub 路由，把失效源迁移到可用实例。"""
    from app.rsshub import route_heal
    return route_heal(progress_cb=lambda i, n, cur: state.update(
        f"体检 RSSHub 信源（{i}/{n}）：{cur}"),
        should_cancel=state.check)

@app.post("/api/rsshub/upgrade")
def api_rsshub_upgrade():
    """v2.13.4 一键自动下载并升级内置 RSSHub（后台任务，失败自动回滚）。"""
    ok, msg = run_in_background("rsshub_upgrade", _job_rsshub_upgrade)
    return {"started": ok, "message": msg}


@app.post("/api/rsshub/heal")
def api_rsshub_heal():
    """v2.21 路由规则体检：探测全部 RSSHub 信源，失效路由自动迁移可用实例。"""
    ok, msg = run_in_background("rsshub_heal", _job_rsshub_heal)
    return {"started": ok, "message": msg}


@app.get("/api/rsshub/status")
def api_rsshub_status():
    """v2.12.0 内置 RSSHub 本地服务状态（前端路由库卡显示 + 实例下拉联动）。"""
    import main
    return main.rsshub_service_status()


@app.get("/api/reports")
def api_reports():
    reports = db.list_reports()
    for r in reports:
        r["word_count"] = sum(
            len(b.get("text", "")) for s in (r.get("body") or [])
            for b in (s.get("blocks") or []))
    return {"reports": reports}


@app.get("/api/has_report/{item_id}")
def api_has_report(item_id: int):
    """v2.13.9 再次编译确认兜底：REPORTS 快照可能未载入（收藏夹等场景），
    前端用该端点查该线索是否已有专报草稿（避免直接覆盖旧稿且不告知）。"""
    r = db.get_report_by_item(item_id)
    if not r:
        return {"has": False}
    return {"has": True, "report_id": r["id"], "status": r.get("status") or "",
            "title": r.get("title") or "", "exported": bool(r.get("exported_at"))}


@app.get("/api/report/{report_id}")
def api_report(report_id: int):
    r = db.get_report(report_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    r["word_count"] = sum(
        len(b.get("text", "")) for s in (r.get("body") or [])
        for b in (s.get("blocks") or []))
    return r


class ReportUpdate(BaseModel):
    title: Optional[str] = None
    editor_note: Optional[str] = None
    body: Optional[list] = None
    conclusion: Optional[str] = None
    status: Optional[str] = None


@app.put("/api/report/{report_id}")
def api_report_update(report_id: int, req: ReportUpdate):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if not fields:
        return {"ok": False, "message": "无更新字段"}
    db.update_report(report_id, fields)
    return {"ok": True}


class ReportsDeleteReq(BaseModel):
    ids: list = []


@app.post("/api/reports/delete")
def api_reports_delete(req: ReportsDeleteReq):
    """v2.9.6 批量删除专报（行右键单删走 DELETE /api/report/{id}）。"""
    n = db.delete_reports([int(i) for i in req.ids if str(i).isdigit()])
    return {"ok": True, "removed": n}


@app.delete("/api/report/{report_id}")
def api_report_delete(report_id: int):
    n = db.delete_reports([report_id])
    return {"ok": n > 0, "removed": n}


@app.post("/api/report/{report_id}/export")
def api_report_export(report_id: int):
    try:
        r = export_report(report_id)
        return r
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class BatchExport(BaseModel):
    report_ids: list


@app.post("/api/reports/export")
def api_reports_export(req: BatchExport):
    results = []
    for rid in req.report_ids:
        try:
            r = export_report(rid)
            results.append({"report_id": rid, **{k: v for k, v in r.items()
                                                 if k != "problems"},
                            "problems": r.get("problems")})
        except Exception as e:
            results.append({"report_id": rid, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"results": results, "ok": ok_n, "total": len(results)}


class MultiCompileReq(BaseModel):
    item_ids: list


def _job_compile_multi(state, item_ids):
    """多线索综合专报编译（ai 组任务，支持打断；放弃编译在结果内含 reason）。"""
    from app.ai.multi import compile_multi_items
    try:
        return compile_multi_items(
            item_ids,
            progress_cb=lambda msg: state.update(msg),
            should_cancel=lambda: state.cancelled)
    except Exception as e:
        state.update(f"综合编译失败: {type(e).__name__}: {e}")
        raise


_CUSTOM_EXT_OK = {".docx", ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}


_CUSTOM_MAX_BYTES = 25 * 1024 * 1024


class CustomUploadReq(BaseModel):
    name: str = ""
    data: str = ""          # 文件字节 base64（前端 FileReader 读出）


import base64 as _b64


import uuid as _uuid


import html as _htmlmod


def _custom_index() -> dict:
    import os as _os
    d = os.path.join(os.path.dirname(db.DB_PATH), "custom_uploads")
    _os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "_index.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _custom_index_save(idx: dict):
    import os as _os
    d = os.path.join(os.path.dirname(db.DB_PATH), "custom_uploads")
    p = os.path.join(d, "_index.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False)


@app.post("/api/custom/upload")
def api_custom_upload(req: CustomUploadReq):
    """上传自定义材料：保存文件→提取文本/判定是否扫描件→登记索引。"""
    import os as _os
    from app.ai import custom as _c
    name = (req.name or "").strip() or "材料"
    ext = _os.path.splitext(name)[1].lower()
    if ext not in _CUSTOM_EXT_OK:
        return {"ok": False, "message": "支持的格式：docx / pdf / png / jpg / jpeg / webp"}
    try:
        raw = _b64.b64decode(req.data or "")
    except (ValueError, TypeError):
        return {"ok": False, "message": "文件数据解析失败"}
    if len(raw) > _CUSTOM_MAX_BYTES:
        return {"ok": False, "message": "文件超过 25MB 上限"}
    if not raw:
        return {"ok": False, "message": "文件内容为空"}
    stem = _uuid.uuid4().hex[:12]
    d = os.path.join(os.path.dirname(db.DB_PATH), "custom_uploads")
    _os.makedirs(d, exist_ok=True)
    disk = os.path.join(d, stem + ext)
    with open(disk, "wb") as f:
        f.write(raw)
    meta = {"file_name": name, "disk": disk, "ext": ext,
            "created": db.now_iso()[:10], "size": len(raw)}
    try:
        m = _c.build_material(disk)
        meta["kind"] = m["kind"]
        meta["text_len"] = len((m.get("text") or "").strip())
        meta["pages"] = m.get("pages") or len(m.get("images") or [])
        meta["vision"] = bool(m.get("images"))
    except Exception as e:
        try:
            _os.remove(disk)
        except OSError:
            pass
        return {"ok": False, "message": f"文件无法读取：{type(e).__name__}: {str(e)[:90]}"}
    idx = _custom_index()
    up_id = "up" + _uuid.uuid4().hex[:10]
    idx[up_id] = meta
    _custom_index_save(idx)
    kind_cn = {"docx": "Word 文档", "pdf_text": "PDF（文字版）",
               "pdf_scan": "PDF（扫描件，将转图片识别）", "image": "图片"}.get(meta["kind"], "材料")
    return {"ok": True, "up_id": up_id, "name": name, "kind": meta["kind"],
            "kind_cn": kind_cn, "text_len": meta.get("text_len", 0),
            "pages": meta.get("pages", 0), "vision": meta.get("vision", False),
            "size": meta["size"]}


class CustomMaterialIn(BaseModel):
    type: str = "upload"       # upload / link / text（v2.18.0 粘贴文本）
    up_id: str = ""            # upload 材料
    url: str = ""              # link 材料
    text: str = ""             # text 材料：用户粘贴的全文
    title: str = ""            # 可选自定义材料标题


class CustomCompileReq(BaseModel):
    topic: str = ""
    note: str = ""
    materials: list = []


def _intake_materials(state, mats) -> list:
    """v2.13.3 材料 intake（自定义专报与弃编译补料共用）：链接抓正文 / 上传文件解译
    / v2.18.0 粘贴文本直读 → 合成线索登记；返回可编译材料列表（[{item_id,kind,kind_display,images?}]）。"""
    from app.fetcher import html_sites
    from app.ai import custom as _c
    idx = _custom_index()
    mats_real = []
    s = db.all_settings()
    proxy = s.get("proxy_url") if s.get("proxy_enabled") == "1" else None
    for k, m in enumerate(mats, 1):
        try:
            if m.get("type") == "text":
                txt = (m.get("text") or "").strip()
                if len(txt) < 30:
                    state.update(f"材料 {k}：粘贴文本过短（不足 30 字），已跳过")
                    continue
                state.update(f"读取材料 {k}（粘贴文本，{len(txt)} 字）…")
                title = ((m.get("title") or "").strip() or "粘贴文本材料")[:60]
                iid = db.insert_custom_item(
                    title=title, url="custom://text/",
                    summary=txt[:200], content=txt[:26000])
                if iid:
                    mats_real.append({"item_id": iid, "kind": "text",
                                      "kind_display": "粘贴文本"})
            elif m.get("type") == "link":
                url = (m.get("url") or "").strip()
                if not url.startswith("http"):
                    state.update(f"材料 {k}：链接格式无效，已跳过")
                    continue
                state.update(f"读取材料 {k}（网页链接）：{url[:60]}…")
                text, err = html_sites.fetch_page_text(url, proxy=proxy, max_chars=24000)
                if not text or len(text.strip()) < 60:
                    state.update(f"材料 {k}：链接抓取失败（{str(err)[:60] or '无可读正文'}），已跳过")
                    continue
                title = (m.get("title") or "").strip()
                if not title:
                    # 尝试从正文首行提取作为备选标题，避免取 url 末尾无意义哈希
                    first_line = text.strip().split("\n")[0].strip()[:60]
                    if len(first_line) >= 4 and not first_line.startswith("http"):
                        title = first_line
                    else:
                        title = url.rstrip("/").split("/")[-1][:46]
                iid = db.insert_custom_item(title=title, url=url,
                                            summary=text.strip()[:200],
                                            content=text.strip())
                if iid:
                    mats_real.append({"item_id": iid, "kind": "link",
                                      "kind_display": "网页链接"})
            else:
                up = idx.get(m.get("up_id") or "")
                if not up or not os.path.isfile(up.get("disk") or ""):
                    state.update(f"材料 {k}：上传记录已失效，已跳过")
                    continue
                state.update(f"读取材料 {k}（{up.get('file_name')}）…")
                mm = _c.build_material(up["disk"])
                base_title = (m.get("title") or "").strip() or                 (os.path.splitext(os.path.basename(up.get("file_name") or "材料"))[0][:42])
                iid = db.insert_custom_item(
                    title=base_title,
                    url="custom://upload/" + (m.get("up_id") or ""),
                    summary=(mm.get("text") or "").strip()[:200],
                    content=(mm.get("text") or "").strip())
                if iid:
                    mats_real.append({"item_id": iid,
                                      "kind": up.get("kind") or mm.get("kind"),
                                      "kind_display": up.get("kind_cn") or "",
                                      "images": mm.get("images") or []})
        except Exception as ex:
            state.update(f"材料 {k} 处理失败（已跳过）：{type(ex).__name__}: {str(ex)[:70]}")
    return mats_real


@app.post("/api/custom/compile")
def api_custom_compile(req: CustomCompileReq):
    """提交自定义专报编译任务（材料已选好：uploads + links）。"""
    mats = [(m if isinstance(m, dict) else m.dict()) for m in req.materials]
    if not mats:
        return {"started": False, "message": "请先添加至少一份材料（粘贴文本 / 链接 / 上传文件）"}
    if len(mats) > 6:
        return {"started": False, "message": "一次最多编译 6 份材料"}

    def _job_custom(state):
        try:
            mats_real = _intake_materials(state, mats)
            if not mats_real:
                return {"ok": False, "reason": "全部材料不可用（链接抓取失败或文件失效）"}
            from app.ai import custom as _c
            r = _c.compile_custom(mats_real, topic=req.topic, note=req.note,
                                  progress_cb=state.update,
                                  should_cancel=lambda: state.cancelled)
            if r.get("ok"):
                state.update(f"完成：{r.get('title', '')[:40]}（脚注 {r.get('footnotes')} 个，"
                             f"请在专报管理-综合专报查看）")
            return r
        except Exception as e:
            state.update(f"自定义编译失败: {type(e).__name__}: {str(e)[:120]}")
            raise
    ok, msg = run_in_background("custom_compile", _job_custom)
    return {"started": ok, "message": msg}


class SupplementReq(BaseModel):
    note: str = ""
    materials: list = []       # 与 CustomCompileReq.materials 同形（upload / link）


def _job_supplement(state, rid, mats, note):
    try:
        mats_real = _intake_materials(state, mats)
        if not mats_real:
            return {"ok": False, "reason": "全部补充材料不可用（链接抓取失败或文件失效）"}
        from app.ai.custom import compile_supplement
        r = compile_supplement(rid, mats_real, extra_note=note,
                               progress_cb=state.update,
                               should_cancel=lambda: state.cancelled)
        if r.get("ok"):
            state.update(f"完成：{r.get('title', '')[:40]}"
                         f"（已放回原线索的专报行，请在专报管理查看）")
        return r
    except Exception as e:
        state.update(f"补充材料重编失败: {type(e).__name__}: {str(e)[:120]}")
        raise


@app.post("/api/reports/{rid}/supplement")
def api_report_supplement(rid: int, req: SupplementReq):
    """v2.13.3 对「已放弃」专报行人工补充材料后重新编译（沿用原线索信息，落回原行）。"""
    row = db.get_report(rid)
    if not row:
        return {"started": False, "message": "报告不存在"}
    mats = [(m if isinstance(m, dict) else m.dict()) for m in req.materials]
    if not mats:
        return {"started": False, "message": "请先添加至少 1 份补充材料（粘贴文本 / 链接 / 上传文件）"}
    if len(mats) > 6:
        return {"started": False, "message": "一次最多补充 6 份材料"}
    ok, msg = run_in_background("supplement", _job_supplement, rid, mats, req.note)
    return {"started": ok, "message": msg}


@app.post("/api/compile_multi")
def api_compile_multi(req: MultiCompileReq):
    ids = list(req.item_ids)
    if len(ids) < 2:
        return {"started": False, "message": "请选择 2 条及以上线索再生成综合专报"}
    if len(ids) > 5:
        return {"started": False, "message": "综合专报一次最多融合 5 条线索"}
    ok, msg = run_in_background("multi_compile", _job_compile_multi, ids)
    return {"started": ok, "message": msg}


class MultiDeleteReq(BaseModel):
    ids: list[int]


@app.post("/api/multireports/delete")
def api_multi_delete(req: MultiDeleteReq):
    """v2.13.y1 综合专报批量删除。"""
    removed = 0
    for mid_ in req.ids:
        try:
            db.delete_multi_report(int(mid_))
            removed += 1
        except Exception:
            continue
    db.log("reports", f"综合专报批量删除 {removed}/{len(req.ids)} 篇")
    return {"ok": True, "removed": removed}


@app.get("/api/multireports")
def api_multireports():
    out = []
    with db.get_conn() as _conn:
        for r in db.list_multi_reports():
            r["word_count"] = sum(
                len(b.get("text", "")) for s in (r.get("body") or [])
                for b in (s.get("blocks") or []))
            r["footnote_count"] = len(r.get("citations") or [])
            ids = r.get("item_ids") or []
            r["custom"] = False
            if ids:
                marks = ",".join("?" * len(ids))
                row = _conn.execute(
                    f"SELECT COUNT(*) c FROM items WHERE id IN ({marks}) "
                    f"AND COALESCE(kind,0)=2", list(ids)).fetchone()
                r["custom"] = row["c"] == len(list(ids))
            out.append(r)
    return {"reports": out}


@app.get("/api/multireport/{mid}")
def api_multireport(mid: int):
    r = db.get_multi_report(mid)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    r["word_count"] = sum(
        len(b.get("text", "")) for s in (r.get("body") or [])
        for b in (s.get("blocks") or []))
    return r


class MultiUpdate(BaseModel):
    title: Optional[str] = None
    editor_note: Optional[str] = None
    body: Optional[list] = None
    conclusion: Optional[str] = None
    status: Optional[str] = None


@app.put("/api/multireport/{mid}")
def api_multireport_update(mid: int, req: MultiUpdate):
    fields = {k: v for k, v in req.dict().items() if v is not None}
    if not fields:
        return {"ok": False, "message": "无更新字段"}
    db.update_multi_report(mid, fields)
    return {"ok": True}


@app.delete("/api/multireport/{mid}")
def api_multireport_delete(mid: int):
    db.delete_multi_report(mid)
    return {"ok": True}


@app.post("/api/multireport/{mid}/export")
def api_multireport_export(mid: int):
    from app.exporter.docx_builder import export_multi_report
    try:
        return export_multi_report(mid)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class AIEditReq(BaseModel):
    instruction: str


def _apply_ai_edit(req: AIEditReq, kind: str, report: dict) -> dict:
    """AI 对话式修改（同步调用）：修改成功即落库，返回 {ok, reply, report}。

    v2.9.2：每轮修改以 {q,a,before,after,ts} 追加进 reports/multi_reports
    的 chat_log（持久化会话，前端恢复聊天与版本链；AI 也以历史为上下文）。"""
    from app.ai.edit import ai_edit_report
    try:
        out = ai_edit_report(kind, report, req.instruction,
                             history=report.get("chat_log"))
    except Exception as e:
        return {"ok": False, "reply": f"AI 修改失败: {type(e).__name__}: {e}"}
    if out.get("ok") and out.get("report"):
        nr = out["report"]
        fields = {"title": nr["title"], "editor_note": nr["editor_note"],
                  "body": nr["body"], "conclusion": nr["conclusion"],
                  "status": "reviewed"}
        if kind == "single":
            db.update_report(report["id"], fields)
        else:
            db.update_multi_report(report["id"], fields)
        db.append_chat_log(kind, report["id"], {
            "q": req.instruction, "a": out.get("reply") or "",
            "before": {k: report.get(k) for k in
                       ("title", "editor_note", "body", "conclusion")},
            "after": {k: nr[k] for k in
                      ("title", "editor_note", "body", "conclusion")},
            "ts": db.now_iso()})
    return out


class ChatDelReq(BaseModel):
    index: int


@app.post("/api/report/{report_id}/chat_delete")
def api_report_chat_delete(report_id: int, req: ChatDelReq):
    r = db.get_report(report_id)
    if not r:
        return {"ok": False, "message": "专报不存在"}
    db.remove_chat_log_entry("single", report_id, req.index)
    return {"ok": True, "chat": db.get_report(report_id).get("chat_log") or []}


@app.post("/api/multireport/{mid}/chat_delete")
def api_multi_chat_delete(mid: int, req: ChatDelReq):
    r = db.get_multi_report(mid)
    if not r:
        return {"ok": False, "message": "综合专报不存在"}
    db.remove_chat_log_entry("multi", mid, req.index)
    return {"ok": True, "chat": db.get_multi_report(mid).get("chat_log") or []}


@app.post("/api/report/{report_id}/chat_clear")
def api_report_chat_clear(report_id: int):
    if not db.get_report(report_id):
        return {"ok": False, "message": "专报不存在"}
    db.clear_chat_log("single", report_id)
    return {"ok": True, "chat": []}


@app.post("/api/multireport/{mid}/chat_clear")
def api_multi_chat_clear(mid: int):
    if not db.get_multi_report(mid):
        return {"ok": False, "message": "综合专报不存在"}
    db.clear_chat_log("multi", mid)
    return {"ok": True, "chat": []}


class ChatRestoreReq(BaseModel):
    chat: list = []


@app.post("/api/report/{report_id}/chat_restore")
def api_report_chat_restore(report_id: int, req: ChatRestoreReq):
    """v2.9.5 撤销「清空/删除单轮」：全量恢复前端回传的聊天记录。"""
    if not db.get_report(report_id):
        return {"ok": False, "message": "专报不存在"}
    db.set_chat_log("single", report_id, req.chat)
    return {"ok": True, "chat": db.get_report(report_id).get("chat_log") or []}


@app.post("/api/multireport/{mid}/chat_restore")
def api_multi_chat_restore(mid: int, req: ChatRestoreReq):
    if not db.get_multi_report(mid):
        return {"ok": False, "message": "综合专报不存在"}
    db.set_chat_log("multi", mid, req.chat)
    return {"ok": True, "chat": db.get_multi_report(mid).get("chat_log") or []}


@app.post("/api/report/{report_id}/ai_edit")
def api_report_ai_edit(report_id: int, req: AIEditReq):
    r = db.get_report(report_id)
    if not r:
        return {"ok": False, "reply": "专报不存在"}
    return _apply_ai_edit(req, "single", {
        "id": r["id"], "title": r.get("title") or "",
        "editor_note": r.get("editor_note") or "",
        "body": r.get("body") or [], "conclusion": r.get("conclusion") or "",
        "chat_log": r.get("chat_log") or []})


@app.post("/api/multireport/{mid}/ai_edit")
def api_multireport_ai_edit(mid: int, req: AIEditReq):
    r = db.get_multi_report(mid)
    if not r:
        return {"ok": False, "reply": "综合专报不存在"}
    return _apply_ai_edit(req, "multi", {
        "id": r["id"], "title": r.get("title") or "",
        "editor_note": r.get("editor_note") or "",
        "body": r.get("body") or [], "conclusion": r.get("conclusion") or "",
        "chat_log": r.get("chat_log") or []})


def _ai_edit_stream_impl(kind: str, rid: int, instruction: str):
    """SSE 生成器：events=status/reasoning/content/retry/done/error。

    v2.9.2：模型按"【思考】…【JSON】{…}"流式输出——【JSON】标记前的原文按
    转发给前端为思考过程；标记后的文本计入生成字数。模型不输出标记时降级为
    字数进度。"""
    import json as _json
    from fastapi.responses import StreamingResponse  # noqa: F401（标注用）
    from app.ai import client as ai_client
    from app.ai.edit import (build_ai_edit_messages, apply_ai_edit_data,
                             split_stream_text)

    def _emit(obj: dict):
        return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"

    def gen():
        if kind == "multi":
            r = db.get_multi_report(rid)
        else:
            r = db.get_report(rid)
        if not r:
            yield _emit({"type": "error", "message": "专报不存在"})
            return
        instr = (instruction or "").strip()
        if not instr:
            yield _emit({"type": "error", "message": "请输入修改指令"})
            return
        report = {"title": r.get("title") or "",
                  "editor_note": r.get("editor_note") or "",
                  "body": r.get("body") or [],
                  "conclusion": r.get("conclusion") or "",
                  "chat_log": r.get("chat_log") or []}
        model = db.get_setting("model_compile") or db.get_setting("model_screen")
        messages = build_ai_edit_messages(kind, report, instr, streaming=True,
                                          history=report.get("chat_log"))
        yield _emit({"type": "status",
                     "message": "AI 正在思考修改思路，随后逐段生成…"})

        buffer = []
        holder = {}

        def on_delta(t, text):
            buffer.append((t, text))

        import threading

        def run():
            try:
                holder["content"] = ai_client.chat_stream(
                    messages, model, temperature=0.2, max_tokens=14000,
                    json_mode=False, on_delta=on_delta)
            except Exception as e:
                holder["error"] = f"{type(e).__name__}: {e}"

        th = threading.Thread(target=run, daemon=True)
        th.start()

        # 思考/生成路由：摆动缓冲，识别【思考】【JSON】段落标记
        pen = ""
        mode = "seek"          # seek → think → json
        think_emitted = False  # 路由已发过思考帧（收尾不再补发，防重复）
        gen_chars = 0
        MARK_THINK, MARK_JSON = "【思考】", "【JSON】"
        keep = max(len(MARK_THINK), len(MARK_JSON))

        def flush_route():
            nonlocal pen, mode, think_emitted, gen_chars
            out = []
            while True:
                if mode == "seek":
                    i = pen.find(MARK_THINK)
                    if i >= 0:
                        pen = pen[i + len(MARK_THINK):]
                        mode = "think"
                        continue
                    if len(pen) > 500:       # 无思考标记：降级为字数进度
                        mode = "json"
                        continue
                    if len(pen) > keep:
                        pen = pen[-keep:]
                    break
                if mode == "think":
                    j = pen.find(MARK_JSON)
                    if j >= 0:
                        seg = pen[:j]
                        if seg.strip():
                            think_emitted = True
                            out.append(("reasoning", seg))
                        pen = pen[j + len(MARK_JSON):]
                        mode = "json"
                        continue
                    if len(pen) > keep:
                        tail = pen[-keep:]
                        head = pen[:-keep]
                        if head.strip():
                            think_emitted = True
                            out.append(("reasoning", head))
                        pen = tail
                    break
                # mode == "json"
                if pen:
                    gen_chars += len(pen)
                    out.append(("progress", str(len(pen))))
                    pen = ""
                break
            return out

        while th.is_alive() or buffer:
            if buffer:
                for t, text in buffer:
                    if t == "content":
                        pen += text
                    else:                      # retry / reasoning(delta 自带)
                        yield _emit({"type": t, "content": text})
                for tt, txt in flush_route():
                    if tt == "reasoning":
                        yield _emit({"type": "reasoning", "content": txt})
                    elif tt == "progress":
                        gen_chars += int(txt)
                        yield _emit({"type": "content", "content": txt})
                buffer = []
            if th.is_alive():
                import time as _t
                _t.sleep(0.15)
        th.join(timeout=1)
        # 收尾：flush_route 遇"未出思考标记"模式会把 pen 整段当 reasoning 吐出
        if mode == "seek" and pen.strip():
            yield _emit({"type": "reasoning", "content": pen.strip()})
            pen = ""
        for tt, txt in flush_route():
            if tt == "reasoning":
                yield _emit({"type": "reasoning", "content": txt})
            elif tt == "progress":
                gen_chars += int(txt)
                yield _emit({"type": "content", "content": txt})
        if "content" not in holder:
            msg = holder.get("error", "AI 修改失败")
            yield _emit({"type": "error", "message": msg[:300]})
            return
        full = holder["content"]
        _think, json_text = split_stream_text(full)
        if _think and not think_emitted:
            # 路由期间未收到思考帧（极端分块）才补发完整思考，避免前端重复
            yield _emit({"type": "reasoning", "content": _think})
        try:
            data = ai_client._extract_json(json_text)
        except Exception as e:
            yield _emit({"type": "error",
                         "message": f"模型输出无法解析为 JSON: {e}"})
            return
        out = apply_ai_edit_data(data)
        wc = 0
        if out.get("ok"):
            nr = out["report"]
            wc = sum(len(b.get("text", ""))
                     for s in nr["body"] for b in (s.get("blocks") or []))
            fields = {"title": nr["title"], "editor_note": nr["editor_note"],
                      "body": nr["body"], "conclusion": nr["conclusion"],
                      "status": "reviewed"}
            if kind == "multi":
                db.update_multi_report(rid, fields)
            else:
                db.update_report(rid, fields)
            db.append_chat_log(kind, rid, {
                "q": instr, "a": out.get("reply") or "",
                "before": {k: report.get(k) for k in
                           ("title", "editor_note", "body", "conclusion")},
                "after": {k: nr[k] for k in
                          ("title", "editor_note", "body", "conclusion")},
                "ts": db.now_iso()})
        report_out = None   # v2.9.4：done 附带最新 chat_log，前端本地状态保持一致
        if out.get("ok"):
            chat = (db.get_multi_report(rid).get("chat_log")
                    if kind == "multi" else db.get_report(rid).get("chat_log")) or []
            report_out = {**nr, "chat_log": chat}
        yield _emit({"type": "done", "ok": out.get("ok", False),
                     "reply": out.get("reply", ""), "word_count": wc,
                     "report": report_out})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/report/{report_id}/ai_edit_stream")
def api_report_ai_edit_stream(report_id: int, req: AIEditReq):
    return _ai_edit_stream_impl("single", report_id, req.instruction)


@app.post("/api/multireport/{mid}/ai_edit_stream")
def api_multireport_ai_edit_stream(mid: int, req: AIEditReq):
    return _ai_edit_stream_impl("multi", mid, req.instruction)


@app.get("/api/settings")
def api_settings():
    s = db.all_settings()   # v2.20: 不含 api_key（密钥走加密通道）
    key = db.get_secret("api_key")
    s["api_key_masked"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else key
    return s


class SettingsUpdate(BaseModel):
    values: dict


@app.post("/api/settings")
def api_settings_update(req: SettingsUpdate):
    for k, v in req.values.items():
        if k == "api_key":
            # v2.20 密钥走 DPAPI 加密存储；掩码值/空值不覆盖真实 key
            if v == "" or v.endswith("…") or "…" in v:
                continue
            db.set_secret("api_key", str(v))
            continue
        db.set_setting(k, str(v))
    db.log("system", "设置已更新: " + ",".join(req.values.keys()))
    return {"ok": True}


@app.post("/api/reset_all")
def api_reset_all():
    """v2.13.8 一键清空所有数据（前端两次确认后调用）：内容+数据模块全清，
    设置回归出厂默认（无任何 API key，需重新填写）；信源管理子模块保留。"""
    try:
        counts = db.reset_all_content()
        # 清空后重建 25 个预置信源（sources 保住了；若被误删也能恢复）
        try:
            if not db.list_sources():
                runner.load_preset_sources()
        except Exception:
            pass
        return {"ok": True, "counts": counts,
                "message": "已清空全部内容与数据，设置已回归原始状态"}
    except Exception as e:
        return {"ok": False, "message": f"清空失败 {type(e).__name__}: {str(e)[:120]}"}


@app.get("/api/logs")
def api_logs(limit: int = 200, stage: Optional[str] = None):
    return {"logs": db.recent_logs(limit, stage=stage)}


@app.get("/api/stats")
def api_stats(days: int = 0):
    """v2.6：统计支持按时间窗（days=0 全部；1=今日；N=最近N天）。
    "线索"口径：COALESCE(published_at, fetched_at)。"""
    import time as _t
    with db.get_conn() as conn:
        if days == 1:   # 今日：按自然日
            today = db.now_iso()[:10]
            params = [today]
            scope = ("JOIN items i2 ON i2.id = t.item_id "
                     "WHERE COALESCE(i2.kind,0)=0 AND substr(COALESCE(i2.published_at, i2.fetched_at),1,10)=?")
            items_c = conn.execute(
                """SELECT COUNT(*) c FROM items
                   WHERE COALESCE(kind,0)=0 AND substr(COALESCE(published_at, fetched_at),1,10)=?""",
                params).fetchone()["c"]
            matched_c = conn.execute(
                f"""SELECT COUNT(*) c FROM screenings t {scope}
                    AND t.is_match=1""", params).fetchone()["c"]
            reports_c = conn.execute(
                f"""SELECT COUNT(*) c FROM reports t {scope}
                    AND t.status != 'abandoned'""", params).fetchone()["c"]
            exported_c = conn.execute(
                f"""SELECT COUNT(*) c FROM reports t {scope}
                    AND t.status='exported'""", params).fetchone()["c"]
        elif days > 1:
            cutoff = _t.strftime("%Y-%m-%dT%H:%M:%S",
                                 _t.localtime(_t.time() - days * 86400))
            params = [cutoff]
            scope = ("JOIN items i2 ON i2.id = t.item_id "
                     "WHERE COALESCE(i2.kind,0)=0 AND COALESCE(i2.published_at, i2.fetched_at) >= ?")
            items_c = conn.execute(
                "SELECT COUNT(*) c FROM items WHERE COALESCE(kind,0)=0 AND COALESCE(published_at, fetched_at) >= ?",
                params).fetchone()["c"]
            matched_c = conn.execute(
                f"""SELECT COUNT(*) c FROM screenings t {scope}
                    AND t.is_match=1""", params).fetchone()["c"]
            reports_c = conn.execute(
                f"""SELECT COUNT(*) c FROM reports t {scope}
                    AND t.status != 'abandoned'""", params).fetchone()["c"]
            exported_c = conn.execute(
                f"""SELECT COUNT(*) c FROM reports t {scope}
                    AND t.status='exported'""", params).fetchone()["c"]
        else:
            items_c = conn.execute("SELECT COUNT(*) c FROM items WHERE COALESCE(kind,0)=0").fetchone()["c"]
            matched_c = conn.execute(
                "SELECT COUNT(*) c FROM screenings sc JOIN items it ON it.id=sc.item_id WHERE COALESCE(it.kind,0)=0 AND sc.is_match=1").fetchone()["c"]
            reports_c = conn.execute(
                "SELECT COUNT(*) c FROM reports WHERE status != 'abandoned'").fetchone()["c"]
            exported_c = conn.execute(
                "SELECT COUNT(*) c FROM reports WHERE status='exported'").fetchone()["c"]
    return {"items": items_c, "matched": matched_c, "reports": reports_c,
            "exported": exported_c,
            "last_run": db.last_fetch_run()}


@app.on_event("startup")
def on_startup():
    db.init_db()
    try:
        from app.db import routine_cleanup
        routine_cleanup()   # v2.13.6 例行清理（init_db 后：日志/任务/上传材料 GC）
    except Exception:
        pass
    if not db.list_sources():
        runner.load_preset_sources()
        db.log("system", "初始化完成：预置信源已装载")
    # 定时采集
    from app.scheduler import start_scheduler
    start_scheduler()


class AiSearchLocalReq(BaseModel):
    kw: str
    days: int = 7          # 0 = 全部


def _job_ai_search_local(state, kw, days):
    """v2.15.0 线索面板 AI 检索改后台任务：结果落 settings（ai_search_result_v2），
    前端任务完成轮询后拉取渲染（原同步等待 10-40 秒界面无变化，用户感知"没反应"）。"""
    from app.ai.local_search import ai_search_local
    r = ai_search_local(kw, days)
    try:
        r["kw"] = kw   # v2.15.0 前端渲染条需要检索词
        db.set_setting("ai_search_result_v2", json.dumps(r, ensure_ascii=False))
    except Exception:
        db.log("system", "AI 检索结果缓存写入失败", "warn")
    if r.get("ok"):
        return {"ok": True, "matched": len(r.get("matched") or []),
                "kws": r.get("kws") or [], "scanned": r.get("scanned") or 0}
    return {"ok": False, "reason": (r.get("error") or "检索失败")[:160]}


@app.get("/api/ai_search_local_result")
def api_ai_search_local_result():
    return db.get_setting("ai_search_result_v2") or {"ok": False}


@app.post("/api/ai_search_local")
def api_ai_search_local(req: AiSearchLocalReq):
    import re as _re
    kw = (req.kw or "").strip()
    if not kw:
        return {"started": False, "message": "请填写关键词或检索主体"}
    ok, msg = run_in_background("ai_search_local",
                                _job_ai_search_local, kw, req.days,
                                label=kw[:40])
    return {"started": ok, "message": msg}


class ArchiveClassifyReq(BaseModel):
    days: int = 0          # 时间窗天数（0=全部）
    q: str = ""            # 关键词（下推 list_items SQL）
    month: str = ""        # 月份 YYYY-MM（按发布月份过滤）
    max_items: int = 500   # 参与分类的上限


def _job_archive_classify(state, days, month, q, max_items):
    """v2.13.4 一键 AI 智能分类（v2.15.0 信源恢复）：按时间窗/关键词取窗口内线索，
    app.ai.local_search.ai_classify 分批主题归类（ai 组），结果落 settings
    archive_classify_result（GET /api/archive/ai_classify_result 读取渲染）。"""
    from app.ai.local_search import ai_classify
    from app.db import list_items as _li
    state.update("筛选取窗口内线索…")
    items = _li(days=(int(days) if days and days > 0 else None),
                q=(q or ""), limit=int(max_items or 500))
    items = [x for x in items if x.get("kind") in (0, None)]
    month = (month or "").strip()
    if month:
        items = [x for x in items if (x.get("published_at") or "").startswith(month)]
    if not items:
        return {"ok": False, "reason": "当前筛选条件下没有线索可分类",
                "total": 0, "topics": 0}
    state.update(f"筛选到 {len(items)} 条线索，开始 AI 主题归类（分批）…")
    r = ai_classify(items, note_cb=state.update,
                    should_cancel=lambda: state.cancelled)
    topics = r.get("topics") or []
    if not topics:
        return {"ok": False, "reason": "AI 未返回分类结果", "total": 0, "topics": 0}
    payload = {"created_at": db.now_iso(),
               "total": r.get("total", 0), "topics": topics,
               "days": int(days or 0), "month": month, "q": (q or "")}
    try:
        db.set_setting("archive_classify_result", json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        db.log("system", f"分类结果落库失败: {type(e).__name__}: {e}", "warn")
    db.log("ai", f"AI 分类完成：{len(topics)} 个主题 / 共 {payload['total']} 条（筛选 {len(items)} 条）")
    return {"ok": True, "total": payload["total"], "topics": len(topics)}


@app.post("/api/archive/ai_classify")
def api_archive_ai_classify(req: ArchiveClassifyReq):
    ok, msg = run_in_background("ai_classify", _job_archive_classify,
                                req.days, req.month, req.q, req.max_items)
    return {"started": ok, "message": msg}


@app.get("/api/help_doc")
def api_help_doc():
    """v2.13.5 使用说明书浮窗数据源：交付目录 README.md。"""
    cands = []
    try:
        if getattr(sys, "frozen", False):
            cands.append(os.path.join(os.path.dirname(sys.executable), "README.md"))
    except Exception:
        pass
    cands.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"))
    cands.append(os.path.join(WEB_DIR, "README.md")) if WEB_DIR else None
    for f in cands:
        try:
            if f and os.path.isfile(f):
                with open(f, encoding="utf-8") as fh:
                    return {"ok": True, "text": fh.read()}
        except (OSError, ValueError):
            continue
    fallback = "# 使用说明\n\n未能读取使用说明文件（README.md）；\n请联系创作者或在交付目录确认该文件存在。"


class ArchiveDeleteReq(BaseModel):
    ids: list


@app.post("/api/archive/delete")
def api_archive_delete(req: ArchiveDeleteReq):
    """v2.13.6 线索数据库单/批删除（有专报引用的线索拒绝并说明）。"""
    r = db.delete_items(req.ids)
    if r["deleted"]:
        db.log("archive", f"线索删除 {r['deleted']} 条"
                          + (f"，拦截 {len(r['blocked'])} 条（已编译成专报）" if r["blocked"] else ""))
    return {"ok": True, "deleted": r["deleted"], "blocked": r["blocked"]}


@app.get("/api/archive/ai_classify_result")
def api_archive_classify_result():
    raw = db.get_setting("archive_classify_result") or ""
    if not raw:
        return {"ok": False, "result": None}
    try:
        return {"ok": True, "result": json.loads(raw)}
    except ValueError:
        return {"ok": False, "result": None}
