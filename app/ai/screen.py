"""AI 筛选：按六项金融稳定筛选要求批量判定，并同步输出评分维度。

v2.21 批次并发：多批次并行调用（并发数=设置 ai_concurrency）；推理型模型
思考占满首试的情况由 max_tokens 初值 8000 规避（此前 4000 首试常失败、
再翻倍重试，一批白等十几秒——用户报"筛选慢"的根因）。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app import db
from app.ai import client
from app.tasks import TaskCancelled
# 六项筛选要求全文（源自"编译助手"）
CRITERIA_TEXT = """1. 配合总行研究丰富宏观审慎和金融稳定管理工具箱、拓展中央银行维护金融市场稳定职能、探索特定情境下向非银机构提供流动性稳定市场的机制安排。
2. 跟踪低利率和利率波动环境下银行业、保险业风险、经营模式和转型问题；研究银行、保险公司与其他非银机构之间风险交叉传染；关注会计准则切换和未来偿付能力监管规则调整对保险公司的影响。
3. 关注不良资产处置现状与问题，研究优化不良资产处置制度建设。
4. 跟踪影响资本市场平稳运行的国内外风险因素；研究中长期资金入市、新股发行、退市、减持、回购、高频及量化交易、衍生品、中介机构监管等制度安排；跟踪国际关于影子银行、高频交易、股票代币化等证券监管规则变化及对我国借鉴。
5. 加强金融稳定领域法律法规、政策性文件制定修订调研，推动建立金融风险防控处置长效机制。
6. 配合总行做好 FSB、BCBS 等国际组织及工作组相关工作，以及第三次中国金融部门评估规划（FSAP）成果转化、FSB 第二轮中国国家同行评估等工作。"""

# 判定逻辑部分（可被 custom_screen_criteria 整体替换；{criteria} 占位）
JUDGE_PROMPT = """你是中国人民银行某分行"金融稳定专报"的线索筛选专家。你的任务是判断境外金融监管机构、国际组织和金融机构发布的内容是否符合单位的编译线索筛选要求。

筛选规则（必须严格遵守）：
1. 必须基于线索内容实质判断，不能只看标题字面。
2. 关联度弱、仅泛泛涉及金融市场或宏观经济者，判定为不符合。
3. 纯商业宣传、市场营销、会议预告、招聘、纯统计公报类内容，判定为不符合。
4. 演讲、研究报告、政策文件、监管规则、评估报告等内容需认真评估其与六项要求的实质关联。

六项筛选要求如下：
{criteria}"""

# 评分规则 + 输出格式（恒定拼接，保证自定义提示词时不破坏输出契约）
OUTPUT_SPEC = """

评分规则（对每条线索输出三个 0-100 整数分）：
- relevance 主题贴合性：与上述筛选要求核心方向的贴合深度，直接命中核心议题者高分，泛泛相关者低分。
- authority 机构权威性：发布机构层级与公信力（官方一手监管机构/国际组织高分 > 官方研究部门 > 商业机构研究）。
- completeness 内容完整性：标题与摘要体现的信息量、是否具备编译成篇的素材潜力。
时效性（timeliness）由系统按发布时间自动计算，不需要你输出。
{custom_rules}

