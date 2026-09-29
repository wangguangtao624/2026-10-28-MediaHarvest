"""「文件」页：像资源管理器一样看产出的文件和中间文件。

对应需求：
「最好能提供一个可视化的界面，像打开文件夹一样，能直观看到下载的文件
（特别是中间文件保存在哪里）」
「输出目录也支持在可视化区域直接查看」

布局
----
左侧：三个入口（Markdown 文稿 / 音视频中间文件 / 全部），点一下切换右侧列表
右侧：当前目录的面包屑 + 文件表（名称/大小/时间/关联作品）+ 右键菜单

中间文件目录会**显式标出来**（用户特别问了"保存在哪里"），
并且每个文件都尽量关联回作品标题——文件名是 item_id 这种数字串，
不关联的话根本认不出是什么。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QMenu, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..export.inventory import FileEntry, human_dir, scan_dir
from .fileops import open_path, reveal_path
from .widgets import Card, InfoButton, SectionTitle, hint


class FilesPage(QWidget):
    """产出文件浏览器。"""

    COLS = ["名称", "类型", "大小", "修改时间", "关联作品"]

    def __init__(self, ctx, theme: str = "light") -> None:
        super().__init__()
        self.ctx = ctx
        self.theme = theme
        self.items: list = []
        self.doc_dir = ""
        self.media_dir = ""
        self.entries: list[FileEntry] = []
        self._mode = "doc"
        self._build()

    # ---------------------------------------------------------- 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("文件")
        title.setProperty("role", "h1")
        head.addWidget(title)
        head.addWidget(InfoButton(
            "关于这一页",
            "<p>这里能看到两类产出：</p>"
            "<p><b>Markdown 文稿</b>：转录出来的 .md / .csv / .json，"
            "以及索引页和 Canvas 看板。这些是你要的成品。</p>"
            "<p><b>音视频中间文件</b>：为了提取文案而下载的 mp4 / m4a。"
            "它们只是「中间产物」——如果配置页里没勾「保留音视频」，"
            "转录完就会被自动删掉，所以这里可能是空的。</p>"
            "<p style='color:#888'>提示：双击任意文件用系统默认程序打开；"
            "右键可以「打开所在文件夹」「复制路径」。</p>",
            self, self.theme))
        head.addStretch(1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.reload)
        head.addWidget(self.btn_refresh)
        root.addLayout(head)

        # --- 目录切换 ---
        tabs = QHBoxLayout()
        tabs.setSpacing(8)
        self.tab_buttons: dict[str, QPushButton] = {}
        for key, label in (("doc", "Markdown 文稿"),
                           ("media", "音视频中间文件"),
                           ("all", "全部（递归）")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setProperty("nav", "tab")
            b.clicked.connect(lambda _c, k=key: self.set_mode(k))
            self.tab_buttons[key] = b
            tabs.addWidget(b)
        tabs.addStretch(1)
        root.addLayout(tabs)

        # --- 目录路径条 ---
        self.path_card = Card()
        p = self.path_card.body
        p.setSpacing(8)
        row1 = QHBoxLayout()
        self.label_doc_path = QLabel()
        self.label_doc_path.setWordWrap(True)
        # ⚠️ 用户要求「完整路径显示不全，需要修复为完整显示」。
        # 这里做三件事：不截断、允许换行、允许选中复制（方便直接粘到资源管理器）。
        self.label_doc_path.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.label_doc_path.setToolTip("完整路径，可直接选中复制")
        row1.addWidget(self.label_doc_path, 1)
        b_open_doc = QPushButton("打开目录")
        b_open_doc.clicked.connect(lambda: self._open_dir(self.doc_dir))
        b_pick_doc = QPushButton("更改…")
        b_pick_doc.clicked.connect(lambda: self._pick_dir("doc"))
        b_copy_doc = QPushButton("复制路径")
        b_copy_doc.setToolTip("把文稿目录的完整路径复制到剪贴板")
        b_copy_doc.clicked.connect(lambda: self._copy_dir(self.doc_dir))
        row1.addWidget(b_open_doc)
        row1.addWidget(b_pick_doc)
        row1.addWidget(b_copy_doc)
        p.addLayout(row1)

        row2 = QHBoxLayout()
        self.label_media_path = QLabel()
        self.label_media_path.setWordWrap(True)
        self.label_media_path.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.label_media_path.setToolTip("完整路径，可直接选中复制")
        row2.addWidget(self.label_media_path, 1)
        b_open_media = QPushButton("打开目录")
        b_open_media.clicked.connect(lambda: self._open_dir(self.media_dir))
        b_pick_media = QPushButton("更改…")
        b_pick_media.clicked.connect(lambda: self._pick_dir("media"))
        b_copy_media = QPushButton("复制路径")
        b_copy_media.setToolTip("把中间文件目录的完整路径复制到剪贴板")
        b_copy_media.clicked.connect(lambda: self._copy_dir(self.media_dir))
        row2.addWidget(b_open_media)
        row2.addWidget(b_pick_media)
        row2.addWidget(b_copy_media)
        p.addLayout(row2)
        root.addWidget(self.path_card)

        # --- 汇总条 ---
        self.label_summary = QLabel("")
        self.label_summary.setProperty("role", "muted")
        root.addWidget(self.label_summary)

        # --- 文件表 ---
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 多选：为了支持「批量添加到知识库」
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, w in ((1, 70), (2, 90), (3, 120), (4, 200)):
            self.table.setColumnWidth(c, w)
        self.table.itemDoubleClicked.connect(self._on_dbl)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_menu)
        self.table.setToolTip(
            "双击打开（文件夹则进入）；可按住 Ctrl/Shift 多选，"
            "右键「添加到知识库」批量归档")
        root.addWidget(self.table, 1)

        self.label_empty = hint(
            "这里还是空的。完成一次采集+转录后，产出会出现在这里。")
        root.addWidget(self.label_empty)

        self.set_mode("doc")

    # ---------------------------------------------------------- 数据
    def configure(self, doc_dir: str, media_dir: str) -> None:
        self.doc_dir = doc_dir or ""
        self.media_dir = media_dir or ""

    def set_items(self, items: list) -> None:
        self.items = items or []
        self.reload()

    def reload(self) -> None:
        # ⚠️ 用完整路径，不截断。样式上让「标签」和「路径」可区分：
        # 标签加粗，路径用等宽字体，方便一眼找到路径的起点与结尾。
        self.label_doc_path.setText(
            f"<b>Markdown 文稿目录</b><br>"
            f"<span style='font-family:Consolas,monospace'>{human_dir(self.doc_dir)}</span>"
            + ("" if self.doc_dir and Path(self.doc_dir).exists()
               else "　<span style='color:#C62828'>(目录不存在)</span>"))
        self.label_media_path.setText(
            f"<b>音视频中间目录</b><br>"
            f"<span style='font-family:Consolas,monospace'>{human_dir(self.media_dir)}</span>"
            + ("" if self.media_dir and Path(self.media_dir).exists()
               else "　<span style='color:#C62828'>(目录不存在)</span>"))

        if self._mode == "doc":
            self.entries = scan_dir(self.doc_dir)
        elif self._mode == "media":
            self.entries = scan_dir(self.media_dir)
        else:
            self.entries = (scan_dir(self.doc_dir) + scan_dir(self.media_dir))

        self._fill()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        for k, b in self.tab_buttons.items():
            b.setChecked(k == mode)
        self.reload()

    def _by_id(self, item_id: str) -> str:
        for it in self.items:
            if getattr(it, "item_id", "") == item_id:
                return getattr(it, "title", "") or ""
        return ""

    def _fill(self) -> None:
        files = [e for e in self.entries if e.kind == "file"]
        dirs = [e for e in self.entries if e.kind == "dir"]
        rows = dirs + files

        self.table.setRowCount(len(rows))
        total = 0
        for r, e in enumerate(rows):
            is_dir = e.kind == "dir"
            name_cell = QTableWidgetItem(("📁 " if is_dir else "") + e.name)
            name_cell.setData(Qt.UserRole, e.path)
            self.table.setItem(r, 0, name_cell)
            self.table.setItem(r, 1, QTableWidgetItem(
                "文件夹" if is_dir else _cat_text(e.category)))
            self.table.setItem(r, 2, QTableWidgetItem(e.size_text))
            self.table.setItem(r, 3, QTableWidgetItem(e.mtime_text))
            title = e.title or self._by_id(e.item_id)
            self.table.setItem(r, 4, QTableWidgetItem(title))
            if not is_dir:
                total += e.size

        n_media = sum(1 for e in files if e.category == "media")
        self.label_summary.setText(
            f"共 {len(files)} 个文件（其中音视频 {n_media} 个）"
            f"　合计 {_human(total)}")
        self.label_empty.setVisible(not rows)

    # ---------------------------------------------------------- 操作
    def _copy_dir(self, path: str) -> None:
        if not path:
            QMessageBox.information(self, "未设置", "这个目录还没有设置。")
            return
        QApplication.clipboard().setText(path)
        self.ctx.status_message("完整路径已复制到剪贴板")

    def _open_dir(self, path: str) -> None:
        if not path:
            QMessageBox.information(self, "未设置", "这个目录还没有设置。")
            return
        if not Path(path).exists():
            QMessageBox.warning(self, "目录不存在", f"{path}\n\n可能已被移动或删除。")
            return
        try:
            open_path(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    def _pick_dir(self, which: str) -> None:
        cur = self.doc_dir if which == "doc" else self.media_dir
        d = QFileDialog.getExistingDirectory(self, "选择目录", cur or "")
        if not d:
            return
        if which == "doc":
            self.doc_dir = d
            if hasattr(self.ctx, "set_output_dir"):
                self.ctx.set_output_dir(d)
        else:
            self.media_dir = d
            if hasattr(self.ctx, "set_media_dir"):
                self.ctx.set_media_dir(d)
        self.reload()

    def _cur_entry(self) -> FileEntry | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        cell = self.table.item(row, 0)
        if cell is None:
            return None
        path = cell.data(Qt.UserRole)
        for e in self.entries:
            if e.path == path:
                return e
        return None

    def _on_dbl(self, _item: QTableWidgetItem) -> None:
        row = self.table.currentRow()
        cell = self.table.item(row, 0)
        if cell is None:
            return
        path = cell.data(Qt.UserRole)
        if path and Path(path).is_dir():
            self.doc_dir = path
            self._mode = "doc"
            self.reload()
            return
        try:
            open_path(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    def _selected_entries(self) -> list[FileEntry]:
        """当前选中的所有文件条目（跳过文件夹）。"""
        out: list[FileEntry] = []
        by_path = {e.path: e for e in self.entries}
        for idx in self.table.selectionModel().selectedRows():
            cell = self.table.item(idx.row(), 0)
            if cell is None:
                continue
            p = cell.data(Qt.UserRole)
            e = by_path.get(p)
            if e is not None and e.kind == "file":
                out.append(e)
        return out

    def _on_menu(self, pos) -> None:
        # ⚠️ 右键点在未选中的行上时，Qt 默认不会选中它。为了让「打开」
        # 这类单文件操作符合直觉（作用于右键的那一行），这里补一次选中；
        # 但如果用户已经多选了，就不要破坏他的多选。
        row_at = self.table.rowAt(pos.y())
        if row_at >= 0 and not self.table.selectionModel().isRowSelected(
                row_at, self.table.rootIndex()):
            self.table.selectRow(row_at)

        e = self._cur_entry()
        sels = self._selected_entries()
        if e is None and not sels:
            return

        m = QMenu(self)
        # ---- 单个文件 ----
        a_open = m.addAction("打开")
        a_reveal = m.addAction("打开所在文件夹")
        m.addSeparator()
        # ---- 知识库（多选也走这里）----
        kb_label = (f"添加到知识库（{len(sels)} 个文件）" if len(sels) > 1
                    else "添加到知识库")
        a_kb = m.addAction(kb_label)
        a_kb.setEnabled(True)
        a_kb.setToolTip("复制到知识库目录，按「平台/作者」自动分文件夹")
        a_kb_set = m.addAction("设置知识库目录…")
        m.addSeparator()
        a_copy = m.addAction("复制完整路径")
        if len(sels) > 1:
            a_copy.setEnabled(False)

        act = m.exec_(self.table.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act is a_open:
            try:
                open_path(e.path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "打不开", str(exc))
        elif act is a_reveal:
            try:
                reveal_path(e.path)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "打不开", str(exc))
        elif act is a_kb:
            self._add_to_kb(sels or ([e] if e else []))
        elif act is a_kb_set:
            self.ctx.choose_kb_dir()
        elif act is a_copy:
            QApplication.clipboard().setText(e.path)
            self.ctx.status_message("路径已复制")

    def _add_to_kb(self, entries: list[FileEntry]) -> None:
        """把选中的文件批量归档到知识库。"""
        files = [x for x in entries if x.kind == "file"]
        if not files:
            QMessageBox.information(
                self, "没有可归档的文件",
                "文件夹不能直接归档，请选中具体的文件（可多选）。")
            return
        self.ctx.add_paths_to_kb([
            {"path": x.path, "item": None, "name": x.name} for x in files
        ])


def _cat_text(cat: str) -> str:
    return {"doc": "文稿", "media": "音视频", "dir": "文件夹"}.get(cat, "其它")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
