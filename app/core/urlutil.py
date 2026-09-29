"""从「分享文案」里抠出链接。

真实场景：用户在抖音/B站 App 里点「复制链接」，剪贴板拿到的是一整段：

    3.53 NWM:/ 10/22 :8pm Y@z.tE EasySlides | GPT-6 Astra 做PPT 上期视频介绍了…
    # Codex # PPT # GPT6  https://v.douyin.com/g3n27E0f4vw/ 复制此链接，打开Dou音搜索，直接观看视频！

直接把这坨东西当 URL 请求必然报 'not a valid URL'。
所以任何接受链接的地方都要先过一遍这里。
"""

from __future__ import annotations

import re

# URL 合法字符集（故意不含中文/空格，遇到中文自动截断，正好切掉后面的引导语）
_URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
URL_RE = re.compile(rf"https?://[{_URL_CHARS}]+")

# 抖音/字节系的短链域名
SHORT_DOMAINS = ("v.douyin.com", "douyin.com", "iesdouyin.com", "v.kuaishou.com")


def extract_first_url(text: str) -> str:
    """从任意文本里抓第一个 http(s) 链接。抓不到返回空串。"""
    if not text:
        return ""
    m = URL_RE.search(text)
    if not m:
        return ""
    # 中文标点可能紧跟在链接后面，去掉
    return m.group(0).rstrip(".,;:!?")


def extract_urls(text: str) -> list[str]:
    """多行文本 → 每行最多取一个链接，去重保序。

    用户可能一次粘好几条分享文案（一行一条），也可能一条文案里混了多个链接。
    这里按行处理：每行取第一个链接，避免同一条视频的封面图链接也混进来。
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        u = extract_first_url(line)
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def clean_url(text: str) -> str:
    """单个输入框用：能抠出链接就抠，抠不出就原样返回（交给调用方报错）。"""
    return extract_first_url(text) or (text or "").strip()
