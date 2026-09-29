"""「历史」页：跨任务的累计记录。

对应需求：
    「能查看历史文件、历史记录以及历史转录接口」
    「『历史』那块也欠缺点东西，界面和功能需要再优化优化」

为什么和「结果文档」页分开
--------------------------
结果页是**本次任务**的视角；历史页是**跨任务**的视角——换过好几次采集任务
之后，回头还能找到上个月转过的那条视频。

这一版的改进（旧版只有一个概览条 + 一张表，太单薄）
----------------------------------------------------
1. **概览卡**从 5 个数字扩到 8 个，并补上「素材总时长」「失败数」这类
   真正有信息量的指标；
2. 新增**按平台分布**一块，一眼看出「我主要在采哪个平台、哪个平台转录失败多」；
3. 新增**历史任务卡**（旧版压根没展示 tasks 表）：每个任务一张卡片，
   显示平台、目标、时间、作品数 / 完成数、内容模式，右键可跳过去看结果；
4. 新增**最近动态时间线**：最近 30 条有状态变动的条目，按时间倒序，
   带状态色点，比翻表格快；
5. 转录记录表保留，但补上「是否有中间文件」列，并把右键菜单做全
   （打开文稿 / 打开中间文件 / 打开所在文件夹 / 打开原文 / 复制路径 / 复制 ID）。

第二轮改进（用户反馈「平台分布点不开」「动态跳不过去」「中间文件占地方」）
--------------------------------------------------------------------
6. **平台分布改成可展开的两级树**（原来是张死表）：平台 → 该平台的每条作品，
   双击任意一条直接跳到「结果文档」看它的 Markdown。层级做成
   平台 → 作品 → 该作品产出的文件，像目录一样一路点下去；
7. **最近动态 / 历史任务都能点着跳了**：拖动条上每行可双击跳文稿，
   任务卡上有「查看结果」按钮，不再只是「看看而已」；
8. **一键清除中间文件**：中间的音视频（.mp4/.mp3/.wav…）转录完就没用了，
   顶栏加了按钮，先统计占了多少空间，二次确认后批量删除，
   只删**数据库里记录的**中间文件 + 输出目录下的缓存，不碰导出文稿。

三个区块用小标签页切换，避免一屏塞太多东西。
"""

from __future__ import annotations

import time
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .editor import find_notepadpp, open_with_editor
from .fileops import open_path, reveal_path
from .theme import tokens
from .widgets import Card, InfoButton, SectionTitle


