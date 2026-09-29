"""连通性校验：cookie 配好后先跑通，再拿去抓数据。"""

from __future__ import annotations

from .cookie_manager import CookieManager
from .http import HttpClient

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _client(cookie_header: str, referer: str) -> HttpClient:
    """统一走 curl_cffi（Chrome 指纹 + 绕系统代理）。"""
    return HttpClient(cookie=cookie_header, referer=referer, timeout=20)


def verify_bilibili(cm: CookieManager) -> tuple[bool, str]:
    """B站：调 nav 接口，返回 (是否登录, 用户名或错误)。"""
    ok, msg = cm.quick_check("bilibili")
    if not ok:
        return False, msg
    try:
        s = _client(cm.to_header("bilibili"), "https://www.bilibili.com/")
        r = s.get("https://api.bilibili.com/x/web-interface/nav")
        d = r.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"网络请求失败：{exc}"
    if d.get("code") == 0:
        data = d.get("data") or {}
        name = data.get("uname", "已登录")
        extra = f"（UID {data.get('mid')}）" if data.get("mid") else ""
        return True, f"已登录：{name}{extra}"
    return False, f"B站返回 code={d.get('code')}：{d.get('message')}（cookie 可能已过期）"


def verify_douyin(cm: CookieManager) -> tuple[bool, str]:
    """抖音：先查关键字段，再实际请求站点确认没有被踢回登录。"""
    ok, msg = cm.quick_check("douyin")
    if not ok:
        return False, msg
    try:
        s = _client(cm.to_header("douyin"), "https://www.douyin.com/")
        r = s.get("https://www.douyin.com/")
    except Exception as exc:  # noqa: BLE001
        return False, f"网络请求失败：{exc}"

    if r.status_code != 200:
        return False, f"抖音返回 HTTP {r.status_code}"

    # 抖音网页端登录后会带上 uid 相关信息；未登录会重定向到登录页
    final = r.url or ""
    if "login" in final.lower():
        return False, "被重定向到登录页，cookie 无效或已过期"

    set_cookie = r.headers.get("Set-Cookie", "")
    text = r.text or ""
    # 登录态页面会包含用户相关信息；这里用 ttwid / sessionid 是否被服务端认可来判断
    if '"login"' in text and "passport" in text and len(text) < 5000:
        return False, "返回的是登录页，cookie 未生效"
    return True, f"抖音 cookie 可用（HTTP 200，{len(text)} 字节）"


def verify_xiaohongshu(cm: CookieManager) -> tuple[bool, str]:
    """小红书：走带签名封装的登录自检（/user/me）。

    红书的接口一律要 x-s / x-s-common / x-t 签名，所以这里不能直接
    用裸 HttpClient，得复用爬虫自己的 _request（内部调 xhshow 签名）。
    """
    ok, msg = cm.quick_check("xiaohongshu")
    if not ok:
        return False, msg
    try:
        from ..crawler import registry as _reg
        crawler = _reg.build("xiaohongshu",
                             cookie=cm.to_header("xiaohongshu"))
        ok, msg = crawler.check_login()
        return ok, msg
    except Exception as exc:  # noqa: BLE001
        return False, f"校验异常：{str(exc)[:200]}"


VERIFIERS = {
    "bilibili": verify_bilibili,
    "douyin": verify_douyin,
    "xiaohongshu": verify_xiaohongshu,
}


def verify(cm: CookieManager, platform: str) -> tuple[bool, str]:
    fn = VERIFIERS.get(platform)
    if not fn:
        return False, f"暂不支持校验平台：{platform}"
    return fn(cm)
