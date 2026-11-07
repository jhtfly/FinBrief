"""HTML 列表页与 sitemap 适配：通用选择器引擎 + 日期解析，数据驱动。"""
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.fetcher.rss import UA, _strip_html

# 各站常见日期文本格式，按序尝试
DATE_FORMATS = [
    "%Y-%m-%d", "%d %B %Y", "%B %d, %Y", "%B %d %Y", "%d.%m.%Y", "%d/%m/%Y",
    "%m/%d/%Y", "%Y/%m/%d", "%d %b %Y", "%b %d, %Y", "%b %d %Y",
    "%Y年%m月%d日", "%d %B, %Y",
]


def parse_date_text(text: str):
    """解析 '23 October 2026' / 'October 23, 2026' / '2026-10-23' / '2026年10月23日' 等。"""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%dT00:00:00")
        except ValueError:
            continue
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})", text)
    if m:
        return f"{m.group(1)}T{m.group(2)}:00"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return f"{m.group(1)}T00:00:00"
    return None


def _client(timeout, proxy):
    return httpx.Client(
        timeout=timeout, verify=False, follow_redirects=True,
        proxy=proxy if proxy else None,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"},
    )


def _in_window(pub: str | None, window_days: int) -> bool:
    if pub is None:
        return True  # 无日期的条目放行，由筛选环节人工核对
    try:
        ts = time.mktime(time.strptime(pub, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return True
    return ts >= time.time() - window_days * 86400


def _date_from_url(url: str, regexp: str):
    """从 URL 提取日期。仅当年月日齐全时返回日期；否则返回 None
    （无日期条目放行，由筛选环节人工核对，避免月粒度日期误杀窗口内条目）。"""
    m = re.search(regexp, url)
    if not m:
        return None
    g = m.groups()
    if len(g) < 3 or not all(g):
        return None
    y, mo, d = g[0], g[1], g[2]
    try:
        return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%dT00:00:00")
    except ValueError:
        return None


def _slug_to_title(url: str):
    """把 URL 末段 slug 转为可读标题。"""
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.\w{2,4}$", "", slug)
    words = re.sub(r"[-_]+", " ", slug).strip()
    return " ".join(w[:1].upper() + w[1:] for w in words.split()) if words else url


# ---------------------------------------------------------------- HTML 列表页
def fetch_html_list(cfg: dict, timeout: int = 20, window_days: int = 7,
                    proxy: str | None = None):
    """按选择器配置抓取列表页。返回 (items, error)。

    cfg 字段：
      list_url      列表页地址
      item          条目容器选择器；为空则对全页做链接模式
      title / link / date / summary   相对条目的选择器
      date_attr     日期取自属性（如 datetime）
      date_regexp   日期文本再提取正则
      link_regexp   链接模式：不用容器，直接按正则收集全页链接
      url_date_regexp  从 URL 提取日期 r"/(\\d{4})/(\\d{2})/(\\d{2})/"
      date_in_parent 日期文本在链接父容器中找（配合 link_regexp）
    """
    list_url = cfg.get("list_url", "")
    if not list_url:
        return [], "配置缺少 list_url"
    try:
        with _client(timeout, proxy) as client:
            resp = client.get(list_url)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as e:
        return [], f"下载失败: {type(e).__name__}: {e}"

    soup = BeautifulSoup(html, "lxml")

    # 模式A：链接正则收集（Goldman / WorldBank / IADI 类）
    if cfg.get("link_regexp"):
        pat = re.compile(cfg["link_regexp"])
        seen, out = set(), []
        for a in soup.find_all("a", href=True):
            link = urljoin(list_url, a["href"])
            if not pat.search(link) or link in seen:
                continue
            seen.add(link)
            title = _strip_html(a.get_text()) or _slug_to_title(link)
            if len(title) < 8:  # 文本太短（图标/短词），退用 slug
                title = _slug_to_title(link)
            pub = None
            if cfg.get("url_date_regexp"):
                pub = _date_from_url(link, cfg["url_date_regexp"])
            elif cfg.get("date_in_parent"):
                parent = a.find_parent(["div", "article", "section", "li"])
                ptxt = parent.get_text(" ", strip=True) if parent else ""
                m = re.search(
                    r"((\d{1,2}\s+[A-Z][a-z]+,?\s+\d{4})|([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})|(\d{4}-\d{2}-\d{2}))",
                    ptxt)
                if m:
                    pub = parse_date_text(m.group(1))
            if not _in_window(pub, window_days):
                continue
            out.append({"title": title[:300], "url": link, "published_at": pub,
                        "summary": ""})
            if len(out) >= (cfg.get("max_items") or 40):
                break
        return out, None

    # 模式B：容器选择器
    containers = soup.select(cfg.get("item", ""))
    if not containers:
        return [], "条目选择器未匹配到任何元素"

    seen_urls, out = set(), []
    for box in containers:
        tnode = box.select_one(cfg["title"]) if cfg.get("title") else None
        title = _strip_html(tnode.get_text()) if tnode else _strip_html(box.get_text())
        if cfg.get("title_regexp"):  # 从标题文本再提取（整卡链接类站点）
            m = re.search(cfg["title_regexp"], title)
            if m:
                title = m.group(1) if m.groups() else m.group(0)
        if not title:
            continue
        link = ""
        if cfg.get("link"):
            lnode = box.select_one(cfg["link"])
            if lnode:
                link = lnode.get("href") or lnode.get_text(strip=True)
        if not link:
            a = box.find("a", href=True)
            link = a["href"] if a else ""
        if not link:
            continue
        link = urljoin(list_url, link)
        if link in seen_urls:
            continue
        seen_urls.add(link)
        pub = None
        if cfg.get("url_date_regexp"):
            pub = _date_from_url(link, cfg["url_date_regexp"])
        if pub is None and cfg.get("date_regexp") and not cfg.get("date"):
            # 无日期节点时对条目全文做正则匹配（摩根士丹利中国的中文日期等）
            m = re.search(cfg["date_regexp"], box.get_text(" ", strip=True))
            if m:
                pub = parse_date_text(m.group(0))
        if pub is None and cfg.get("date"):
            dnode = box.select_one(cfg["date"])
            if dnode:
                raw = dnode.get(cfg["date_attr"]) if cfg.get("date_attr") else None
                if not raw:  # 属性缺失时回退到节点文本（如 <time>22 Oct 2026</time>）
                    raw = dnode.get_text()
                raw = raw or ""
                if cfg.get("date_regexp"):
                    m = re.search(cfg["date_regexp"], raw)
                    raw = m.group(0) if m else raw
                pub = parse_date_text(raw)
        if not _in_window(pub, window_days):
            continue
        summary = ""
        if cfg.get("summary"):
            snode = box.select_one(cfg["summary"])
            if snode:
                summary = _strip_html(snode.get_text())[:600]
        out.append({"title": title[:300], "url": link, "published_at": pub,
                    "summary": summary})
    return out, None


# ---------------------------------------------------------------- sitemap
def fetch_sitemap(cfg: dict, timeout: int = 20, window_days: int = 7,
                  proxy: str | None = None):
    """sitemap 模式（FINMA / CDIC / MAS 类 JS 渲染站）。

    cfg 字段：
      sitemap_url   sitemap 地址（urlset 或 index）
      include       URL 过滤正则（必填，防止全站入库）
      date_from     lastmod（默认，需 sitemap 提供 lastmod）| url
      url_date_regexp  date_from=url 时从 URL 提取日期
      max_items     最多取多少条
    """
    url0 = cfg.get("sitemap_url", "")
    include = re.compile(cfg.get("include", "."))
    max_items = int(cfg.get("max_items") or 60)
    try:
        with _client(timeout, proxy) as client:
            resp = client.get(url0)
            resp.raise_for_status()
            xml = resp.text
    except httpx.HTTPError as e:
        return [], f"下载失败: {type(e).__name__}: {e}"

    soup = BeautifulSoup(xml, "lxml")
    entries = []
    if soup.find("urlset") is None and soup.find_all("sitemap"):
        # sitemap index：逐个子图抓取（最多5个）
        for si in soup.find_all("sitemap")[:5]:
            loc = si.find("loc")
            if not loc:
                continue
            try:
                with _client(timeout, proxy) as client:
                    r2 = client.get(loc.get_text())
                    r2.raise_for_status()
                    sub = BeautifulSoup(r2.text, "lxml")
                    entries.extend(sub.find_all("url"))
            except httpx.HTTPError:
                continue
    else:
        entries = soup.find_all("url")

    out = []
    for u in entries:
        loc = u.find("loc")
        if not loc:
            continue
        url = loc.get_text().strip()
        if not include.search(url):
            continue
        pub = None
        if cfg.get("date_from", "lastmod") == "url":
            pub = _date_from_url(url, cfg.get("url_date_regexp", r"/(\d{4})/(\d{2})/"))
        else:
            lm = u.find("lastmod")
            if lm:
                pub = parse_date_text(lm.get_text()[:10])
        if not _in_window(pub, window_days):
            continue
        out.append({"title": _slug_to_title(url), "url": url,
                    "published_at": pub, "summary": ""})
        if len(out) >= max_items:
            break
    return out, None


# ---------------------------------------------------------------- 正文提取
def extract_date_from_page(url: str, timeout: int = 15,
                           proxy: str | None = None):
    """从文章详情页提取发布日期（用于补全列表页无日期的线索）。
    依次尝试：meta 声明 -> <time datetime> -> 页面文本日期模式。返回 ISO 串或 None。
    """
    try:
        with _client(timeout, proxy) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
    except (httpx.HTTPError, Exception):
        return None

    soup = BeautifulSoup(html, "lxml")

    # 1) 常见 meta 声明
    META_KEYS = {"article:published_time", "datepublished", "pubdate",
                 "publishdate", "dc.date", "dc.date.issued", "date",
                 "og:article:published_time", "parsely-pub-date"}
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("itemprop")
               or meta.get("name") or "").lower()
        if key in META_KEYS:
            v = (meta.get("content") or "").strip()
            m = re.match(r"(\d{4}-\d{2}-\d{2})", v)
            if m:
                return m.group(1) + "T00:00:00"
            d = parse_date_text(v[:40])
            if d:
                return d

    # 2) <time datetime="...">
    for t in soup.find_all("time")[:10]:
        dt = t.get("datetime") or t.get("content") or ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", dt)
        if m:
            return m.group(1) + "T00:00:00"
        d = parse_date_text(t.get_text(" ", strip=True)[:40])
        if d:
            return d

    # 3) 页面正文前部的日期文本
    body = soup.get_text(" ", strip=True)[:4000]
    m = re.search(
        r"((\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})"
        r"|((January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})"
        r"|(\d{4}-\d{2}-\d{2})|(20\d{2}年\d{1,2}月\d{1,2}日))",
        body, re.I)
    if m:
        return parse_date_text(m.group(1))
    return None


def fetch_page_text(url: str, timeout: int = 25, proxy: str | None = None,
                    max_chars: int = 60000):
    """抓取正文页并做通用正文提取（取文字量最大的容器），供 AI 编译用。"""
    try:
        with _client(timeout, proxy) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as e:
        return "", f"下载失败: {type(e).__name__}: {e}"

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "aside", "form", "iframe"]):
        tag.decompose()
    best, best_len = None, 0
    for cand in soup.find_all(["article", "main", "div", "section"]):
        text = cand.get_text("\n", strip=True)
        if len(text) > best_len:
            best, best_len = cand, len(text)
    body = best or soup.body or soup
    text = body.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text[:max_chars], None

def extract_title_from_html(html: str) -> str:
    """从网页 HTML 中提取真实标题（<title> 或 og:title）。"""
    try:
        soup = BeautifulSoup(html, "lxml")
        t_tag = soup.find("title")
        if t_tag:
            t = t_tag.get_text(strip=True)
            if t: return t
        og_t = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
        if og_t and og_t.get("content"):
            return og_t["content"].strip()
    except Exception:
        pass
    return ""
