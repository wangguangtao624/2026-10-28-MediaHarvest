"""知识库（Knowledge Base）：把选中的文稿一键归档到指定目录。

用户需求原话
------------
「在结果文档或文件模块，支持多选生成的逐字稿或文件（包括历史转录记录），
右键菜单中增加『添加到知识库』选项。支持设置知识库目录，点击后一键将
选中的文件复制到对应知识库的文件夹中。」

为什么是「复制」而不是「移动」
------------------------------
中间文件目录可能被「一键清除」清空，文稿目录也可能被用户手工整理。
知识库是**长期归档**，源目录是**工作区**——移动会让工作区出现空洞，
再想重新导出或重新转录就找不到源了。所以一律复制（`copy2`，保留时间戳）。

目录组织
--------
知识库不做复杂分类，只按「平台/作者」两层分子目录：

    <知识库>/bilibili/某某UP主/视频标题.md
    <知识库>/douyin/某博主/另一条.md

理由：Obsidian / 本地搜索都靠文件名和 frontmatter 的 tags 做检索，
目录再深反而不好找。平台+作者是「想找某个人的东西」时最自然的入口。

重名处理
--------
同一个作者下可能有同名视频（或同一视频的不同版本）。直接覆盖会丢数据，
所以遇到已存在文件时加 ` (2)`、` (3)` 后缀。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core.models import Item


@dataclass
class CopyResult:
    """一次归档的结果。"""
    copied: list[tuple[str, str]]      # [(源路径, 目标路径)]
    skipped: list[tuple[str, str]]     # [(源路径, 原因)]
    dest_root: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.copied)

    def summary(self) -> str:
        n = len(self.copied)
        s = f"已归档 {n} 个文件"
        if self.skipped:
            s += f"，{len(self.skipped)} 个跳过"
        return s


def _safe_segment(name: str, fallback: str = "未分类") -> str:
    """目录名安全化：去掉 Windows 非法字符并限长。

    只用于**目录名**（平台 / 作者），所以不关心扩展名，截断随意。
    """
    s = sanitize(name)
    return s[:60] or fallback


def sanitize(name: str) -> str:
    """去掉 Windows 非法字符与首尾空格/点。**不截断**。"""
    bad = '\\/:*?"<>|\r\n\t'
    s = "".join("_" if ch in bad else ch for ch in (name or ""))
    return s.strip().rstrip(". ")


def safe_filename(name: str, fallback: str = "untitled.md",
                  max_len: int = 90) -> str:
    """文件名安全化：非法字符替换 + 限长，**但绝不切掉扩展名**。

    ⚠️ 这里踩过一个真实且很坑的 bug：
    以前直接对整串 `s[:80]`，结果长标题被从中间切断，
    `.md` 后缀一起没了 —— 归档到知识库的文件变成「无扩展名」，
    Obsidian 不认、双击打不开，用户看到的就是「格式不太对」。

    正确做法：把 stem 和 suffix 拆开，**只截 stem**，再拼回后缀；
    并且截断长度要按「UTF-8 字节」算（中文一个字符 3 字节，
    Windows 单段路径上限 255 字节，90 个中文字就已经 270 字节了）。
    """
    raw = name or ""
    # ⚠️ 先 sanitize 再拆后缀：反过来的话，标题里的 `/` 会被 Path 当成分隔符，
    # 「带/斜杠. md」会被切成只剩后半段。斜杠属于**标题内容**，不是路径。
    p = Path(sanitize(raw))
    stem, suffix = p.stem, p.suffix
    stem = sanitize(stem)
    # 后缀只保留「看起来正常」的（避免把标题里的 . 当成后缀）
    if suffix and len(suffix) <= 12 and suffix[1:].isalnum():
        suffix = "." + suffix[1:].lower()
    else:
        suffix = ""

    # 按字节截断 stem，同时不切坏多字节字符
    limit = max(8, max_len - len(suffix.encode("utf-8")))
    out = ""
    used = 0
    for ch in stem:
        b = len(ch.encode("utf-8"))
        if used + b > limit:
            break
        out += ch
        used += b
    out = out.rstrip(". ")
    if not out:
        return fallback
    return out + suffix


def unique_path(path: Path) -> Path:
    """路径已存在时追加 (2)、(3)…，绝不覆盖用户已有文件。"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(2, 1000):
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    return parent / f"{stem} ({int(__import__('time').time())}){suffix}"


def dest_for(kb_root: str | Path, item: Item | None, filename: str,
             flat: bool = False) -> Path:
    """算出某个文件在知识库里的目标路径。

    flat=True 时全部平铺在根目录（不想分子目录的用户可以选）。
    item 为 None（比如文件页里的散文件）时落到 `_misc/`。
    """
    root = Path(kb_root)
    dest_name = safe_filename(filename)
    if flat or item is None:
        return root / dest_name
    platform = _safe_segment(item.platform, "unknown")
    author = _safe_segment(item.author or "", "未知作者")
    return root / platform / author / dest_name


def add_files(kb_root: str | Path, entries: list[dict],
              flat: bool = False) -> CopyResult:
    """把一批文件复制进知识库。

    entries 每项是：
        {"path": 源文件绝对路径, "item": Item | None, "name": 目标文件名(可选)}

    逐条 try/except：某一个失败不影响其余，最后统一汇报。
    """
    root = Path(kb_root)
    res = CopyResult(copied=[], skipped=[], dest_root=str(root))
    if not kb_root:
        res.skipped.append(("", "还没设置知识库目录"))
        return res

    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        res.skipped.append(("", f"知识库目录无法创建：{exc}"))
        return res

    for e in entries:
        src = Path(e.get("path") or "")
        if not src or not src.is_file():
            res.skipped.append((str(src), "源文件不存在"))
            continue
        name = e.get("name") or src.name
        dst = dest_for(root, e.get("item"), name, flat=flat)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst = unique_path(dst)
            shutil.copy2(str(src), str(dst))
            res.copied.append((str(src), str(dst)))
        except Exception as exc:  # noqa: BLE001
            res.skipped.append((str(src), f"复制失败：{exc}"))
    return res


def kb_size(root: str | Path) -> tuple[int, int]:
    """返回 (文件数, 总字节数)，用于在设置里显示知识库有多大。"""
    base = Path(root) if root else None
    if not base or not base.exists():
        return 0, 0
    n = 0
    total = 0
    try:
        for f in base.rglob("*"):
            try:
                if f.is_file():
                    n += 1
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        return n, total
    return n, total