输出 JSON 格式：
{{"results": [{{"id": 线索编号, "is_match": true或false, "criteria": [命中要求序号1-6], "match_points": "匹配点说明（50字内）", "reason": "判定理由（60字内）", "scores": {{"relevance": 0-100整数, "authority": 0-100整数, "completeness": 0-100整数}}}}]}}"""


def _criteria_text() -> str:
    """筛选要求：优先用户自定义，否则内置六项。"""
    custom = (db.get_setting("custom_screen_criteria") or "").strip()
    return custom if custom else CRITERIA_TEXT


def _custom_rules() -> str:
    custom = (db.get_setting("score_custom_rules") or "").strip()
    return f"单位补充评分细则（优先级高于上述一般规则）：\n{custom}" if custom else ""


def _system_prompt() -> str:
    """筛选系统提示词：整体替换（保输出契约）+ 附加要求。"""
    prompt = JUDGE_PROMPT.format(criteria=_criteria_text()) + OUTPUT_SPEC
    extra = (db.get_setting("screen_extra") or "").strip()
    if extra:
        prompt += f"\n\n单位附加筛选要求（必须遵守）：\n{extra}"
    return prompt


def _timeliness(published_at) -> int:
    """时效分：越新越高。无日期按 50（待核）。"""
    if not published_at:
        return 50
    try:
        pub = time.mktime(time.strptime(published_at[:19], "%Y-%m-%dT%H:%M:%S"))
        age = (time.time() - pub) / 86400
    except ValueError:
        return 50
    if age <= 1:
        return 100
    if age <= 2:
        return 90
    if age <= 3:
        return 80
    if age <= 5:
        return 65
    if age <= 7:
        return 50
    return 25


def _final_score(detail: dict) -> int | None:
    """按设置权重归一化合成总分。缺维度按 50 计。"""
    w = {"relevance": db.get_setting("score_weight_relevance", "40"),
         "timeliness": db.get_setting("score_weight_timeliness", "25"),
         "authority": db.get_setting("score_weight_authority", "20"),
         "completeness": db.get_setting("score_weight_completeness", "15")}
    try:
        weights = {k: max(0, int(v)) for k, v in w.items()}
    except ValueError:
        weights = {"relevance": 40, "timeliness": 25, "authority": 20,
                   "completeness": 15}
    total_w = sum(weights.values())
    if total_w <= 0:
        weights = {k: 1 for k in weights}
        total_w = 4
    filled = {k: detail.get(k) for k in weights}
    if all(v is None for v in filled.values()):
        return None
    vals = {k: (50 if v is None else max(0, min(100, int(v))))
            for k, v in filled.items()}
    return round(sum(weights[k] * vals[k] for k in weights) / total_w)


def screen_items(item_ids: list, batch_size: int = 8, progress_cb=None,
                 should_cancel=None) -> dict:
    """批量筛选线索。返回 {ok: n, match: n, error: str|None}。
    v2.6 支持协作打断：批次间检查应取消标志，被打断时删除本轮已写判定。"""
    settings = db.all_settings()
    model = settings.get("model_screen", "")
    items = []
    for iid in item_ids:
        it = db.get_item(iid)
        if it:
            items.append(it)
    if not items:
        return {"ok": 0, "match": 0, "error": "没有待筛选的线索"}

    # ===== v2.21 并发批筛选：多批并行调用（并发数=ai_concurrency），打断可回滚 =====
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    try:
        workers = max(1, min(6, int(settings.get("ai_concurrency", "2"))))
    except (TypeError, ValueError):
        workers = 2
    done = matched = 0
    error = None
    screened_ids = []   # 本轮写入判定的线索（打断回滚用）
    cancelRequested = [False]
    lock = threading.Lock()

    def _screen_batch(batch):
        """单批筛选（线程内执行）；返回 (ok_n, match_n)。AIError 记日志返回 0。"""
        nonlocal error
        if should_cancel and should_cancel():
            cancelRequested[0] = True
            return 0, 0
        lines = []
        for n, it in enumerate(batch, 1):
            summary = (it.get("summary") or "")[:400]
            pub = (it.get("published_at") or "日期未知")[:10]
            lines.append(
                f"编号{n}｜来源机构：{it['source_name_en']}（{it['source_name_cn']}）｜"
                f"发布时间：{pub}\n标题：{it['title']}\n摘要：{summary or '（无摘要，请依据标题与机构性质判断）'}")
        messages = [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": "待筛选线索：\n\n" + "\n\n".join(lines)},
        ]
        try:
            data = client.chat_json(messages, model, temperature=0.1, max_tokens=8000)
        except client.AIError as e:
            with lock:
                error = str(e)
            db.log("screen", f"筛选批次失败（{batch[0]['source_name_cn']} 等 {len(batch)} 条）: {e}", "error")
            return 0, 0
        results = data.get("results", [])
        by_id = {}
        for r_ in results:
            try:
                by_id[int(r_.get("id"))] = r_
            except (TypeError, ValueError):
                continue
        ok_n = match_n = 0
        local_ids = []
        for n, it in enumerate(batch, 1):
            r_ = by_id.get(n)
            if r_ is None:
                continue
            is_match = bool(r_.get("is_match"))
            criteria = [c for c in (r_.get("criteria") or [])
                        if isinstance(c, int) and 1 <= c <= 6]
            # 评分：AI 三维度 + 系统时效分 → 按权重合成总分
            scores = r_.get("scores") or {}
            detail = {
                "relevance": scores.get("relevance"),
                "timeliness": _timeliness(it.get("published_at")),
                "authority": scores.get("authority"),
                "completeness": scores.get("completeness"),
            }
            final = _final_score(detail)
            db.upsert_screening(
                it["id"], is_match, criteria,
                (r_.get("match_points") or "")[:300],
                (r_.get("reason") or "")[:400], model,
                score=final, score_detail=detail)
            local_ids.append(it["id"])
            ok_n += 1
            if is_match:
                match_n += 1
        with lock:
            screened_ids.extend(local_ids)
        return ok_n, match_n

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_screen_batch, b) for b in batches]
            for fut in futs:
                ok_n, match_n = fut.result()
                with lock:
                    done += ok_n
                    matched += match_n
                if progress_cb:
                    try:
                        progress_cb(done, len(items), matched)
                    except Exception:
                        pass
        if cancelRequested[0]:
            raise TaskCancelled("任务已被用户打断")
    except TaskCancelled:
        db.delete_screenings(screened_ids)
        db.log("screen", f"筛选任务被打断：已回滚本轮 {len(screened_ids)} 条判定", "warn")
        raise
    db.log("screen", f"AI筛选完成：{done}/{len(items)} 条判定，符合 {matched} 条"
                     + (f"，末批错误: {error}" if error else ""))
    return {"ok": done, "match": matched, "error": error}
