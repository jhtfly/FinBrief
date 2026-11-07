"""AI 搜索引擎信源抓取（v2.6）。

针对无 RSS / 被反爬拦截的机构：用搜索引擎抓取该机构最近的新闻线索。
- 引擎链：DuckDuckGo HTML 端点（对爬虫最友好）→ 必应 → 百度，逐个自动尝试；
  某引擎拿不到结果即切换下一个（"失效兜底"）。
- SERP 结果 → 逐条抓原文页提取日期与真实标题 → AI（内置/自定义提示词）
  确认哪些与单位的编译筛选要求明确相关。
- 入库到虚拟信源 ai_search（src_group=custom，不参与站点采集），
  之后照常走"一键筛选 / 编译 / 评分"流程。
"""
import json
import re
from urllib.parse import quote, urljoin, urlparse, unquote

import httpx
from bs4 import BeautifulSoup

from app import db
from app.ai import client
from app.fetcher import html_sites
from app.tasks import TaskCancelled

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}

SYSTEM_PROMPT = """你是境外金融线索甄别助手。给你一组通过搜索引擎找到的窗口内线索（含标题、摘要、日期、域名）。
请判断每条是否值得纳入"金融稳定专报"的线索库：
- 保留与单位筛选要求明确相关、来自境外监管机构/国际组织/权威研究或媒体原文的线索；
- 剔除社交媒体转发、首页导航、广告、无关内容。
输出 JSON：{"picked":[{"no":结果编号,"criteria":[命中要求序号1-6],"brief":"一句话说明（30字内）"}]}
没有值得保留的就输出 {"picked":[]}。{extra}"""



def _proxy():
    s = db.all_settings()
    if s.get("proxy_enabled") == "1" and s.get("proxy_url"):
        return s["proxy_url"]
    return None


def _system_prompt() -> str:
    """确认提示词：整体替换 + 附加（契约自动保留）。"""
    prompt = (db.get_setting("custom_search_confirm_prompt") or "") \
        .strip() or SYSTEM_PROMPT
    extra = (db.get_setting("search_confirm_extra") or "").strip()
    if extra and "{extra}" not in prompt:
        prompt += f"\n单位附加确认要求（必须遵守）：\n{extra}"
    elif extra:
        prompt = prompt.replace("{extra}", f"\n单位附加确认要求（必须遵守）：\n{extra}")
    if '"picked"' not in prompt:
        prompt += '\n\n输出 JSON：{"picked":[{"no":结果编号,"criteria":[1],"brief":"说明"}]}'
    return prompt


# ---------------- 搜索引擎 ----------------
def _serp_ddg(query, proxy, timeout):
    """DuckDuckGo HTML 端点：对爬虫最友好（无 JS）。"""
    url = "https://html.duckduckgo.com/html/"
    with httpx.Client(timeout=timeout, proxy=proxy, headers=UA,
                      follow_redirects=True) as c:
        r = c.post(url, data={"q": query, "kl": "wt-wt"})
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for res in soup.select("div.result"):
        a = res.select_one("a.result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if "uddg=" in href:
            href = unquote(href.split("uddg=")[1].split("&")[0])
        snippet = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": href,
                    "snippet": snippet.get_text(" ", strip=True) if snippet else "",
                    "date": ""})
    return out


def _serp_bing(query, proxy, timeout):
    url = "https://www.bing.com/search"
    with httpx.Client(timeout=timeout, proxy=proxy, headers=UA,
                      follow_redirects=True) as c:
        r = c.get(url, params={"q": query, "setmkt": "en-US", "count": "30"})
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        sn = li.select_one(".b_caption p, p")
        dt = ""
        tspan = li.select_one("span.news_dt, span[aria-label]")
        if tspan:
            dt = tspan.get_text(strip=True)
        out.append({"title": a.get_text(" ", strip=True), "url": a["href"],
                    "snippet": sn.get_text(" ", strip=True) if sn else "",
                    "date": dt})
    return out


