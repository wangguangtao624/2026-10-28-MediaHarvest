"""Cookie 管理器：外置存储、多格式解析、连通性校验。

设计原则：
- cookie 绝不写死在代码里，全部放在 data/cookies/<platform>.json
- 同时支持两种粘贴格式：
    1. EditThisCookie 导出的 JSON 数组（推荐，用户正在用这个）
    2. 浏览器 F12 里复制的原始字符串 "name=value; name2=value2"
- 保存后立刻可校验连通性，跑不通就提示重新导出
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DEFAULT_COOKIE_DIR = Path(__file__).resolve().parents[2] / "data" / "cookies"

# 各平台登录必需的关键 cookie，用于快速自检
REQUIRED_KEYS = {
    "bilibili": ["SESSDATA", "bili_jct", "DedeUserID"],
    "douyin": ["sessionid", "passport_csrf_token", "ttwid"],
    # 小红书：a1 + web_session 是登录态的硬性凭据，缺一不可。
    # web_session 失效时服务端返回 code=-100「登录已过期」，只能重新导出。
    "xiaohongshu": ["a1", "web_session"],
}

TUTORIAL = {
    "bilibili": """【B站 Cookie 获取教程】
1. 用 Chrome/Edge 打开 https://www.bilibili.com 并登录
2. 安装扩展 EditThisCookie（Chrome 应用商店搜即可）
3. 地址栏右侧点 EditThisCookie 图标 → 点「导出」按钮（向右的箭头）
4. 全选复制弹出的 JSON，粘贴到本程序即可
5. 点「验证连通性」，看到「已登录：你的昵称」就说明配好了

注意：SESSDATA 有效期约 1 个月，过期后重新导出一次即可。""",
    "douyin": """【抖音 Cookie 获取教程】
1. 用 Chrome/Edge 打开 https://www.douyin.com 并登录（建议扫码登录）
2. 安装扩展 EditThisCookie
3. 点图标 → 「导出」→ 复制 JSON
4. 粘贴到本程序并点「验证连通性」

注意：抖音风控比 B站严格，sessionid / ttwid 缺一不可。
若验证失败，先确认网页端确实是登录状态再重新导出。""",
    "telegram": """【Telegram 说明】
公开频道/群组：直接粘贴 https://t.me/xxx/123 形式的链接即可，无需 cookie。
私有群组：需要 Telegram API 凭据（api_id / api_hash），
到 https://my.telegram.org 申请后填入配置。""",
    "xiaohongshu": """【小红书 Cookie 获取教程】
1. 用 Chrome/Edge 打开 https://www.xiaohongshu.com 并确认已登录
2. 安装扩展 Cookie-Editor（Chrome 应用商店搜即可）或 EditThisCookie
3. 点扩展图标 → 选择「Export」→ 选「Export as JSON」（复制到剪贴板）
4. 粘贴到本程序，点「验证连通性」，看到「已登录：你的昵称」即成功

必备字段：a1 与 web_session —— 这两个是登录态凭据，缺一个都抓不了。

注意：
· 小红书签名（x-s）由程序自动生成，无需你操心。
· 抓取过密会触发风控，web_session 会被服务端作废，返回
  code=-100「登录已过期」。此状态等多久都不会恢复，
  只能回到浏览器重新导出一次 cookie。
· 建议抓取时把「并发」调小，避免触发风控。""",
}


