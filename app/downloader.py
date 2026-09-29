"""下载层：基于 yt-dlp。

实测踩坑：本机开着系统代理（Clash），yt-dlp 会读注册表走 127.0.0.1:7897，
访问 B站等国内站点会 502。因此默认显式禁用代理；国外站点（YouTube）可传入代理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# (已下载字节, 总字节)；总字节为 0 表示服务器没给长度，调用方应显示不确定态
ProgressCb = Callable[[int, int], None]
# 返回 True 表示要中止本次下载（用户点了跳过/后退/停止）；暂停在内部阻塞实现
Checkpoint = Callable[[], bool]


class DownloadAborted(RuntimeError):
    """用户主动中止下载（区别于网络失败，不该被标记成 failed）。"""


class Downloader:
    def __init__(self, media_dir: str | Path, proxy: str = "",
                 download_video: bool = False, cookie: str = "",
                 cookie_domain: str = ".bilibili.com") -> None:
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.proxy = proxy
        self.download_video = download_video
        self.cookie = cookie
        # Netscape cookie 文件要求写域名，抖音/B站域名不同
        self.cookie_domain = cookie_domain

    def _opts(self, out_tmpl: str) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "outtmpl": out_tmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            # 关键：不传 proxy 时显式置空，绕过系统代理
            "proxy": self.proxy if self.proxy else "",
            "retries": 3,
            "socket_timeout": 30,
        }
        if self.download_video:
            opts["format"] = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            opts["merge_output_format"] = "mp4"
        else:
            # 只要音频，转录够用，省 90% 流量和磁盘
            opts["format"] = "bestaudio/best"
        if self.cookie:
            opts["cookiefile"] = self._write_cookie_file()
        return opts

    def _write_cookie_file(self) -> str:
        """yt-dlp 的 cookiefile 需要 Netscape 格式。"""
        path = self.media_dir / "_cookies.txt"
        lines = ["# Netscape HTTP Cookie File"]
        for part in self.cookie.replace("\n", ";").split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            lines.append(f"{self.cookie_domain}\tTRUE\t/\tFALSE\t0\t{k.strip()}\t{v.strip()}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def download_direct(self, stream_url: str, item_id: str,
                        referer: str = "",
                        on_progress: ProgressCb | None = None,
                        checkpoint: Checkpoint | None = None) -> str:
        """直连下载（抖音用）：不走 yt-dlp，省掉一次解析。

        抖音的 play_addr 有防盗链，必须带 Referer，否则拿不到内容。
        checkpoint 在每个数据块调用：可在下载中途暂停/跳过。
        """
        from .core.http import HttpClient

        client = HttpClient(cookie=self.cookie, referer=referer or "https://www.douyin.com/")
        r = client.get(stream_url, timeout=60, stream=True)
        if r.status_code != 200:
            raise RuntimeError(f"直连下载失败：HTTP {r.status_code}")

        total = int(r.headers.get("Content-Length") or 0)
        # 从 Content-Type 猜扩展名，猜不出就按 mp4
        ctype = (r.headers.get("Content-Type") or "").lower()
        ext = "mp4"
        for k, v in (("audio/mpeg", "mp3"), ("audio/mp4", "m4a"),
                     ("video/webm", "webm"), ("video/x-flv", "flv")):
            if k in ctype:
                ext = v
                break
        path = self.media_dir / f"{item_id}.{ext}"

        got = 0
        chunks: list[bytes] = []
        for chunk in r.iter_content():
            if checkpoint is not None and checkpoint():
                raise DownloadAborted("下载已被用户中止")
            if not chunk:
                continue
            chunks.append(chunk)
            got += len(chunk)
            if on_progress:
                on_progress(got, total)
        body = b"".join(chunks)
        if len(body) < 1024:
            raise RuntimeError(f"下载内容异常（{len(body)} 字节），可能是防盗链拦截")

        path.write_bytes(body)
        return str(path.resolve())

    def download(self, url: str, item_id: str,
                 on_progress: ProgressCb | None = None,
                 checkpoint: Checkpoint | None = None) -> str:
        """下载并返回本地文件路径。失败抛 RuntimeError。"""
        import yt_dlp

        out_tmpl = str(self.media_dir / f"{item_id}.%(ext)s")
        opts = self._opts(out_tmpl)
        if on_progress or checkpoint:
            def _hook(d: dict) -> None:
                # 每个数据块都会回调一次：先看用户有没有要求中止/暂停
                if checkpoint is not None and checkpoint():
                    raise DownloadAborted("下载已被用户中止")
                if on_progress is None:
                    return
                # downloading 阶段有已下载/总字节；finished 阶段补一次 100%
                if d.get("status") == "downloading":
                    got = d.get("downloaded_bytes") or 0
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    on_progress(got, total)
                elif d.get("status") == "finished":
                    on_progress(1, 1)
            opts["progress_hooks"] = [_hook]
            opts["noprogress"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info is None:
                    raise RuntimeError("无法解析该链接")
                # 多 P / 合集时取真正下载的那个
                if info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        raise RuntimeError("合集为空")
                    info = entries[0]
                return str(Path(ydl.prepare_filename(info)).resolve())
        except DownloadAborted:
            raise
        except yt_dlp.utils.DownloadError as exc:
            # yt-dlp 会把 hook 里抛的异常包进 DownloadError，这里还原出来，
            # 否则「用户点了跳过」会被当成下载失败
            if "已被用户中止" in str(exc):
                raise DownloadAborted("下载已被用户中止") from exc
            raise RuntimeError(f"下载失败: {exc}") from exc
