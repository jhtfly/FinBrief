"""RSS 源适配：httpx 下载（统一 UA/代理/超时）+ feedparser 解析。"""
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _entry_date(entry):
    """取条目发布时间，规范为本地 ISO 字符串；取不到返回 None。"""
    for key in ("published_parsed", "updated_parsed"):
        st = getattr(entry, key, None)
        if st:
            try:
                dt = datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
                return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            except (OverflowError, ValueError):
                continue
    for key in ("published", "updated"):
        raw = getattr(entry, key, None)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S")
            except (TypeError, ValueError):
                continue
    return None


def _strip_html(text: str) -> str:
    import re
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_rss(rss_url: str, timeout: int = 20, window_days: int = 7,
              proxy: str | None = None):
    """抓取 RSS。返回 (items, error) 二元组，成功时 error 为 None。

    items: [{title, url, published_at, summary}]，仅保留最近 window_days 天。
    """
    try:
        with httpx.Client(
            timeout=timeout, verify=False, follow_redirects=True,
            proxy=proxy if proxy else None,
            headers={"User-Agent": UA},
        ) as client:
            resp = client.get(rss_url)
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPError as e:
        return [], f"下载失败: {type(e).__name__}: {e}"

    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        return [], f"RSS 解析失败: {parsed.bozo_exception}"

    cutoff = time.time() - window_days * 86400
    out = []
    for e in parsed.entries:
        title = _strip_html(getattr(e, "title", "") or "")
        link = getattr(e, "link", "") or ""
        if not title or not link:
            continue
        pub = _entry_date(e)
        if pub is not None:
            try:
                ts = time.mktime(time.strptime(pub, "%Y-%m-%dT%H:%M:%S"))
                if ts < cutoff:  # 超窗资料不入库（旧文不刷屏）
                    continue
            except ValueError:
                pass
        summary = _strip_html(getattr(e, "summary", "") or "")[:600]
        out.append({"title": title, "url": link, "published_at": pub,
                    "summary": summary})
    return out, None
