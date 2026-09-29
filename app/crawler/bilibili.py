"""B站采集适配器。

实测结论（2026-09）：
- 单视频解析：无需 cookie，可用
- UP主空间列表 / 主题搜索 / 评论：B站已强制登录，必须提供 cookie
  未登录时 nav 返回 -101、空间返回 -352、搜索返回 412

因此本适配器在缺少 cookie 时会自动降级到 manual 模式，而不是直接报错。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from functools import reduce
from typing import Any

from ..core.http import HttpClient
from ..core.models import Comment, Item, STATUS_PENDING

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _mixin_key(orig: str) -> str:
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, "")[:32]


def _enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    key = _mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = round(time.time())
    params = dict(sorted(params.items()))
    params = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + key).encode()).hexdigest()
    return params


def bv2av(bvid: str) -> str:
    """BV 号转 AV 号（旧算法）。

    ⚠️ 仅适用于 2023 年以前的老视频：B站 av 号早已突破 int32 上限
    （实测新视频真实 aid=117337885774958，此算法会算出负数）。
    生产环境请用 BilibiliCrawler.resolve_aid()，它会走 view 接口拿真实值。
    """
    table = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
    tr = {c: i for i, c in enumerate(table)}
    s = [11, 10, 3, 8, 4, 6]
    xor = 177451812
    add = 8728348608
    if not (len(bvid) == 12 and bvid[:2] == "BV"):
        return ""
    try:
        r = sum(tr[bvid[s[i]]] * 58 ** i for i in range(6))
        return str((r - add) ^ xor)
    except KeyError:
        return ""


def parse_cookie(text: str) -> dict[str, str]:
    """支持两种输入：原始 Cookie 字符串，或 name=value; name2=value2 形式。"""
    jar: dict[str, str] = {}
    if not text:
        return jar
    for part in text.replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        jar[k.strip()] = v.strip()
    return jar


class BilibiliCrawler:
    platform = "bilibili"

    def __init__(self, cookie: str = "", timeout: int = 15) -> None:
        self.cookie_text = cookie or ""
        self.timeout = timeout
        # 走统一 HTTP 层：curl_cffi 模拟 Chrome 指纹 + no_proxy=* 绕过系统代理
        self.session = HttpClient(
            cookie=self.cookie_text,
            referer="https://www.bilibili.com/",
            timeout=timeout,
        )
        self._wbi_keys: tuple[str, str] | None = None
        self._aid_cache: dict[str, str] = {}

    # ---------- 基础能力 ----------
    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie_text.strip())

    def check_login(self) -> tuple[bool, str]:
        """检测 cookie 是否有效。返回 (是否登录, 用户名或错误信息)。"""
        if not self.has_cookie:
            return False, "未提供 cookie"
        try:
            r = self.session.get(
                "https://api.bilibili.com/x/web-interface/nav", timeout=self.timeout)
            d = r.json()
        except Exception as exc:  # noqa: BLE001
            return False, f"请求失败: {exc}"
        if d.get("code") == 0:
            return True, str((d.get("data") or {}).get("uname", "已登录"))
        return False, f"code={d.get('code')} msg={d.get('message')}"

    def _get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_keys:
            return self._wbi_keys
        r = self.session.get(
            "https://api.bilibili.com/x/web-interface/nav", timeout=self.timeout)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"获取 wbi 密钥失败（需要登录 cookie）：code={d.get('code')} {d.get('message')}")
        wbi = (d.get("data") or {}).get("wbi_img") or {}
        img_key = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
        sub_key = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("wbi 密钥为空")
        self._wbi_keys = (img_key, sub_key)
        return self._wbi_keys

    def _wbi_get(self, url: str, params: dict) -> dict[str, Any]:
        img_key, sub_key = self._get_wbi_keys()
        signed = _enc_wbi(params, img_key, sub_key)
        r = self.session.get(url, params=signed, timeout=self.timeout)
        return r.json()

    # ---------- 采集模式 ----------
    def fetch_creator(self, mid: str, max_count: int = 20) -> list[Item]:
        """抓取指定 UP 主主页的视频列表。需要 cookie。"""
        if not self.has_cookie:
            raise RuntimeError("博主主页模式需要 B站登录 cookie")
        items: list[Item] = []
        page_size = min(30, max(max_count, 1))
        pn = 1
        while len(items) < max_count:
            d = self._wbi_get(
                "https://api.bilibili.com/x/space/wbi/arc/search",
                {"mid": mid, "ps": page_size, "pn": pn},
            )
            if d.get("code") != 0:
                raise RuntimeError(f"空间接口失败: code={d.get('code')} {d.get('message')}")
            vlist = ((d.get("data") or {}).get("list") or {}).get("vlist") or []
            if not vlist:
                break
            for v in vlist:
                items.append(self._vlist_to_item(v))
                if len(items) >= max_count:
                    break
            pn += 1
            time.sleep(0.6)  # 控制频率，避免触发风控
        return items[:max_count]

    def fetch_topic(self, keyword: str, max_count: int = 20,
                    order: str = "totalrank") -> list[Item]:
        """按主题关键词搜索视频。需要 cookie。"""
        if not self.has_cookie:
            raise RuntimeError("主题搜索模式需要 B站登录 cookie")
        items: list[Item] = []
        page = 1
        while len(items) < max_count:
            d = self._wbi_get(
                "https://api.bilibili.com/x/web-interface/wbi/search/type",
                {"search_type": "video", "keyword": keyword, "page": page, "order": order},
            )
            if d.get("code") != 0:
                raise RuntimeError(f"搜索接口失败: code={d.get('code')} {d.get('message')}")
            result = (d.get("data") or {}).get("result") or []
            if not result:
                break
            for r in result:
                # 搜索结果里混有直播/番剧等没有 bvid 的条目，直接跳过
                if not str(r.get("bvid", "")).startswith("BV"):
                    continue
                items.append(self._search_to_item(r))
                if len(items) >= max_count:
                    break
            page += 1
            time.sleep(0.6)
        return items[:max_count]

    def resolve_aid(self, item_id: str) -> str:
        """BV 号 -> 真实 aid。走 view 接口，兼容新版超限 av 号。"""
        if not item_id:
            return ""
        if item_id.isdigit():
            return item_id
        if item_id in self._aid_cache:
            return self._aid_cache[item_id]
        aid = ""
        try:
            d = self.session.get_json(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": item_id},
            )
            if d.get("code") == 0:
                aid = str((d.get("data") or {}).get("aid", "") or "")
        except Exception:  # noqa: BLE001
            aid = ""
        if aid:
            self._aid_cache[item_id] = aid
        return aid

    def fetch_comments(self, item_id: str, max_count: int = 50) -> list[Comment]:
        """抓取视频评论（传 BV 号或 aid 都行）。需要 cookie 才能稳定拿到。"""
        if not self.has_cookie:
            return []
        aid = self.resolve_aid(item_id)
        if not aid:
            return []
        out: list[Comment] = []
        pn = 1
        while len(out) < max_count:
            try:
                # 新接口 x/v2/reply 已废弃，必须走 wbi 签名版，否则返回空
                d = self._wbi_get(
                    "https://api.bilibili.com/x/v2/reply/wbi/main",
                    {"type": 1, "oid": aid, "pn": pn, "ps": 20, "sort": 2},
                )
            except Exception:  # noqa: BLE001
                break
            if d.get("code") != 0:
                break
            replies = (d.get("data") or {}).get("replies") or []
            if not replies:
                break
            for c in replies:
                out.append(Comment(
                    item_id=str(aid),
                    user=(c.get("member") or {}).get("uname", ""),
                    content=(c.get("content") or {}).get("message", ""),
                    like_count=int(c.get("like", 0)),
                    publish_time=_ts_to_str(c.get("ctime", 0)),
                ))
                if len(out) >= max_count:
                    break
            pn += 1
            time.sleep(0.4)
        return out[:max_count]

    # ---------- 转换 ----------
    def _vlist_to_item(self, v: dict) -> Item:
        bvid = str(v.get("bvid", ""))
        return Item(
            platform=self.platform,
            item_id=bvid,
            title=_clean(v.get("title", "")),
            author=str(v.get("author", "")),
            author_id=str(v.get("mid", "")),
            url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
            duration=_dur_to_sec(v.get("length", "")),
            view_count=int(v.get("play", 0) or 0),
            comment_count=int(v.get("video_review", 0) or 0),
            publish_time=_ts_to_str(v.get("created", 0)),
            desc=str(v.get("description", "")),
            cover=("https:" + v["pic"]) if v.get("pic", "").startswith("//") else str(v.get("pic", "")),
            status=STATUS_PENDING,
        )

    def _search_to_item(self, r: dict) -> Item:
        bvid = str(r.get("bvid", ""))
        return Item(
            platform=self.platform,
            item_id=bvid,
            title=_clean(r.get("title", "")),
            author=str(r.get("author", "")),
            author_id=str(r.get("mid", "")),
            url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
            duration=_dur_to_sec(r.get("duration", "")),
            view_count=int(r.get("play", 0) or 0),
            like_count=int(r.get("like", 0) or 0),
            comment_count=int(r.get("video_review", 0) or 0),
            publish_time=_ts_to_str(r.get("pubdate", 0)),
            desc=str(r.get("description", "")),
            cover=("https:" + r["pic"]) if str(r.get("pic", "")).startswith("//") else str(r.get("pic", "")),
            status=STATUS_PENDING,
        )


# ---------- 工具函数 ----------
def _clean(text: Any) -> str:
    """B站标题里带 <em class=...> 高亮标签，需要去掉。"""
    import re
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def _dur_to_sec(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    parts = str(value or "").split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 0
    return 0


def _ts_to_str(ts: Any) -> str:
    try:
        ts = int(ts)
        if ts <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (TypeError, ValueError):
        return ""
