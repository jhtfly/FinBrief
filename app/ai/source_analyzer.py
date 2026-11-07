"""AI 信源分析：给一个网址，自动设计采集配置并验证；检查失效源并尝试 AI 修复。"""
import json
import re
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app import db
from app.ai import client
from app.fetcher import runner
from app.fetcher.rss import UA, _strip_html

RSS_GUESS_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/index.xml",
                   "/atom.xml", "/feeds", "/news/rss", "/en/rss",
                   "/press-releases/rss", "/sitemap.xml"]

CATEGORY_DESC = "cb_reg=央行与国际金融监管机构; ins=保险/养老监管; dep=存款保险; cons=咨询/评级/投行"


class SourceError(Exception):
    pass


def _proxy():
    s = db.all_settings()
    if s.get("proxy_enabled") == "1" and s.get("proxy_url"):
        return s["proxy_url"]
    return None


_LAST_FETCH_ERR = {"status": None, "reason": ""}


def _fetch(url, timeout=18):
    """v2.17.0 失败时记录状态码/原因（供失败文案细分：浏览器能开≠程序能抓——
    Cloudflare/Akamai 等反爬对程序 UA 直接 403/503，浏览器过了 JS 挑战才正常）。"""
    _LAST_FETCH_ERR["status"] = None
    _LAST_FETCH_ERR["reason"] = ""
    try:
        with httpx.Client(timeout=timeout, verify=False, follow_redirects=True,
                          proxy=_proxy(),
                          headers={"User-Agent": UA,
                                   "Accept-Language": "en-US,en;q=0.9"}) as c:
            r = c.get(url)
            if r.status_code == 200:
                return r
            _LAST_FETCH_ERR["status"] = r.status_code
            if r.status_code in (403, 503, 429):
                _LAST_FETCH_ERR["reason"] = "站点反爬拦截（Cloudflare/Akamai 等，浏览器过人机验证后才放行，程序直抓被拒）"
            elif r.status_code == 404:
                _LAST_FETCH_ERR["reason"] = "页面不存在（404）"
            else:
                _LAST_FETCH_ERR["reason"] = f"HTTP {r.status_code}"
            return None
    except httpx.HTTPError as e:
        _LAST_FETCH_ERR["reason"] = ("连接超时或网络不可达" if isinstance(e, httpx.TimeoutException)
                                     else f"网络错误：{type(e).__name__}")
        return None


def _test_rss(url):
    """快速验证 RSS 候选：返回条目数或 0。"""
    import feedparser
    r = _fetch(url, timeout=12)
    if not r:
        return 0
    try:
        parsed = feedparser.parse(r.content)
        return len(parsed.entries)
    except Exception:
        return 0


def _find_rss_candidates(home_html: str, base: str):
    soup = BeautifulSoup(home_html, "lxml")
    found = []
    for link in soup.find_all("link", type=True):
        t = link.get("type", "") or ""
        if "rss" in t.lower() or "atom" in t.lower():
            href = link.get("href")
            if href:
                found.append(urljoin(base, href))
    for a in soup.find_all("a", href=True):
        h = a["href"]
        low = h.lower()
        if low.endswith(".xml") and any(k in low for k in ("rss", "feed", "sitemap")):
            found.append(urljoin(base, h))
    found += [urljoin(base, p) for p in RSS_GUESS_PATHS]
    uniq = []
    for u in found:
        if u not in uniq:
            uniq.append(u)
    return uniq[:10]


def _article_samples(home_html: str, base: str, limit=14):
    soup = BeautifulSoup(home_html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base, a["href"])
        text = _strip_html(a.get_text(" ", strip=True))
        if urlparse(href).netloc != urlparse(base).netloc:
            continue
        if len(text) < 15 or href.rstrip("/") == base.rstrip("/"):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append((href, text[:90]))
        if len(out) >= limit * 3:
            break
    out = [x for x in out if not re.search(
        r"(login|signin|contact|about|privacy|terms|search|tag/|category/|career|job)",
        x[0], re.I)]
    return out[:limit]


def _slug_from_url(url):
    netloc = urlparse(url).netloc.replace("www.", "")
    slug = re.sub(r"[^a-z0-9]", "", netloc.split(".")[0]) or "site"
    return slug


CONFIG_SCHEMA_DESC = """可选采集方式（mode 三选一）：
1. mode=rss：config = {"rss_url": "RSS/Atom 地址"}（若下方"RSS探测"中已有可用地址，必须优先选 rss）
2. mode=html：config = {"list_url": "新闻列表页URL", "item": "条目容器CSS选择器", "title": "标题选择器(相对条目,可省略)", "link": "链接选择器(相对条目,常为 a)", "date": "日期选择器(可省略)", "date_attr": "日期属性名如datetime(可省略)", "url_date_regexp": "从URL提取日期的正则(需3个捕获组年月日,如 /(\\\\d{4})/(\\\\d{2})/(\\\\d{2})/, 可省略)", "link_regexp": "全页链接正则(与item二选一)", "date_in_parent": true}（日期文字格式支持 2026-10-23 / Oct 23, 2026 / 23 October 2026 / 2026年10月23日）
3. mode=sitemap：config = {"sitemap_url": "sitemap地址", "include": "URL过滤正则(只保留新闻类)", "date_from": "lastmod或url", "url_date_regexp": "...", "max_items": 40}"""

