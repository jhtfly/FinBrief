# -*- coding: utf-8 -*-
"""v2.21.3 直连 RSS/网页拉取器（采集、连通测试、添加信源三处共用）。

背景：investing.com 等 Cloudflare 站按 TLS 指纹拦"非浏览器"HTTP 客户端，
真实浏览器请求头也挡不住直连；但经代理出口即可放行（实测 200）。
策略：真实浏览器 UA×2 + Accept 头 → 直连 → 已配置代理 → 本机常见代理端口兜底。

对得起用户场景：Clash 固定 7897。所有候选都失败时返回 (None, 最后错误)。"""
import httpx

_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
_HEADERS = {
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
              "text/xml;q=0.8, */*;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}
LOCAL_PROXY_FALLBACK = ["http://127.0.0.1:7897"]


def fetch_bytes(url: str, timeout: int = 14, proxy: str | None = None):
    """浏览器指纹多路重试拉取。返回 (content_bytes|None, 最后错误|None)。"""
    last_err = ""
    candidates = [None]
    if proxy:
        candidates.append(proxy)
    candidates += LOCAL_PROXY_FALLBACK
    for use_proxy in candidates:
        for ua in _UAS:
            try:
                with httpx.Client(timeout=timeout, verify=False, follow_redirects=True,
                                  proxy=use_proxy if use_proxy else None,
                                  headers={"User-Agent": ua, **_HEADERS}) as cli:
                    resp = cli.get(url)
                    resp.raise_for_status()
                    return resp.content, None
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {str(e)[:110]}"
            except OSError as e:
                last_err = f"OSError: {str(e)[:110]}"
    return None, last_err or "全部拉取尝试失败"
