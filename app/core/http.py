"""统一 HTTP 客户端。

本机踩坑记录（重要，别改回去）：
1. 系统开着 Clash 代理，国内站点（B站/抖音）走代理会 502 或直接挂起。
   必须设置 no_proxy=* 绕过，requests 的 trust_env=False 在 Windows 上不管用
   （代理来自注册表，不是环境变量）。
2. B站/抖音有 TLS 指纹检测：requests/OpenSSL 的指纹会被挂起，
   curl_cffi 的 impersonate="chrome" 能模拟真实浏览器握手，实测 0.4s 返回。

因此全项目统一用 curl_cffi。
"""

from __future__ import annotations

import os

# 必须在 import curl_cffi 之前设置，libcurl 初始化时会读取
os.environ.setdefault("no_proxy", "*")
os.environ.setdefault("NO_PROXY", "*")

from typing import Any  # noqa: E402

from curl_cffi import requests as curl_requests  # noqa: E402
from curl_cffi.const import CurlOpt  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class HttpClient:
    """带浏览器指纹的 HTTP 客户端。"""

    def __init__(self, cookie: str = "", referer: str = "",
                 impersonate: str = "chrome", timeout: int = 15,
                 extra_headers: dict[str, str] | None = None,
                 proxy: str = "") -> None:
        """proxy: 显式代理，形如 http://127.0.0.1:7897。

        国内站点（B站/抖音）留空即可直连；Telegram 这类境外站点必须显式传代理，
        否则会走系统代理拿到 502。
        """
        self.timeout = timeout
        self.impersonate = impersonate
        self.proxy = proxy
        if proxy:
            # libcurl 会读 NO_PROXY 环境变量，把我们显式设的代理也一起无视掉，
            # 所以这里必须把 NOPROXY 显式置空，代理才真正生效。
            self.session = curl_requests.Session(
                impersonate=impersonate,
                proxies={"http": proxy, "https": proxy},
                curl_options={CurlOpt.NOPROXY: b""},
            )
        else:
            self.session = curl_requests.Session(impersonate=impersonate)
        headers = dict(DEFAULT_HEADERS)
        if referer:
            headers["Referer"] = referer
        if cookie:
            headers["Cookie"] = cookie
        if extra_headers:
            headers.update(extra_headers)
        self.session.headers.update(headers)

    def get(self, url: str, params: dict | None = None,
            timeout: int | None = None, stream: bool = False) -> Any:
        return self.session.get(url, params=params,
                                timeout=timeout or self.timeout,
                                stream=stream)

    def get_json(self, url: str, params: dict | None = None,
                 timeout: int | None = None) -> dict:
        r = self.get(url, params=params, timeout=timeout)
        return r.json()

    def set_cookie(self, cookie: str) -> None:
        if cookie:
            self.session.headers["Cookie"] = cookie

    @property
    def raw(self) -> Any:
        return self.session


def quick_get(url: str, cookie: str = "", referer: str = "",
              timeout: int = 15, proxy: str = "") -> Any:
    """一次性请求。"""
    return HttpClient(cookie=cookie, referer=referer,
                      timeout=timeout, proxy=proxy).get(url)
