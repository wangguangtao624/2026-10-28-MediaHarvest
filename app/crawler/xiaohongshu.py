"""小红书（Xiaohongshu / RedNote）采集适配器。

实测结论（2026-09，带网页登录 cookie）
--------------------------------------
网页端所有数据接口都在 `edith.xiaohongshu.com`，且**每个请求都必须带
`x-s` / `x-s-common` / `x-t` 签名头**，否则返回 461「账号存在异常」或
406（空 body）。签名算法是小红书自研的（mnsv2 + 自定义 Base64），
自己实现维护成本极高，所以这里用成熟的开源库 **xhshow** 来签。

✅ 已验证可用的接口
- `/api/sns/web/v2/user/me`            → 登录态自检（拿自己的 user_id）
- `/api/sns/web/v1/user_posted`        → 主页笔记列表（GET，游标翻页）
- `/api/sns/web/v1/feed`               → 单篇笔记详情（POST + x-rap-param）
- `/api/sns/web/v1/search/notes`       → 关键词搜索（POST + x-rap-param）
- `/api/sns/web/v2/comment/page`       → 评论列表（GET，游标翻页）

⚠️ 踩过的坑（改代码前务必读）
1. **签名头必须逐个请求重新生成**，且 `requests.Session` 上叠加的旧签名头
   会覆盖新的 —— 所以每次请求都要 `headers.update(new_sign)`，
   或者干脆不复用 session。这里统一走 `_request()` 一处生成。
2. **`x-rap-param` 只对 feed / search 这两个接口需要**（`x_rap=True`），
   评论接口带上反而 406。
3. **评论接口的参数要精简**：只传 `note_id` + `cursor` + `xsec_token`。
   多传 `image_formats` / `top_comment_id` 会直接 406（服务端把多余参数
   当成签名不匹配）。这是花了很久才试出来的，别再"顺手补齐"参数。
4. **短时间高频请求会被临时限流**（表现同样是 406，冷却十几秒后自愈）。
   所以下面所有翻页都带 `_sleep()` 节流。
5. 笔记列表接口返回的 `data` 可能是 `null`（比如查了不存在的用户），
   必须 `(resp.get("data") or {})` 兜住，否则 AttributeError。
6. **`xsec_token` 是必须的**：主页/搜索返回的每条笔记都附带该字段，
   详情和评论接口都要回传它，否则拿不到内容。它和 note_id 配对使用，
   不能跨笔记复用。

视频地址
--------
`/feed` 返回的 `note_card.video.media.stream.h264[]` 里有 `master_url`，
按 `weight`（越大越清晰）/ `quality_type` 挑一条即可。小红书视频是
**图文笔记和视频笔记共用同一套结构**，`type` 字段区分（normal / video）。
纯图文笔记没有 video 字段，采集时直接跳过并标记原因。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from ..core.http import HttpClient
from ..core.models import Comment, Item
from ..core.urlutil import clean_url

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

API = "https://edith.xiaohongshu.com"

# 主页链接里抠 user_id（24 位十六进制）或 xsec_token
USER_ID_RE = re.compile(r"/user/profile/([0-9a-fA-F]{24})")
NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})")
# 分享短链 xhslink.com/xxxx
SHORT_RE = re.compile(r"(?:xhslink\.com|xiaohongshu\.com/l)/([A-Za-z0-9]+)")
# 分享文案里常带 ?xsec_token=xxx
XSEC_RE = re.compile(r"xsec_token=([A-Za-z0-9_\-=+/]+)")
# 搜索接口要一个 search_id，随便给个十六进制串即可通过格式校验
_SEARCH_ID_ALPHABET = "0123456789abcdef"


def _new_search_id() -> str:
    import random
    return "".join(random.choice(_SEARCH_ID_ALPHABET) for _ in range(16))


class XiaohongshuCrawler:
    """小红书采集器。签名依赖 xhshow，未安装时给出明确指引。"""

    platform = "xiaohongshu"

    def __init__(self, cookie: str = "", timeout: int = 20) -> None:
        self.cookie_text = cookie or ""
        self.timeout = timeout
        self.session = HttpClient(
            cookie=self.cookie_text,
            referer="https://www.xiaohongshu.com/",
            timeout=timeout,
        )
        self.session.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": "https://www.xiaohongshu.com",
        })
        self._signer = None
        # 签名格式：**只允许 xys**。xyw 会让服务端作废会话（伪装成
        # code=-100「登录已过期」），见 _request 的长注释。
        # 这个字段保留是为了以后若服务端强制换格式，只改这一处。
        self._sign_format = "xys"
        self._cookies = self._parse_cookies(self.cookie_text)
        # ⚠️ 关键：HttpClient 只设了个 `Cookie: a=1; b=2` 的原始头，
        # 但小红书要求**实际发送的 cookie 与签名时用的 cookie 完全一致**。
        # 用 requests 的 cookie jar 逐个塞进去，保证两边同源；
        # 原始 Cookie 头去掉，避免和 jar 拼出重复 cookie。
        if self._cookies:
            for k, v in self._cookies.items():
                self.session.session.cookies.set(k, v, domain=".xiaohongshu.com")
            self.session.session.headers.pop("Cookie", None)

    # -------------------------------------------------- cookie / 签名
    @staticmethod
    def _parse_cookies(text: str) -> dict[str, str]:
        """把 cookie 字符串或 JSON 解析成 dict。

        用户从「Cookie-Editor」之类的扩展导出时，多半是 JSON 数组；
        从浏览器 DevTools 复制则是 `a=1; b=2` 形式。两种都要能吃。
        """
        text = (text or "").strip()
        if not text:
            return {}
        import json
        if text.startswith("[") or text.startswith("{"):
            try:
                data = json.loads(text)
            except Exception:  # noqa: BLE001
                data = None
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            if isinstance(data, list):
                out = {}
                for c in data:
                    if isinstance(c, dict) and c.get("name"):
                        out[str(c["name"])] = str(c.get("value", ""))
                return out
        out: dict[str, str] = {}
        for part in text.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _signer_obj(self):
        """惰性构造签名器，缺依赖时抛出带安装指引的错误。"""
        if self._signer is None:
            try:
                from xhshow import Xhshow
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "小红书需要 xhshow 库来生成 x-s 签名，请先安装：\n"
                    "    pip install xhshow\n"
                    f"（原始错误：{exc}）") from exc
            self._signer = Xhshow()
        return self._signer

    @property
    def has_cookie(self) -> bool:
        return bool(self._cookies.get("a1") and self._cookies.get("web_session"))

    def check_login(self) -> tuple[bool, str]:
        """用 /user/me 自检登录态（这是最权威的判断）。"""
        if not self.has_cookie:
            return False, "缺少 a1 / web_session cookie"
        try:
            me = self._get_json("/api/sns/web/v2/user/me")
        except Exception as exc:  # noqa: BLE001
            return False, f"请求失败：{exc}"
        if me.get("code") == 0 and me.get("data"):
            nick = me["data"].get("nickname") or ""
            return True, f"登录态有效（{nick}）"
        # ⚠️ 这里踩过的坑：调试期间高频探测接口，小红书会把 web_session
        # **直接作废**（不是限流等一等就好，是彻底失效）。表现就是
        # code=-100「登录已过期」，而且换 IP、等很久都不会恢复。
        # 所以遇到这个提示时，唯一解法是**重新导出 cookie**，
        # 顺便提醒用户别频繁刷接口。
        if me.get("code") == -100:
            return False, ("登录已过期：小红书已作废当前会话。"
                           "请重新从浏览器导出 cookie（建议用 Cookie-Editor 扩展）。"
                           "另外注意别频繁刷新，高频请求会直接导致会话失效。")
        return False, f"cookie 可能已过期：{me.get('msg') or me.get('code')}"

    @staticmethod
    def _session_dead(data: dict) -> bool:
        """是不是「登录态被作废」。

        ⚠️ -100 是最容易被误判的状态：它**不会**表现为请求失败，而是
        code=-100 + 空数据。用户看到「搜索完成，共 0 条」会以为主题没内容，
        实际是 cookie 已经不能用了。所以任何接口遇到 -100 都要直说。
        """
        return (data or {}).get("code") == -100

    def _send(self, signer, uri: str, params: dict | None,
              payload: dict | None, rap: bool, fmt: str) -> Any:
        """按给定签名格式发一次请求。"""
        if payload is None:
            headers = signer.sign_headers_get(
                uri=uri, cookies=self._cookies, params=params or {},
                x_rap=rap, sign_format=fmt)
        else:
            headers = signer.sign_headers_post(
                uri=uri, cookies=self._cookies, payload=payload or {},
                x_rap=rap, sign_format=fmt)
        # 每次都用新的 headers 覆盖，避免上一轮的 x-s 残留
        self.session.session.headers.update(headers)
        url = API + uri
        if payload is None:
            return self.session.get(url, params=params, timeout=self.timeout)
        body = signer.build_json_body(payload)
        return self.session.session.post(url, data=body, timeout=self.timeout)

    def _request(self, uri: str, *, params: dict | None = None,
                 payload: dict | None = None, rap: bool = False) -> Any:
        """统一发请求入口：签名 → 带上 → 发送。

        ⚠️ 所有接口都必须走这里。散落各处手动签头，迟早出
        「有的请求忘了签名 → 461」的问题。

        ⚠️⚠️ 签名格式**只能用 xys**，绝对不要自动尝试 xyw（血泪教训，2026-09-28）：
        - xys 是验证过的可用格式（凌晨搜索成功过 20 条）。
        - xhshow 文档说「数据接口要 xyw」，我曾据此把默认改成 xyw
          并加了「被拒自动换格式」的逻辑 —— 结果**xyw 签名的请求
          会让服务端直接作废会话**：同一 cookie，xyw 请求一发，
          再用什么格式都是 -100「登录已过期」。
        - 最恶劣的是它伪装成「cookie 失效」，引导用户重导 cookie，
          而新导入的 cookie 又立刻被下一次 xyw 请求刷死，死循环。
        461/406 一律按「限流」处理（退避重试），不再换签名格式赌运气。
        """
        signer = self._signer_obj()
        return self._send(signer, uri, params, payload, rap, self._sign_format)

    def _get_json(self, uri: str, params: dict | None = None) -> dict:
        r = self._request(uri, params=params)
        data = self._as_json(r, uri)
        return self._retry_on_fake_dead(uri, data, params=params)

    def _post_json(self, uri: str, payload: dict, rap: bool = False) -> dict:
        r = self._request(uri, payload=payload, rap=rap)
        data = self._as_json(r, uri)
        return self._retry_on_fake_dead(uri, data, payload=payload, rap=rap)

    def _retry_on_fake_dead(self, uri: str, data: dict, *,
                            params: dict | None = None,
                            payload: dict | None = None,
                            rap: bool = False) -> dict:
        """code=-100 且当前用的**不是 xys** 时，换 xys 重试一次再下结论。

        ⚠️ 保险丝（2026-09-28）：实测 xyw 格式的请求**会让服务端作废
        会话**——同一会话先发 xyw 再发 xys，两次都 -100，会话直接死了
        （19:55 用户刚导入的 cookie 被 GUI 里残留的 xyw 代码当场刷死）。
        所以这里只在「当前格式不是 xys」时才触发换格式重试——
        **默认 xys 的情况下绝不发 xyw 请求**，宁可把误报说成失效，
        也不冒把活会话搞死的风险。
        """
        if (self._sign_format == "xys" or (data or {}).get("code") != -100):
            # 默认 xys 下绝不发别的格式（防把活会话刷死），-100 就是真失效
            return data
        r2 = self._send(self._signer_obj(), uri, params, payload, rap, "xys")
        data2 = self._as_json(r2, uri)
        if (data2 or {}).get("code") != -100:
            self._sign_format = "xys"
            return data2
        return data

    @staticmethod
    def _as_json(resp, uri: str) -> dict:
        """把响应变成 dict，并把已知的异常状态翻成人话。"""
        code = getattr(resp, "status_code", 0)

        # ⚠️ 461 **不是**「账号异常」。实测（2026-09）发现两件事：
        #   1. 小红书用 461 表示「这次请求被挑战/限流」，绝大多数情况是
        #      短时间请求过密，**等一会儿就能恢复**，不是账号被封。
        #   2. 461 的响应体**仍然是合法 JSON**，真实原因在 body 里：
        #        搜索被限流  → {"code":0,"success":true,"data":{}}   ← 空数据
        #        详情拿不到  → {"code":300031,"msg":"当前笔记暂时无法浏览"}
        #   旧代码一看到 461 就抛「账号存在异常」，把 body 里的真实原因
        #   全盖掉了 —— 用户会跑去换 cookie，白折腾一轮。
        #   所以这里先解析 body，按 body 的 code 给具体原因。
        if code == 461:
            body: dict = {}
            try:
                b = resp.json()
                if isinstance(b, dict):
                    body = b
            except Exception:  # noqa: BLE001
                body = {}
            bcode = body.get("code")
            msg = (body.get("msg") or body.get("message") or "").strip()
            # body 给了明确的业务错误码 → 用它，这是真正的原因
            if bcode not in (0, None):
                raise RuntimeError(
                    f"小红书接口返回 {bcode}"
                    f"{'：' + msg if msg else ''}（HTTP 461）")
            # code=0 但没有数据 → 软风控（限流降级），不是账号问题
            raise RuntimeError(
                "小红书拒绝了这次请求（HTTP 461，未给出具体错误码）。"
                "通常是短时间内请求太频繁触发了限流。"
                "请等几分钟再试，并放慢节奏（减少条数、不要反复连续搜索）。"
                "登录态本身是正常的，一般不需要重新导出 cookie。")
        if code == 406:
            raise RuntimeError(
                "小红书接口 406（请求过密或签名参数不匹配）。"
                "等十几秒再试；如果持续出现，多半是登录态需要更新。")
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            raise RuntimeError(
                f"接口 {uri} 返回非 JSON（HTTP {code}）："
                f"{getattr(resp, 'text', '')[:120]}")
        return data if isinstance(data, dict) else {}

    # -------------------------------------------------- 限流退避
    # ⚠️ 实测：461 绝大多数是「短时间请求过密」触发的限流，**等一会就好**。
    # 旧代码遇到 461 直接放弃，一次本来能成功的采集就变成 0 条，
    # 用户只会看到「搜索完成，共 0 条」却不知道为什么。
    _RETRYABLE = ("461", "406", "429", "请求过密", "触发了限流")

    def _is_retryable(self, exc: BaseException) -> bool:
        msg = str(exc)
        return any(k in msg for k in self._RETRYABLE)

    def _request_json(self, uri: str, *, params: dict | None = None,
                      payload: dict | None = None, rap: bool = False,
                      tries: int = 3) -> dict:
        """带退避重试的 JSON 请求。只对限流类错误重试。

        退避间隔 3 → 7 → 15 秒（再叠加随机抖动，避免多任务整齐划一）。
        参数错误、登录过期这类重试多少次都一样，直接抛，不浪费时间。
        """
        import random
        delay = 3.0
        last: BaseException | None = None
        for i in range(max(1, tries)):
            try:
                if payload is None:
                    return self._get_json(uri, params)
                return self._post_json(uri, payload, rap)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if not self._is_retryable(exc) or i >= max(1, tries) - 1:
                    raise
                time.sleep(delay + random.uniform(0, 1.5))
                delay *= 2.2
        raise last if last else RuntimeError(f"请求 {uri} 失败")

    @staticmethod
    def _sleep(sec: float = 0.8) -> None:
        """翻页节流。小红书对高频很敏感（406），宁可慢一点。

        ⚠️ 固定间隔本身就是爬虫特征：服务端看到「每次都恰好 1.2 秒」
        很容易判为脚本。这里叠一点随机抖动，更像真人翻页。
        """
        import random
        time.sleep(sec * random.uniform(0.75, 1.45))

    # -------------------------------------------------- 输入解析
    @staticmethod
    def resolve_input(text: str) -> tuple[str, str]:
        """识别用户粘贴的内容 → (类型, 值)。

        类型：user_id / note_id / short / unknown

        小红书 App「复制链接」给的是一段分享文案，里面是
        `http://xhslink.com/xxxx` 短链；网页版复制则是带
        `?xsec_token=` 的完整 URL。两种都要能处理。
        """
        text = clean_url((text or "").strip())
        m = USER_ID_RE.search(text)
        if m:
            return "user_id", m.group(1)
        m = NOTE_ID_RE.search(text)
        if m:
            return "note_id", m.group(1)
        if SHORT_RE.search(text):
            return "short", text
        if re.fullmatch(r"[0-9a-fA-F]{24}", text):
            # 光给一串 24 位 hex，无法区分是用户还是笔记，按笔记试
            return "note_id", text
        return "unknown", text

    def resolve_short(self, url: str) -> tuple[str, str, str]:
        """跟进短链重定向 → (类型, 值, xsec_token)。"""
        token = ""
        m = XSEC_RE.search(url or "")
        if m:
            token = m.group(1)
        try:
            r = self.session.get(url, timeout=self.timeout)
            final = str(getattr(r, "url", "") or url)
        except Exception:  # noqa: BLE001
            final = url
        kind, val = self.resolve_input(final)
        if not token:
            m2 = XSEC_RE.search(final)
            if m2:
                token = m2.group(1)
        return kind, val, token

    def resolve_user_id(self, text: str) -> str:
        """把任意形态的主页输入解析成 user_id。"""
        kind, val = self.resolve_input(text)
        if kind == "user_id":
            return val
        if kind == "short":
            k2, v2, _ = self.resolve_short(text)
            return v2 if k2 == "user_id" else ""
        return ""

    # -------------------------------------------------- 主页笔记
    def fetch_creator(self, user_id: str, max_count: int = 20) -> list[Item]:
        """拉某个博主主页的笔记列表（游标翻页）。"""
        uid = self.resolve_user_id(user_id) or (user_id or "").strip()
        if not uid:
            return []
        out: list[Item] = []
        cursor = ""
        # 主页接口单页最多 30 条；多翻几页凑够 max_count
        guard = 0
        while len(out) < max_count and guard < 12:
            guard += 1
            # 限流退避重试；第一页失败时把原因抛给上层，别静默返回 0 条
            try:
                data = self._request_json("/api/sns/web/v1/user_posted", params={
                    "num": "30", "cursor": cursor or "", "user_id": uid,
                })
            except Exception:  # noqa: BLE001
                if not out:
                    raise
                break
            if data.get("code") != 0:
                if not out:
                    if self._session_dead(data):
                        raise RuntimeError(
                            "小红书登录态已失效（code -100）：请重新从浏览器导出 "
                            "cookie 再试。注意别频繁反复刷新，高频请求会直接"
                            "导致会话被作废。")
                    raise RuntimeError(
                        f"小红书主页返回 {data.get('code')}"
                        f"：{data.get('msg') or data.get('message') or '未知原因'}")
                break
            d = data.get("data") or {}
            notes = d.get("notes") or []
            if not notes:
                break
            for n in notes:
                it = self._note_to_item(n)
                if it is not None:
                    out.append(it)
                if len(out) >= max_count:
                    break
            if not d.get("has_more"):
                break
            cursor = d.get("cursor") or ""
            if not cursor:
                break
            self._sleep()
        return out[:max_count]

    def fetch_manual(self, urls: list[str]) -> list[Item]:
        """手动链接模式：逐条解析分享链接。"""
        out: list[Item] = []
        for raw in urls:
            raw = (raw or "").strip()
            if not raw:
                continue
            kind, val = self.resolve_input(raw)
            token = ""
            m = XSEC_RE.search(raw)
            if m:
                token = m.group(1)
            if kind == "short":
                kind, val, token = self.resolve_short(raw)
            if kind != "note_id":
                continue
            it = self.fetch_detail(val, token)
            if it is not None:
                out.append(it)
            self._sleep()
        return out

    def enrich_items(self, items: list[Item],
                     on_progress: Callable[[int, int, str], None] | None = None,
                     should_stop: Callable[[], bool] | None = None,
                     ) -> list[Item]:
        """给列表抓来的 Item 补详情（正文 / 图片列表 / 话题标签）。

        ⚠️ 为什么必须补：小红书的**列表接口（搜索/主页）只给标题和统计**，
        正文 desc、图片列表、话题标签**全部只存在于详情接口**。
        不补的话图文作品导出出来就是空壳（实测：搜索结果里
        13/20 条是图文，desc 全是空字符串）。

        对视频作品也一并补 —— 顺带拿到最清晰的 stream_url 和时长，
        后面下载就不用再多请求一次。

        失败不致命：单条补不到就保留列表信息继续，不中断整批。

        失败条数会记在 `self.last_enrich_failed` / `self.last_enrich_error`
        上，上层据此提示用户「有 N 条没取到正文」，而不是默默少数据。
        """
        total = len(items)
        self.last_enrich_failed = 0
        self.last_enrich_error = ""
        for idx, it in enumerate(items, 1):
            if should_stop and should_stop():
                break
            if on_progress:
                on_progress(idx, total, it.title or it.item_id)
            token = getattr(it, "xsec_token", "") or ""
            try:
                full = self.fetch_detail(it.item_id, token)
            except Exception as exc:  # noqa: BLE001
                full = None
                self.last_enrich_error = str(exc)[:120]
            if full is not None:
                # 只覆盖详情里才有的字段，保留列表里已经拿到的（点赞等更稳）
                if full.title:
                    it.title = full.title
                if full.desc:
                    it.desc = full.desc
                if full.author and not it.author:
                    it.author = full.author
                if full.cover:
                    it.cover = full.cover
                if full.publish_time and not it.publish_time:
                    it.publish_time = full.publish_time
                if full.images:
                    it.images = full.images
                    it.image_count = full.image_count
                if full.tags:
                    it.tags = full.tags
                if full.stream_url:
                    it.stream_url = full.stream_url
                if full.duration:
                    it.duration = full.duration
                # 点赞/评论数以详情为准（列表可能是粗略值）
                if full.like_count:
                    it.like_count = full.like_count
                if full.comment_count:
                    it.comment_count = full.comment_count
                it.media_kind = full.media_kind
            else:
                self.last_enrich_failed += 1
            self._sleep()
        return items

    def fetch_topic(self, keyword: str, max_count: int = 20,
                    sort: str = "general", **_ignored: Any) -> list[Item]:
        """关键词搜索。sort: general（综合）/ time_descending（最新）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        out: list[Item] = []
        page = 1
        while len(out) < max_count and page <= 10:
            payload = {
                "keyword": keyword,
                "page": page,
                "page_size": 20,
                "search_id": _new_search_id(),
                "sort": sort,
                "note_type": 0,        # 0=不限，1=视频，2=图文
            }
            try:
                data = self._request_json("/api/sns/web/v1/search/notes",
                                          payload=payload, rap=True)
            except Exception:  # noqa: BLE001
                # ⚠️ 第一页就失败时**必须抛出去**。
                # 旧代码这里直接 break，返回空列表，上层就显示
                # 「搜索完成，共 0 条」——用户会以为是「没搜到这个主题」，
                # 实际是请求被限流/拒绝了，两者应对方式完全不同。
                if not out:
                    raise
                break
            if data.get("code") != 0:
                # 接口给了业务错误码（非限流），说清楚再停
                if not out:
                    if self._session_dead(data):
                        raise RuntimeError(
                            "小红书登录态已失效（code -100）：请重新从浏览器导出 "
                            "cookie 再试。注意别频繁反复搜索，高频请求会直接"
                            "导致会话被作废。")
                    raise RuntimeError(
                        f"小红书搜索返回 {data.get('code')}"
                        f"：{data.get('msg') or data.get('message') or '未知原因'}")
                break
            d = data.get("data") or {}
            items = d.get("items") or []
            if not items:
                break
            for raw in items:
                nc = raw.get("note_card") or raw
                it = self._note_to_item(nc, note_id=raw.get("id"),
                                        token=raw.get("xsec_token"))
                if it is not None:
                    out.append(it)
                if len(out) >= max_count:
                    break
            if not d.get("has_more"):
                break
            page += 1
            self._sleep(1.2)
        return out[:max_count]

    def fetch_detail(self, note_id: str, xsec_token: str = "") -> Item | None:
        """单篇笔记详情（POST + x-rap-param）。"""
        payload = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": "pc_feed",
            "xsec_token": xsec_token or "",
        }
        try:
            data = self._request_json("/api/sns/web/v1/feed", payload=payload,
                                      rap=True, tries=2)
        except Exception:  # noqa: BLE001
            return None
        if data.get("code") != 0:
            # -100 = 会话作废。不记下来的话，enrich_items 会把「整批都没取到
            # 正文」报成「这些图文没有正文」，用户根本想不到是 cookie 的问题。
            if self._session_dead(data):
                self.last_enrich_error = (
                    "登录态已失效（code -100），请重新导出 cookie")
            return None
        items = (data.get("data") or {}).get("items") or []
        if not items:
            return None
        card = items[0].get("note_card") or {}
        return self._card_to_item(card, note_id=note_id, token=xsec_token)

    # -------------------------------------------------- 评论
    def fetch_comments(self, item_id: str, max_count: int = 50,
                       xsec_token: str = "") -> list[Comment]:
        """抓评论（含子评论展开）。

        ⚠️ 参数**只能**有 note_id / cursor / xsec_token 三个，
        多传任何一个都会 406。这是接口的硬约束。
        """
        out: list[Comment] = []
        cursor = ""
        guard = 0
        while len(out) < max_count and guard < 15:
            guard += 1
            try:
                data = self._get_json("/api/sns/web/v2/comment/page", params={
                    "note_id": item_id,
                    "cursor": cursor or "",
                    "xsec_token": xsec_token or "",
                })
            except Exception:  # noqa: BLE001
                break
            if data.get("code") != 0:
                break
            d = data.get("data") or {}
            rows = d.get("comments") or []
            if not rows:
                break
            for c in rows:
                out.append(self._to_comment(c))
                # 子评论已经在响应里一起返回，直接展开（不再单独请求，省风控）
                for sub in (c.get("sub_comments") or []):
                    if len(out) >= max_count:
                        break
                    sc = self._to_comment(sub)
                    sc.parent_id = str(c.get("id") or "")
                    out.append(sc)
                if len(out) >= max_count:
                    break
            if not d.get("has_more"):
                break
            cursor = d.get("cursor") or ""
            if not cursor:
                break
            self._sleep(1.0)
        return out[:max_count]

    def _to_comment(self, c: dict) -> Comment:
        user = c.get("user_info") or {}
        inter = c.get("interact_info") or {}
        return Comment(
            item_id=str(c.get("id") or ""),
            user=(user.get("nickname") or user.get("nick_name") or ""),
            content=(c.get("content") or "").strip(),
            like_count=self._num(inter.get("liked_count")),
            publish_time=self._ts(c.get("create_time")),
        )

    @staticmethod
    def _num(v: Any) -> int:
        """把小红书返回的计数转成 int。

        ⚠️ 这个接口的计数**不一定是数字**！实测会有这些形态：
            1234          int
            "1234"        纯数字字符串
            "7.6万"       中文「万」后缀（用户量大的笔记就是这种）
            "1.2亿"       「亿」后缀
            ""            None          → 0
            1234.0        float
        直接用 int() 会抛 ValueError: invalid literal for int()
        （"7.6万"），整个详情解析就挂了。所以统一走这里。
        """
        if v is None:
            return 0
        if isinstance(v, bool):
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).strip().replace(",", "").replace("+", "")
        if not s:
            return 0
        mult = 1.0
        for suffix, m in (("亿", 1e8), ("万", 1e4), ("w", 1e4), ("k", 1e3)):
            if s.endswith(suffix):
                mult = m
                s = s[: -len(suffix)]
                break
        try:
            return int(float(s) * mult)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _ts(v: Any) -> str:
        """小红书时间戳是毫秒。转成 'YYYY-MM-DD HH:MM'。"""
        try:
            n = int(v)
        except (TypeError, ValueError):
            return ""
        if n > 10 ** 12:      # 毫秒
            n //= 1000
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(n))
        except (ValueError, OSError):
            return ""

    # -------------------------------------------------- 结构转换
    def _note_to_item(self, n: dict, note_id: str | None = None,
                      token: str | None = None) -> Item | None:
        """列表里的简版笔记 → Item。

        列表接口只给了标题、封面、作者、时间，**没有正文和视频地址**；
        真正的详情要靠 /feed。这里先把壳搭起来，等真正下载时再补详情。
        """
        nid = note_id or n.get("note_id") or n.get("id") or ""
        if not nid:
            return None
        user = n.get("user") or {}
        inter = n.get("interact_info") or {}
        title = (n.get("display_title") or n.get("title") or "").strip()
        if not title:
            title = (n.get("desc") or "").strip()[:60]
        item = Item(
            platform="xiaohongshu",
            item_id=str(nid),
            title=title or str(nid),
            author=(user.get("nickname") or user.get("nick_name") or ""),
            duration=0,
            url=f"https://www.xiaohongshu.com/explore/{nid}",
            like_count=self._num(inter.get("liked_count")),
            comment_count=self._num(inter.get("comment_count")),
            publish_time=self._ts(n.get("time")),
            media_kind=("video" if (n.get("type") == "video") else "image"),
        )
        # 列表接口不给正文/视频地址，但给了评论必需的 xsec_token，先带上
        item.xsec_token = token or n.get("xsec_token") or ""
        return item

    def _card_to_item(self, card: dict, note_id: str = "",
                      token: str = "") -> Item | None:
        """详情 note_card → Item（这里才有正文和视频地址）。"""
        nid = card.get("note_id") or note_id
        if not nid:
            return None
        user = card.get("user") or {}
        inter = card.get("interact_info") or {}
        title = (card.get("title") or "").strip()
        desc = (card.get("desc") or "").strip()
        if not title:
            title = desc[:60] or str(nid)
        ntype = card.get("type") or "normal"

        item = Item(
            platform="xiaohongshu",
            item_id=str(nid),
            title=title,
            author=(user.get("nickname") or user.get("nick_name") or ""),
            duration=self._num((card.get("video") or {}).get("media", {})
                               .get("video", {}).get("duration")),
            url=f"https://www.xiaohongshu.com/explore/{nid}",
            desc=desc,
            like_count=self._num(inter.get("liked_count")),
            comment_count=self._num(inter.get("comment_count")),
            publish_time=self._ts(card.get("time")),
            media_kind=("video" if ntype == "video" else "image"),
        )
        # ⚠️ 两个不同的东西要一起带出去，别塞进同一个字段：
        #   stream_url  = 真正的视频地址（下载器直接用，省一次详情请求）
        #   xsec_token  = 评论接口必须回传的凭证，模型里没这字段，
        #                 挂成动态属性（dataclass 允许 setattr）。
        item.xsec_token = token or ""
        # ---- 图文作品：把图片列表和原生话题标签一起抓下来 ----
        # 小红书的图集正文**只存在于详情接口**，列表接口完全不给，
        # 所以必须在这里落库，否则导出时什么都拿不到。
        imgs = self._collect_images(card)
        item.images = json.dumps(imgs, ensure_ascii=False) if imgs else ""
        item.image_count = len(imgs)
        item.tags = self._collect_tags(card)
        item.cover = self._first_image(card)
        if ntype == "video":
            item.stream_url = self._pick_stream(card) or ""
        return item

    @staticmethod
    def _collect_images(card: dict) -> list[dict]:
        """收集图集里的每一张图（URL + 宽高）。

        实测结构（2026-09）：
            image_list: [
              {"file_id": "...", "width": "1440", "height": "1920",
               "url_default": "http://sns-webpic-qc.xhscdn.com/...",
               "url_pre": "...", "url": "", "stream": "{}",
               "live_photo": "False", "info_list": [...]},
              ...
            ]
        ⚠️ 注意 url 字段经常是**空串**，真正能用的是 url_default。
        url_pre 是预览小图，只在 url_default 缺失时兜底。
        """
        out: list[dict] = []
        seen: set[str] = set()
        for im in (card.get("image_list") or []):
            u = ""
            w = h = 0
            if isinstance(im, dict):
                for k in ("url_default", "url", "url_pre"):
                    v = im.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        u = v
                        break
                # 有些图 url_default 也没有，藏在 info_list 里
                if not u:
                    for info in (im.get("info_list") or []):
                        if isinstance(info, dict):
                            v = info.get("url")
                            if isinstance(v, str) and v.startswith("http"):
                                u = v
                                break
                w = XiaohongshuCrawler._num(im.get("width"))
                h = XiaohongshuCrawler._num(im.get("height"))
            elif isinstance(im, str) and im.startswith("http"):
                u = im
            if not u or u in seen:
                continue
            seen.add(u)
            out.append({"url": u, "w": w, "h": h})
        return out

    @staticmethod
    def _collect_tags(card: dict) -> str:
        """收集平台原生的话题标签（#红烧肉[话题]# 这种）。

        tag_list 里每个元素形如 {"id": "...", "name": "红烧肉", "type": "topic"}。
        这些标签是作者自己打的，比关键词抽取准得多，单独存一列。
        """
        names: list[str] = []
        for t in (card.get("tag_list") or []):
            if isinstance(t, dict):
                nm = (t.get("name") or "").strip()
                if nm:
                    names.append(nm)
            elif isinstance(t, str) and t.strip():
                names.append(t.strip())
        # 去重保序
        return ",".join(dict.fromkeys(names))

    @staticmethod
    def _first_image(card: dict) -> str:
        """取封面图地址。同样要防 image_list 元素是字符串的情况。"""
        imgs = card.get("image_list") or []
        if not imgs:
            return ""
        first = imgs[0]
        if isinstance(first, dict):
            for k in ("url_default", "url", "url_pre", "url_default_webp"):
                u = first.get(k)
                if isinstance(u, str) and u:
                    return u
            return ""
        if isinstance(first, str) and first.startswith("http"):
            return first
        return ""

    @staticmethod
    def _pick_stream(card: dict) -> str:
        """从 media.stream 里挑最清晰的视频地址。

        ⚠️ 实测（2026-09）真实结构长这样：
            video.media.stream = {"EF4":[...], "EF5":[...], "EF6":[], "EF7":[]}
        编码档位 key **不是** h264/h265/av1，而是 EF4~EF7 这种名字，
        而且不同笔记的档位集合会变。所以**不能写死 key 列表**，
        必须遍历所有 key。每个元素是 dict，带 weight（越大越清晰）
        + master_url。
        （历史上还遇到过元素直接是 URL 字符串的情况，一并兼容。）

        另外 `video.media_v2` 是一个 **JSON 字符串**（不是 dict！），
        旧代码对它调 .get() 会抛 'str' object has no attribute 'get'，
        导致整个详情解析崩溃。所以要 json.loads 后再用。
        """
        v = card.get("video") or {}
        media = v.get("media") or {}
        stream = media.get("stream") or {}

        best, best_w = "", -1
        # 遍历所有档位（EF4/EF5/... 或 h264/h265/av1 都可能出现）
        for arr in (stream.values() if isinstance(stream, dict) else []):
            if not isinstance(arr, list):
                continue
            for s in arr:
                # 形态 1：纯字符串 URL —— 没有 weight，最低优先级
                if isinstance(s, str):
                    if s.startswith("http") and best_w < 0:
                        best, best_w = s, 0
                    continue
                if not isinstance(s, dict):
                    continue
                # 形态 2：正常 dict
                url = s.get("master_url") or s.get("url") or ""
                if not url:
                    continue
                w = XiaohongshuCrawler._num(s.get("weight"))
                if w > best_w:
                    best, best_w = url, w
        if best:
            return best

        # 兜底：media_v2（可能是 JSON 字符串，也可能是 dict）
        mv2 = v.get("media_v2")
        if isinstance(mv2, str) and mv2.strip():
            try:
                mv2 = json.loads(mv2)
            except (ValueError, TypeError):
                mv2 = {}
        if not isinstance(mv2, dict):
            return ""
        inner = mv2.get("stream") or {}
        if isinstance(inner, dict):
            for arr in inner.values():
                if not isinstance(arr, list):
                    continue
                for s in arr:
                    if isinstance(s, dict) and s.get("master_url"):
                        return s["master_url"]
                    if isinstance(s, str) and s.startswith("http"):
                        return s
        for key in ("video", "origin_video", "backup_video"):
            x = mv2.get(key) or {}
            if isinstance(x, str):
                if x.startswith("http"):
                    return x
                continue
            if isinstance(x, dict):
                u = x.get("master_url") or x.get("url") or ""
                if u:
                    return u
        return ""