class CookieManager:
    def __init__(self, cookie_dir: str | Path = DEFAULT_COOKIE_DIR) -> None:
        self.cookie_dir = Path(cookie_dir)
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 路径 ----------
    def path_of(self, platform: str) -> Path:
        return self.cookie_dir / f"{platform}.json"

    def exists(self, platform: str) -> bool:
        p = self.path_of(platform)
        return p.exists() and p.stat().st_size > 0

    # ---------- 解析 ----------
    @staticmethod
    def parse(text: str) -> list[dict[str, Any]]:
        """把粘贴内容解析成 EditThisCookie 风格的列表。

        容错处理：兼容带 BOM、单引号、尾部逗号、以及 Cookie 字符串。
        """
        raw = (text or "").strip()
        if not raw:
            return []

        # 情况 1：JSON 数组（EditThisCookie 导出）
        if raw.startswith("["):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # 尝试清理后再解析
                data = json.loads(raw.replace("\t", " ").strip())
            return [c for c in data if isinstance(c, dict) and c.get("name")]

        # 情况 2：纯 Cookie 字符串
        cookies: list[dict[str, Any]] = []
        for part in raw.replace("\n", ";").split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                cookies.append({
                    "domain": "", "name": k, "value": v,
                    "path": "/", "hostOnly": False, "httpOnly": False,
                    "secure": False, "session": False,
                })
        return cookies

    # ---------- 存取 ----------
    def load(self, platform: str) -> list[dict[str, Any]]:
        if not self.exists(platform):
            return []
        try:
            data = json.loads(self.path_of(platform).read_text(encoding="utf-8"))
            return [c for c in data if isinstance(c, dict) and c.get("name")]
        except Exception:  # noqa: BLE001
            return []

    def save(self, platform: str, text: str) -> int:
        """保存粘贴内容。返回保存的 cookie 条数。"""
        cookies = self.parse(text)
        if not cookies:
            raise ValueError("没能解析出任何 cookie，请检查粘贴内容")
        for c in cookies:
            c.setdefault("domain", f".{platform}.com")
            c.setdefault("path", "/")
        self.path_of(platform).write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(cookies)

    def clear(self, platform: str) -> None:
        p = self.path_of(platform)
        if p.exists():
            p.unlink()

    # ---------- 格式转换 ----------
    def to_header(self, platform: str) -> str:
        """转成请求头用的 "k=v; k2=v2"。"""
        return "; ".join(f"{c['name']}={c['value']}"
                         for c in self.load(platform) if c.get("name"))

    def to_netscape(self, platform: str, domain: str | None = None) -> str:
        """转成 Netscape 格式，供 yt-dlp 的 cookiefile 使用。"""
        domain = domain or f".{platform}.com"
        lines = ["# Netscape HTTP Cookie File"]
        for c in self.load(platform):
            name = c.get("name", "")
            if not name:
                continue
            exp = c.get("expirationDate")
            if not exp or exp <= 0:
                exp = int(time.time()) + 86400 * 365
            lines.append("\t".join([
                str(c.get("domain") or domain),
                "TRUE" if not c.get("hostOnly") else "FALSE",
                str(c.get("path") or "/"),
                "TRUE" if c.get("secure") else "FALSE",
                str(int(exp)),
                name,
                str(c.get("value", "")),
            ]))
        return "\n".join(lines) + "\n"

    def write_netscape_file(self, platform: str) -> str:
        """写出临时 Netscape 文件，返回路径。"""
        path = self.cookie_dir / f"_{platform}_netscape.txt"
        path.write_text(self.to_netscape(platform), encoding="utf-8")
        return str(path)

    # ---------- 自检 ----------
    def missing_keys(self, platform: str) -> list[str]:
        """缺少哪些登录必需字段。"""
        names = {c.get("name") for c in self.load(platform)}
        return [k for k in REQUIRED_KEYS.get(platform, []) if k not in names]

    def quick_check(self, platform: str) -> tuple[bool, str]:
        """不联网的快速检查：文件是否存在、关键字段是否齐全。"""
        if not self.exists(platform):
            return False, f"未配置 {platform} 的 cookie"
        missing = self.missing_keys(platform)
        if missing:
            return False, f"缺少关键字段：{', '.join(missing)}（可能未登录，请重新导出）"
        return True, f"已配置 {len(self.load(platform))} 条 cookie"

    @staticmethod
    def tutorial(platform: str) -> str:
        return TUTORIAL.get(platform, "请用 EditThisCookie 导出后粘贴。")
