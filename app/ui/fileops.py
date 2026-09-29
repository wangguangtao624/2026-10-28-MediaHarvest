"""文件/文件夹快速打开与剪贴板小工具。

Windows 上「打开所在文件夹并选中该文件」没有官方 API，只能调 explorer 的
`/select,` 参数。注意参数写法：`/select,` 和路径必须是**两个独立的参数**，
写成 `/select,C:\\a\\b.md` 在路径含空格时会失效。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


def is_windows() -> bool:
    return sys.platform == "win32"


def open_path(path: str | Path) -> None:
    """用系统默认程序打开文件或文件夹。"""
    p = str(Path(path))
    if not Path(p).exists():
        raise FileNotFoundError(f"路径不存在：{p}")
    if is_windows():
        os.startfile(p)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def reveal_path(path: str | Path) -> None:
    """打开所在文件夹，并尽量选中该文件。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{p}")
    if not is_windows():
        open_path(p if p.is_dir() else p.parent)
        return
    if p.is_dir():
        os.startfile(str(p))  # noqa: S606
        return
    subprocess.Popen(["explorer", "/select,", os.path.normpath(str(p))])


def to_file_url(path: str | Path) -> str:
    """转成 Markdown / Qt 都能识别的 file:/// URL。"""
    p = Path(path).resolve()
    return "file:///" + quote(str(p).replace("\\", "/"), safe="/:")


def from_url(url: str) -> str:
    """file:/// 或普通路径 → 本地路径。"""
    if url.startswith("file:"):
        parsed = urlparse(url)
        raw = unquote(parsed.path)
        # Windows 下会得到 /C:/xxx，去掉开头的斜杠
        if is_windows() and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
        return raw
    return unquote(url)