def _serp_baidu(query, proxy, timeout):
    url = "https://www.baidu.com/s"
    with httpx.Client(timeout=timeout, proxy=proxy, headers=UA,
                      follow_redirects=True) as c:
        r = c.get(url, params={"wd": query}, headers={**UA, "Referer": "https://www.baidu.com/"})
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for res in soup.select("div.result, div.c-container"):
        a = res.select_one("h3 a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if href.startswith("/link?url="):
            href = urljoin("https://www.baidu.com/", href)
        sn = res.select_one(".c-abstract, [class*=content-right]")
        out.append({"title": a.get_text(" ", strip=True), "url": href,
                    "snippet": sn.get_text(" ", strip=True) if sn else "",
                    "date": ""})
    return out


def _serp_google(query, proxy, timeout):
    """v2.13.8 谷歌 HTML 端点（无 JS，首选引擎；被反爬时由 auto 链兜底）。"""
    url = "https://www.google.com/search"
    with httpx.Client(timeout=timeout, proxy=proxy, headers={**UA, "Cookie": "CONSENT=YES+cb"},
                      follow_redirects=True) as c:
        r = c.get(url, params={"q": query, "num": "20", "hl": "en"})
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for res in soup.select("div.g, div.tF2Cxc, div.MjjYud"):
        a = res.select_one("h3 a[href], a[href] h3")
        if a is None:
            continue
        href = a.get("href") or (a.find_parent("a").get("href") if a.find_parent("a") else "")
        if not href or not str(href).startswith("http"):
            continue
        sn = res.select_one(".VwiC3b, .aCOpRe, span.aCOpRe")
        out.append({"title": a.get_text(" ", strip=True), "url": str(href),
                    "snippet": sn.get_text(" ", strip=True) if sn else "",
                    "date": ""})
    return out


ENGINES = {"google": _serp_google, "ddg": _serp_ddg, "bing": _serp_bing, "baidu": _serp_baidu}
ENGINE_NAME = {"google": "谷歌", "ddg": "DuckDuckGo", "bing": "必应", "baidu": "百度"}


def _resolve_baidu_link(url, proxy, timeout, client_):
    if "baidu.com/link?url=" in url:
        try:
            r = client_.get(url)
            if r.status_code == 200:
                return str(r.url)
        except httpx.HTTPError:
            pass
    return url


def collect_serp(keywords: list, engine: str = "auto", per_kw: int = 20,
                 progress_cb=None, should_cancel=None) -> tuple:
    """跑 SERP 收集候选。返回 (candidates, used_engine)。"""
    settings = db.all_settings()
    timeout = min(int(settings.get("request_timeout", 20)), 16)
    proxy = _proxy()
    engines = ["google", "ddg", "bing", "baidu"] if engine == "auto" else [engine]
    candidates, used = [], None
    for eng in engines:
        if should_cancel and should_cancel():
            raise TaskCancelled("任务已被用户打断")
        if progress_cb:
            progress_cb(f"{ENGINE_NAME[eng]} 搜索中…")
        cand = []
        for kw in keywords:
            if should_cancel and should_cancel():
                raise TaskCancelled("任务已被用户打断")
            try:
                cand += ENGINES[eng](kw, proxy, timeout)
            except httpx.HTTPError:
                continue
        # 去重 + 过滤
        seen = set()
        clean = []
        for x in cand:
            u = x["url"].split("#")[0]
            if not u.startswith("http") or u in seen:
                continue
            if re.search(r"(login|signin|mailto)", u, re.I):
                continue
            seen.add(u)
            clean.append(x)
        if len(clean) >= 3:
            candidates, used = clean[:per_kw], eng
            break
        # v2.15.0 引擎空结果也留痕（运行日志可查各引擎具体的失败情形）
        try:
            db.log("ai", f"[AI 搜索] 引擎 {ENGINE_NAME.get(eng, eng)} 未取得可用结果"
                         f"（反爬/无代理/网络原因时常见，继续尝试下一引擎）", "warn")
        except Exception:
            pass
    return candidates, used


def search_clues(keywords: list, engine: str = "auto",
                 progress_cb=None, should_cancel=None) -> dict:
    """AI 搜索抓取线索主流程：SERP → 原文页 → AI 确认 → 入库。"""
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        return {"ok": 0, "error": "请填写机构 / 关键词"}

    candidates, used = collect_serp(keywords, engine, progress_cb=progress_cb,
                                    should_cancel=should_cancel)
    if not candidates:
        return {"ok": 0, "error": "所有搜索引擎均未返回可用结果（可稍后重试或手动指定引擎）",
                "engine_tried": [ENGINE_NAME[e] for e in
                                 (ENGINES if engine == "auto" else [engine])]}
    if progress_cb:
        progress_cb(f"{ENGINE_NAME.get(used, used)} 返回 {len(candidates)} 条，抓取原文页…")

    # 逐条抓原文页（日期与真实标题）
    settings = db.all_settings()
    proxy = _proxy()
    enriched = []
    for i, cand in enumerate(candidates[:14], 1):
        if should_cancel and should_cancel():
            raise TaskCancelled("任务已被用户打断")
        if progress_cb:
            progress_cb(f"原文页 {i}/{min(len(candidates),14)}…")
        url = cand["url"]
        title, pub, snip = cand["title"], "", cand["snippet"][:300]
        try:
            text, err = html_sites.fetch_page_text(url, proxy=proxy, max_chars=6000)
        except Exception:
            text = ""
        try:
            pub = html_sites.extract_date_from_page(url, proxy=proxy) or pub
        except Exception:
            pass
        cand2 = dict(cand)
        cand2["page_date"] = pub or ""
        cand2["page_title"] = title
        enriched.append(cand2)

    # AI 确认
    if should_cancel and should_cancel():
        raise TaskCancelled("任务已被用户打断")
    if progress_cb:
        progress_cb("AI 确认线索相关性…")
    lines = []
    for n, c in enumerate(enriched, 1):
        dom = urlparse(c["url"]).netloc
        pub = c.get("page_date") or c.get("date") or "未知"
        lines.append(f"编号{n}｜{c['page_title']}｜{dom}｜{pub}\n  摘要：{c['snippet'][:160]}")
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": "搜索结果：\n\n" + "\n".join(lines)},
    ]
    model = db.get_setting("model_screen") or db.get_setting("model_compile")
    data = client.chat_json(messages, model, temperature=0.1, max_tokens=3000)
    picked = []
    by_no = {p.get("no"): p for p in (data.get("picked") or [])}
    for n, c in enumerate(enriched, 1):
        p = by_no.get(n)
        if not p:
            continue
        try:
            crit = [int(x) for x in (p.get("criteria") or []) if 1 <= int(x) <= 6]
        except (TypeError, ValueError):
            crit = []
        picked.append({"item": c, "criteria": crit, "brief": (p.get("brief") or "")[:80]})
    if not picked:
        return {"ok": 1, "picked": 0,
                "message": f"{ENGINE_NAME.get(used, used)} 返回 {len(enriched)} 条，AI 判断均与筛选要求无关"}

    # 入库（虚拟信源 ai_search）
    src = db.get_source("ai_search")
    if not src:
        db.upsert_source({
            "key": "ai_search", "name_cn": "AI 搜索引擎抓取",
            "name_en": "AI Search", "category": "cb_reg", "mode": "rss",
            "config": {"rss_url": ""}, "enabled": 0, "in_common": 0,
            "src_group": "custom", "note": "虚拟信源：搜索引擎抓取的线索归集于此",
        })
        src = db.get_source("ai_search")
    src_id = src["id"]
    inserted = 0
    for p in picked:
        it = p["item"]
        pub = it.get("page_date") or ""
        if pub:
            try:
                pub = _to_iso(pub)
            except Exception:
                pub = ""
        if db.insert_item(src_id, it["page_title"][:500], it["url"].split("#")[0],
                          pub or None, it["snippet"][:400]):
            inserted += 1
            db.log("search", f"AI 搜索入库：{it['page_title'][:50]}"
                             + (f"（命中要求 {p['criteria']}）" if p["criteria"] else ""))
    return {"ok": 1, "picked": inserted, "engine": ENGINE_NAME.get(used, used),
            "candidates": len(enriched)}


def _to_iso(pub: str) -> str:
    """把页面日期文本转 ISO（简版解析，解析失败原样返回）。"""
    text = str(pub).strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}T00:00:00"
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}T00:00:00"
    return text
