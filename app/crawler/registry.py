"""平台注册表：GUI 和 CLI 都从这里拿到平台能力，避免到处写 if。

每个平台声明：
- label：界面显示名
- need_cookie：是否需要 cookie（决定启动时是否弹配置提醒）
- need_proxy：是否需要代理（只有 Telegram 需要）
- modes：支持的采集模式
- input_hint / input_placeholder：输入框提示
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..core.models import MODE_CREATOR, MODE_MANUAL, MODE_TOPIC


@dataclass
class PlatformSpec:
    key: str
    label: str
    need_cookie: bool = False
    need_proxy: bool = False
    modes: tuple[str, ...] = (MODE_CREATOR, MODE_TOPIC, MODE_MANUAL)
    input_hint: str = ""
    placeholder: str = ""
    note: str = ""
    factory: Callable | None = None


def _bilibili(cookie: str = "", proxy: str = "", **_):
    from .bilibili import BilibiliCrawler
    return BilibiliCrawler(cookie=cookie)


def _douyin(cookie: str = "", proxy: str = "", **_):
    from .douyin import DouyinCrawler
    return DouyinCrawler(cookie=cookie)


def _telegram(cookie: str = "", proxy: str = "", **_):
    from .telegram import TelegramCrawler
    return TelegramCrawler(proxy=proxy)


def _local(cookie: str = "", proxy: str = "", **_):
    from .local import LocalSource
    return LocalSource()


def _xiaohongshu(cookie: str = "", proxy: str = "", **_):
    from .xiaohongshu import XiaohongshuCrawler
    return XiaohongshuCrawler(cookie=cookie)


PLATFORMS: dict[str, PlatformSpec] = {
    "bilibili": PlatformSpec(
        key="bilibili",
        label="哔哩哔哩",
        need_cookie=True,
        modes=(MODE_CREATOR, MODE_TOPIC, MODE_MANUAL),
        input_hint="UP主主页链接或 UID，例如 https://space.bilibili.com/946974 或 946974",
        placeholder="https://space.bilibili.com/946974",
        note="列表/搜索/评论都需要登录 cookie；不配 cookie 只能用「手动链接」模式",
        factory=_bilibili,
    ),
    "douyin": PlatformSpec(
        key="douyin",
        label="抖音",
        need_cookie=True,
        modes=(MODE_CREATOR, MODE_MANUAL),
        input_hint="博主主页链接，例如 https://www.douyin.com/user/MS4wLjABAAAA...",
        placeholder="https://www.douyin.com/user/MS4wLjABAAAA",
        note="⚠️ 实测：抖音主题搜索仍被风控拦截（verify_check），仅主页模式可用；"
             "评论/作品接口已带上 a_bogus 签名，可正常抓取",
        factory=_douyin,
    ),
    "telegram": PlatformSpec(
        key="telegram",
        label="Telegram",
        need_cookie=False,
        need_proxy=True,
        modes=(MODE_CREATOR, MODE_MANUAL),
        input_hint="公开频道/群组，例如 @durov 或 https://t.me/durov",
        placeholder="@durov",
        note="⚠️ 国内访问需代理；仅公开频道可用，私有群组请用「手动链接」粘贴 t.me/<频道>/<消息号>",
        factory=_telegram,
    ),
    "xiaohongshu": PlatformSpec(
        key="xiaohongshu",
        label="小红书",
        need_cookie=True,
        modes=(MODE_CREATOR, MODE_TOPIC, MODE_MANUAL),
        input_hint="博主主页链接，例如 https://www.xiaohongshu.com/user/profile/5e55607e0000000001008163",
        placeholder="https://www.xiaohongshu.com/user/profile/5e55607e...",
        note="⚠️ 所有接口都需要 x-s 签名（依赖 xhshow 库）；"
             "短时间高频请求会被临时限流（第 406 号错误），等十几秒即可恢复。"
             "仅视频笔记能转录，图文笔记会自动跳过。",
        factory=_xiaohongshu,
    ),
    "local": PlatformSpec(
        key="local",
        label="本地文件",
        need_cookie=False,
        modes=(MODE_CREATOR,),
        input_hint="选择本地文件夹，或直接填写文件夹路径",
        placeholder=r"D:\videos",
        note="视频/音频格式不限（mp4/mkv/mov/avi/mp3/wav…），跳过下载直接转录",
        factory=_local,
    ),
}


def get(platform: str) -> PlatformSpec:
    if platform not in PLATFORMS:
        raise KeyError(f"未知平台：{platform}，可选：{list(PLATFORMS)}")
    return PLATFORMS[platform]


def build(platform: str, cookie: str = "", proxy: str = ""):
    """构造采集器实例。"""
    spec = get(platform)
    if spec.factory is None:
        raise NotImplementedError(f"{spec.label} 暂未实现")
    return spec.factory(cookie=cookie, proxy=proxy)


def normalize_query(platform: str, text: str) -> str:
    """把用户粘贴的主页链接/UID/频道名统一成接口要的查询值。

    用户填什么都可能：完整 URL、@用户名、纯数字、带 ?from_ssr=1 尾巴……
    这里统一洗干净，避免后端接口拿到脏参数。
    """
    import re

    text = (text or "").strip()
    if not text:
        return ""

    if platform == "bilibili":
        m = re.search(r"space\.bilibili\.com/(\d+)", text)
        if m:
            return m.group(1)
        m = re.search(r"(?:mid|uid)=(\d+)", text)
        if m:
            return m.group(1)
        return text if text.isdigit() else text

    if platform == "douyin":
        from .douyin import DouyinCrawler
        kind, val = DouyinCrawler.resolve_input(text)
        return val if kind == "sec_uid" else text

    if platform == "telegram":
        from .telegram import TelegramCrawler
        kind, val = TelegramCrawler.parse_channel(text)
        return val if kind == "channel" else text

    if platform == "xiaohongshu":
        from .xiaohongshu import XiaohongshuCrawler
        return XiaohongshuCrawler.resolve_input(text)[1]

    return text   # local 直接用目录路径


def platform_list() -> list[PlatformSpec]:
    return list(PLATFORMS.values())


def cookie_platforms() -> list[str]:
    return [k for k, v in PLATFORMS.items() if v.need_cookie]