def _dur(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "-"
    if not sec:
        return "-"
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _words(n) -> str:
    n = int(n or 0)
    if n >= 10000:
        return f"{n/10000:.1f} 万"
    return f"{n:,}"


def _hours(sec) -> str:
    h = float(sec or 0) / 3600.0
    if h >= 1:
        return f"{h:.1f} 小时"
    return f"{float(sec or 0)/60:.0f} 分钟"


def _ago(ts) -> str:
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "-"
    if not ts:
        return "-"
    d = time.time() - ts
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d//60)} 分钟前"
    if d < 86400:
        return f"{int(d//3600)} 小时前"
    if d < 86400 * 30:
        return f"{int(d//86400)} 天前"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class _ClickRow(QFrame):
    """一个整体可双击的容器行。

    ⚠️ 为什么子控件都设了 WA_TransparentForMouseEvents：
    鼠标事件会先落到最上层的小 QLabel 上，父容器的 mouseDoubleClickEvent
    根本收不到 → 双击标题文字时「点了没反应」。让子控件对鼠标透明，
    事件就会穿透到这一层，整行任何位置都能点。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._on_click = None
        self.setProperty("role", "clickrow")
        self.setCursor(Qt.PointingHandCursor)

    def set_click(self, fn) -> None:
        self._on_click = fn

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if self._on_click is not None:
            try:
                self._on_click()
            except Exception:  # noqa: BLE001
                pass
        super().mouseDoubleClickEvent(e)


# 平台中文名（界面里显示「哔哩哔哩（bilibili）」这样，既好认又保留原始 key）
PLAT_CN = {
    "bilibili": "哔哩哔哩", "douyin": "抖音", "telegram": "Telegram",
    "local": "本地文件", "youtube": "YouTube", "xiaohongshu": "小红书",
    "kuaishou": "快手", "weibo": "微博", "twitter": "Twitter/X",
}


class HistoryPage(QWidget):
    """历史任务 + 平台分布 + 最近动态 + 转录记录。"""

    COLS = ["平台", "标题", "作者", "时长", "字数", "转录模型", "中间文件", "时间"]

    # 中间文件类型：只有这些后缀才会被「一键清除中间文件」删掉，
    # 导出文稿（.md/.txt/.json/.csv）永远不动。
    MEDIA_EXT = {
        ".mp4", ".mkv", ".flv", ".webm", ".mov", ".avi", ".m4v", ".ts",
        ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus",
    }
    DOC_EXT = {".md", ".txt", ".json", ".csv", ".html", ".srt", ".ass", ".vtt"}

    def __init__(self, ctx, theme: str = "light") -> None:
        super().__init__()
        self.ctx = ctx
        self.theme = theme
        self.rows: list[dict] = []
        # 平台树的节点缓存：platform → 该平台的作品列表（供展开时按需填充）
        self._plat_items: dict[str, list[dict]] = {}
        self._build()

    # ---------------------------------------------------------- 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        head = QHBoxLayout()
        t = QLabel("历史记录")
        t.setProperty("role", "h1")
        head.addWidget(t)
        head.addWidget(InfoButton(
            "关于历史记录",
            "<p>这里存的是<b>跨任务</b>的累计记录——换过好几次采集任务之后，"
            "回头还能找到以前转过的内容。</p>"
            "<p>四个小标签页：</p>"
            "<ul style='margin-left:-12px'>"
            "<li><b>转录记录</b>：每条转出过文字的作品，可搜索、可双击打开文稿</li>"
            "<li><b>历史任务</b>：每次采集任务一张卡，看当时采了多少、成了多少</li>"
            "<li><b>平台分布</b>：各平台采了多少 / 转了多少 / 失败多少</li>"
            "<li><b>最近动态</b>：最近 30 条有状态变动的条目，按时间倒序</li>"
            "</ul>"
            "<p style='color:#888'>只统计「数据库中真实存在」的记录；"
            "你手动删掉的文稿不会影响这里的数据，但打开会提示文件不存在。</p>",
            self, self.theme))
        head.addStretch(1)
        self.btn_clean = QPushButton("一键清除中间文件")
        self.btn_clean.setToolTip(
            "把转录完成后已无用的音视频中间文件删掉，腾出空间。\n"
            "只删数据库里记录的中间文件，导出的文稿一律不动。")
        self.btn_clean.clicked.connect(self.clean_intermediate)
        head.addWidget(self.btn_clean)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新从数据库读取（切换页面时也会自动刷新）")
        self.btn_refresh.clicked.connect(self.reload)
        head.addWidget(self.btn_refresh)
        root.addLayout(head)

        # --- 概览卡 ---
        self.stat_card = Card()
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.stat_labels: dict[str, QLabel] = {}
        specs = [
            ("items", "采集作品"), ("transcripts", "已转录"),
            ("words", "总字数"), ("duration", "素材总时长"),
            ("comments", "评论条数"), ("media", "留存中间文件"),
            ("failed", "失败条目"), ("tasks", "历史任务"),
        ]
        for i, (key, label) in enumerate(specs):
            cell = Card(role="panel")
            cell.body.setSpacing(2)
            v = QLabel("—")
            v.setProperty("role", "stat")
            k = QLabel(label)
            k.setProperty("role", "muted")
            cell.body.addWidget(v)
            cell.body.addWidget(k)
            self.stat_labels[key] = v
            grid.addWidget(cell, i // 4, i % 4)
        self.stat_card.body.addLayout(grid)
        root.addWidget(self.stat_card)

        # --- 标签页切换 ---
        tabs = QHBoxLayout()
        tabs.setSpacing(8)
        self.tab_group: list[QPushButton] = []
        self.stack = QStackedWidget()
        for i, (key, label) in enumerate((
                ("tr", "转录记录"), ("tasks", "历史任务"),
                ("plat", "平台分布"), ("recent", "最近动态"))):
            b = QPushButton(label)
            b.setProperty("nav", "tab")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c, idx=i: self.switch_tab(idx))
            self.tab_group.append(b)
            tabs.addWidget(b)
        tabs.addStretch(1)
        root.addLayout(tabs)

        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_transcripts())
        self.stack.addWidget(self._build_tasks())
        self.stack.addWidget(self._build_platforms())
        self.stack.addWidget(self._build_recent())
        self.tab_group[0].setChecked(True)

    def switch_tab(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        if idx in (1, 2, 3) and not self.rows:
            self.reload()

    # ---------------- 标签 1：转录记录 ----------------
    def _build_transcripts(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("搜索"))
        self.edit_kw = QLineEdit()
        self.edit_kw.setPlaceholderText("标题 / 作者 / 逐字稿内容，回车搜索")
        self.edit_kw.returnPressed.connect(self.reload)
        filt.addWidget(self.edit_kw, 1)
        filt.addWidget(QLabel("平台"))
        self.combo_plat = QComboBox()
        self.combo_plat.addItem("全部", "")
        # ⚠️ 别再手写平台清单：新增平台时这里漏改过一次（加了小红书但筛选里没有）。
        # 统一从注册表取，保证和采集配置页的选项永远一致。
        try:
            from ..crawler import registry as _reg
            for spec in _reg.platform_list():
                self.combo_plat.addItem(spec.label, spec.key)
        except Exception:  # noqa: BLE001
            for key in ("bilibili", "douyin", "xiaohongshu", "telegram", "local"):
                self.combo_plat.addItem(key, key)
        self.combo_plat.currentIndexChanged.connect(self.reload)
        filt.addWidget(self.combo_plat)
        self.combo_sort = QComboBox()
        self.combo_sort.addItem("按时间倒序", "time")
        self.combo_sort.addItem("按字数倒序", "words")
        self.combo_sort.currentIndexChanged.connect(self._fill)
        filt.addWidget(self.combo_sort)
        b = QPushButton("搜索")
        b.clicked.connect(self.reload)
        filt.addWidget(b)
        v.addLayout(filt)

        # ⚠️ 用户反馈：「点击查看文稿会自动跳转到结果文档，这个跳转很不合理，
        # 就应该直接在历史记录这里显示」。所以这里改成**左右分栏**：
        # 左边列表、右边就地预览全文，不用离开本页。
        self.tr_split = QSplitter(Qt.Horizontal)
        self.tr_split.setChildrenCollapsible(False)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(6)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        for c, wd in ((0, 80), (2, 120), (3, 66), (4, 76), (5, 140),
                      (6, 76), (7, 96)):
            self.table.setColumnWidth(c, wd)
        self.table.itemDoubleClicked.connect(self._on_dbl)
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_menu)
        self.table.setToolTip("单击右侧就地预览；双击在预览里展开全文；右键更多操作")
        lv.addWidget(self.table, 1)

        self.label_tr_info = QLabel("")
        self.label_tr_info.setProperty("role", "muted")
        self.label_tr_info.setWordWrap(True)
        lv.addWidget(self.label_tr_info)
        self.tr_split.addWidget(left)

        self.tr_split.addWidget(self._build_tr_preview())
        self.tr_split.setStretchFactor(0, 3)
        self.tr_split.setStretchFactor(1, 4)
        self.tr_split.setSizes([460, 600])
        v.addWidget(self.tr_split, 1)
        return w

    def _build_tr_preview(self) -> QWidget:
        """历史页的右侧预览面板（就地看全文 + 各种打开方式）。"""
        panel = QFrame()
        panel.setProperty("role", "panel")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(12, 10, 12, 10)
        pv.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.label_pv_title = QLabel("选中左边任意一条，这里会显示全文")
        self.label_pv_title.setProperty("role", "h2")
        self.label_pv_title.setWordWrap(True)
        self.label_pv_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.label_pv_title, 1)
        pv.addLayout(head)

        self.label_pv_meta = QLabel("")
        self.label_pv_meta.setProperty("role", "muted")
        self.label_pv_meta.setWordWrap(True)
        self.label_pv_meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        pv.addWidget(self.label_pv_meta)

        # 打开方式一条龙：这些以前必须先跳到「结果文档」页才能用，
        # 用户反馈「跳过去以后很多功能都用不了」——现在本页就能用。
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.btn_pv_editor = QPushButton("用 Notepad++ 打开")
        self.btn_pv_editor.setToolTip("用外部编辑器打开这份逐字稿的源文件")
        self.btn_pv_editor.clicked.connect(self._pv_open_editor)
        btns.addWidget(self.btn_pv_editor)

        self.btn_pv_folder = QPushButton("打开所在文件夹")
        self.btn_pv_folder.setToolTip("在资源管理器里定位到这份文稿")
        self.btn_pv_folder.clicked.connect(self._pv_reveal)
        btns.addWidget(self.btn_pv_folder)

        self.btn_pv_copy = QPushButton("复制全文")
        self.btn_pv_copy.setToolTip("把逐字稿正文复制到剪贴板")
        self.btn_pv_copy.clicked.connect(self._pv_copy)
        btns.addWidget(self.btn_pv_copy)

        self.btn_pv_kb = QPushButton("添加到知识库")
        self.btn_pv_kb.setToolTip("把这份文稿复制一份到知识库目录")
        self.btn_pv_kb.clicked.connect(self._pv_add_kb)
        btns.addWidget(self.btn_pv_kb)

        btns.addStretch(1)
        pv.addLayout(btns)

        self.pv = QTextBrowser()
        self.pv.setOpenExternalLinks(True)
        self.pv.setPlaceholderText(
            "选中左边任意一条，这里会显示完整的逐字稿正文。")
        pv.addWidget(self.pv, 1)

        self.label_pv_path = QLabel("")
        self.label_pv_path.setProperty("role", "mono")
        self.label_pv_path.setWordWrap(True)
        self.label_pv_path.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        pv.addWidget(self.label_pv_path)

        self._pv_data: dict | None = None
        return panel

    # ---------------- 标签 2：历史任务 ----------------
    def _build_tasks(self) -> QWidget:
        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(0)
        self.scroll_tasks = QScrollArea()
        self.scroll_tasks.setWidgetResizable(True)
        self.scroll_tasks.setFrameShape(QFrame.NoFrame)
        self.tasks_host = QWidget()
        self.tasks_col = QVBoxLayout(self.tasks_host)
        self.tasks_col.setContentsMargins(0, 0, 8, 0)
        self.tasks_col.setSpacing(8)
        self.tasks_col.addStretch(1)
        self.scroll_tasks.setWidget(self.tasks_host)
        ov.addWidget(self.scroll_tasks)
        return outer

    # ---------------- 标签 3：平台分布 ----------------
    def _build_platforms(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(SectionTitle("按平台分布（点开展开看具体文件）", self.theme))

        # ⚠️ 为什么从 QTableWidget 换成 QTreeWidget：
        # 用户反馈「平台分布点击后应该可以像四级目录一样展开，查看并点击具体文件」。
        # 表格天生是平的，表达不了父子层级；树控件才有点开-收起的心智模型。
        self.plat_tree = QTreeWidget()
        self.plat_tree.setHeaderLabels(
            ["平台 / 作品", "采集", "已转录", "总字数", "失败", "时长"])
        self.plat_tree.setRootIsDecorated(True)
        self.plat_tree.setUniformRowHeights(True)
        self.plat_tree.setAlternatingRowColors(True)
        self.plat_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.plat_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.plat_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.plat_tree.customContextMenuRequested.connect(self._plat_menu)
        self.plat_tree.itemDoubleClicked.connect(self._on_plat_dbl)
        # 按需填充：第一次展开某个平台时才去查它的作品列表
        self.plat_tree.itemExpanded.connect(self._on_plat_expanded)
        self.plat_tree.setToolTip(
            "点平台左侧三角展开作品列表；双击作品查看它的 Markdown 文稿。")
        hdr = self.plat_tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c, wd in ((1, 64), (2, 64), (3, 88), (4, 56), (5, 76)):
            self.plat_tree.setColumnWidth(c, wd)
        v.addWidget(self.plat_tree, 1)

        self.label_plat_hint = QLabel("")
        self.label_plat_hint.setProperty("role", "muted")
        self.label_plat_hint.setWordWrap(True)
        v.addWidget(self.label_plat_hint)
        return w

    # ---------------- 标签 4：最近动态 ----------------
    def _build_recent(self) -> QWidget:
        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(0)
        self.scroll_recent = QScrollArea()
        self.scroll_recent.setWidgetResizable(True)
        self.scroll_recent.setFrameShape(QFrame.NoFrame)
        self.recent_host = QWidget()
        self.recent_col = QVBoxLayout(self.recent_host)
        self.recent_col.setContentsMargins(0, 0, 8, 0)
        self.recent_col.setSpacing(4)
        self.recent_col.addStretch(1)
        self.scroll_recent.setWidget(self.recent_host)
        ov.addWidget(self.scroll_recent)
        return outer

    # ---------------------------------------------------------- 数据
    def reload(self) -> None:
        db = self.ctx.pipeline.db
        try:
            st = db.stats()
        except Exception:  # noqa: BLE001
            st = {}
        try:
            dst = db.duration_stats()
        except Exception:  # noqa: BLE001
            dst = {"total_sec": 0, "avg_sec": 0}
        for key, lb in self.stat_labels.items():
            if key == "duration":
                lb.setText(_hours(dst.get("total_sec", 0)))
            else:
                v = st.get(key, 0) or 0
                lb.setText(_words(v) if key == "words" else f"{v:,}")

        plat = self.combo_plat.currentData() or ""
        kw = self.edit_kw.text().strip()
        try:
            self.rows = db.list_transcripts(
                limit=500, platform=plat or None, keyword=kw)
        except Exception:  # noqa: BLE001
            self.rows = []
        self._fill()
        self._fill_tasks()
        self._fill_platforms()
        self._fill_recent()
        # 刷新后左侧行会重建，右侧预览也跟着重来
        self._show_preview(None)

    # ------------------------------------------------- 就地预览（右侧面板）
    def _show_preview(self, d: dict | None) -> None:
        """把某条记录渲染到右侧预览面板。d=None 表示回到空态。"""
        pv = getattr(self, "pv", None)
        if pv is None:
            return
        t = tokens(self.theme)
        self._pv_data = None

        if not d:
            self.label_pv_title.setText("选中左边任意一条，这里会显示全文")
            self.label_pv_meta.setText("")
            self.label_pv_path.setText("")
            pv.clear()
            pv.setPlaceholderText(
                "选中左边任意一条，这里会显示完整的逐字稿正文。\n\n"
                "（本页即可阅读、用 Notepad++ 打开、打开所在文件夹、"
                "复制全文或归档到知识库，不需要再跳到「结果文档」。）")
            return

        try:
            full = self.ctx.pipeline.db.get_transcript(
                d.get("platform", ""), d.get("item_id", "")) or {}
        except Exception:  # noqa: BLE001
            full = {}
        self._pv_data = full or dict(d)
        body = (full.get("transcript") or "").strip()

        # ⚠️ 图文作品没有逐字稿，正文在 desc（原文案）里。
        # 以前这里只看 transcript，图文作品点开永远显示「还没有逐字稿正文」，
        # 用户会以为抓取失败了。这里退一步用 desc 兜底。
        kind = (full.get("media_kind") or d.get("media_kind") or "video")
        is_image = kind == "image"
        desc = (full.get("desc") or d.get("desc") or "").strip()
        n_img = 0
        img_urls: list[str] = []
        raw_imgs = full.get("images") or d.get("images") or ""
        if raw_imgs:
            try:
                import json as _json
                lst = _json.loads(raw_imgs)
                if isinstance(lst, list):
                    for x in lst:
                        if isinstance(x, dict) and x.get("url"):
                            img_urls.append(str(x["url"]))
                        elif isinstance(x, str) and x.startswith("http"):
                            img_urls.append(x)
                    n_img = len(img_urls)
            except Exception:  # noqa: BLE001
                n_img = 0
        if not n_img:
            try:
                n_img = int(full.get("image_count") or d.get("image_count") or 0)
            except (TypeError, ValueError):
                n_img = 0
        # 图文作品用 desc 当正文
        if not body and is_image and desc:
            body = desc
        tags_raw = (full.get("tags") or d.get("tags") or "").strip()

        title = full.get("title") or d.get("title") or d.get("item_id") or ""
        self.label_pv_title.setText(title)

        wc = len(body)
        meta = [
            full.get("platform") or d.get("platform") or "",
            f"作者 {full.get('author') or d.get('author') or '—'}",
        ]
        if is_image:
            meta.append(f"图文 {n_img} 张图" if n_img else "图文")
        else:
            meta.append(f"{_dur(full.get('duration') or d.get('duration') or 0)}")
        meta += [
            f"{wc:,} 字",
            full.get("asr_engine") or d.get("asr_engine") or "",
        ]
        self.label_pv_meta.setText("　·　".join(x for x in meta if x and x != "—"
                                                or x.startswith("作者")))

        # 本地文件路径：完整显示，不截断（历史坑：以前截成 …\后3段）
        lp = full.get("local_path") or d.get("local_path") or ""
        if lp:
            exists = Path(lp).exists()
            mark = "✓" if exists else "✗（已删除）"
            self.label_pv_path.setText(f"{mark} {lp}")
            self.label_pv_path.setStyleSheet(
                f"color:{t.text_muted if exists else t.text_faint}")
        else:
            self.label_pv_path.setText("（这条没有保留中间文件）")
            self.label_pv_path.setStyleSheet(f"color:{t.text_faint}")

        if not body:
            if is_image:
                pv.setHtml(
                    f"<div style='color:{t.text_muted}'>这条图文作品没有取到正文。"
                    "<br><br>可能是采集时详情接口被风控拦了，"
                    "重新采集一次通常就有了。</div>")
            else:
                pv.setHtml(
                    f"<div style='color:{t.text_muted}'>这条记录还没有逐字稿正文。"
                    "<br><br>可能当初只下载了没转录，或者转录结果已被清理。</div>")
            return

        # 正文用 <pre> 风格保换行；同时把 URL 变成可点的链接
        import re as _re
        safe = (body.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))
        safe = _re.sub(r"(https?://[^\s<>]+)",
                       r"<a href='\1' style='color:%s'>\1</a>" % t.accent,
                       safe)
        head = ""
        # 图文作品：正文上方给一行图片链接，方便直接点开看
        if is_image and img_urls:
            links = " ".join(
                f"<a href='{u}' style='color:{t.accent}'>图{i}</a>"
                for i, u in enumerate(img_urls[:30], 1))
            head = (f"<div style='color:{t.text_muted};font-size:12px;"
                    f"margin-bottom:8px'>图片（{len(img_urls)} 张）：{links}</div>")
        if tags_raw:
            tags = " ".join(f"#{x}" for x in tags_raw.split(",") if x.strip())
            head += (f"<div style='color:{t.text_muted};font-size:12px;"
                     f"margin-bottom:10px'>话题：{tags}</div>")
        pv.setHtml(
            f"{head}"
            f"<div style='color:{t.text};font-size:13px;line-height:1.75;"
            f"white-space:pre-wrap;font-family:\"Microsoft YaHei UI\",sans-serif'>"
            f"{safe}</div>")

    def _on_row_selected(self) -> None:
        """单击某行 → 右侧就地预览（不跳页）。"""
        self._show_preview(self._cur())

    # ------------------------------------------------- 预览面板的按钮
    def _pv_file(self) -> str:
        """当前预览记录的**已导出文稿路径**（没有就返回空）。"""
        d = self._pv_data or {}
        if not d:
            return ""
        lp = d.get("local_path") or ""
        # 中间文件跟文稿不同目录，这里要的是「导出的 .md」
        try:
            f = self.ctx.page_docs.files.get(d.get("item_id", ""), {}) or {}
        except Exception:  # noqa: BLE001
            f = {}
        return f.get("transcript") or ""

    def _pv_open_editor(self) -> None:
        d = self._pv_data or {}
        if not d:
            return
        p = self._pv_file()
        if not (p and Path(p).is_file()):
            QMessageBox.information(
                self, "还没有导出文件",
                "这条记录在历史里，但没有找到它导出的 .md 文稿"
                "（可能是旧任务、或者文稿被移动/删除了）。\n\n"
                "可以先在「结果文档」页重新导出一次。")
            return
        editor = ""
        try:
            from .main_window import _load_cfg
            editor = str(_load_cfg().get("editor_path", "") or "")
        except Exception:  # noqa: BLE001
            pass
        used, desc = open_with_editor(p, editor)
        self.ctx.status_message(desc)

    def _pv_reveal(self) -> None:
        d = self._pv_data or {}
        if not d:
            return
        for cand in (self._pv_file(), d.get("local_path") or ""):
            if cand and Path(cand).exists():
                try:
                    reveal_path(cand)
                    return
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "打不开", str(exc))
                    return
        QMessageBox.information(self, "找不到文件",
                                "这条记录对应的文件已不在磁盘上。")

    def _pv_copy(self) -> None:
        d = self._pv_data or {}
        body = (d.get("transcript") or "").strip()
        if not body:
            return
        QApplication.clipboard().setText(body)
        self.ctx.status_message(f"已复制 {len(body):,} 字到剪贴板")

    def _pv_add_kb(self) -> None:
        d = self._pv_data or {}
        if not d:
            return
        p = self._pv_file()
        if not (p and Path(p).is_file()):
            QMessageBox.information(
                self, "还没有导出文件",
                "这条记录没有可归档的 .md 文稿。\n"
                "先在「结果文档」页导出后再试。")
            return
        from ..core.models import Item
        try:
            it = Item(platform=d.get("platform", ""),
                      item_id=d.get("item_id", ""),
                      title=d.get("title", ""), author=d.get("author", ""))
        except Exception:  # noqa: BLE001
            it = None
        self.ctx.add_paths_to_kb(
            [{"path": p, "item": it, "name": Path(p).name}], self)

    # ---------------- 转录记录 ----------------
    def _fill(self) -> None:
        rows = list(self.rows)
        if (self.combo_sort.currentData() or "time") == "words":
            rows.sort(key=lambda d: -(d.get("words") or 0))
        self.table.setRowCount(len(rows))
        total_words = 0
        with_media = 0
        for r, d in enumerate(rows):
            lp = d.get("local_path") or ""
            has_media = bool(lp) and Path(lp).exists()
            if has_media:
                with_media += 1
            w = d.get("words") or 0
            total_words += w
            # 图文作品没有时长，显示「图文 N 图」比 0:00 有意义
            if (d.get("media_kind") or "video") == "image":
                ni = d.get("image_count") or 0
                dur_txt = f"图文 {ni}图" if ni else "图文"
            else:
                dur_txt = _dur(d.get("duration") or 0)
            cells = [
                d.get("platform", ""),
                d.get("title", "") or d.get("item_id", ""),
                d.get("author", ""),
                dur_txt,
                f"{w:,}",
                d.get("asr_engine", "") or "-",
                "有" if has_media else ("已删" if lp else "-"),
                time.strftime("%m-%d %H:%M",
                              time.localtime(d.get("updated_at") or 0)),
            ]
            for c, val in enumerate(cells):
                cell = QTableWidgetItem(str(val))
                if c == 0:
                    cell.setData(Qt.UserRole, d)
                if c == 6:
                    t = tokens(self.theme)
                    # ⚠️ setForeground 要 QColor，不能直接给 "#RRGGBB" 字符串
                    # （typ 检查会报 "argument 1 has unexpected type 'str'"）
                    cell.setForeground(QColor(
                        t.success if has_media
                        else (t.text_faint if lp else t.text_muted)))
                self.table.setItem(r, c, cell)
        self.label_tr_info.setText(
            f"共 {len(rows)} 条转录记录，合计 {_words(total_words)} 字"
            f"，其中 {with_media} 条还留着中间文件。双击打开文稿。")

    # ---------------- 历史任务 ----------------
    def _fill_tasks(self) -> None:
        while self.tasks_col.count() > 1:
            it = self.tasks_col.takeAt(0)
            wdg = it.widget()
            if wdg is not None:
                wdg.deleteLater()
        try:
            tasks = self.ctx.pipeline.db.list_tasks(limit=100)
        except Exception:  # noqa: BLE001
            tasks = []
        if not tasks:
            lb = QLabel("还没有历史任务。完成一次采集后这里会列出每个任务的概况。")
            lb.setProperty("role", "muted")
            self.tasks_col.insertWidget(0, lb)
            return
        for i, tk in enumerate(tasks):
            self.tasks_col.insertWidget(i, self._task_card(tk))

    def _task_card(self, tk: dict) -> QWidget:
        t = tokens(self.theme)
        card = QFrame()
        card.setProperty("role", "taskcard")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 11, 14, 11)
        row.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(3)
        title = QLabel(f"{tk.get('platform', '')} · {tk.get('name', '') or '未命名任务'}")
        title.setProperty("role", "h2")
        left.addWidget(title)
        q = tk.get("query") or "（手动链接）"
        sub = QLabel(f"目标：{q}　模式：{tk.get('mode', '-')}")
        sub.setProperty("role", "muted")
        left.addWidget(sub)
        row.addLayout(left, 1)

        # 中段：统计 pill
        stats = QHBoxLayout()
        stats.setSpacing(6)
        item_n = tk.get("item_count") or 0
        done_n = tk.get("done_count") or 0
        for text in (f"{item_n} 作品", f"{done_n} 完成"):
            p = QLabel(text)
            p.setProperty("role", "pill")
            stats.addWidget(p)
        lim = tk.get("comment_limit")
        if tk.get("fetch_comments"):
            p = QLabel(f"评论 {lim if lim else '默认'} 条")
            p.setProperty("role", "pill")
            stats.addWidget(p)
        row.addLayout(stats)

        right = QVBoxLayout()
        right.setSpacing(3)
        ts = tk.get("created_at") or 0
        when = QLabel(time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                      if ts else "-")
        when.setProperty("role", "mono")
        when.setAlignment(Qt.AlignRight)
        right.addWidget(when)
        ago = QLabel(_ago(ts))
        ago.setProperty("role", "faint")
        ago.setAlignment(Qt.AlignRight)
        right.addWidget(ago)
        row.addLayout(right)

        # 跳到该任务产出的文稿（按输出目录过滤，精准落到它的文件上）
        btn = QPushButton("查看结果")
        btn.setToolTip("跳到「结果文档」页，只看这个任务产出的文稿")
        btn.clicked.connect(lambda _c=False, _tk=tk: self._open_task(_tk))
        row.addWidget(btn)

        card.setToolTip("双击卡片：查看该任务的文稿；右键：更多操作")
        card.setContextMenuPolicy(Qt.CustomContextMenu)
        card.customContextMenuRequested.connect(
            lambda pos, _tk=tk, _c=card: self._task_menu(pos, _tk, _c))
        card.mouseDoubleClickEvent = lambda _e, _tk=tk: self._open_task(_tk)
        return card

    def _open_task(self, tk: dict) -> None:
        """尽量把「结果文档」页过滤到这个任务的输出上。

        任务表里存了 output_dir；结果页本身没有「按任务过滤」的入口，
        所以退一步：直接打开该任务的输出目录（真实存在的那个目录），
        并在结果页里把标题里含任务名/目标的条目选中。
        这样用户点一下「至少能落到东西上」，不是死链接。
        """
        out = tk.get("output_dir") or ""
        if out and Path(out).is_dir():
            try:
                reveal_path(out)
            except Exception:  # noqa: BLE001
                pass
        try:
            items = self.ctx.pipeline.list_items()
        except Exception:  # noqa: BLE001
            items = []
        name = (tk.get("name") or "").strip()
        q = (tk.get("query") or "").strip()
        hit = None
        if name or q:
            for it in items:
                blob = f"{getattr(it, 'title', '')} {getattr(it, 'author', '')}"
                if (name and name in blob) or (q and q in blob):
                    hit = it
                    break
        if hit is not None:
            self.ctx.open_transcript(hit.platform, hit.item_id)
        else:
            self.ctx.goto_key("docs")
            self.ctx.status_message(
                f"已切到结果文档；该任务输出目录：{out or '（未记录）'}")

    def _task_menu(self, pos, tk: dict, card: QWidget) -> None:
        m = QMenu(self)
        a_view = m.addAction("查看该任务的文稿")
        a_copy = m.addAction("复制任务信息")
        a_out = m.addAction("打开输出目录")
        act = m.exec_(card.mapToGlobal(pos))
        if act is a_view:
            self._open_task(tk)
        elif act is a_copy:
            QApplication.clipboard().setText(
                f"{tk.get('platform')} · {tk.get('name')} · "
                f"{tk.get('query')} · {tk.get('created_at')}")
            self.ctx.status_message("任务信息已复制")
        elif act is a_out:
            d = tk.get("output_dir") or ""
            if d and Path(d).exists():
                try:
                    open_path(d)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "打不开", str(exc))
            else:
                QMessageBox.information(self, "目录不在",
                                        "该任务的输出目录不存在或已被移动。")

    # ---------------- 平台分布 ----------------
    def _fill_platforms(self) -> None:
        """平台 → 作品 两级树。

        ⚠️ 作品节点**按需填充**（先挂个占位子节点，展开时才查数据库）：
        几千条记录一次性全建成 QTreeWidgetItem 会卡到界面假死。
        """
        tree = getattr(self, "plat_tree", None)
        if tree is None:      # 兼容旧引用
            return
        tree.clear()
        self._plat_items.clear()
        try:
            rows = self.ctx.pipeline.db.platform_breakdown()
        except Exception:  # noqa: BLE001
            rows = []
        t = tokens(self.theme)
        best = ""
        best_n = -1
        for d in rows:
            plat = d.get("platform", "") or "unknown"
            n = d.get("items") or 0
            if n > best_n:
                best, best_n = plat, n
            root = QTreeWidgetItem([
                f"{PLAT_CN.get(plat, plat)}（{plat}）",
                f"{n:,}",
                f"{d.get('transcripts') or 0:,}",
                _words(d.get("words") or 0),
                f"{d.get('failed') or 0:,}" if (d.get("failed") or 0) else "",
                "",
            ])
            root.setData(0, Qt.UserRole, {"kind": "platform", "platform": plat})
            root.setToolTip(0, f"{PLAT_CN.get(plat, plat)}：{n} 个作品。"
                              "点左侧三角展开查看具体文件。")
            if d.get("failed"):
                root.setForeground(4, QColor(t.danger))
            f = root.font(0)
            f.setBold(True)
            root.setFont(0, f)
            # 占位子节点：让三角箭头出现（真正的子节点在展开时才填）
            root.addChild(QTreeWidgetItem(["加载中…"]))
            tree.addTopLevelItem(root)
        self.label_plat_hint.setText(
            f"采集最多的平台是 {PLAT_CN.get(best, best)}（{best_n} 个作品）。"
            f"展开平台行可以逐个查看文件，双击直接打开对应文稿。"
            if best else "还没有数据。")

    def _load_plat_children(self, root: QTreeWidgetItem) -> None:
        """把某个平台下的作品填进树（第一次展开时调用）。"""
        data = root.data(0, Qt.UserRole) or {}
        plat = data.get("platform", "")
        root.takeChildren()          # 清掉「加载中…」
        if plat in self._plat_items:
            items = self._plat_items[plat]
        else:
            try:
                items = self.ctx.pipeline.db.list_transcripts(
                    limit=800, platform=plat or None)
            except Exception:  # noqa: BLE001
                items = []
            self._plat_items[plat] = items
        if not items:
            empty = QTreeWidgetItem(["(该平台还没有转录记录)"])
            empty.setForeground(0, QColor(tokens(self.theme).text_faint))
            empty.setFlags(Qt.ItemIsEnabled)
            root.addChild(empty)
            return
        for d in items:
            title = (d.get("title") or d.get("item_id") or "")[:70]
            lp = d.get("local_path") or ""
            has_media = bool(lp) and Path(lp).exists()
            child = QTreeWidgetItem([
                f"　{title}",
                "",
                "✓" if (d.get("words") or 0) else "",
                f"{d.get('words') or 0:,}",
                "",
                _dur(d.get("duration") or 0),
            ])
            child.setData(0, Qt.UserRole, {
                "kind": "item", "platform": plat,
                "item_id": d.get("item_id", ""), "row": d,
            })
            child.setToolTip(
                0, f"{title}\n作者：{d.get('author') or '-'}\n"
                   f"模型：{d.get('asr_engine') or '-'}　"
                   f"中间文件：{'有' if has_media else '已删/无'}\n"
                   f"双击打开文稿")
            if not has_media:
                child.setForeground(0, QColor(tokens(self.theme).text_muted))
            root.addChild(child)

    def _on_plat_expanded(self, item: QTreeWidgetItem) -> None:
        """平台行被展开 → 若还只有「加载中…」占位，就把作品填进去。"""
        data = item.data(0, Qt.UserRole) or {}
        if data.get("kind") != "platform":
            return
        if item.childCount() == 1 and item.child(0).text(0) == "加载中…":
            self._load_plat_children(item)

    def _preview_row(self, row: dict) -> None:
        """把任意一条记录渲染到右侧预览（供平台树调用）。

        平台树里的 row 没有 local_path 之外的信息，先切到转录记录页
        找到同一条，找不到就直接拿 row 渲染。
        """
        self.stack.setCurrentIndex(0)
        try:
            self.tab_group[0].setChecked(True)
        except Exception:  # noqa: BLE001
            pass
        self._show_preview(row)

    def _on_plat_dbl(self, item: QTreeWidgetItem, _col: int) -> None:
        """双击行为分两段（用户要求「双击应该直接展开内容，再次双击才是打开」）。

        - 平台行：第一次双击展开，已展开则收起。
        - 作品行：早已展开可见，双击直接**就地预览**（不跳页）。
          再双击一次是「拉大预览」的等效效果，保持手感一致。
        """
        data = item.data(0, Qt.UserRole) or {}
        kind = data.get("kind")
        if kind == "platform":
            item.setExpanded(not item.isExpanded())
            return
        if kind != "item":
            return
        row = data.get("row") or {}
        if not row:
            return
        # 切到「转录记录」标签页并用同一条数据渲染预览 —— 这样双击作品
        # 是在历史页内部完成阅读，而不是被弹到「结果文档」去。
        self.stack.setCurrentIndex(0)
        try:
            self.tab_group[0].setChecked(True)
        except Exception:  # noqa: BLE001
            pass
        # 在转录列表里定位到同一行（没有就只渲染预览）
        hit = -1
        for r, d in enumerate(self.rows):
            if (d.get("platform") == row.get("platform")
                    and d.get("item_id") == row.get("item_id")):
                hit = r
                break
        if hit >= 0:
            self.table.selectRow(hit)
            self.table.scrollToItem(self.table.item(hit, 0))
        else:
            self._show_preview(row)
        try:
            total = sum(self.tr_split.sizes()) or 1000
            self.tr_split.setSizes([int(total * 0.28), int(total * 0.72)])
        except Exception:  # noqa: BLE001
            pass

    def _plat_menu(self, pos) -> None:
        item = self.plat_tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.UserRole) or {}
        m = QMenu(self)
        if data.get("kind") == "platform":
            a_exp = m.addAction("展开全部作品")
            a_col = m.addAction("折叠")
            act = m.exec_(self.plat_tree.viewport().mapToGlobal(pos))
            if act is a_exp:
                item.setExpanded(True)
            elif act is a_col:
                item.setExpanded(False)
            return
        d = data.get("row") or {}
        a_open = m.addAction("在右侧就地预览")
        a_editor = m.addAction("用 Notepad++ 打开逐字稿")
        a_media = m.addAction("打开中间文件")
        a_folder = m.addAction("打开所在文件夹")
        m.addSeparator()
        a_jump = m.addAction("跳转到结果文档页")
        m.addSeparator()
        a_copy = m.addAction("复制作品 ID")
        act = m.exec_(self.plat_tree.viewport().mapToGlobal(pos))
        if act is a_open:
            self._preview_row(d)
        elif act is a_editor:
            self._preview_row(d)
            self._pv_open_editor()
        elif act is a_jump:
            self._open_in_docs(d)
        elif act is a_media:
            p = d.get("local_path") or ""
            if p and Path(p).exists():
                try:
                    open_path(p)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "打不开", str(exc))
            else:
                QMessageBox.information(self, "文件不在", "该中间文件不存在。")
        elif act is a_folder:
            p = d.get("local_path") or ""
            if p and Path(p).exists():
                try:
                    reveal_path(p)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "打不开", str(exc))
        elif act is a_copy:
            QApplication.clipboard().setText(d.get("item_id", ""))
            self.ctx.status_message("作品 ID 已复制")

    # ---------------- 最近动态 ----------------
    def _fill_recent(self) -> None:
        while self.recent_col.count() > 1:
            it = self.recent_col.takeAt(0)
            wdg = it.widget()
            if wdg is not None:
                wdg.deleteLater()
        try:
            rows = self.ctx.pipeline.db.recent_activity(limit=30)
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            lb = QLabel("还没有动态。开始一次下载/转录后这里会实时反映。")
            lb.setProperty("role", "muted")
            self.recent_col.insertWidget(0, lb)
            return
        t = tokens(self.theme)
        for i, d in enumerate(rows):
            row = _ClickRow()
            row.set_click(lambda _d=d: self.ctx.open_transcript(
                _d.get("platform", ""), _d.get("item_id", "")))
            row.setToolTip("双击这行可以跳过去看它产出的 Markdown 文稿")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(6, 4, 6, 4)
            rl.setSpacing(8)
            dot = QLabel("●")
            dot.setStyleSheet(
                f"color:{t.local_status(d.get('status') or '')};font-size:11px;")
            dot.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            rl.addWidget(dot)
            has_doc = bool(d.get("words") or 0)
            title = (d.get("title") or d.get("item_id") or "")[:60]
            name = QLabel(("📄 " if has_doc else "　") + title)
            name.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            if has_doc:
                name.setCursor(Qt.PointingHandCursor)
            else:
                name.setToolTip("这条还没有产出文稿")
            rl.addWidget(name, 1)
            st = QLabel(STATUS_CN.get(d.get("status") or "", d.get("status") or ""))
            st.setProperty("role", "pill")
            st.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            rl.addWidget(st)
            when = QLabel(_ago(d.get("updated_at")))
            when.setProperty("role", "mono")
            when.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            rl.addWidget(when)
            self.recent_col.insertWidget(i, row)

    # ---------------------------------------------------------- 清中间文件
    def _scan_media(self) -> tuple[list[Path], int]:
        """找出所有可删的中间文件，返回 (路径列表, 总字节数)。

        两个来源：
        1. 数据库里每条记录的 local_path（存在才计）；
        2. 输出目录下的 media/ 子树（下载但没入库成功的残留）。
        文稿（.md/.txt/…）一律跳过——哪怕用户在配置里把输出目录设错了也不能误删。
        """
        seen: set[str] = set()
        files: list[Path] = []

        def add(p: Path) -> None:
            try:
                key = str(p.resolve()).lower()
            except Exception:  # noqa: BLE001
                key = str(p).lower()
            if key in seen or not p.is_file():
                return
            if p.suffix.lower() in self.DOC_EXT:
                return
            seen.add(key)
            files.append(p)

        try:
            rows = self.ctx.pipeline.db.list_transcripts(limit=5000)
        except Exception:  # noqa: BLE001
            rows = []
        for d in rows:
            lp = d.get("local_path") or ""
            if lp:
                add(Path(lp))

        # 输出目录下的 media 子树
        # ⚠️ Pipeline 上并没有 cfg 对象——media_dir 是 Pipeline 的属性，
        # output_dir 存在落盘的 config.json 里。早期版本这里写了
        # `self.ctx.pipeline.cfg.output_dir`，直接 AttributeError，
        # 又因为被 try/except 吞掉，表现成「扫描结果永远是空的」。
        try:
            media_dir = str(getattr(self.ctx.pipeline, "media_dir", "") or "")
        except Exception:  # noqa: BLE001
            media_dir = ""
        out = ""
        try:
            from .main_window import _load_cfg
            out = str(_load_cfg().get("output_dir", "") or "")
        except Exception:  # noqa: BLE001
            out = ""
        for base in (media_dir, str(Path(out) / "media") if out else ""):
            if not base:
                continue
            root = Path(base)
            if not root.is_dir():
                continue
            try:
                for p in root.rglob("*"):
                    if p.is_file() and p.suffix.lower() in self.MEDIA_EXT:
                        add(p)
            except Exception:  # noqa: BLE001
                continue

        total = 0
        for p in files:
            try:
                total += p.stat().st_size
            except Exception:  # noqa: BLE001
                pass
        return files, total

    @staticmethod
    def _mb(n: int) -> str:
        if n >= 1024 ** 3:
            return f"{n / 1024**3:.2f} GB"
        if n >= 1024 ** 2:
            return f"{n / 1024**2:.1f} MB"
        return f"{n / 1024:.0f} KB"

    def clean_intermediate(self) -> None:
        """一键清除中间文件（音视频），带空间统计 + 二次确认。"""
        files, total = self._scan_media()
        if not files:
            QMessageBox.information(
                self, "没有可清理的文件",
                "没找到中间的音视频文件。\n\n"
                "（可能转录后已经自动删除了，或者本来就没下载过。）")
            return
        # 按后缀归类，让用户在确认框里一眼看清删的是什么
        by_ext: dict[str, int] = {}
        for p in files:
            by_ext[p.suffix.lower()] = by_ext.get(p.suffix.lower(), 0) + 1
        breakdown = "、".join(
            f"{ext or '(无后缀)'} {n} 个"
            for ext, n in sorted(by_ext.items(), key=lambda kv: -kv[1]))

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("确认清除中间文件")
        box.setText(
            f"将删除 <b>{len(files)}</b> 个中间文件，"
            f"约 <b>{self._mb(total)}</b>。")
        box.setInformativeText(
            f"文件类型：{breakdown}\n\n"
            "⚠️ 这些音视频转录完成后已不再需要，但删除后<b>无法恢复</b>"
            "（想留着的话，可在配置页勾上「处理完保留音视频中间文件」）。\n\n"
            "已导出的文稿（Markdown / txt / json / csv）<b>不会被删除</b>。")
        b_ok = box.addButton("确认删除", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec_()
        if box.clickedButton() is not b_ok:
            return

        ok = 0
        failed = 0
        freed = 0
        for p in files:
            try:
                size = p.stat().st_size
                p.unlink()
                ok += 1
                freed += size
            except Exception:  # noqa: BLE001
                failed += 1

        # 数据库里清掉 local_path 引用，避免历史页一直显示「已删」
        try:
            for d in self.rows:
                lp = d.get("local_path") or ""
                if lp and not Path(lp).exists():
                    self.ctx.pipeline.db.update_item(
                        d.get("platform", ""), d.get("item_id", ""),
                        local_path="")
        except Exception:  # noqa: BLE001
            pass

        msg = f"已删除 {ok} 个文件，释放 {self._mb(freed)}。"
        if failed:
            msg += f"\n有 {failed} 个被占用或没有权限，没能删掉。"
        QMessageBox.information(self, "清理完成", msg)
        self.ctx.status_message(msg.replace("\n", " "))
        self.reload()

    # ---------------------------------------------------------- 交互
    def _cur(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.rows):
            return None
        cell = self.table.item(row, 0)
        if cell is not None:
            d = cell.data(Qt.UserRole)
            if isinstance(d, dict):
                return d
        return self.rows[row] if row < len(self.rows) else None

    def _on_dbl(self, _i) -> None:
        """双击一行 → 就地预览，**不跳页**。

        用户原话：「点击『查看文稿』会自动跳转到结果文档，这个跳转很不合理，
        就应该直接在历史记录这里显示」。所以双击改成「确保右侧预览显示这条
        记录并把预览区拉大」，想跳去结果页的话走右键菜单第一项。
        """
        d = self._cur()
        if not d:
            return
        self._show_preview(d)
        # 双击顺便把预览拉到更宽（列表让位给正文）
        try:
            total = sum(self.tr_split.sizes()) or 1000
            self.tr_split.setSizes([int(total * 0.28), int(total * 0.72)])
        except Exception:  # noqa: BLE001
            pass

    def _open_in_docs(self, d: dict) -> None:
        """真正跳到「结果文档」页（右键菜单里的显式选项）。"""
        self.ctx.open_transcript(d.get("platform", ""), d.get("item_id", ""))

    def _on_menu(self, pos) -> None:
        d = self._cur()
        if not d:
            return
        m = QMenu(self)
        # 顺序：先本页能做的事（就地预览/打开），再才是「离开本页」的跳转
        a_prev = m.addAction("在右侧预览")
        a_editor = m.addAction("用 Notepad++ 打开逐字稿")
        a_media = m.addAction("打开中间文件")
        a_folder = m.addAction("打开所在文件夹")
        m.addSeparator()
        a_kb = m.addAction("添加到知识库")
        m.addSeparator()
        a_jump = m.addAction("跳转到结果文档页")
        a_url = m.addAction("打开原文链接")
        m.addSeparator()
        a_copy_id = m.addAction("复制作品 ID")
        a_copy_path = m.addAction("复制中间文件路径")
        act = m.exec_(self.table.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act is a_prev:
            self._show_preview(d)
            self._on_dbl(None)
        elif act is a_editor:
            self._show_preview(d)
            self._pv_open_editor()
        elif act is a_media:
            p = d.get("local_path") or ""
            if p and Path(p).exists():
                try:
                    open_path(p)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, "打不开", str(exc))
            else:
                QMessageBox.information(
                    self, "文件不在",
                    "这个作品的音视频没有保留（可能转录后被自动删除了），"
                    "或者已被移动。\n\n可以在配置页把「处理完保留音视频中间文件」勾上。")
        elif act is a_folder:
            self._show_preview(d)
            self._pv_reveal()
        elif act is a_kb:
            self._show_preview(d)
            self._pv_add_kb()
        elif act is a_jump:
            self._open_in_docs(d)
        elif act is a_url:
            u = d.get("url") or ""
            if u:
                from PyQt5.QtCore import QUrl
                from PyQt5.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl(u))
        elif act is a_copy_id:
            QApplication.clipboard().setText(d.get("item_id", ""))
            self.ctx.status_message("作品 ID 已复制")
        elif act is a_copy_path:
            QApplication.clipboard().setText(d.get("local_path") or "")
            self.ctx.status_message("中间文件路径已复制")


# 状态中文名（最近动态用；与主窗口的 STATUS_TEXT 保持一致）
STATUS_CN = {
    "pending": "待处理", "queued": "已勾选", "downloading": "下载中",
    "downloaded": "已下载", "transcribing": "转录中", "done": "已完成",
    "failed": "失败", "skipped": "已跳过",
}
