"""产出物清单：把「导出的文稿」和「下载的中间文件」列出来给界面看。

用户诉求原话：
「最好能提供一个可视化的界面，像打开文件夹一样，能直观看到下载的文件
（特别是中间文件保存在哪里），并且能查看历史文件、历史记录以及历史转录接口。」

所以这里不做花哨的事，就是**老老实实扫目录 + 查库**，产出一个可直接
喂给表格/树控件的扁平结构。三条数据来源：

1. 文稿目录（output_dir）：扫 *.md / *.csv / *.json，按任务分组；
2. 中间文件目录（media_dir）：扫音视频，标注大小与「对应哪个作品」；
3. 数据库：历史任务与历史转录记录（哪条转过、用的什么模型）。

设计上刻意让每个文件都带 **绝对路径**，因为界面上的「打开」「复制路径」
「打开所在文件夹」都要用它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 中间文件的常见扩展名
MEDIA_EXTS = {".mp4", ".m4a", ".mp3", ".webm", ".flv", ".mkv", ".mov",
              ".aac", ".wav", ".ts", ".m4s"}
# 文稿类扩展名
DOC_EXTS = {".md", ".csv", ".json", ".txt"}


@dataclass
class FileEntry:
    """一个文件（或目录）条目，供界面直接渲染。"""
    name: str
    path: str            # 绝对路径
    size: int = 0        # 字节；目录为 0
    mtime: float = 0.0
    kind: str = "file"   # file / dir
    category: str = ""   # doc / media / other
    item_id: str = ""    # 关联的作品 id（能对上时）
    title: str = ""      # 关联的作品标题

    @property
    def size_text(self) -> str:
        if self.kind == "dir":
            return "—"
        n = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} GB"

    @property
    def mtime_text(self) -> str:
        if not self.mtime:
            return ""
        import time
        return time.strftime("%m-%d %H:%M", time.localtime(self.mtime))


def _categorize(p: Path) -> str:
    s = p.suffix.lower()
    if s in MEDIA_EXTS:
        return "media"
    if s in DOC_EXTS:
        return "doc"
    return "other"


def scan_dir(root: str | Path, *, only_dirs: bool = False,
             recursive: bool = True, limit: int = 3000) -> list[FileEntry]:
    """扫一个目录，返回扁平条目列表（目录在前）。"""
    base = Path(root) if root else None
    if not base or not base.exists():
        return []
    out: list[FileEntry] = []
    try:
        if only_dirs:
            for d in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                if d.is_dir():
                    out.append(FileEntry(name=d.name, path=str(d.resolve()),
                                         kind="dir", category="dir"))
            return out

        # 先列一层子目录（方便「像资源管理器一样」导航）
        for d in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if d.is_dir():
                out.append(FileEntry(name=d.name, path=str(d.resolve()),
                                     kind="dir", category="dir"))

        it = base.rglob("*") if recursive else base.iterdir()
        n = 0
        for f in it:
            if n >= limit:
                break
            try:
                if not f.is_file():
                    continue
                st = f.stat()
                rel = f.relative_to(base)
                out.append(FileEntry(
                    name=str(rel), path=str(f.resolve()), size=st.st_size,
                    mtime=st.st_mtime, kind="file",
                    category=_categorize(f), item_id=f.stem))
                n += 1
            except (OSError, ValueError):
                continue
    except OSError:
        return out
    return out


def scan_media(media_dir: str | Path,
               items: list | None = None) -> list[FileEntry]:
    """扫中间文件目录，并把条目关联回作品（标题更好认）。"""
    entries = scan_dir(media_dir, recursive=True)
    by_id = {}
    for it in (items or []):
        by_id[getattr(it, "item_id", "")] = getattr(it, "title", "") or ""
    for e in entries:
        if e.kind != "file":
            continue
        title = by_id.get(e.item_id)
        if title:
            e.title = title
    return entries


def human_dir(path: str | Path) -> str:
    """返回**完整**路径。

    ⚠️ 历史坑：这里以前会把路径截成「…\\最后3段」以避免撑爆界面，
    结果用户根本看不清目录到底在哪（尤其是 D 盘、多级子目录的情况），
    明确反馈「完整路径显示不全，需要修复为完整显示」。

    正确做法是**不截断**，交给界面处理换行/省略：
    界面用 QLabel 的 setWordWrap(True) + 可选中复制，
    这样既完整又能看全。截断只该发生在渲染层，不该发生在数据层。
    """
    if not path:
        return "（未设置）"
    try:
        return str(Path(path))
    except Exception:  # noqa: BLE001
        return str(path)
