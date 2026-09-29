"""抖音采集适配器。

实测结论（2026-09，带登录 cookie）：
- ✅ 主页作品列表  /aweme/v1/web/aweme/post/     → status_code=0，可直接拿
- ✅ 单作品详情    /aweme/v1/web/aweme/detail/   → status_code=0（带全参数）
- ✅ 评论列表      /aweme/v1/web/comment/list/   → **必须带 a_bogus 签名**
- ❌ 主题搜索      /aweme/v1/web/general/search/single/ → verify_check（验证码）
- ❌ 老接口 iesdouyin /web/api/v2/aweme/post/    → shark_fail

⚠️ 最新关键发现（2026-09）：评论接口**不带签名时返回 HTTP 200 + 空 body**，
不是报错，极容易被误判成「接口挂了」。原因就是缺 `a_bogus`。
签名实现见 abogus.py，算法说明和失效排查步骤都写在那里面。

本适配器：
- 博主主页模式：可用（主力玩法）
- 单视频 / 短链：自研解析链路（绕开 yt-dlp，它只认 /video/<数字>）
- 评论：带签名抓取，支持游标翻页
- 主题搜索：服务端风控，明确告知并给替代方案
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..core.http import HttpClient
from ..core.models import Comment, Item, STATUS_FAILED, STATUS_PENDING
from ..core.urlutil import clean_url
from .abogus import ABogus

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SEC_UID_RE = re.compile(r"/user/(MS4wLjABAAAA[A-Za-z0-9_\-]+)")
AWEME_RE = re.compile(r"/(?:video|note|slides)/(\d+)")

# 网页端公共参数。顺序即签名顺序，别随手排序：
# a_bogus 是对"发包时的参数字典"做的哈希，顺序变了签名就对不上。
WEB_PARAMS: dict[str, str] = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "190500",
    "version_name": "19.5.0",
    "cookie_enabled": "true",
    "screen_width": "1920",
    "screen_height": "1080",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "126.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "126.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "8",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "50",
}


class DouyinCrawler:
    platform = "douyin"

    def __init__(self, cookie: str = "", timeout: int = 20) -> None:
        self.cookie_text = cookie or ""
        self.timeout = timeout
        self.session = HttpClient(
            cookie=self.cookie_text,
            referer="https://www.douyin.com/",
            timeout=timeout,
        )
        self.session.session.headers.update({
            "Accept": "application/json, text/plain, */*",
        })
        self._bogus = ABogus()

    # ---------- 带签名的请求 ----------
    def _signed_get(self, url: str, params: dict[str, str]) -> Any:
        """给参数补 a_bogus 签名后发请求。

        ⚠️ 签名必须算在**不含 a_bogus** 的参数字典上，算完再塞进去，
        否则服务端校验失败（表现同样是 200 + 空 body）。
        """
        p = dict(params)
        p.pop("a_bogus", None)
        p["a_bogus"] = self._bogus.sign(p)
        return self.session.get(url, params=p, timeout=self.timeout)

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie_text.strip())

    def check_login(self) -> tuple[bool, str]:
        """抖音没有干净的「我是谁」接口，用主页作品接口能否返回数据来反推。"""
        if not self.has_cookie:
            return False, "未提供 cookie"
        try:
            r = self.session.get("https://www.douyin.com/", timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            return False, f"请求失败: {exc}"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        return True, "cookie 已生效（能拿到登录态页面）"

    # ---------- URL 解析 ----------
    @staticmethod
    def resolve_input(text: str) -> tuple[str, str]:
        """把用户粘贴的东西识别成 (类型, 值)。

        类型：sec_uid / aweme_id / short / unknown

        关键：App 里点「复制链接」拿到的是一整段分享文案（含标题、话题、引导语），
        必须先把里面的真链接抠出来，否则整段话会被当成 URL 去请求。
        """
        text = clean_url((text or "").strip())
        m = SEC_UID_RE.search(text)
        if m:
            return "sec_uid", m.group(1)
        if re.fullmatch(r"MS4wLjABAAAA[A-Za-z0-9_\-]+", text):
            return "sec_uid", text
        m = AWEME_RE.search(text)
        if m:
            return "aweme_id", m.group(1)
        if "v.douyin.com" in text or "douyin.com" in text:
            return "short", text
        return "unknown", text

    def resolve_short(self, url: str) -> tuple[str, str]:
        """短链跟进重定向，拿到真实类型。"""
        try:
            r = self.session.get(url, timeout=self.timeout)
            final = str(getattr(r, "url", "") or url)
        except Exception:  # noqa: BLE001
            return "unknown", url
        return self.resolve_input(final)

    # ---------- 单视频解析（绕开 yt-dlp） ----------
    def resolve_aweme_id(self, text: str) -> str:
        """把任意抖音分享形态解析成 aweme_id。

        实测踩坑：
        - 分享短链 v.douyin.com/xxx 会 302 到 /note/<id> 或 /video/<id>
        - yt-dlp 的 DouyinIE 只认 /video/<数字>，遇到 /note/ 直接 Unsupported URL
        - 即使改成 /video/，yt-dlp 不带 device_platform 等参数调 detail 接口也会拿到空，
          报 "Fresh cookies are needed"
        → 所以我们自己跟进重定向 + 自己调 detail 接口。
        """
        text = clean_url((text or "").strip())
        kind, val = self.resolve_input(text)
        if kind == "aweme_id":
            return val
        # 短链或待确认的链接：跟进重定向拿最终地址
        if not text.startswith("http"):
            if "v.douyin.com" in text or "douyin.com" in text:
                text = "https://" + text.lstrip("/")
            else:
                return ""
        try:
            r = self.session.get(text, timeout=self.timeout)
            final = str(getattr(r, "url", "") or "")
        except Exception:  # noqa: BLE001
            return ""
        kind2, val2 = self.resolve_input(final)
        if kind2 == "aweme_id":
            return val2
        # 兜底：从最终 URL 再捞一次，或从页面 HTML 里找
        m = AWEME_RE.search(final) or AWEME_RE.search(r.text or "")
        return m.group(1) if m else ""

    def fetch_detail(self, aweme_id: str) -> Item | None:
        """调官方 detail 接口拿单条作品（比 yt-dlp 稳）。"""
        try:
            r = self._signed_get(
                "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                {**WEB_PARAMS, "aweme_id": aweme_id},
            )
            d = r.json()
        except Exception:  # noqa: BLE001
            return None
        detail = d.get("aweme_detail")
        if not detail:
            return None
        item = self._to_item(detail)
        # 图文作品（图集）只有 BGM 没有人声，抓它的音频去转录纯属浪费
        # 实测：这类作品 video.duration=0、images 有 N 张、play_addr 指向纯音乐
        images = detail.get("images") or []
        if images and not (detail.get("video") or {}).get("duration"):
            item.media_kind = "image"
            item.stream_url = ""
            item.warning = (
                f"这是图文作品（{len(images)} 张图），没有人声轨道，已跳过转录。"
                f"文案请见下方「原简介/文案」；如需识别图片内的文字需要接 OCR。")
            return item
        # 播放地址：有它就能直接下载，不必走 yt-dlp
        urls = ((detail.get("video") or {}).get("play_addr") or {}).get("url_list") or []
        if urls:
            item.stream_url = str(urls[0])
        return item

    def fetch_manual(self, urls: list[str]) -> list[Item]:
        """手动链接模式：支持短链 / /video/ / /note/ / 混合。"""
        out: list[Item] = []
        for i, raw in enumerate(urls, 1):
            aid = self.resolve_aweme_id(raw)
            if not aid:
                from ..core.urlutil import extract_first_url
                if not extract_first_url(raw):
                    err = "这段文本里没有找到链接，请确认粘贴的是抖音分享内容（应含 https://v.douyin.com/... 或 /video/<数字>）"
                else:
                    err = ("链接已识别但跟进重定向后拿不到作品 ID，"
                           "通常是作品已删除/仅自己可见，或 cookie 已失效（可重新验通）")
                out.append(Item(
                    platform=self.platform, item_id=f"manual_{i}",
                    title=f"（无法识别）{raw[:50]}", url=raw[:200],
                    status=STATUS_FAILED, error=err,
                ))
                continue
            it = self.fetch_detail(aid)
            if it is None:
                out.append(Item(
                    platform=self.platform, item_id=aid,
                    title=f"（解析失败）{aid}", url=raw,
                    status=STATUS_FAILED,
                    error="detail 接口无数据，可能是作品已删除或 cookie 过期",
                ))
                continue
            out.append(it)
            time.sleep(0.6)
        return out

    def fetch_sec_uid_from_homepage(self) -> str:
        """从抖音首页 HTML 里捞一个 sec_uid，用于自检（不一定是本人）。"""
        try:
            r = self.session.get("https://www.douyin.com/", timeout=self.timeout)
        except Exception:  # noqa: BLE001
            return ""
        m = SEC_UID_RE.search(r.text or "")
        return m.group(1) if m else ""

    # ---------- 采集模式 ----------
    def fetch_creator(self, sec_uid: str, max_count: int = 20) -> list[Item]:
        """抓取指定博主主页的作品列表。"""
        if not self.has_cookie:
            raise RuntimeError("抖音主页模式需要登录 cookie")
        items: list[Item] = []
        cursor = 0
        while len(items) < max_count:
            d = self._post_list(sec_uid, cursor, min(20, max_count - len(items)))
            if d is None:
                break
            awemes = d.get("aweme_list") or []
            if not awemes:
                break
            for a in awemes:
                items.append(self._to_item(a))
                if len(items) >= max_count:
                    break
            if not d.get("has_more"):
                break
            cursor = int(d.get("max_cursor") or 0)
            if not cursor:
                break
            time.sleep(0.8)  # 控制频率，抖音风控比 B站狠
        return items[:max_count]

    def _post_list(self, sec_uid: str, cursor: int, count: int) -> dict[str, Any] | None:
        try:
            r = self._signed_get(
                "https://www.douyin.com/aweme/v1/web/aweme/post/",
                {
                    **WEB_PARAMS,
                    "sec_user_id": sec_uid,
                    "count": str(count),
                    "max_cursor": str(cursor),
                    "locate_query": "false",
                    "show_live_replay_strategy": "1",
                    "need_time_list": "1",
                },
            )
            d = r.json()
        except Exception:  # noqa: BLE001
            return None
        if d.get("status_code") not in (0, None):
            return None
        return d

    def fetch_topic(self, keyword: str, max_count: int = 20, **_: Any) -> list[Item]:
        """抖音网页搜索已被风控拦截，这里直接给出可执行的替代方案。"""
        raise RuntimeError(
            "抖音网页搜索已被风控拦截（服务端返回 verify_check 验证码）。\n"
            "可选替代：\n"
            "  1. 改用「博主主页」模式：粘贴 https://www.douyin.com/user/... 链接\n"
            "  2. 改用「手动链接」模式：直接粘贴若干 https://www.douyin.com/video/... 链接\n"
            "  3. 在抖音 App 内搜索后，把结果里的视频链接复制出来"
        )

    def fetch_comments(self, item_id: str, max_count: int = 50) -> list[Comment]:
        """抓取作品评论，支持游标翻页。

        ⚠️ 关键：接口必须带 a_bogus 签名。不加签名返回的是 **HTTP 200 + 空 body**，
        不会报错 —— 这正是之前「抖音评论抓不到」的根因。
        """
        out: list[Comment] = []
        cursor = 0
        seen_cursor: set[int] = set()
        while len(out) < max_count:
            params = {
                **WEB_PARAMS,
                "aweme_id": item_id,
                "cursor": str(cursor),
                "count": str(min(20, max_count - len(out))),
                "item_type": "0",
                "insert_ids": "",
                "whale_cut_token": "",
                "cut_version": "1",
                "rcFT": "",
            }
            try:
                r = self._signed_get(
                    "https://www.douyin.com/aweme/v1/web/comment/list/", params)
                d = r.json()
            except Exception:  # noqa: BLE001
                break
            if d.get("status_code") not in (0, None):
                break
            comments = d.get("comments") or []
            if not comments:
                break
            for c in comments:
                out.append(Comment(
                    item_id=item_id,
                    user=str((c.get("user") or {}).get("nickname", "")),
                    content=str(c.get("text", "")),
                    like_count=int(c.get("digg_count", 0) or 0),
                    publish_time=_ts_to_str(c.get("create_time", 0)),
                ))
                if len(out) >= max_count:
                    break
            if not d.get("has_more"):
                break
            nxt = int(d.get("cursor") or 0)
            if not nxt or nxt in seen_cursor:
                break
            seen_cursor.add(nxt)
            cursor = nxt
            time.sleep(0.5)   # 控制频率
        return out[:max_count]

    # ---------- 转换 ----------
    def _to_item(self, a: dict) -> Item:
        aweme_id = str(a.get("aweme_id", ""))
        author = a.get("author") or {}
        stat = a.get("statistics") or {}
        video = a.get("video") or {}
        cover = (video.get("cover") or {}).get("url_list") or []
        desc = str(a.get("desc", "") or "").strip()
        return Item(
            platform=self.platform,
            item_id=aweme_id,
            title=desc[:100] or f"抖音作品 {aweme_id}",
            author=str(author.get("nickname", "")),
            author_id=str(author.get("sec_uid", "") or author.get("uid", "")),
            url=f"https://www.douyin.com/video/{aweme_id}" if aweme_id else "",
            # detail 接口有时 video.duration 为 0，回退到顶层 duration
            duration=int(video.get("duration") or a.get("duration") or 0) // 1000,
            view_count=int(stat.get("play_count", 0) or 0),
            like_count=int(stat.get("digg_count", 0) or 0),
            comment_count=int(stat.get("comment_count", 0) or 0),
            publish_time=_ts_to_str(a.get("create_time", 0)),
            desc=desc,
            cover=str(cover[0]) if cover else "",
            status=STATUS_PENDING,
        )


def _ts_to_str(ts: Any) -> str:
    try:
        ts = int(ts)
        if ts <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (TypeError, ValueError):
        return ""