ANALYZE_PROMPT = """你是网站数据采集配置专家。根据提供的网站首页探测信息，为该网站设计一个新闻/公告采集配置，供采集程序使用。

{schema}

首页信息：
- 网站标题：{title}
- RSS探测（地址 -> 可用条目数）：{rss}
- 页面上的文章链接样本（链接 | 链接文字）：
{samples}

输出 JSON：
{{"name_cn": "机构中文名（规范简称，如 金融稳定理事会）", "name_en": "英文缩写或原名",
  "category": "cb_reg|ins|dep|cons 之一（{cat}）",
  "mode": "rss|html|sitemap", "config": {{...按所选mode的schema...}},
  "reason": "选择该方式的理由（40字内）"}}

要求：CSS 选择器必须依据上面的真实链接样本设计；无 RSS 可用时才选 html/sitemap；config 中所有 URL 必须完整。"""


REFINE_PROMPT = """你之前为网站设计的采集配置验证失败，请根据错误信息修正。

网站信息：
- RSS探测：{rss}
- 文章链接样本：
{samples}
- 你之前的配置：mode={mode}，config={config}
- 验证结果：{error}

修正要求：
- 若错误是"选择器未匹配"，检查样本链接所在页面结构，改用 link_regexp 全页链接正则（匹配文章 URL 路径特征）通常更稳；
- 若错误是下载失败 404，检查路径是否遗漏语言前缀（多语言站点 sitemap 常为 /en/sitemap.xml、/zh/sitemap.xml 等，首页 HTML 中 <link rel="alternate" hreflang> 也有线索）；
- config 中所有 URL 必须完整；日期正则需 3 个捕获组（年月日）。

输出修正后的 JSON（只含这两个字段）：{{"mode": "rss|html|sitemap", "config": {{...}}}}"""


def _verify_src(src: dict) -> dict:
    items, err = runner._fetch_one(src, 20, 30, _proxy())
    return {"ok": err is None and len(items) > 0,
            "items": len(items) if err is None else 0, "error": err}


def analyze_url(url: str) -> dict:
    """分析网址并返回候选 source dict（含 verify 结果）。失败抛 SourceError。"""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"

    r = _fetch(url)
    if not r:
        r = _fetch(base)
    if not r:
        _why = _LAST_FETCH_ERR.get("reason") or "连接失败"
        raise SourceError(f"网址无法访问（可尝试开启代理后重试）：{url} —— 原因：{_why}。"
                          f"注意：浏览器能打开不代表程序能抓——很多站点对自动化请求直接返回 403/503。")
    home_html = r.text
    final_url = str(r.url)
    soup = BeautifulSoup(home_html, "lxml")
    title = _strip_html(soup.title.get_text()) if soup.title else final_url

    # RSS 探测
    candidates = _find_rss_candidates(home_html, final_url)
    rss_status = []
    working_rss = []
    for cand in candidates:
        n = _test_rss(cand)
        rss_status.append(f"{cand} -> {n} 条")
        if n > 0:
            working_rss.append((cand, n))

    samples = _article_samples(home_html, final_url)
    if not working_rss and not samples:
        raise SourceError("首页未发现文章链接样本，无法设计采集配置")

    prompt = ANALYZE_PROMPT.format(
        schema=CONFIG_SCHEMA_DESC, title=title,
        rss="\n".join(rss_status) or "（未发现候选）",
        samples="\n".join(f"{h} | {t}" for h, t in samples) or "（无）",
        cat=CATEGORY_DESC)
    data = client.chat_json(
        [{"role": "system", "content": "你是网站采集配置专家，只输出 JSON。"},
         {"role": "user", "content": prompt}],
        db.get_setting("model_screen"), temperature=0.1, max_tokens=2500)

    mode = data.get("mode")
    config = data.get("config") or {}
    if mode == "rss" and working_rss:
        config = {"rss_url": working_rss[0][0]}
    if mode not in ("rss", "html", "sitemap"):
        raise SourceError(f"AI 返回未知采集方式: {mode}")

    src = {
        "key": _slug_from_url(final_url),
        "name_cn": (data.get("name_cn") or "").strip() or _slug_from_url(final_url),
        "name_en": (data.get("name_en") or "").strip() or urlparse(final_url).netloc,
        "category": data.get("category") if data.get("category") in
                    ("cb_reg", "ins", "dep", "cons") else "cb_reg",
        "mode": mode,
        "config": config,
        "enabled": 0,
        "sort_no": 900,
        "note": f"AI添加 · {(data.get('reason') or '')[:60]}",
    }
    # 验证：窗口放宽到 30 天试采；失败则带错误反馈让 AI 修正一次
    verify = _verify_src(src)
    if not verify["ok"] and mode in ("html", "sitemap"):
        try:
            data2 = client.chat_json(
                [{"role": "system", "content": "你是网站采集配置专家，只输出 JSON。"},
                 {"role": "user", "content": REFINE_PROMPT.format(
                     rss="\n".join(rss_status) or "（未发现候选）",
                     samples="\n".join(f"{h} | {t}" for h, t in samples) or "（无）",
                     mode=mode,
                     config=json.dumps(config, ensure_ascii=False),
                     error=verify.get("error") or "获取 0 条")}],
                db.get_setting("model_screen"), temperature=0.2, max_tokens=2000)
            mode2 = data2.get("mode")
            config2 = data2.get("config") or {}
            db.log("sources", f"[{src['name_cn']}] 首次验证失败，AI 二次修正 -> "
                              f"{mode2}：{json.dumps(config2, ensure_ascii=False)[:150]}")
            if mode2 in ("rss", "html", "sitemap") and config2:
                src2 = dict(src, mode=mode2, config=config2)
                if mode2 == "rss" and working_rss:
                    src2["config"] = {"rss_url": working_rss[0][0]}
                verify2 = _verify_src(src2)
                if verify2["ok"]:
                    src["mode"], src["config"], verify = mode2, config2, verify2
                    src["note"] = f"AI添加（二次修正） · {(data.get('reason') or '')[:40]}"
        except client.AIError:
            pass
    src["_verify"] = verify
    src["_rss_status"] = rss_status
    return src


