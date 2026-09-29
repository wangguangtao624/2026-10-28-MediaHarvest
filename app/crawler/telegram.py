"""Telegram 公开频道/群组采集适配器。

实测结论（2026-09）：
- t.me 在国内必须走代理，直连会 502；代理生效后 HTTP 200
- 公开频道列表页 https://t.me/s/<channel> 可直接抓，无需登录
  页面里每条消息带 data-post="<channel>/<msg_id>"，视频消息带
  tgme_widget_message_video_player 标记
- 翻页用 ?before=<msg_id>
- 单条消息用 yt-dlp 的 telegram:embed 提取器下载（t.me/<channel>/<id>）

限制：
- 只对**公开**频道/群组有效；私有群组网页版拿不到，需改用手动链接模式
- Telegram 没有关键词搜索接口，主题模式对 Telegram 不适用
"""

from __future__ import annotations

import html as html_lib
import re
import time
from typing import Any

from ..core.http import HttpClient
from ..core.models import Item, STATUS_PENDING

BLOCK_RE = re.compile(r'data-post="(?P<post>[^"]+)"(?P<body>.*?)(?=data-post="|\Z)', re.S)
TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(?P<t>.*?)</div>\s*</div>', re.S)
TAG_RE = re.compile(r"<[^>]+>")
VIDEO_MARKERS = (
    "tgme_widget_message_video_player",
    "tgme_widget_message_video",
    "tgme_widget_message_roundvideo",
)


class TelegramCrawler:
    platform = "telegram"

    def __init__(self, proxy: str = "", timeout: int = 25) -> None:
        # 必须显式传代理；留空会走系统代理拿到 502
        self.proxy = proxy
        self.timeout = timeout
        self.session = HttpClient(
            referer="https://t.me/",
            timeout=timeout,
            proxy=proxy,
        )
        self.session.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    @property
    def has_cookie(self) -> bool:
        return True  # Telegram 不需要 cookie，只用代理

    def check_login(self) -> tuple[bool, str]:
        """Telegram 没有登录态概念，这里实际验的是代理能不能通。"""
        try:
            r = self.session.get("https://t.me/s/telegram", timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            return False, f"连接失败（检查代理设置）：{str(exc)[:120]}"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, "Telegram 可访问（公开频道无需登录）"

    # ---------- URL 解析 ----------
    @staticmethod
    def parse_channel(text: str) -> tuple[str, str]:
        """把输入识别成 (类型, 值)：channel / post / unknown。"""
        text = (text or "").strip()
        m = re.match(r"^@?([A-Za-z0-9_]{4,32})$", text)
        if m:
            return "channel", m.group(1)
        m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]{4,32})(?:/(\d+))?", text)
        if m:
            if m.group(2):
                return "post", f"{m.group(1)}/{m.group(2)}"
            return "channel", m.group(1)
        return "unknown", text

    # ---------- 采集 ----------
    def fetch_creator(self, channel: str, max_count: int = 20,
                      video_only: bool = True) -> list[Item]:
        """抓取公开频道/群组里的消息（默认只留带视频的）。"""
        items: list[Item] = []
        before: str | None = None
        pages = 0
        while len(items) < max_count and pages < 20:
            url = f"https://t.me/s/{channel}"
            try:
                r = self.session.get(
                    url, params={"before": before} if before else None,
                    timeout=self.timeout)
                body = r.text
            except Exception as exc:  # noqa: BLE001
                if not items:
                    raise RuntimeError(f"抓取失败（检查代理）：{str(exc)[:150]}") from exc
                break
            if r.status_code != 200:
                if not items:
                    raise RuntimeError(f"频道不可访问：HTTP {r.status_code}"
                                       f"（私有群组或频道名写错）")
                break
            batch, last = self._parse_page(body, channel, video_only)
            if not batch:
                break
            for it in batch:
                if it.item_id in {x.item_id for x in items}:
                    continue
                items.append(it)
                if len(items) >= max_count:
                    break
            if not last:
                break
            before = last.split("/", 1)[-1]
            pages += 1
            time.sleep(0.5)
        return items[:max_count]

    def _parse_page(self, body: str, channel: str,
                    video_only: bool) -> tuple[list[Item], str]:
        out: list[Item] = []
        last_post = ""
        for m in BLOCK_RE.finditer(body):
            post = m.group("post")
            seg = m.group("body")
            last_post = post
            is_video = any(k in seg for k in VIDEO_MARKERS)
            if video_only and not is_video:
                continue
            text = self._extract_text(seg)
            msg_id = post.split("/", 1)[-1]
            out.append(Item(
                platform=self.platform,
                item_id=post.replace("/", "_"),
                title=(text[:60].strip() or f"Telegram 消息 {msg_id}"),
                author=channel,
                author_id=channel,
                url=f"https://t.me/{post}",
                publish_time=self._extract_time(seg),
                desc=text[:2000],
                cover=self._extract_cover(seg),
                status=STATUS_PENDING,
            ))
        return out, last_post

    def fetch_topic(self, keyword: str, max_count: int = 20, **_: Any) -> list[Item]:
        raise RuntimeError(
            "Telegram 没有公开的关键词搜索接口。\n"
            "请用「博主主页」模式填频道名（如 @durov 或 https://t.me/durov），"
            "或用「手动链接」模式直接粘贴 t.me/<频道>/<消息号>。"
        )

    def fetch_comments(self, item_id: str, max_count: int = 50) -> list[Any]:
        # 公开频道的讨论区需要登录 Telegram 账号，网页匿名拿不到
        return []

    # ---------- HTML 抽取 ----------
    @staticmethod
    def _extract_text(seg: str) -> str:
        m = TEXT_RE.search(seg)
        if not m:
            return ""
        raw = m.group("t")
        raw = re.sub(r"<br\s*/?>", "\n", raw)
        raw = TAG_RE.sub("", raw)
        return html_lib.unescape(raw).strip()

    @staticmethod
    def _extract_time(seg: str) -> str:
        m = re.search(r'<time[^>]*datetime="([^"]+)"', seg)
        if not m:
            return ""
        return m.group(1).replace("T", " ").split("+")[0].split(".")[0]

    @staticmethod
    def _extract_cover(seg: str) -> str:
        m = re.search(r"background-image:url\(&#39;([^&]+)&#39;\)", seg)
        if m:
            return m.group(1)
        m = re.search(r"background-image:url\('([^']+)'\)", seg)
        return m.group(1) if m else ""
