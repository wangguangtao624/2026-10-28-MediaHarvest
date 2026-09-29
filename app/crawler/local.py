"""本地文件适配器：把硬盘上已有的音视频变成可勾选的 Item。

对应需求「快速提取下载视频里的文案，视频格式不限」——
这一路不需要下载，直接把本地文件喂给 faster-whisper。

faster-whisper 底层用 PyAV 解码，理论上 ffmpeg 能解的格式它都能解。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..core.models import Item, STATUS_PENDING

VIDEO_EXT = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v",
    ".ts", ".mpg", ".mpeg", ".3gp", ".ogv", ".vob", ".rmvb", ".asf",
}
AUDIO_EXT = {
    ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wma", ".aiff",
}
ALL_EXT = VIDEO_EXT | AUDIO_EXT

# 单文件上限，超过就跳过（默认 4GB）
DEFAULT_MAX_BYTES = 4 * 1024 ** 3


def is_media(path: str | Path) -> bool:
    return Path(path).suffix.lower() in ALL_EXT


def scan_dir(directory: str | Path, recursive: bool = True,
             max_bytes: int = DEFAULT_MAX_BYTES) -> list[Path]:
    """扫描目录里的音视频文件，按文件名排序。"""
    root = Path(directory)
    if not root.is_dir():
        return []
    it = root.rglob("*") if recursive else root.glob("*")
    out: list[Path] = []
    for p in it:
        try:
            if not p.is_file() or p.suffix.lower() not in ALL_EXT:
                continue
            if p.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out, key=lambda x: x.name.lower())


def file_item(path: str | Path) -> Item:
    """把单个文件包装成 Item。"""
    p = Path(path)
    # item_id 用路径哈希，保证同一文件重复导入不会撞主键
    digest = hashlib.md5(str(p.resolve()).encode("utf-8")).hexdigest()[:12]
    try:
        size_mb = p.stat().st_size / 1024 / 1024
    except OSError:
        size_mb = 0.0
    return Item(
        platform="local",
        item_id=f"L{digest}",
        title=p.stem[:100] or p.name,
        author="本地文件",
        author_id=str(p.parent),
        url=str(p.resolve()),
        desc=f"{p.name}（{size_mb:.1f} MB）",
        local_path=str(p.resolve()),   # 本地文件本身就是「已下载」状态
        cover="",
        status=STATUS_PENDING,
    )


def build_items(paths: list[str | Path]) -> list[Item]:
    """把一批路径（文件或目录）统一变成 Item 列表。"""
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(scan_dir(p))
        elif p.is_file() and is_media(p):
            files.append(p)
    # 去重（同一路径只出现一次）
    seen: set[str] = set()
    out: list[Item] = []
    for f in files:
        key = str(f.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(file_item(f))
    return out


class LocalSource:
    """统一接口，让 pipeline 不用特判本地文件。"""

    platform = "local"

    def __init__(self) -> None:
        pass

    @property
    def has_cookie(self) -> bool:
        return True   # 本地文件不需要 cookie

    def check_login(self) -> tuple[bool, str]:
        return True, "本地文件，无需登录"

    def fetch_creator(self, directory: str, max_count: int = 50, **_: object) -> list[Item]:
        return build_items([directory])[:max_count]

    def fetch_topic(self, keyword: str, max_count: int = 20, **_: object) -> list[Item]:
        raise RuntimeError("本地文件模式不支持主题搜索，请选择文件夹或直接拖入文件")

    def fetch_comments(self, item_id: str, max_count: int = 50) -> list[object]:
        return []

    @staticmethod
    def size_of(path: str) -> str:
        try:
            mb = os.path.getsize(path) / 1024 / 1024
        except OSError:
            return "未知"
        return f"{mb:.1f} MB" if mb < 1024 else f"{mb/1024:.2f} GB"