def add_source_from_url(url: str) -> dict:
    """分析+验证+入库。返回给前端的结果 dict。"""
    src = analyze_url(url)
    verify = src.pop("_verify", {})
    src.pop("_rss_status", None)
    if db.get_source(src["key"]):  # 同名源已存在：用路径后缀区分
        suffix = re.sub(r"[^a-z0-9]", "", urlparse(url).path)[:16]
        if suffix:
            src["key"] = f"{src['key']}_{suffix}"
    src["enabled"] = 1 if verify.get("ok") else 0
    src["src_group"] = "custom"    # AI 添加的信源归入自定义组
    # v2.7：验证通过自动启用 = 自动加入常用（用户心智：启用即参与采集）
    src["in_common"] = 1 if verify.get("ok") else 0
    db.upsert_source(src)
    db.log("sources", f"AI 添加信源 [{src['name_cn']}]（{src['mode']}），验证"
                      f"{'通过' if verify.get('ok') else '未通过'}：{verify}")
    return {"ok": True, "name_cn": src["name_cn"], "mode": src["mode"],
            "enabled": src["enabled"], "verify": verify}


def check_and_fix_sources(progress_cb=None, only_key=None) -> list:
    """逐站连通性检查；失效站尝试 AI 重新分析修复。返回结果列表。
    v2.13.2：only_key 传入时只检查修复该信源（信源管理行右键菜单入口）。"""
    if only_key:
        s0 = db.get_source(only_key)
        sources = [s0] if s0 else []
    else:
        sources = db.list_sources(only_enabled=True)
    results = []
    proxy = _proxy()
    for i, s in enumerate(sources, 1):
        if progress_cb:
            progress_cb(i, len(sources), s["name_cn"])
        r = runner.probe_site(s)
        entry = {"source": s["name_cn"], "ok": r["ok"], "items": r["items"],
                 "action": ""}
        if not r["ok"]:
            cfg = s["config"]
            base_url = cfg.get("list_url") or cfg.get("rss_url") \
                or cfg.get("sitemap_url") or ""
            if base_url:
                p = urlparse(base_url)
                base = f"{p.scheme}://{p.netloc}"
                try:
                    new_src = analyze_url(base)
                    verify = new_src.get("_verify", {})
                    if verify.get("ok") and new_src["key"] == s["key"]:
                        new_src.pop("_verify", None); new_src.pop("_rss_status", None)
                        new_src["enabled"] = s["enabled"]
                        new_src["src_group"] = s["src_group"]
                        new_src["in_common"] = s["in_common"]
                        new_src["sort_no"] = s["sort_no"]
                        new_src["note"] = f"AI修复 {time.strftime('%Y-%m-%d')}"
                        db.upsert_source(new_src)
                        entry["action"] = f"AI 已更新配置（{new_src['mode']}，验证获取 {verify['items']} 条）"
                    elif verify.get("ok"):
                        entry["action"] = "AI 配置验证通过但站点标识不一致，已忽略"
                    else:
                        entry["action"] = f"AI 建议配置验证未通过（{verify.get('error') or '0条'}），保留原配置"
                except (client.AIError, SourceError) as e:
                    entry["action"] = f"AI 修复失败: {str(e)[:80]}"
            else:
                entry["action"] = "无可用配置地址，跳过 AI 修复"
        results.append(entry)
    fixed = sum(1 for x in results if "AI 已更新" in x.get("action", ""))
    db.log("sources", f"信源检查完成：{sum(1 for x in results if x['ok'])}/{len(results)} 可用，"
                      f"AI 修复成功 {fixed} 个")
    return results
