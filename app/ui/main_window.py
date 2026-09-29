"""主窗口：左侧导航 + 右侧内容区（Agent 产品常见的布局）。

为什么推翻原来的「四步向导」
--------------------------
原来是顶部四个步骤标签 + 一个 QStackedWidget，问题是：

1. 步骤是**线性**的，但用户的实际动作不是——他可能想回去改配置、
   想翻上次转好的稿子、想看看中间文件堆哪了。向导结构把这些都塞进
   「返回上一步」，来回点很累。
2. 「文件」和「历史」这两块的诉求根本不属于任何一步，硬塞进去很别扭。
3. 顶部一排灰色标签当导航，视觉上信息密度低、层级也弱。

改成侧边栏之后：导航常驻、随时可点，页面之间是**平级**关系。
视觉上按主流 Agent 产品的做法收敛：品牌区在最上、导航项选中时用
强调色浅底、内容区用「背景分层 + 细边卡片」而不是加一堆边框阴影。

页面
----
采集配置 / 选择作品 / 下载转录 / 结果文档   ← 主流程，顺序执行时自动前进
文件 / 历史                                ← 随时可看，不参与流程
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, QUrl
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
    QDialog, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from ..core.control import ProcessControl
from ..core.cookie_manager import CookieManager
from ..core.models import (
    Item, OutputOptions, Task, MODE_CREATOR, MODE_MANUAL, MODE_TOPIC,
    STATUS_FAILED,
)
from ..core.verify import verify
from ..asr.engine import (
    COMPUTE_CHOICES, FasterWhisperEngine, MODEL_APPROX_GB, cached_models,
    gpu_free_gb, suggest_concurrency, vram_estimate_gb,
)
from ..crawler.registry import PLATFORMS, get, normalize_query
from ..export import knowledge
from ..export.writer import render_item_markdown
from ..pipeline import Pipeline
from . import hints
from .cookie_dialog import CookieDialog
from .editor import find_notepadpp, open_with_editor
from .fileops import from_url, open_path, reveal_path, to_file_url
from .files_page import FilesPage
from .history_page import HistoryPage
from .theme import build_qss, repolish, tokens
from .widgets import (
    Card, Collapsible, InfoButton, SectionTitle, divider, hint, labeled,
)
from .workers import CollectWorker, ExportWorker, ProcessWorker, VerifyWorker

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "mediaharvest.db"
MEDIA_DIR = ROOT / "data" / "media"
CONFIG_PATH = ROOT / "data" / "config.json"

MODE_LABEL = {
    MODE_CREATOR: "博主主页",
    MODE_TOPIC: "主题搜索",
    MODE_MANUAL: "手动链接",
}

# 导航项：(键, 图标文字, 标题, 副标题)
# ⚠️ 「文件」页已下线：它只是把「文稿目录 + 中间文件在哪」列了一遍，
# 而这些信息在「结果文档」页右键就有、在「历史」页也能看到，
# 属于纯重复入口。用户明确反馈「感觉完全没必要保留」。
# 保留 page_files 对象（内部还在用它做 kb 归档 / 目录同步），只是不放进导航。
NAV_ITEMS = [
    ("setup", "⚙", "采集配置", "平台、目标与处理选项"),
    ("preview", "☑", "选择作品", "勾选要处理的条目"),
    ("run", "▶", "下载转录", "进度、日志与运行控制"),
    ("docs", "📄", "结果文档", "逐字稿与导出文件"),
    ("history", "🕘", "历史", "跨任务的历史记录"),
]
NAV_KEYS = [k for k, *_ in NAV_ITEMS]

STATUS_TEXT = {
    "pending": "待处理", "queued": "已勾选", "downloading": "下载中",
    "downloaded": "已下载", "transcribing": "转录中", "done": "✓ 已完成",
    "failed": "✗ 失败", "skipped": "已跳过",
}
STATUS_ROLE = {
    "done": "success", "failed": "danger", "skipped": "faint",
    "downloading": "accent", "transcribing": "accent", "downloaded": "accent",
    "pending": "muted", "queued": "muted",
}


# ---------------------------------------------------------------- 通用工具
def _load_cfg() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_cfg(cfg: dict) -> None:
    """合并写回：只覆盖传进来的键，别把别处的设置抹掉。"""
    cur = _load_cfg()
    cur.update(cfg)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def _fmt_dur(sec: int) -> str:
    if not sec:
        return "-"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}" if m else f"0:{s:02d}"


def _escape(text: str) -> str:
    """日志正文要嵌进 HTML，先转义，否则标题里的 < > & 会把排版搞乱。"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _open_safe(parent: QWidget, fn, *args) -> None:
    """包一层：打开文件失败时给出可读提示，而不是静默失败或直接崩。"""
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(parent, "打不开", str(exc))


def _page_title(text: str, desc: str, theme: str = "light",
                info: tuple[str, str] | None = None) -> QWidget:
    """内容区统一页头：大标题 + 一行说明 +（可选）说明按钮。"""
    w = QWidget()
    col = QVBoxLayout(w)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(3)
    row = QHBoxLayout()
    row.setSpacing(8)
    t = QLabel(text)
    t.setProperty("role", "h1")
    row.addWidget(t)
    if info:
        row.addWidget(InfoButton(info[0], info[1], w, theme))
    row.addStretch(1)
    col.addLayout(row)
    d = QLabel(desc)
    d.setProperty("role", "muted")
    col.addWidget(d)
    return w


# ================================================================ ① 采集配置
class SetupPage(QWidget):
    """配置页。所有「看不懂会出事」的选项旁边都挂了 ⓘ 说明。"""

    def __init__(self, ctx: "MainWindow") -> None:
        super().__init__()
        self.ctx = ctx
        self._build()

    # ---------------------------------------------------------- 构建
    def _build(self) -> None:
        cfg = _load_cfg()
        self.theme = self.ctx.theme

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = _make_scroll()
        outer.addWidget(scroll)
        root = QVBoxLayout()
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)
        scroll.setWidget(_wrap(root))

        root.addWidget(_page_title(
            "采集配置", "选平台、填目标、配好处理选项，然后开始采集",
            self.theme,
            ("关于这一页",
             "<p>这一页决定「去哪儿采、采什么、采回来怎么处理」。</p>"
             "<p><b>采集</b>只拉作品列表（标题/时长/播放量），不下载文件，很快。</p>"
             "<p><b>下载与转录</b>发生在下一步——勾好作品之后再开始。</p>"
             "<p style='color:#888'>提示：提取文案必须先下载音视频，这一步绕不开。"
             "下载下来的文件是「中间文件」，转完文字后可以保留也可以自动删掉，"
             "在下面的「输出目录」里配置。</p>")))

        # --- Cookie 状态条 ---
        self.banner = QLabel()
        self.banner.setProperty("role", "banner")
        self.banner.setWordWrap(True)
        brow = QHBoxLayout()
        self.btn_recheck = QPushButton("重新验通")
        self.btn_cookie = QPushButton("Cookie 配置")
        self.btn_recheck.clicked.connect(self.recheck)
        self.btn_cookie.clicked.connect(lambda: self.ctx.open_cookie_dialog())
        bwrap = QWidget()
        bl = QHBoxLayout(bwrap)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.banner, 1)
        bl.addWidget(self.btn_recheck)
        bl.addWidget(self.btn_cookie)
        self.banner_row = bwrap
        root.addWidget(bwrap)

        # --- 采集目标 ---
        card1 = Card()
        card1.body.addWidget(SectionTitle("采集目标", self.theme))
        l1 = card1.body

        row_plat = QHBoxLayout()
        row_plat.setSpacing(8)
        self.combo_platform = QComboBox()
        for key, spec in PLATFORMS.items():
            self.combo_platform.addItem(spec.label, key)
        self.combo_platform.setMinimumWidth(180)
        row_plat.addWidget(QLabel("平台"))
        row_plat.addWidget(self.combo_platform)
        self.combo_platform.currentIndexChanged.connect(self.on_platform_changed)
        row_plat.addSpacing(18)
        row_plat.addWidget(QLabel("模式"))
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, QRadioButton] = {}
        for m in (MODE_CREATOR, MODE_TOPIC, MODE_MANUAL):
            rb = QRadioButton(MODE_LABEL[m])
            self.mode_group.addButton(rb)
            self.mode_buttons[m] = rb
            rb.toggled.connect(self.on_mode_changed)
            row_plat.addWidget(rb)
        row_plat.addStretch(1)
        l1.addLayout(row_plat)

        row_q = QHBoxLayout()
        row_q.setSpacing(8)
        row_q.addWidget(QLabel("目标"))
        self.edit_query = QLineEdit()
        self.btn_browse = QPushButton("选择文件夹")
        self.btn_browse.clicked.connect(self.browse_dir)
        row_q.addWidget(self.edit_query, 1)
        row_q.addWidget(self.btn_browse)
        l1.addLayout(row_q)

        self.label_hint = hint("")
        l1.addWidget(self.label_hint)

        self.label_urls = QLabel("链接列表（每行一条，也支持直接粘贴 App 的整段分享文案）")
        self.edit_urls = QPlainTextEdit()
        self.edit_urls.setPlaceholderText(
            "https://www.bilibili.com/video/BV1xx411c7mD\n"
            "https://www.douyin.com/video/7351234567890123456\n"
            "https://t.me/durov/531\n"
            "—— 也可以直接粘贴抖音/B站 App 复制出来的一整段分享文案，"
            "会自动抠出里面的链接")
        self.edit_urls.setMinimumHeight(84)
        l1.addWidget(self.label_urls)
        l1.addWidget(self.edit_urls)

        row_n = QHBoxLayout()
        row_n.setSpacing(8)
        row_n.addWidget(QLabel("采集数量"))
        self.spin_max = QSpinBox()
        self.spin_max.setRange(1, 500)
        self.spin_max.setValue(int(cfg.get("max_count", 20)))
        self.spin_max.setSuffix(" 个作品")
        row_n.addWidget(self.spin_max)
        row_n.addWidget(InfoButton(
            "关于采集数量", f"<p>{hints.MISC_TIPS['max_count']}</p>",
            self, self.theme))
        row_n.addStretch(1)
        l1.addLayout(row_n)

        # ⚠️ 「每个作品抓多少评论」的输入框**从这里移走**了。
        # 理由：不同作品的评论价值差别很大（有的想全抓、有的只要前几条），
        # 在配置页定一个全局数字反而变成了「猜一个数」。现在：
        #   - 这里只放一个总开关「是否抓评论」；
        #   - 具体条数在「选择作品」页按作品设，或双击作品时在旁边填。
        self.chk_comments = QCheckBox("抓取评论（条数稍后在选择作品时逐条设置）")
        self.chk_comments.setChecked(bool(cfg.get("fetch_comments", False)))
        row_c = QHBoxLayout()
        row_c.addWidget(self.chk_comments)
        row_c.addWidget(InfoButton(
            "关于评论抓取", f"<p>{hints.MISC_TIPS['comments']}</p>",
            self, self.theme))
        row_c.addStretch(1)
        l1.addLayout(row_c)

        self.row_proxy = QWidget()
        rp = QHBoxLayout(self.row_proxy)
        rp.setContentsMargins(0, 0, 0, 0)
        rp.setSpacing(8)
        rp.addWidget(QLabel("代理"))
        self.edit_proxy = QLineEdit()
        self.edit_proxy.setText(cfg.get("proxy", "http://127.0.0.1:7897"))
        self.edit_proxy.setPlaceholderText("http://127.0.0.1:7897")
        rp.addWidget(self.edit_proxy, 1)
        l1.addWidget(self.row_proxy)

        self.label_note = QLabel()
        self.label_note.setProperty("role", "warning")
        self.label_note.setWordWrap(True)
        l1.addWidget(self.label_note)
        root.addWidget(card1)

        # --- 处理选项（精简：只留 3 个核心开关）---
        card2 = Card()
        card2.body.addWidget(SectionTitle(
            "处理选项", self.theme,
            ("转录为什么必须先下载",
             "<p><b>提取文案 = 先拿到音频，再跑识别模型。</b>"
             "没有音视频文件就无从转录，所以「转录」勾上之后，"
             "程序一定会先下载。</p>"
             "<p>下载下来的 mp4/m4a 属于<b>中间产物</b>，转成文字后就没用了。"
             "去留由下面「处理完是否保留音视频」决定：</p>"
             "<ul style='margin-left:-12px'>"
             "<li>勾选保留 → 留在中间目录，以后想换个更大的模型重转一遍很方便；</li>"
             "<li>不勾选 → 转录完成的瞬间自动删除，只留 Markdown，最省磁盘。</li>"
             "</ul>"
             "<p style='color:#888'>注意：本地文件模式下不会删你原来的素材文件，"
             "只清理程序自己下载下来的那些。</p>")))
        l2 = card2.body

        self.chk_transcribe = QCheckBox("转录生成逐字稿（勾上后会自动先下载音视频）")
        self.chk_keep_media = QCheckBox("处理完保留音视频中间文件（不勾则转录后自动删除）")
        self.chk_transcribe.setChecked(True)
        self.chk_keep_media.setChecked(bool(cfg.get("keep_media", True)))

        row_t = QHBoxLayout()
        row_t.addWidget(self.chk_transcribe)
        row_t.addWidget(InfoButton(
            "关于「转录生成逐字稿」", hints.MISC_DETAILS["keep_media"],
            self, self.theme))
        row_t.addStretch(1)
        l2.addLayout(row_t)

        row_k = QHBoxLayout()
        row_k.addWidget(self.chk_keep_media)
        row_k.addWidget(InfoButton(
            "关于「保留音视频」", hints.MISC_DETAILS["keep_media"],
            self, self.theme))
        l2.addLayout(row_k)

        self.label_keep_hint = hint(
            "不保留时：转录一完成就删掉该文件，磁盘占用最小（推荐只要文案时这样选）。")
        l2.addWidget(self.label_keep_hint)
        root.addWidget(card2)

        # --- 生成内容（三种互斥模式）---
        card_out = Card()
        card_out.body.addWidget(SectionTitle(
            "生成内容", self.theme,
            ("三种模式怎么选",
             "<p>只影响<b>导出文档长什么样</b>，不影响转录本身。三种模式互斥，"
             "每种里面已经把「一份正常文稿该有的东西」都配齐了——"
             "时间戳、纯文本、原简介、评论、关键词标签，不用你再一项项勾。</p>"
             "<p><b>带时间戳</b>：正文前加 <code>[mm:ss -&gt; mm:ss]</code>，"
             "适合做字幕、写文章时对时间定位。同时附一份无时间戳的纯文本方便复制。</p>"
             "<p><b>纯文本</b>：只有整段文字，最干净，适合直接拿去用。</p>"
             "<p><b>Obsidian</b>：加 YAML frontmatter、层级标签、折叠点，"
             "在 Obsidian 里能按属性/标签筛选。</p>"
             "<p style='color:#888'>三种模式都会自动从正文里抽<b>关键词作为标签</b>"
             "（非 Obsidian 模式贴在文档末尾）。</p>")))
        lo = card_out.body

        # ⚠️ 提示标签必须**先于**单选按钮创建。
        # 单选按钮的 toggled 会连到 _on_content_mode_changed，而那个方法要读
        # self.label_content 来刷新文案。如果先建按钮、后建标签，
        # 下面 setChecked(True) 触发信号时标签还不存在，slot 里抛
        # AttributeError —— PyQt5 对「槽函数里的未捕获异常」是直接 qFatal
        # 终止进程（Windows 上表现为静默退出，无 traceback），很难查。
        self.label_content = hint("")
        lo.addWidget(self.label_content)

        # 用单选按钮表达「三选一」，比复选框更直白——用户一眼就知道只能挑一个
        self.mode_content_group = QButtonGroup(self)
        self.mode_content_buttons: dict[str, QRadioButton] = {}
        mode_specs = [
            (OutputOptions.MODE_TIMESTAMPED, "带时间戳的逐字稿",
             "时间戳 + 纯文本 + 简介 + 评论"),
            (OutputOptions.MODE_PLAIN, "纯文本逐字稿",
             "整段文字 + 简介 + 评论，最干净"),
            (OutputOptions.MODE_OBSIDIAN, "Obsidian 格式逐字稿",
             "frontmatter + 层级标签 + 时间戳 + 纯文本"),
        ]
        for key, label, desc in mode_specs:
            row = QHBoxLayout()
            row.setSpacing(10)
            rb = QRadioButton(label)
            rb.setMinimumWidth(160)
            self.mode_content_group.addButton(rb)
            self.mode_content_buttons[key] = rb
            row.addWidget(rb)
            d = QLabel(desc)
            d.setProperty("role", "faint")
            row.addWidget(d)
            row.addStretch(1)
            lo.addLayout(row)
        # 默认选中「带时间戳」（放在 connect 之前的按钮已就绪）
        for rb in self.mode_content_buttons.values():
            rb.toggled.connect(self._on_content_mode_changed)
        self.mode_content_buttons[OutputOptions.MODE_TIMESTAMPED].setChecked(True)

        root.addWidget(card_out)

        # --- 高级设置（默认折叠）---
        # 这里的每一项都有个好默认值。收起来之后配置页只剩「采集目标 +
        # 处理选项 + 生成内容 + 输出目录」四块，肉眼可见地短了一大截。
        card_adv = Card()
        card_adv.body.addWidget(SectionTitle("高级设置", self.theme))
        la = card_adv.body
        la.addWidget(hint(
            "下面这些默认值已经按你的机器调好了，不确定就别动。"
            "想调速度/准确率的取舍时再展开。"))

        adv = Collapsible(
            "展开高级设置（模型 · 设备 · 精度 · 解码 · 并发 · 目录与外部工具）",
            self.theme, summary="当前：small · auto · float16 · beam5",
        )
        la.addWidget(adv)
        alp = adv.body

        # 识别模型 / 运行设备 —— 两个最需要解释的下拉框
        row_asr = QHBoxLayout()
        row_asr.setSpacing(8)
        row_asr.addWidget(QLabel("识别模型"))
        self.combo_model = QComboBox()
        for key in hints.MODEL_ORDER:
            self.combo_model.addItem(hints.MODEL_INFO[key]["label"], key)
        self.combo_model.setMinimumWidth(200)
        row_asr.addWidget(self.combo_model)
        self.info_model = InfoButton("识别模型怎么选", "", self, self.theme)
        row_asr.addWidget(self.info_model)
        self.combo_model.currentIndexChanged.connect(self._on_model_changed)

        row_asr.addSpacing(24)
        row_asr.addWidget(QLabel("运行设备"))
        self.combo_device = QComboBox()
        for key in ("auto", "cuda", "cpu"):
            self.combo_device.addItem(hints.DEVICE_INFO[key]["label"], key)
        self.combo_device.setMinimumWidth(190)
        row_asr.addWidget(self.combo_device)
        self.info_device = InfoButton("运行设备怎么选", "", self, self.theme)
        row_asr.addWidget(self.info_device)
        self.combo_device.currentIndexChanged.connect(self._on_device_changed)
        row_asr.addStretch(1)
        alp.addLayout(row_asr)

        self.label_model = hint("")
        alp.addWidget(self.label_model)

        row_p1 = QHBoxLayout()
        row_p1.setSpacing(8)
        row_p1.addWidget(QLabel("计算精度"))
        self.combo_compute = QComboBox()
        for val, label in COMPUTE_CHOICES:
            self.combo_compute.addItem(label, val)
        row_p1.addWidget(self.combo_compute, 2)
        self.info_compute = InfoButton("计算精度怎么选", "", self, self.theme)
        row_p1.addWidget(self.info_compute)
        self.combo_compute.currentIndexChanged.connect(self._on_compute_changed)

        row_p1.addSpacing(24)
        row_p1.addWidget(QLabel("解码策略"))
        self.combo_beam = QComboBox()
        self.combo_beam.addItem("beam5（准，默认）", 5)
        self.combo_beam.addItem("beam1（快约 30%，同音字略差）", 1)
        row_p1.addWidget(self.combo_beam, 1)
        row_p1.addWidget(InfoButton("关于解码策略", f"<p>{hints.MISC_TIPS['beam']}</p>",
                                    self, self.theme))
        row_p1.addStretch(1)
        alp.addLayout(row_p1)

        row_p2 = QHBoxLayout()
        row_p2.setSpacing(8)
        row_p2.addWidget(QLabel("执行模式"))
        self.combo_pipeline = QComboBox()
        self.combo_pipeline.addItem("重叠：下载与转录并行（推荐，最快）", "overlap")
        self.combo_pipeline.addItem("串行：逐条下载再转录（最稳）", "serial")
        self.combo_pipeline.setCurrentIndex(0)
        row_p2.addWidget(self.combo_pipeline, 2)
        row_p2.addWidget(InfoButton("关于执行模式", f"<p>{hints.MISC_TIPS['pipeline']}</p>",
                                    self, self.theme))

        row_p2.addSpacing(24)
        row_p2.addWidget(QLabel("并发转录"))
        self.spin_conc = QSpinBox()
        self.spin_conc.setRange(1, 4)
        self.spin_conc.setValue(1)
        self.spin_conc.setSuffix(" 个实例")
        row_p2.addWidget(self.spin_conc)
        self.info_conc = InfoButton("关于并发转录", f"<p>{hints.MISC_TIPS['concurrency']}</p>",
                                    self, self.theme)
        row_p2.addWidget(self.info_conc)
        self.btn_auto_conc = QPushButton("按显存自动")
        self.btn_auto_conc.setToolTip("读取当前空闲显存，估算能跑几个转录实例")
        self.btn_auto_conc.clicked.connect(self.auto_concurrency)
        row_p2.addWidget(self.btn_auto_conc)
        row_p2.addStretch(1)
        alp.addLayout(row_p2)

        row_p3 = QHBoxLayout()
        row_p3.setSpacing(8)
        row_p3.addWidget(QLabel("CPU 线程"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(0, 32)
        self.spin_threads.setValue(12)
        self.spin_threads.setSpecialValueText("自动")
        row_p3.addWidget(self.spin_threads)
        row_p3.addWidget(InfoButton("关于 CPU 线程", f"<p>{hints.MISC_TIPS['cpu_threads']}</p>",
                                    self, self.theme))
        self.label_threads_hint = hint("（实测给足线程比默认快约 20%）")
        row_p3.addWidget(self.label_threads_hint, 1)
        alp.addLayout(row_p3)

        self.label_perf = hint("")
        alp.addWidget(self.label_perf)

        # 让摘要跟着选项变，收起时也能看到当前配置
        for combo in (self.combo_model, self.combo_device,
                      self.combo_compute, self.combo_beam):
            combo.currentIndexChanged.connect(self._refresh_adv_summary)
        self._adv = adv
        root.addWidget(card_adv)

        # --- 输出目录（拆成两份：成品 / 中间文件）---
        card_dir = Card()
        card_dir.body.addWidget(SectionTitle(
            "输出目录", self.theme,
            ("为什么分成两个目录",
             "<p><b>Markdown 文稿目录</b>：成品。逐字稿 .md、评论 .md、"
             "汇总 csv/json、索引页 INDEX.md、Canvas 看板都放这里。"
             "这个目录你会长期翻看，建议放同步盘或 Obsidian 库里。</p>"
             "<p><b>音视频中间目录</b>：过程产物。为了提取文案而下载的 mp4/m4a。"
             "它们体积大、用完即弃，所以单独放一个目录，"
             "既能一眼看出占了多少空间，也方便「不保留」时整体清理。</p>"
             "<p style='color:#888'>两个目录都可以随时在「文件」页里查看和更改。</p>")))
        ld = card_dir.body

        d1 = QHBoxLayout()
        d1.setSpacing(8)
        lb1 = QLabel("Markdown 文稿")
        lb1.setFixedWidth(110)
        lb1.setProperty("role", "muted")
        self.edit_output = QLineEdit(cfg.get("output_dir", str(ROOT / "out")))
        b_out = QPushButton("浏览")
        b_out.clicked.connect(lambda: self.browse_output("doc"))
        b_open_out = QPushButton("打开")
        b_open_out.clicked.connect(
            lambda: _open_safe(self, open_path, self.edit_output.text().strip()))
        d1.addWidget(lb1)
        d1.addWidget(self.edit_output, 1)
        d1.addWidget(b_out)
        d1.addWidget(b_open_out)
        ld.addLayout(d1)

        d2 = QHBoxLayout()
        d2.setSpacing(8)
        lb2 = QLabel("音视频中间文件")
        lb2.setFixedWidth(110)
        lb2.setProperty("role", "muted")
        self.edit_media_dir = QLineEdit(
            cfg.get("media_dir", str(ROOT / "data" / "media")))
        b_media = QPushButton("浏览")
        b_media.clicked.connect(lambda: self.browse_output("media"))
        b_open_media = QPushButton("打开")
        b_open_media.clicked.connect(
            lambda: _open_safe(self, open_path, self.edit_media_dir.text().strip()))
        d2.addWidget(lb2)
        d2.addWidget(self.edit_media_dir, 1)
        d2.addWidget(b_media)
        d2.addWidget(b_open_media)
        ld.addLayout(d2)

        self.label_dir_hint = hint("")
        ld.addWidget(self.label_dir_hint)
        # ⚠️ 输出目录搬进「高级设置」：
        # 用户反馈「采集配置里的『外部工具与知识库』以及『输出目录』，
        # 应该归到高级配置里，让用户自己去设路径即可。这些东西没必要
        # 每次都让用户选，不需要堆那么多选项」。
        # 所以不再 root.addWidget(card_dir)，而是插到折叠区里。
        alp.addWidget(card_dir)

        # --- 外部工具与知识库 ---
        # 这两项以前只能通过右键菜单发现（「添加到知识库」会引导设目录），
        # 用户反馈说根本不知道在哪配。挪到配置页显式曝光。
        card_ext = Card()
        card_ext.body.addWidget(SectionTitle(
            "外部工具与知识库", self.theme,
            ("这两项是干什么的",
             "<p><b>Notepad++ 路径</b>：双击逐字稿、或点「用 Notepad++ 打开」时，"
             "用哪个编辑器打开。留空会自动在注册表和常见安装位置里找；"
             "找不到就退回系统默认程序。</p>"
             "<p><b>知识库目录</b>：右键「添加到知识库」时，选中的文稿会被"
             "<b>复制</b>（不是移动）到这个目录，按「平台/作者」分子文件夹。"
             "原文件不动，所以可以放心重复归档。</p>")))
        le = card_ext.body

        e1 = QHBoxLayout()
        e1.setSpacing(8)
        lb3 = QLabel("Notepad++ 路径")
        lb3.setFixedWidth(110)
        lb3.setProperty("role", "muted")
        self.edit_editor = QLineEdit(str(cfg.get("editor_path", "") or ""))
        self.edit_editor.setPlaceholderText("留空 = 自动探测（推荐）")
        b_ed = QPushButton("浏览")
        b_ed.clicked.connect(self._browse_editor)
        b_ed_auto = QPushButton("自动探测")
        b_ed_auto.setToolTip("在注册表和常见安装目录里找 Notepad++")
        b_ed_auto.clicked.connect(self._detect_editor)
        self.label_editor = hint("")
        e1.addWidget(lb3)
        e1.addWidget(self.edit_editor, 1)
        e1.addWidget(b_ed_auto)
        e1.addWidget(b_ed)
        le.addLayout(e1)
        le.addWidget(self.label_editor)

        e2 = QHBoxLayout()
        e2.setSpacing(8)
        lb4 = QLabel("知识库目录")
        lb4.setFixedWidth(110)
        lb4.setProperty("role", "muted")
        self.edit_kb = QLineEdit(str(cfg.get("kb_dir", "") or ""))
        self.edit_kb.setPlaceholderText("留空 = 每次归档时再选")
        b_kb = QPushButton("浏览")
        b_kb.clicked.connect(self._browse_kb)
        b_open_kb = QPushButton("打开")
        b_open_kb.clicked.connect(
            lambda: _open_safe(self, open_path, self.edit_kb.text().strip()))
        e2.addWidget(lb4)
        e2.addWidget(self.edit_kb, 1)
        e2.addWidget(b_kb)
        e2.addWidget(b_open_kb)
        le.addLayout(e2)

        e3 = QHBoxLayout()
        e3.setSpacing(8)
        self.chk_kb_flat = QCheckBox("归档时平铺，不按「平台/作者」分文件夹")
        self.chk_kb_flat.setChecked(bool(cfg.get("kb_flat", False)))
        self.chk_kb_flat.setToolTip(
            "勾上 = 所有文稿直接丢进知识库根目录；\n"
            "不勾 = 按「平台/作者」建两层子文件夹（推荐）。")
        e3.addWidget(self.chk_kb_flat)
        e3.addStretch(1)
        le.addLayout(e3)

        # 编辑器路径一改就存，探测结果也顺手写进去
        self.edit_editor.editingFinished.connect(self._commit_editor)
        self.edit_kb.editingFinished.connect(
            lambda: _save_cfg({"kb_dir": self.edit_kb.text().strip()}))
        self.chk_kb_flat.toggled.connect(
            lambda v: _save_cfg({"kb_flat": bool(v)}))
        # 同样收进高级设置（原先 root.addWidget(card_ext) 会占掉半屏）
        alp.addWidget(card_ext)
        # 进页面时顺手刷一下探测结果
        QTimer.singleShot(0, self._detect_editor)

        root.addStretch(1)
        row_go = QHBoxLayout()
        self.label_ready = hint("")
        row_go.addWidget(self.label_ready, 1)
        self.btn_start = QPushButton("开始采集")
        self.btn_start.setProperty("role", "primary")
        self.btn_start.setMinimumHeight(38)
        self.btn_start.clicked.connect(self.start_collect)
        row_go.addWidget(self.btn_start)
        root.addLayout(row_go)

        self._restore_perf_cfg(cfg)
        self._restore_output_opts(cfg)
        self._init_perf_hint()
        self._on_model_changed()
        self._on_device_changed()
        self._on_compute_changed()
        self.on_platform_changed()

    # ---------------------------------------------------------- 说明文案
    def _on_model_changed(self) -> None:
        key = self.combo_model.currentData()
        self.info_model.update_html(
            f"识别模型：{hints.MODEL_INFO[key]['label']}", hints.model_detail(key))
        # 下拉框本身也挂 tooltip，鼠标悬停就能看到，不用点开
        self.combo_model.setToolTip(hints.model_tip(key))
        self._update_model_label()

    def _on_device_changed(self) -> None:
        key = self.combo_device.currentData()
        self.info_device.update_html(
            f"运行设备：{hints.DEVICE_INFO[key]['label']}", hints.device_detail(key))
        self.combo_device.setToolTip(hints.device_tip(key))

    def _on_compute_changed(self) -> None:
        key = self.combo_compute.currentData() or "auto"
        self.info_compute.update_html(
            f"计算精度：{hints.COMPUTE_INFO[key]['label']}", hints.compute_detail(key))
        self.combo_compute.setToolTip(hints.compute_tip(key))

    # ---------------------------------------------------------- 交互
    def platform(self) -> str:
        return self.combo_platform.currentData()

    def spec(self):
        return get(self.platform())

    def mode(self) -> str:
        for m, rb in self.mode_buttons.items():
            if rb.isChecked():
                return m
        return MODE_CREATOR

    def on_platform_changed(self) -> None:
        spec = self.spec()
        for m, rb in self.mode_buttons.items():
            rb.setEnabled(m in spec.modes)
        if self.mode() not in spec.modes:
            self.mode_buttons[spec.modes[0]].setChecked(True)
        self.row_proxy.setVisible(spec.need_proxy)
        self.btn_browse.setVisible(spec.key == "local")
        self.label_note.setText(spec.note)
        self.label_note.setVisible(bool(spec.note))
        self.update_banner()
        self.on_mode_changed()

    def on_mode_changed(self) -> None:
        manual = self.mode() == MODE_MANUAL
        self.label_urls.setVisible(manual)
        self.edit_urls.setVisible(manual)
        self.edit_query.setVisible(not manual)
        self.btn_browse.setVisible(
            self.spec().key == "local" and not manual)
        if manual:
            self.label_hint.setText("把链接一行一个粘进去，不需要 cookie")
        else:
            self.label_hint.setText(self.spec().input_hint)
            self.edit_query.setPlaceholderText(self.spec().placeholder)
        self._update_ready()

    def update_banner(self) -> None:
        spec = self.spec()
        if not spec.need_cookie:
            self.banner_row.setVisible(spec.need_proxy)
            if spec.need_proxy:
                self.banner.setProperty("role", "banner-info")
                self.banner.setText(
                    "ℹ️ Telegram 在国内需要代理才能访问，请确认下面的代理地址可用")
                repolish(self.banner)
                self.btn_cookie.setVisible(False)
            return
        self.banner_row.setVisible(True)
        self.btn_cookie.setVisible(True)
        ok, msg = self.ctx.cm.quick_check(spec.key)
        if ok:
            self.banner.setProperty("role", "banner-ok")
            self.banner.setText(f"✅ {spec.label}：{msg}（采集前建议点一次「重新验通」）")
        else:
            self.banner.setProperty("role", "banner-bad")
            self.banner.setText(
                f"⚠️ {spec.label}：{msg} —— 未配置将无法采集，只能使用「手动链接」模式")
        repolish(self.banner)

    def recheck(self) -> None:
        spec = self.spec()
        if not spec.need_cookie:
            return
        self.btn_recheck.setEnabled(False)
        self.banner.setText("正在验证…")
        self._vw = VerifyWorker(lambda: verify(self.ctx.cm, spec.key))
        self._vw.done.connect(self._on_recheck)
        self._vw.start()

    def _on_recheck(self, ok: bool, msg: str) -> None:
        self.btn_recheck.setEnabled(True)
        self.banner.setProperty("role", "banner-ok" if ok else "banner-bad")
        self.banner.setText(f"✅ {msg}" if ok else
                            f"❌ {msg} —— 请重新用 EditThisCookie 导出粘贴")
        repolish(self.banner)

    def _update_model_label(self) -> None:
        key = self.combo_model.currentData()
        cached = cached_models()
        if key in cached:
            self.label_model.setText(f"✅ {key} 模型已下载到本地，可直接使用")
        else:
            gb = MODEL_APPROX_GB.get(key, 0)
            self.label_model.setText(
                f"⚠️ {key} 模型尚未下载（约 {gb:.2f} GB），首次转录时会自动从国内镜像"
                f"下载。已下载：{', '.join(cached) if cached else '无'}")

    def _sync_content_checks(self) -> None:
        """（兼容旧调用）内容模式变了 → 刷新提示文案。"""
        self._on_content_mode_changed()

    def _on_content_mode_changed(self, *_a) -> None:
        """三种内容模式互斥，这里只负责把「选了哪个」讲清楚。

        ⚠️ 必须做存在性检查：这个方法是 Qt 信号槽，构建过程中就会被调用，
        此时界面部件可能还没建全。槽函数里抛异常 → PyQt5 直接终止进程。
        """
        lb = getattr(self, "label_content", None)
        if lb is None:
            return
        key = self.content_mode()
        tips = {
            OutputOptions.MODE_TIMESTAMPED:
                "已选：带时间戳的逐字稿 → 文档里会有 [mm:ss -> mm:ss] 前缀，"
                "同时附一份无时间戳的纯文本方便复制。",
            OutputOptions.MODE_PLAIN:
                "已选：纯文本逐字稿 → 只输出整段文字（+ 简介 + 评论），最干净。",
            OutputOptions.MODE_OBSIDIAN:
                "已选：Obsidian 格式 → 加 YAML frontmatter、层级标签（platform/"
                "author/topic）与折叠点，Obsidian 里可按属性筛选。",
        }
        lb.setText(tips.get(key, ""))
        lb.setProperty("role", "muted")
        repolish(lb)
        self._update_ready()

    def content_mode(self) -> str:
        buttons = getattr(self, "mode_content_buttons", None)
        if not buttons:
            return OutputOptions.MODE_TIMESTAMPED
        for m, rb in buttons.items():
            if rb.isChecked():
                return m
        return OutputOptions.MODE_TIMESTAMPED

    def _refresh_adv_summary(self) -> None:
        """把高级设置的当前值浓缩成一行，收起时也能看到。"""
        adv = getattr(self, "_adv", None)
        if adv is None or not getattr(self, "combo_model", None):
            return
        model = self.combo_model.currentData() or "small"
        device = self.combo_device.currentData() or "auto"
        compute = self.combo_compute.currentData() or "auto"
        beam = self.combo_beam.currentData() or 5
        adv.set_summary(
            f"当前：{model} · {device} · {compute} · beam{beam}"
            f" · 并发{self.spin_conc.value()}")

    def _update_ready(self) -> None:
        """底部一行「准备就绪」小结，让用户点开始前心里有数。"""
        lb = getattr(self, "label_ready", None)
        if lb is None:
            return
        n_parts = []
        if self.chk_transcribe.isChecked():
            n_parts.append(f"转录（{self.combo_model.currentData()}）")
        if self.chk_comments.isChecked():
            n_parts.append("抓评论")
        if not n_parts:
            n_parts.append("只采集列表")
        n_parts.append(self.mode_label_short())
        lb.setText(f"即将执行：{' + '.join(n_parts)}")
        lb.setProperty("role", "muted")
        repolish(lb)
        self._refresh_adv_summary()

    def mode_label_short(self) -> str:
        return {
            OutputOptions.MODE_TIMESTAMPED: "带时间戳逐字稿",
            OutputOptions.MODE_PLAIN: "纯文本逐字稿",
            OutputOptions.MODE_OBSIDIAN: "Obsidian 格式",
        }.get(self.content_mode(), "逐字稿")

    # ---------------------------------------------------------- 性能探测
    def _init_perf_hint(self) -> None:
        """探测显卡，给出精度与并发建议。"""
        if not FasterWhisperEngine.cuda_available():
            self.label_perf.setText(
                "未检测到可用 CUDA 显卡，将使用 CPU 转录。"
                "实测同一段素材显卡 76 秒 / CPU 344 秒（约 4.5 倍差距），"
                "建议把「识别模型」降到 base 或 tiny。")
            self.spin_conc.setValue(1)
            self.spin_conc.setEnabled(False)
            return
        major, name = FasterWhisperEngine.detect_arch()
        free = gpu_free_gb()
        rec = "int8_float16" if major >= 8 else "float16"
        for i in range(self.combo_compute.count()):
            if self.combo_compute.itemData(i) == rec:
                self.combo_compute.setCurrentIndex(i)
                break
        # 模型默认值也按显存推荐一次（用户没存过配置时才覆盖）
        if not _load_cfg().get("model"):
            best = hints.recommend_model()
            for i in range(self.combo_model.count()):
                if self.combo_model.itemData(i) == best:
                    self.combo_model.setCurrentIndex(i)
                    break
        n, _ = self._suggest_conc(self.combo_model.currentData(), rec)
        self.spin_conc.setValue(n)
        self.label_perf.setText(
            f"显卡：{name or 'CUDA'}（算力 {major}.x），空闲显存 {free:.1f} GB。"
            f"推荐精度 {rec}，建议并发 {n} 个实例"
            f"（估算单实例约 {vram_estimate_gb(self.combo_model.currentData(), rec):.2f} GB）。")
        if not getattr(self, "_perf_hooked", False):
            self.combo_model.currentIndexChanged.connect(self._refresh_perf_hint)
            self.combo_compute.currentIndexChanged.connect(self._refresh_perf_hint)
            self._perf_hooked = True

    def _refresh_perf_hint(self) -> None:
        if not FasterWhisperEngine.cuda_available():
            return
        major, name = FasterWhisperEngine.detect_arch()
        rec = "int8_float16" if major >= 8 else "float16"
        model = self.combo_model.currentData()
        free = gpu_free_gb()
        n, _ = self._suggest_conc(model, rec)
        self.label_perf.setText(
            f"显卡：{name or 'CUDA'}（算力 {major}.x），空闲显存 {free:.1f} GB。"
            f"推荐精度 {rec}，当前模型 {model} 建议并发 {n} 个实例"
            f"（估算单实例约 {vram_estimate_gb(model, rec):.2f} GB）。")
        self._update_ready()

    def _avg_duration(self) -> float:
        """已采集素材的平均时长（秒）；没有素材时返回 0。"""
        try:
            durs = [it.duration for it in getattr(self.ctx, "items", []) if it.duration]
        except Exception:  # noqa: BLE001
            return 0.0
        return sum(durs) / len(durs) if durs else 0.0

    def _suggest_conc(self, model: str, compute: str) -> tuple[int, float]:
        avg = self._avg_duration()
        return suggest_concurrency(model, compute, avg_duration_sec=avg), avg

    def auto_concurrency(self) -> None:
        compute = self.combo_compute.currentData() or "float16"
        if compute == "auto":
            major, _ = FasterWhisperEngine.detect_arch()
            compute = "int8_float16" if major >= 8 else "float16"
        n, avg = self._suggest_conc(self.combo_model.currentData(), compute)
        self.spin_conc.setValue(n)
        if avg and avg < 60:
            self.ctx.status_message(
                f"素材平均时长仅 {avg:.0f} 秒，并发不会更快（固定开销占主），已设为 1 个实例")
        else:
            self.ctx.status_message(f"已按显存把并发设为 {n} 个转录实例")

    def browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择包含视频的文件夹")
        if d:
            self.edit_query.setText(d)

    def browse_output(self, which: str) -> None:
        cur = (self.edit_output if which == "doc" else self.edit_media_dir).text()
        d = QFileDialog.getExistingDirectory(self, "选择目录", cur or "")
        if not d:
            return
        if which == "doc":
            self.edit_output.setText(d)
            self.ctx.set_output_dir(d)
        else:
            self.edit_media_dir.setText(d)
            self.ctx.set_media_dir(d)

    # ------------------------------------------------- 外部工具 / 知识库
    def _browse_editor(self) -> None:
        cur = self.edit_editor.text().strip()
        p, _ = QFileDialog.getOpenFileName(
            self, "选择文本编辑器可执行文件", cur or "C:/",
            "可执行文件 (*.exe);;所有文件 (*)")
        if p:
            self.edit_editor.setText(p)
            self._commit_editor()

    def _commit_editor(self) -> None:
        """路径改动落盘，并把探测状态显式告诉用户（别让他猜）。"""
        p = self.edit_editor.text().strip()
        _save_cfg({"editor_path": p})
        self._refresh_editor_hint(p)

    def _detect_editor(self) -> None:
        """探测 Notepad++；已手动指定且有效时不覆盖用户选择。"""
        if not hasattr(self, "label_editor"):
            return
        try:
            found = find_notepadpp(self.edit_editor.text().strip())
        except Exception:  # noqa: BLE001
            found = ""
        # 手动填的路径存在于磁盘 → 视为用户有意为之，不改输入框
        manual = self.edit_editor.text().strip()
        if manual and Path(manual).exists():
            self._refresh_editor_hint(manual)
            return
        if found and not manual:
            self.edit_editor.setText(found)
            _save_cfg({"editor_path": found})
        self._refresh_editor_hint(found or manual)

    def _refresh_editor_hint(self, path: str) -> None:
        lb = getattr(self, "label_editor", None)
        if lb is None:
            return
        if path and Path(path).exists():
            lb.setText(f"✓ 已就绪：{path}")
            lb.setProperty("role", "ok")
        elif path:
            lb.setText(f"✗ 路径不存在：{path}（双击文稿会退回系统默认程序）")
            lb.setProperty("role", "warn")
        else:
            lb.setText("未找到 Notepad++，双击文稿将用系统默认程序打开。"
                       "装了 Notepad++ 的话点「自动探测」。")
            lb.setProperty("role", "muted")
        try:
            lb.style().unpolish(lb)
            lb.style().polish(lb)
        except Exception:  # noqa: BLE001
            pass

    def _browse_kb(self) -> None:
        cur = self.edit_kb.text().strip()
        d = QFileDialog.getExistingDirectory(self, "选择知识库目录", cur or "")
        if d:
            self.edit_kb.setText(d)
            _save_cfg({"kb_dir": d})
            self.ctx.status_message(f"知识库目录已设为 {d}")

    # ---------------------------------------------------------- 配置读写
    def collect_cfg(self) -> dict:
        return {
            "platform": self.platform(),
            "mode": self.mode(),
            "query": self.edit_query.text().strip(),
            "urls": [u.strip() for u in self.edit_urls.toPlainText().splitlines()
                     if u.strip()],
            "max_count": self.spin_max.value(),
            # 评论数量不再来自配置页：默认给一个值，实际按作品在预览页逐条覆盖
            "comment_limit": int(_load_cfg().get("comment_limit", 20)),
            "proxy": self.edit_proxy.text().strip(),
            "fetch_comments": self.chk_comments.isChecked(),
            # 转录必须有音视频，所以「要不要下」由 needs_download 决定；
            # do_download 保留只为兼容旧任务记录
            "do_download": self.chk_transcribe.isChecked(),
            "do_transcribe": self.chk_transcribe.isChecked(),
            "keep_media": self.chk_keep_media.isChecked(),
            "model": self.combo_model.currentData(),
            "device": self.combo_device.currentData(),
            "output_dir": self.edit_output.text().strip(),
            "media_dir": self.edit_media_dir.text().strip(),
            "compute_type": self.combo_compute.currentData() or "auto",
            "beam_size": int(self.combo_beam.currentData() or 5),
            "pipeline_mode": self.combo_pipeline.currentData() or "overlap",
            "concurrency": int(self.spin_conc.value()),
            "cpu_threads": int(self.spin_threads.value()),
            # 内容输出：一个 mode 决定一切，其余段落开关由它推导
            "output_opts": OutputOptions(mode=self.content_mode()),
        }

    def start_collect(self) -> None:
        cfg = self.collect_cfg()
        spec = get(cfg["platform"])
        if spec.need_cookie:
            ok, msg = self.ctx.cm.quick_check(cfg["platform"])
            if not ok and cfg["mode"] != MODE_MANUAL:
                QMessageBox.warning(
                    self, "缺少 Cookie",
                    f"{spec.label} 的 cookie 未配置或缺少关键字段：\n{msg}\n\n"
                    "请先点「Cookie 配置」完成配置并验通，或改用「手动链接」模式。")
                return
        if cfg["mode"] == MODE_MANUAL and not cfg["urls"]:
            QMessageBox.warning(self, "缺少链接", "手动链接模式需要先粘贴至少一条链接")
            return
        if cfg["mode"] != MODE_MANUAL and not cfg["query"]:
            QMessageBox.warning(self, "缺少目标",
                                "请填写采集目标（博主主页 / 主题关键词 / 文件夹）")
            return
        if not cfg["output_dir"]:
            QMessageBox.warning(self, "缺少输出目录", "请选择 Markdown 输出目录")
            return
        if not cfg["media_dir"]:
            QMessageBox.warning(self, "缺少中间目录", "请选择音视频中间文件目录")
            return

        _save_cfg({
            "proxy": cfg["proxy"], "model": cfg["model"],
            "device": cfg["device"],
            "output_dir": cfg["output_dir"], "media_dir": cfg["media_dir"],
            "keep_media": cfg["keep_media"],
            "max_count": cfg["max_count"],
            "fetch_comments": cfg["fetch_comments"],
            "compute_type": cfg["compute_type"],
            "beam_size": cfg["beam_size"],
            "pipeline_mode": cfg["pipeline_mode"],
            "cpu_threads": cfg["cpu_threads"],
            "concurrency": cfg["concurrency"],
            "output_opts": cfg["output_opts"].to_dict(),
        })
        self.ctx.start_collect(cfg)

    def _restore_output_opts(self, cfg: dict) -> None:
        """旧配置里存的是一堆勾选，这里用 from_dict 反推成三种模式之一。"""
        opts = OutputOptions.from_dict(cfg.get("output_opts") or {})
        rb = self.mode_content_buttons.get(opts.mode)
        if rb is not None:
            rb.setChecked(True)
        self._on_content_mode_changed()

    def _restore_perf_cfg(self, cfg: dict) -> None:
        def _pick(combo: QComboBox, val, cast=None) -> None:
            for i in range(combo.count()):
                d = combo.itemData(i)
                if cast is not None:
                    try:
                        d = cast(d)
                    except (TypeError, ValueError):
                        continue
                if d == val:
                    combo.setCurrentIndex(i)
                    return

        if cfg.get("model"):
            _pick(self.combo_model, cfg["model"])
        if cfg.get("device"):
            _pick(self.combo_device, cfg["device"])
        if cfg.get("compute_type"):
            _pick(self.combo_compute, cfg["compute_type"])
        if cfg.get("beam_size"):
            _pick(self.combo_beam, int(cfg["beam_size"]), int)
        if cfg.get("pipeline_mode"):
            _pick(self.combo_pipeline, cfg["pipeline_mode"])
        if cfg.get("cpu_threads") is not None:
            self.spin_threads.setValue(int(cfg["cpu_threads"]))
        if cfg.get("concurrency"):
            self.spin_conc.setValue(int(cfg["concurrency"]))
        self._refresh_adv_summary()


# ================================================================ ② 选择作品
class PreviewPage(QWidget):
    COLS = ["", "标题", "作者", "时长", "播放", "评论", "抓取条数", "状态"]

    def __init__(self, ctx: "MainWindow") -> None:
        super().__init__()
        self.ctx = ctx
        self.items: list[Item] = []
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        root.addWidget(_page_title(
            "选择作品", "勾选要进入下载与转录的条目；每个作品的评论抓取条数可单独设置",
            self.ctx.theme,
            ("关于这一步",
             "<p>这里只是「挑哪些」，还没开始下载。</p>"
             "<p>后面点「开始处理」才会依次下载 → 转录 → 导出。"
             "中途可以在「下载转录」页暂停、跳过或重做某一条。</p>"
             "<p><b>评论区怎么用</b>：在「抓取条数」列双击任意一格直接改；"
             "或者双击整行打开该作品的评论预览面板，在右侧填好条数再确认。"
             "留空/填 -1 表示跟随配置页的默认值。</p>")))

        self.label_info = QLabel("")
        self.label_info.setProperty("role", "muted")
        self.label_info.setWordWrap(True)
        root.addWidget(self.label_info)

        # 评论面板的显式开关（除了双击行以外，也给一个可见入口）
        row_panel = QHBoxLayout()
        self.btn_show_panel = QPushButton("查看评论预览")
        self.btn_show_panel.setToolTip(
            "先选中左侧任意一行，再点这里打开评论预览面板")
        self.btn_show_panel.clicked.connect(self.show_panel_for_current)
        row_panel.addWidget(self.btn_show_panel)
        row_panel.addStretch(1)
        root.addLayout(row_panel)

        # 左右分栏：左边勾选表格，右边（可选）单个作品的评论预览 + 条数设置
        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 42)
        self.table.setColumnWidth(6, 84)
        self.table.itemDoubleClicked.connect(self._on_dblclick)
        self.table.setToolTip(
            "双击「抓取条数」列可直接改数字；双击其他列打开右侧评论预览面板")
        lv.addWidget(self.table, 1)

        # 批量设置条数
        row_batch = QHBoxLayout()
        row_batch.setSpacing(8)
        row_batch.addWidget(QLabel("批量设置所选作品的评论条数"))
        self.spin_batch = QSpinBox()
        self.spin_batch.setRange(-1, 2000)
        self.spin_batch.setValue(20)
        self.spin_batch.setSpecialValueText("跟随默认")
        row_batch.addWidget(self.spin_batch)
        b_apply = QPushButton("应用到所选")
        b_apply.clicked.connect(self.apply_batch_limit)
        row_batch.addWidget(b_apply)
        row_batch.addStretch(1)
        lv.addLayout(row_batch)

        split.addWidget(left)

        # --- 右侧：评论预览 + 条数 ---
        self.panel = Card()
        # ⚠️ 用户反馈「预览窗口太小，看不清评论，排版也有问题」。
        # 原来最小宽 280 太窄，用户/内容两列一挤就全是省略号。
        # 改成 420 起步，并给分栏一个更宽的比例（4:3）。
        self.panel.setMinimumWidth(420)
        head_panel = QHBoxLayout()
        head_panel.addWidget(SectionTitle("作品评论预览", self.ctx.theme))
        head_panel.addStretch(1)
        # ⚠️ 用户反馈「双击打开后就叉不掉了」。面板必须有个明确的关闭入口。
        self.btn_panel_close = QPushButton("✕ 关闭")
        self.btn_panel_close.setProperty("role", "ghost")
        self.btn_panel_close.setToolTip("关闭评论预览面板（也可以按 Esc）")
        self.btn_panel_close.clicked.connect(self.hide_panel)
        head_panel.addWidget(self.btn_panel_close)
        self.panel.body.addLayout(head_panel)

        self.label_panel_title = QLabel("双击左侧任意一行查看")
        self.label_panel_title.setWordWrap(True)
        self.label_panel_title.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        self.label_panel_title.setProperty("role", "muted")
        self.panel.body.addWidget(self.label_panel_title)

        row_lim = QHBoxLayout()
        row_lim.addWidget(QLabel("本次抓取"))
        self.spin_panel_limit = QSpinBox()
        self.spin_panel_limit.setRange(-1, 2000)
        self.spin_panel_limit.setValue(-1)
        self.spin_panel_limit.setSpecialValueText("跟随默认")
        self.spin_panel_limit.setSuffix(" 条")
        row_lim.addWidget(self.spin_panel_limit)
        self.btn_panel_apply = QPushButton("应用")
        self.btn_panel_apply.clicked.connect(self.apply_panel_limit)
        row_lim.addWidget(self.btn_panel_apply)
        row_lim.addStretch(1)
        self.panel.body.addLayout(row_lim)
        self.label_panel_hint = hint("该作品共有 — 条评论")
        self.panel.body.addWidget(self.label_panel_hint)

        # 评论列表：内容列必须能看全。
        # ⚠️ 原来的问题是 QTableWidget 单元格默认单行截断，长评论只剩省略号。
        # 这里改成：内容列 QTextEdit 只读（自动换行 + 可滚动 + 可选中复制）。
        self.list_comments = QTableWidget(0, 2)
        self.list_comments.setHorizontalHeaderLabels(["用户", "内容"])
        self.list_comments.verticalHeader().setVisible(False)
        self.list_comments.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.list_comments.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.list_comments.setAlternatingRowColors(True)
        self.list_comments.setWordWrap(True)          # ← 关键：允许换行
        self.list_comments.setTextElideMode(Qt.ElideNone)   # ← 关键：不省略
        hdr_c = self.list_comments.horizontalHeader()
        hdr_c.setSectionResizeMode(0, QHeaderView.Interactive)
        hdr_c.setSectionResizeMode(1, QHeaderView.Stretch)
        self.list_comments.setColumnWidth(0, 110)
        self.list_comments.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)             # ← 行高随内容自适应
        self.list_comments.itemDoubleClicked.connect(self._on_comment_dbl)
        self.list_comments.setToolTip(
            "双击任意一条评论可查看/复制全文")
        self.panel.body.addWidget(self.list_comments, 1)

        self.btn_fetch_comments = QPushButton("抓取这条的评论")
        self.btn_fetch_comments.clicked.connect(self.fetch_panel_comments)
        self.panel.body.addWidget(self.btn_fetch_comments)

        split.addWidget(self.panel)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 3)
        split.setSizes([640, 480])
        split.setCollapsible(1, True)      # 允许把面板拖到最小
        self.panel.setVisible(False)
        self._split = split
        root.addWidget(split, 1)

        row = QHBoxLayout()
        for text, fn in (("全选", self.sel_all), ("全不选", self.sel_none),
                         ("反选", self.sel_invert)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        b_back = QPushButton("← 回到配置")
        b_back.clicked.connect(lambda: self.ctx.goto_key("setup"))
        row.addWidget(b_back)
        self.btn_run = QPushButton("开始处理所选 →")
        self.btn_run.setProperty("role", "primary")
        self.btn_run.setMinimumHeight(34)
        self.btn_run.clicked.connect(self.run_selected)
        row.addWidget(self.btn_run)
        root.addLayout(row)

        self._panel_item: Item | None = None

    def set_items(self, items: list[Item]) -> None:
        self.items = items
        self.table.setRowCount(len(items))
        default_limit = int(_load_cfg().get("comment_limit", 20))
        for r, it in enumerate(items):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if it.selected or it.status != STATUS_FAILED
                              else Qt.Unchecked)
            self.table.setItem(r, 0, chk)
            # 「抓取条数」列：显示本条实际会抓多少（含回退到默认值的展开结果）
            limit_now = it.comment_limit if it.comment_limit and it.comment_limit > 0 \
                else default_limit
            vals = [it.title or it.item_id, it.author or "-", _fmt_dur(it.duration),
                    f"{it.view_count:,}" if it.view_count else "-",
                    f"{it.comment_count:,}" if it.comment_count else "-",
                    f"{limit_now}",
                    STATUS_TEXT.get(it.status, it.status)]
            for c, v in enumerate(vals, start=1):
                cell = QTableWidgetItem(str(v))
                if c == 7:
                    cell.setForeground(QColor(
                        tokens(self.ctx.theme).local_status(it.status)))
                if c == 6:
                    cell.setToolTip(
                        "双击可直接修改；-1 / 留空表示跟随配置页默认值（"
                        f"{default_limit} 条）")
                    cell.setForeground(QColor(
                        tokens(self.ctx.theme).accent if
                        (it.comment_limit and it.comment_limit > 0)
                        else tokens(self.ctx.theme).text_muted))
                self.table.setItem(r, c, cell)
        self.table.resizeColumnToContents(3)
        self.label_info.setText(
            f"共采集到 {len(items)} 条。默认每个作品抓 {default_limit} 条评论；"
            f"想让某条多抓/少抓，双击那一行的「抓取条数」直接改，"
            f"或双击打开右侧评论预览面板。")

    # -------------------------------------------------- 评论条数（按作品）
    def _limit_of(self, it: Item) -> int:
        """这条实际会抓多少：优先用它自己的值，否则回退到默认。"""
        if it.comment_limit and it.comment_limit > 0:
            return it.comment_limit
        return int(_load_cfg().get("comment_limit", 20))

    def _set_limit(self, it: Item, n: int) -> None:
        """写入某条的评论条数（n<=0 表示回到默认）。"""
        it.comment_limit = int(n)
        try:
            self.ctx.pipeline.db.update_item(
                it.platform, it.item_id, comment_limit=int(n))
        except Exception:  # noqa: BLE001
            pass

    def _refresh_limit_cell(self, row: int) -> None:
        if not (0 <= row < len(self.items)):
            return
        it = self.items[row]
        cell = self.table.item(row, 6)
        if cell is not None:
            cell.setText(str(self._limit_of(it)))
            cell.setForeground(QColor(
                tokens(self.ctx.theme).accent if
                (it.comment_limit and it.comment_limit > 0)
                else tokens(self.ctx.theme).text_muted))

    def _on_dblclick(self, cell: QTableWidgetItem) -> None:
        """双击「抓取条数」列 → 就地编辑；双击其他列 → 打开右侧预览面板。"""
        row, col = cell.row(), cell.column()
        if not (0 <= row < len(self.items)):
            return
        it = self.items[row]
        if col == 6:
            from PyQt5.QtWidgets import QInputDialog
            cur = self._limit_of(it)
            n, ok = QInputDialog.getInt(
                self, "设置评论抓取条数",
                f"《{it.title or it.item_id}》本次要抓多少条评论？\n"
                f"（该作品一共约 {it.comment_count or '未知'} 条；"
                f"填 0 或负数表示跟随配置页默认值）",
                cur, -1, 2000, 1)
            if ok:
                self._set_limit(it, n)
                self._refresh_limit_cell(row)
                self.ctx.status_message(
                    f"已把《{it.title or it.item_id}》的评论条数设为 "
                    f"{self._limit_of(it)} 条")
        else:
            self.show_panel(it, row)

    def apply_batch_limit(self) -> None:
        """把「批量设置」里的数字应用到所有勾选的行。"""
        n = int(self.spin_batch.value())
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "没有选中", "先在左侧勾选要应用的作品")
            return
        for r in rows:
            self._set_limit(self.items[r], n)
            self._refresh_limit_cell(r)
        what = "跟随默认值" if n <= 0 else f"{n} 条"
        self.ctx.status_message(f"已把 {len(rows)} 个作品的评论条数设为 {what}")

    def _selected_rows(self) -> list[int]:
        out = []
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell is not None and cell.checkState() == Qt.Checked:
                out.append(r)
        return out

    # -------------------------------------------------- 右侧评论预览面板
    def show_panel_for_current(self) -> None:
        """按当前选中行打开评论面板（按钮入口）。"""
        row = self.table.currentRow()
        if not (0 <= row < len(self.items)):
            QMessageBox.information(self, "先选一行",
                                    "请在左侧表格里点一行，再打开评论预览。")
            return
        self.show_panel(self.items[row], row)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        """Esc 关掉评论预览面板（用户反馈「打开后关不掉」，加个快捷键）。"""
        if ev.key() == Qt.Key_Escape and self.panel.isVisible():
            self.hide_panel()
            return
        super().keyPressEvent(ev)

    def hide_panel(self) -> None:
        """关闭评论预览面板（用户反馈「打开后关不掉」，必须有明确出口）。"""
        self.panel.setVisible(False)
        self._panel_item = None
        self.ctx.status_message("已关闭评论预览")

    def _on_comment_dbl(self, cell: QTableWidgetItem) -> None:
        """双击评论 → 弹窗看全文，可一键复制。

        表格里行高自适应后一般能看全，但超长评论仍可能换行很多，
        给一个「看全文」的出口更稳妥。
        """
        row = cell.row()
        u = self.list_comments.item(row, 0)
        c = self.list_comments.item(row, 1)
        user = u.text() if u else ""
        content = c.text() if c else ""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"评论全文 · {user}")
        dlg.resize(620, 420)
        v = QVBoxLayout(dlg)
        lb = QLabel(f"<b>{_escape(user)}</b>")
        v.addWidget(lb)
        body = QTextBrowser()
        body.setPlainText(content)
        v.addWidget(body, 1)
        hb = QHBoxLayout()
        hb.addStretch(1)
        b_copy = QPushButton("复制内容")
        b_copy.clicked.connect(
            lambda: (QApplication.clipboard().setText(content),
                     self.ctx.status_message("评论内容已复制")))
        hb.addWidget(b_copy)
        b_ok = QPushButton("关闭")
        b_ok.setProperty("role", "primary")
        b_ok.clicked.connect(dlg.accept)
        hb.addWidget(b_ok)
        v.addLayout(hb)
        dlg.exec_()

    def show_panel(self, it: Item, row: int) -> None:
        """打开右侧面板，展示这条作品的评论（已抓到的）+ 条数设置。"""
        self._panel_item = it
        self._panel_row = row
        self.panel.setVisible(True)
        self.label_panel_title.setText(
            f"《{it.title or it.item_id}》\n作者：{it.author or '-'}"
            f"　平台：{it.platform}")
        self.label_panel_hint.setText(
            f"该作品共有 {it.comment_count:,} 条评论（平台给出）" if it.comment_count
            else "平台没有给出评论总数")
        self.spin_panel_limit.setValue(
            it.comment_limit if it.comment_limit and it.comment_limit > 0 else -1)
        self._load_panel_comments(it)

    def _load_panel_comments(self, it: Item) -> None:
        try:
            cs = self.ctx.pipeline.db.get_comments(it.platform, it.item_id)
        except Exception:  # noqa: BLE001
            cs = []
        # 排序：按点赞降序，让「值得看的评论」排前面（与导出一致）
        try:
            cs = sorted(cs, key=lambda c: -(int(c.get("like_count") or 0)))
        except Exception:  # noqa: BLE001
            pass
        self.list_comments.setRowCount(len(cs))
        for r, c in enumerate(cs[:300]):
            user = c.get("user") or "匿名"
            like = c.get("like_count") or 0
            u_item = QTableWidgetItem(f"{user}" + (f"  👍{like}" if like else ""))
            u_item.setToolTip(user)
            txt = (c.get("content") or "").replace("\r", "").strip()
            c_item = QTableWidgetItem(txt)
            c_item.setToolTip(txt)
            self.list_comments.setItem(r, 0, u_item)
            self.list_comments.setItem(r, 1, c_item)
        # 行高随内容自适应（长评论要能看全，不能被裁掉）
        self.list_comments.resizeRowsToContents()
        if not cs:
            self.btn_fetch_comments.setText("抓取这条的评论")
        else:
            more = "（只显示前 300 条）" if len(cs) > 300 else ""
            self.btn_fetch_comments.setText(
                f"重新抓取（已有 {len(cs)} 条）{more}")

    def apply_panel_limit(self) -> None:
        it = self._panel_item
        if it is None:
            return
        self._set_limit(it, int(self.spin_panel_limit.value()))
        self._refresh_limit_cell(getattr(self, "_panel_row", -1))
        self.ctx.status_message(
            f"《{it.title or it.item_id}》的评论条数已设为 {self._limit_of(it)} 条")

    def fetch_panel_comments(self) -> None:
        """就地抓一下这条的评论，看看实际长什么样，再决定抓多少。"""
        it = self._panel_item
        if it is None:
            return
        spec = get(it.platform)
        n = self._limit_of(it)
        if spec.factory is None:
            QMessageBox.information(self, "不支持", f"{spec.label} 不支持抓取评论")
            return
        crawler = spec.factory(
            cookie=self.ctx.cm.to_header(it.platform) if spec.need_cookie else "",
            proxy=self.ctx.page_setup.edit_proxy.text().strip())
        self.btn_fetch_comments.setEnabled(False)
        self.btn_fetch_comments.setText(f"抓取中（{n} 条）…")
        try:
            cs = crawler.fetch_comments(it.item_id, max_count=n)
            for c in cs:
                c.item_id = it.item_id
            saved = self.ctx.pipeline.db.save_comments(it.platform, cs)
            self.label_panel_hint.setText(f"本次抓到并保存 {saved} 条评论")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "抓取失败", str(exc))
        finally:
            self.btn_fetch_comments.setEnabled(True)
        self._load_panel_comments(it)


    def selected_items(self) -> list[Item]:
        out = []
        for r, it in enumerate(self.items):
            cell = self.table.item(r, 0)
            if cell is not None and cell.checkState() == Qt.Checked:
                out.append(it)
        return out

    def sel_all(self) -> None:
        self._set_all(True)

    def sel_none(self) -> None:
        self._set_all(False)

    def sel_invert(self) -> None:
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell is None:
                continue
            cell.setCheckState(
                Qt.Unchecked if cell.checkState() == Qt.Checked else Qt.Checked)

    def _set_all(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for r in range(self.table.rowCount()):
            cell = self.table.item(r, 0)
            if cell is not None:
                cell.setCheckState(state)

    def run_selected(self) -> None:
        items = self.selected_items()
        if not items:
            QMessageBox.warning(self, "未选择", "至少勾选一条")
            return
        self.ctx.start_process(items)


# ================================================================ ③ 下载转录
class RunPage(QWidget):
    """下载转录页：进度条 + 日志 + 运行控制（暂停/前进/后退/选择）。"""

    QCOLS = ["#", "标题", "状态"]

    def __init__(self, ctx: "MainWindow") -> None:
        super().__init__()
        self.ctx = ctx
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        root.addWidget(_page_title(
            "下载转录", "转录中随时可以暂停、跳过、重做某一条",
            self.ctx.theme,
            ("运行中的控制语义",
             "<p><b>暂停</b>：就地挂起，已下载/已转录的部分不丢，再点一次继续。</p>"
             "<p><b>跳过当前</b>：放弃这一条，直接处理下一条（本条标记为已跳过）。</p>"
             "<p><b>重做上一条</b>：放弃当前条目，回到上一条从头再来；"
             "如果已经在第一条，则重做它自己。</p>"
             "<p><b>双击队列任意一条</b>：直接跳过去处理它。</p>"
             "<p><b>停止</b>：结束整个任务，已经处理好的条目结果保留并导出。</p>")))

        card_stage = Card()
        card_stage.body.addWidget(SectionTitle("当前进度", self.ctx.theme))
        self.label_stage = QLabel("准备中…")
        self.label_stage.setWordWrap(True)
        card_stage.body.addWidget(self.label_stage)

        row_bar = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        row_bar.addWidget(self.bar, 1)
        self.label_pct = QLabel("")
        self.label_pct.setProperty("role", "mono")
        self.label_pct.setMinimumWidth(60)
        row_bar.addWidget(self.label_pct)
        self.label_elapsed = QLabel("")
        self.label_elapsed.setProperty("role", "muted")
        row_bar.addWidget(self.label_elapsed)
        card_stage.body.addLayout(row_bar)
        root.addWidget(card_stage)

        card_ctrl = Card()
        card_ctrl.body.addWidget(SectionTitle("运行控制", self.ctx.theme))
        row_ctrl = QHBoxLayout()
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.setMinimumHeight(32)
        self.btn_pause.setToolTip("就地挂起，已下载/已转录的部分不会丢；再点一次继续")
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.btn_prev = QPushButton("⏮ 重做上一条")
        self.btn_prev.setToolTip("放弃当前条目，回到上一条从头再来")
        self.btn_prev.clicked.connect(self.go_back)

        self.btn_next = QPushButton("⏭ 跳过当前")
        self.btn_next.setToolTip("放弃当前条目，直接处理下一条")
        self.btn_next.clicked.connect(self.skip_current)

        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.setProperty("role", "danger")
        self.btn_stop.setToolTip("结束整个任务，已处理好的条目结果保留")
        self.btn_stop.clicked.connect(self.stop_run)

        for b in (self.btn_pause, self.btn_prev, self.btn_next, self.btn_stop):
            b.setEnabled(False)
            row_ctrl.addWidget(b)
        row_ctrl.addStretch(1)
        card_ctrl.body.addLayout(row_ctrl)

        self.queue = QTableWidget(0, len(self.QCOLS))
        self.queue.setHorizontalHeaderLabels(self.QCOLS)
        self.queue.verticalHeader().setVisible(False)
        self.queue.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue.setSelectionMode(QAbstractItemView.SingleSelection)
        self.queue.setAlternatingRowColors(True)
        self.queue.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.queue.setColumnWidth(0, 40)
        self.queue.setMaximumHeight(190)
        self.queue.itemDoubleClicked.connect(self._on_queue_dblclick)
        self.queue.setToolTip("双击任意一条可以直接跳过去处理它")
        card_ctrl.body.addWidget(self.queue)
        root.addWidget(card_ctrl)

        card_log = Card()
        log_head = QHBoxLayout()
        log_head.addWidget(SectionTitle("运行日志", self.ctx.theme,
            ("怎么看这份日志",
             "<p>每一行都是 <code>时间 · 级别 · 内容</code>。级别用颜色区分：</p>"
             "<ul style='margin-left:-12px'>"
             "<li><b style='color:#1A7F37'>完成</b>：某一步成功结束</li>"
             "<li><b style='color:#1976D2'>进行中</b>：下载/转录的实时进度</li>"
             "<li><b style='color:#8A5D00'>提示</b>：不影响结果但值得知道</li>"
             "<li><b style='color:#C62828'>错误</b>：这一条失败了，会继续处理下一条</li>"
             "</ul>"
             "<p>多个任务并行时，每行开头的 <code>[序号]</code> 能直接对上右边队列里的条目。</p>"
             "<p>右上角可以按级别筛选，只留错误能快速定位问题。</p>")))
        log_head.addStretch(1)
        self.combo_log_level = QComboBox()
        for key, label in (("all", "全部"), ("info", "只看进度"),
                           ("warn", "只看提示"), ("error", "只看错误")):
            self.combo_log_level.addItem(label, key)
        self.combo_log_level.setToolTip("按级别筛日志")
        self.combo_log_level.currentIndexChanged.connect(self._rebuild_log)
        log_head.addWidget(self.combo_log_level)
        b_clear_log = QPushButton("清空")
        b_clear_log.setProperty("role", "ghost")
        b_clear_log.clicked.connect(self.clear_log)
        log_head.addWidget(b_clear_log)
        b_copy_log = QPushButton("复制")
        b_copy_log.setProperty("role", "ghost")
        b_copy_log.setToolTip("把当前日志全文复制到剪贴板")
        b_copy_log.clicked.connect(self._copy_log)
        log_head.addWidget(b_copy_log)
        card_log.body.addLayout(log_head)

        self.log = QTextBrowser()
        self.log.setObjectName("runLog")
        self.log.setOpenExternalLinks(False)
        card_log.body.addWidget(self.log, 1)
        root.addWidget(card_log, 1)

        # 日志模型：保留结构化条目，方便按级别重渲染
        self.log_entries: list[dict] = []

        row = QHBoxLayout()
        self.btn_view = QPushButton("查看结果文档 →")
        self.btn_view.setEnabled(False)
        self.btn_view.clicked.connect(lambda: self.ctx.goto_key("docs"))
        row.addStretch(1)
        row.addWidget(self.btn_view)
        root.addLayout(row)

        self._t0 = 0.0
        self._last_byte_progress = ""
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ---------- 进度 / 日志 ----------
    # 日志级别 → (图标, 语义色, 中文短标签)
    # ⚠️ 用户反馈「运行日志排版太丑，没有排版，一眼看不清对应的进度」。
    # 旧版每条是「时间 图标 内容」混排，列不对齐、级别靠颜色区分（色弱看不清）。
    # 新版做成**三列表格**：固定宽时间戳 | 带底色的小标签 | 内容。
    # 并且用 QTextTable 保证跨行对齐，而不是靠空格硬凑。
    _LEVEL_META = {
        "ok":    ("✓", "success",    "完成"),
        "info":  ("→", "running",    "进行"),
        "warn":  ("!", "warning",    "提示"),
        "error": ("✕", "danger",     "错误"),
        "stage": ("◆", "accent",     "阶段"),
        "muted": ("·", "text_faint", "信息"),
    }

    def log_line(self, text: str, level: str = "muted") -> None:
        """追加一条日志。

        level 决定颜色与标签；没传就是普通灰字（兼容老的调用点）。
        """
        import time
        text = str(text)
        # 进度类日志（下载/转录 x/y）做**合并**：同一条目的进度反复刷新时，
        # 只更新最后一条，不要把几百行「3/12」堆满日志。
        if level == "info" and self._is_progress_text(text) and self.log_entries:
            last = self.log_entries[-1]
            if last["level"] == "info" and self._same_progress_key(
                    last["text"], text):
                last["t"] = time.time()
                last["text"] = text
                self._rebuild_log()
                return
        self.log_entries.append({
            "t": time.time(), "level": level, "text": text,
        })
        # 条目太多时截断，避免长时间跑任务把内存和渲染拖慢
        if len(self.log_entries) > 4000:
            del self.log_entries[:1000]
            self._rebuild_log()
            return
        if self._log_visible(level):
            self._rebuild_log()
        else:
            # 即使当前被筛掉，也要更新表头里的总数
            self._rebuild_header_only()

    @staticmethod
    def _is_progress_text(text: str) -> bool:
        """判断是不是「会反复刷新」的进度文本。

        典型形态：`[3/12] 下载中……`、`转录 5/12 45%`、`下载 12.3/48.1 MB`。
        这类文本会以很高的频率产生，全量堆积会把日志刷爆。
        """
        import re
        return bool(re.search(r"\d+\s*/\s*\d+", text)
                    or re.search(r"\d+(\.\d+)?\s*%", text))

    @staticmethod
    def _same_progress_key(a: str, b: str) -> bool:
        """两条进度日志是否属于「同一条目的同一类进度」。

        做法：抹掉数字后再比。`[3/12] 下载中` 与 `[3/12] 下载中 45%`
        抹掉数字后都是 `[] 下载中 %`，视为同一类 → 合并。
        """
        import re
        norm = lambda s: re.sub(r"\d+(\.\d+)?", "#", s)
        return norm(a) == norm(b)

    def log_stage(self, text: str) -> None:
        """阶段分隔线（开始下载 / 开始转录 / 开始导出 这种）。"""
        self.log_line(text, "stage")

    def log_ok(self, text: str) -> None:
        self.log_line(text, "ok")

    def log_warn(self, text: str) -> None:
        self.log_line(text, "warn")

    def log_error(self, text: str) -> None:
        self.log_line(text, "error")

    def _log_visible(self, level: str) -> bool:
        want = self.combo_log_level.currentData() or "all"
        if want == "all":
            return True
        if want == "info":
            return level in ("info", "stage")
        if want == "warn":
            return level in ("warn", "error")
        if want == "error":
            return level == "error"
        return True

    def _log_color(self, level: str) -> str:
        t = tokens(self.ctx.theme)
        role = self._LEVEL_META.get(level, ("·", "text_faint", "信息"))[1]
        return {
            "success": t.success, "running": t.running,
            "warning": t.warning, "danger": t.danger,
            "accent": t.accent, "text_faint": t.text_faint,
        }.get(role, t.text_muted)

    def _entry_html(self, e: dict, row_bg: str = "") -> str:
        """单条日志 → 一个表格行。

        ⚠️ 之所以用 <table> 而不是 <div>：三列要**跨行对齐**，
        靠空格或 inline-block 在 QTextBrowser 里对不齐（字体宽度不一致）。
        表格是唯一稳的做法。
        """
        t = tokens(self.ctx.theme)
        icon, _role, label = self._LEVEL_META.get(e["level"], ("·", "text_faint", "信息"))
        color = self._log_color(e["level"])
        import time
        stamp = time.strftime("%H:%M:%S", time.localtime(e["t"]))
        is_stage = e["level"] == "stage"

        text = _escape(e["text"])
        # 序号 [3] 高亮成等宽小徽章，方便和右侧队列对号
        import re
        text = re.sub(
            r"\[(\d+)\]",
            lambda m: (f"<span style='font-family:Consolas,monospace;"
                       f"background:{t.chip_bg};color:{t.accent};"
                       f"padding:0 4px;border-radius:3px'>[{m.group(1)}]</span>"),
            text)
        text = text.replace("\n", "<br>&nbsp;&nbsp;&nbsp;&nbsp;")

        body_style = (
            f"color:{t.text if not is_stage else color};"
            + ("font-weight:600;" if is_stage else "")
        )
        cells = (
            f"<td width='62' style='font-family:Consolas,monospace;"
            f"color:{t.text_faint};white-space:nowrap'>{stamp}</td>"
            f"<td width='46' align='center'>"
            f"<span style='color:{color};font-weight:600'>{icon}</span>"
            f"<span style='color:{color};font-size:11px'> {label}</span></td>"
            f"<td style='{body_style}'>{text}</td>"
        )
        return (f"<tr bgcolor='{row_bg}'>{cells}</tr>" if row_bg
                else f"<tr>{cells}</tr>")

    def _log_table(self, entries: list[dict]) -> str:
        """把日志条目渲染成一张表（行间交替底色，读起来不串行）。"""
        t = tokens(self.ctx.theme)
        rows = []
        for i, e in enumerate(entries):
            # 阶段行整行加底色，像分隔条一样醒目
            if e["level"] == "stage":
                bg = t.accent_soft
            elif i % 2 == 1:
                bg = t.chip_bg
            else:
                bg = ""
            rows.append(self._entry_html(e, bg))
        return (
            "<table width='100%' cellspacing='0' cellpadding='3' "
            "style='border-collapse:collapse'>" + "".join(rows) + "</table>"
        )

    def _header_html(self, shown: int | None = None) -> str:
        t = tokens(self.ctx.theme)
        total = len(self.log_entries)
        if shown is None:
            shown = sum(1 for e in self.log_entries if self._log_visible(e["level"]))
        n_err = sum(1 for e in self.log_entries if e["level"] == "error")
        n_warn = sum(1 for e in self.log_entries if e["level"] == "warn")
        # 统计条带：错误/提示有数字时才显示，避免占位
        chips = [f"共 {total} 条", f"显示 {shown} 条"]
        if n_err:
            chips.append(
                f"<span style='color:{t.danger};font-weight:600'>错误 {n_err}</span>")
        if n_warn:
            chips.append(
                f"<span style='color:{t.warning}'>提示 {n_warn}</span>")
        return (f"<div style='color:{t.text_muted};border-bottom:1px solid "
                f"{t.border};padding-bottom:5px;margin-bottom:6px'>"
                f"运行日志　" + "　·　".join(chips) + "</div>")

    def _rebuild_header_only(self) -> None:
        """只更新表头（被筛选掉时不必整表重画）。"""
        try:
            self.log.setHtml(self._header_html() + self._log_table(
                [e for e in self.log_entries if self._log_visible(e["level"])]))
            self._scroll_log()
        except Exception:  # noqa: BLE001
            pass

    def _append_html(self, html: str) -> None:      # 兼容旧调用点
        self._rebuild_log()

    def _scroll_log(self) -> None:
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _rebuild_log(self) -> None:
        """按当前筛选级别重画整个日志。"""
        self.log.clear()
        shown = [e for e in self.log_entries if self._log_visible(e["level"])]
        head = self._header_html(len(shown))
        if not shown:
            t = tokens(self.ctx.theme)
            body = (f"<div style='color:{t.text_faint};padding:10px 2px'>"
                    f"（当前筛选下没有日志）</div>")
        else:
            body = self._log_table(shown)
        self.log.setHtml(head + body)
        self._scroll_log()

    def clear_log(self) -> None:
        self.log_entries = []
        self.log.clear()

    def _copy_log(self) -> None:
        from PyQt5.QtWidgets import QApplication
        import time
        lines = [
            f"{time.strftime('%H:%M:%S', time.localtime(e['t']))} "
            f"[{e['level']}] {e['text']}"
            for e in self.log_entries if self._log_visible(e["level"])
        ]
        QApplication.clipboard().setText("\n".join(lines))
        self.ctx.status_message(f"已复制 {len(lines)} 行日志")

    def start_timing(self) -> None:
        import time
        self._t0 = time.time()
        self.bar.setValue(0)
        self.bar.setRange(0, 100)
        self.label_elapsed.setText("已用 0s")
        self._timer.start()

    def stop_timing(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        import time
        if not self._t0:
            return
        s = int(time.time() - self._t0)
        self.label_elapsed.setText(
            f"已用 {s//60:d}:{s%60:02d}" if s >= 60 else f"已用 {s}s")

    def on_progress(self, msg: str, cur: int, total: int) -> None:
        self.label_stage.setText(msg)
        if total and total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(min(cur, total))
            self.label_pct.setText(f"{cur}/{total}")
        else:
            # 长度未知（比如下载没给 Content-Length）→ 走跑马灯，别假装 0%
            self.bar.setRange(0, 0)
            self.label_pct.setText("…")
        # 字节级进度刷得太快，不往日志里塞（进度条已经表达了）；
        # 但**每条的进度**要进日志，否则并行时看不出在干什么。
        if "%" in msg and "MB" in msg:
            self._last_byte_progress = msg
            return
        self.log_line(msg, "info")

    def set_done(self, ok: bool, summary: str) -> None:
        self.label_stage.setText(summary)
        self.btn_view.setEnabled(ok)
        self.log_line(summary, "ok" if ok else "warn")

    # ---------- 队列 ----------
    def set_queue(self, items: list[Item]) -> None:
        self.queue.setRowCount(len(items))
        for r, it in enumerate(items):
            cells = [str(r + 1), it.title or it.item_id,
                     STATUS_TEXT.get(it.status, it.status)]
            for c, v in enumerate(cells):
                cell = QTableWidgetItem(v)
                if c == 2:
                    cell.setForeground(QColor(
                        tokens(self.ctx.theme).local_status(it.status)))
                self.queue.setItem(r, c, cell)

    def mark_item(self, index: int, status: str) -> None:
        if 0 <= index < self.queue.rowCount():
            cell = self.queue.item(index, 2)
            if cell is not None:
                cell.setText(STATUS_TEXT.get(status, status))
                cell.setForeground(QColor(
                    tokens(self.ctx.theme).local_status(status)))
            self.queue.selectRow(index)
        # 并行跑的时候，光看进度条不知道是哪条在动，所以状态变化也写进日志
        k = index + 1
        title = ""
        if 0 <= index < self.queue.rowCount():
            t = self.queue.item(index, 1)
            title = (t.text() if t else "")[:28]
        lvl = {"done": "ok", "failed": "error", "skipped": "warn"}.get(
            status, "info")
        self.log_line(
            f"[{k}] {title} → {STATUS_TEXT.get(status, status)}", lvl)

    def _on_queue_dblclick(self, item: QTableWidgetItem) -> None:
        ctl = getattr(self.ctx, "control", None)
        if ctl is None or not self.ctx.processing:
            return
        ctl.jump_to(item.row())
        self.log_line(f"→ 已请求跳转到第 {item.row() + 1} 条", "warn")

    # ---------- 控制 ----------
    def set_controllable(self, on: bool) -> None:
        for b in (self.btn_pause, self.btn_prev, self.btn_next, self.btn_stop):
            b.setEnabled(on)
        if not on:
            self.btn_pause.setText("⏸ 暂停")

    def toggle_pause(self) -> None:
        ctl = getattr(self.ctx, "control", None)
        if ctl is None:
            return
        paused = ctl.toggle_pause()
        self.btn_pause.setText("▶ 继续" if paused else "⏸ 暂停")
        if paused:
            self.log_warn("⏸ 已暂停（再点一次继续）")
        else:
            self.log_ok("▶ 已继续")

    def go_back(self) -> None:
        ctl = getattr(self.ctx, "control", None)
        if ctl is None:
            return
        ctl.back()
        self.log_warn("⏮ 请求重做上一条…")

    def skip_current(self) -> None:
        ctl = getattr(self.ctx, "control", None)
        if ctl is None:
            return
        ctl.skip_current()
        self.log_warn("⏭ 请求跳过当前条目…")

    def stop_run(self) -> None:
        ctl = getattr(self.ctx, "control", None)
        if ctl is None:
            return
        ctl.stop()
        self.log_error("⏹ 请求停止任务…")


# ================================================================ ④ 结果文档
class DocumentsPage(QWidget):
    """结果文档页：左边点条目，右边看 Markdown 详情；所有路径都能点能右键。"""

    COLS = ["#", "标题", "状态", "字数", "评论", "文档"]

    def __init__(self, ctx: "MainWindow") -> None:
        super().__init__()
        self.ctx = ctx
        self.items: list[Item] = []
        self.files: dict[str, dict[str, str]] = {}
        self.out_dir = ""
        self._current: Item | None = None
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        root.addWidget(_page_title(
            "结果文档", "左边点条目，右边就是这条的 Markdown 全文",
            self.ctx.theme,
            ("右边看到的就是导出到磁盘的内容",
             "<p>如果你改了「生成内容」的勾选，这里显示的会跟着变——"
             "所见即导出。</p>"
             "<p>双击表格任意一行，直接用系统默认程序打开对应的 .md 文件。</p>"
             "<p>右键还有「打开所在文件夹」「复制路径」。</p>")))

        card_dir = Card(role="panel")
        top = QHBoxLayout()
        self.label_out = QLabel("输出目录：未指定")
        self.label_out.setWordWrap(True)
        self.label_out.setProperty("role", "muted")
        top.addWidget(self.label_out, 1)
        for text, fn, tip in (
            ("打开目录", self.open_out_dir, "在资源管理器里打开输出目录"),
            ("重新载入", self.reload_from_db, "从数据库重新载入已有转录结果"),
            ("复制目录", self.copy_out_dir, "复制输出目录路径"),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            top.addWidget(b)
        card_dir.body.addLayout(top)
        root.addWidget(card_dir)

        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        self.label_info = QLabel("")
        self.label_info.setWordWrap(True)
        self.label_info.setProperty("role", "muted")
        ll.addWidget(self.label_info)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 多选：为了「批量添加到知识库」
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        # 标题列占满剩余空间，其余固定宽度；「文档」列给得宽一点，
        # 因为里面是 transcripts/xxx.md 这种相对路径，太窄会全变成省略号。
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.Interactive)
        for c, wd in ((0, 34), (2, 72), (3, 56), (4, 52), (5, 180)):
            self.table.setColumnWidth(c, wd)
        self.table.setColumnWidth(5, 180)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.itemDoubleClicked.connect(self._on_dblclick)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_menu)
        self.table.setToolTip(
            "双击用 Notepad++ 打开文档；可按住 Ctrl/Shift 多选，"
            "右键「添加到知识库」批量归档")
        ll.addWidget(self.table, 1)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)

        bar = QHBoxLayout()
        self.btn_open_doc = QPushButton("用 Notepad++ 打开")
        self.btn_open_doc.setToolTip(
            "优先用 Notepad++ 打开（找不到则回退系统默认程序）")
        self.btn_open_doc.clicked.connect(
            lambda: self._open_file(self._transcript_of(self._current)))
        self.btn_full = QPushButton("全屏查看")
        self.btn_full.setToolTip("把右侧预览放大到整页（再点一次还原，Esc 可退出）")
        self.btn_full.clicked.connect(self.toggle_fullscreen_preview)
        self.btn_reveal = QPushButton("资源管理器中打开")
        self.btn_reveal.setToolTip("在资源管理器里定位到这个 Markdown 文件")
        self.btn_reveal.clicked.connect(
            lambda: self._reveal(self._transcript_of(self._current)))
        self.btn_open_url = QPushButton("打开原文链接")
        self.btn_open_url.clicked.connect(self._open_source)
        for b in (self.btn_open_doc, self.btn_full, self.btn_reveal,
                  self.btn_open_url):
            bar.addWidget(b)
        bar.addStretch(1)
        rl.addLayout(bar)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._on_anchor)
        rl.addWidget(self.view, 1)
        split.addWidget(right)
        self._split = split
        self._right = right
        self._full_preview = False

        split.setSizes([520, 480])
        root.addWidget(split, 1)

    # ---------- 数据 ----------
    def set_items(self, items: list[Item], out_dir: str = "") -> None:
        self.items = items
        if out_dir:
            self.out_dir = out_dir
            self.label_out.setText(f"输出目录：{out_dir}")
        self.table.setRowCount(len(items))
        done = sum(1 for i in items if i.status == "done")
        for r, it in enumerate(items):
            f = self.files.get(it.item_id, {})
            doc = self._rel(f.get("transcript", ""))
            vals = [str(r + 1), it.title or it.item_id,
                    STATUS_TEXT.get(it.status, it.status),
                    str(len(it.transcript or "")),
                    str(it.comment_count or 0), doc or "-"]
            for c, v in enumerate(vals):
                cell = QTableWidgetItem(v)
                if c == 2:
                    cell.setForeground(QColor(
                        tokens(self.ctx.theme).local_status(it.status)))
                if c == 5:
                    cell.setToolTip(f.get("transcript", "") or "尚未导出")
                self.table.setItem(r, c, cell)
        self.label_info.setText(
            f"本次共 {len(items)} 条，其中转录成功 {done} 条。"
            f"点任意一行看详情；右键可打开文件 / 打开所在文件夹 / 复制路径。")
        if items:
            self.table.selectRow(0)
            # ⚠️ 不能只靠 selectRow 触发 itemSelectionChanged：
            # 上一轮已经选中第 0 行时，再 selectRow(0) 不会发信号，
            # 右侧详情就停留在「导出前」的版本（没有文件超链接）。
            self.show_detail(items[0])

    def set_files(self, files: dict[str, dict[str, str]], out_dir: str = "") -> None:
        self.files = files or {}
        if out_dir:
            self.out_dir = out_dir
            self.label_out.setText(f"输出目录：{out_dir}")
        for r, it in enumerate(self.items):
            f = self.files.get(it.item_id, {})
            cell = self.table.item(r, 5)
            if cell is not None:
                cell.setText(self._rel(f.get("transcript", "")) or "-")
                cell.setToolTip(f.get("transcript", "") or "尚未导出")
        if self._current is not None:
            self.show_detail(self._current)

    def _rel(self, path: str) -> str:
        if not path:
            return ""
        try:
            return str(Path(path).relative_to(self.out_dir)) if self.out_dir else path
        except Exception:  # noqa: BLE001
            return path

    # ---------- 详情 ----------
    def _on_select(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        r = rows[0].row()
        if 0 <= r < len(self.items):
            self.show_detail(self.items[r])

    def show_detail(self, item: Item) -> None:
        self._current = item
        ts = ""
        if item.segments:
            try:
                from types import SimpleNamespace
                from ..asr.engine import format_timestamped
                ns = SimpleNamespace(segments=json.loads(item.segments))
                ts = format_timestamped(ns)
            except Exception:  # noqa: BLE001
                ts = ""
        comments = self.ctx.pipeline.comments_of(item.platform, item.item_id)
        opts = (self.ctx.task.output_opts if self.ctx.task else None) or OutputOptions()
        md = render_item_markdown(item, ts, comments or None, opts)
        f = self.files.get(item.item_id, {})
        links = []
        if f.get("transcript"):
            links.append(f"[逐字稿 Markdown]({to_file_url(f['transcript'])})")
        if f.get("comments"):
            links.append(f"[评论 Markdown]({to_file_url(f['comments'])})")
        if links:
            md = "**已导出文件：** " + " ｜ ".join(links) + "\n\n---\n\n" + md
        self.view.setMarkdown(md)

    def _transcript_of(self, item: Item | None) -> str:
        if item is None:
            return ""
        return (self.files.get(item.item_id) or {}).get("transcript", "")

    # ---------- 打开 ----------
    def _on_dblclick(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if row < 0 or row >= len(self.items):
            return
        self._open_file(self._transcript_of(self.items[row]))

    def _open_file(self, path: str) -> None:
        """用 Notepad++ 打开（找不到就回退系统默认程序）。

        ⚠️ 用户明确要求「打开逐字稿文档时默认用 Notepad++ 打开」。
        `open_with_editor` 会自己探测，这里只负责把结果反馈到状态栏，
        探测失败不弹窗（回退到系统默认程序仍然能用），只提示一下。
        """
        if not path:
            QMessageBox.information(self, "还没有文件",
                                    "这一条还没有导出文件。点「重新载入」或重新导出后即可打开。")
            return
        editor = str(_load_cfg().get("editor_path", "") or "")
        try:
            used_npp, msg = open_with_editor(path, editor)
            self.ctx.status_message(msg)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(exc))

    def toggle_fullscreen_preview(self) -> None:
        """把右侧 Markdown 预览放大到整页，再点还原。

        ⚠️ 用户反馈「右侧窗口虽然可以左右拖拽，但无法全屏显示，体验不好」。
        实现方式：把左侧表格和顶部按钮栏藏起来，让 QSplitter 里只剩右侧，
        并把 splitter 比例全给右侧。比真的开新窗口更轻，也不用管生命周期。
        """
        self._full_preview = not getattr(self, "_full_preview", False)
        if self._full_preview:
            self.table.hide()
            self.label_info.hide()
            self._split.setSizes([0, 1])
            self.btn_full.setText("退出全屏")
            self.btn_full.setProperty("role", "primary")
            self.ctx.status_message("已进入全屏预览；再点「退出全屏」或按 Esc 还原")
        else:
            self.table.show()
            self.label_info.show()
            self._split.setSizes([520, 480])
            self.btn_full.setText("全屏查看")
            self.btn_full.setProperty("role", "")
            self.ctx.status_message("已退出全屏预览")
        repolish(self.btn_full)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        """Esc 退出全屏预览（顺手比强求用户去点按钮好）。"""
        if ev.key() == Qt.Key_Escape and getattr(self, "_full_preview", False):
            self.toggle_fullscreen_preview()
            return
        super().keyPressEvent(ev)

    def _reveal(self, path: str) -> None:
        if not path:
            QMessageBox.information(self, "还没有文件", "这一条还没有导出文件路径。")
            return
        _open_safe(self, reveal_path, path)

    def _open_source(self) -> None:
        if self._current is None or not self._current.url:
            QMessageBox.information(self, "没有链接", "这一条没有可打开的原文链接。")
            return
        QDesktopServices.openUrl(QUrl(self._current.url))

    def _on_anchor(self, url: QUrl) -> None:
        s = url.toString()
        if s.startswith("file:"):
            _open_safe(self, open_path, from_url(s))
        elif s.startswith("http"):
            QDesktopServices.openUrl(QUrl(s))

    def open_out_dir(self) -> None:
        if not self.out_dir:
            QMessageBox.information(self, "未指定", "还没有指定输出目录。")
            return
        _open_safe(self, open_path, self.out_dir)

    def copy_out_dir(self) -> None:
        from PyQt5.QtWidgets import QApplication
        QApplication.clipboard().setText(self.out_dir or "")
        self.ctx.status_message("输出目录已复制到剪贴板")

    def reload_from_db(self) -> None:
        """把库里已有转录结果的条目重新载入（换了任务也能回看历史结果）。"""
        items = [i for i in self.ctx.pipeline.list_items() if i.transcript]
        if not items:
            QMessageBox.information(self, "没有历史结果", "数据库里还没有转录好的记录。")
            return
        items.sort(key=lambda x: x.updated_at, reverse=True)
        self.set_items(items[:200], self.out_dir)
        self.ctx.status_message(f"已从数据库载入 {min(len(items), 200)} 条历史结果")

    # ---------- 右键菜单 ----------
    def _selected_items(self) -> list[Item]:
        """当前选中的所有条目（多选支持）。"""
        rows = sorted(i.row() for i in self.table.selectionModel().selectedRows())
        return [self.items[r] for r in rows if 0 <= r < len(self.items)]

    def _kb_entries(self, items: list[Item]) -> list[dict]:
        """把作品列表转成知识库归档条目（逐字稿 + 评论文档都带上）。"""
        out: list[dict] = []
        for it in items:
            f = self.files.get(it.item_id, {})
            tp, cp = f.get("transcript", ""), f.get("comments", "")
            if tp and Path(tp).is_file():
                out.append({"path": tp, "item": it, "name": Path(tp).name})
            if cp and Path(cp).is_file():
                out.append({"path": cp, "item": it, "name": Path(cp).name})
            if not tp and not cp:
                # 没导出过文件但库里有转录正文：现场写一份到临时目录再归档
                if (it.transcript or "").strip():
                    out.append({"__render__": it})
        return out

    def _on_menu(self, pos) -> None:
        # 右键落在未选中行上时先选中它（不破坏已有的多选）
        row_at = self.table.rowAt(pos.y())
        if row_at >= 0 and not self.table.selectionModel().isRowSelected(
                row_at, self.table.rootIndex()):
            self.table.selectRow(row_at)

        sels = self._selected_items()
        if not sels:
            return
        it = sels[0]
        multi = len(sels) > 1
        f = self.files.get(it.item_id, {})
        tpath, cpath = f.get("transcript", ""), f.get("comments", "")

        menu = QMenu(self)
        # 1~2：查看与打开
        act_detail = menu.addAction("在右侧查看 Markdown 详情")
        act_open = menu.addAction(
            f"用 Notepad++ 打开逐字稿" + (f"（{len(sels)} 条）" if multi else ""))
        act_open.setEnabled(bool(tpath))
        act_open_c = menu.addAction("用 Notepad++ 打开评论文档")
        act_open_c.setEnabled(bool(cpath) and not multi)
        # 3：资源管理器（用户要求「支持直接在资源管理器中打开」）
        act_reveal = menu.addAction("在资源管理器中打开")
        act_reveal.setEnabled(bool(tpath or cpath) and not multi)
        menu.addSeparator()
        # 4：知识库（用户要求排位——放在打开类之后、复制类之前）
        act_kb = menu.addAction(
            f"添加到知识库（{len(sels)} 条）" if multi else "添加到知识库")
        act_kb.setToolTip("复制到知识库目录，按「平台/作者」自动分文件夹")
        act_kb_set = menu.addAction("设置知识库目录…")
        menu.addSeparator()
        # 5~8：复制
        act_copy_t = menu.addAction("复制逐字稿路径")
        act_copy_t.setEnabled(bool(tpath) and not multi)
        act_copy_c = menu.addAction("复制评论路径")
        act_copy_c.setEnabled(bool(cpath) and not multi)
        act_url = menu.addAction("打开原文链接")
        act_url.setEnabled(bool(it.url))
        act_copy_url = menu.addAction("复制原文链接")
        act_copy_url.setEnabled(bool(it.url))

        from PyQt5.QtWidgets import QApplication
        chosen = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_detail:
            self.show_detail(it)
        elif chosen is act_open:
            self._open_file(tpath)
        elif chosen is act_open_c:
            self._open_file(cpath)
        elif chosen is act_reveal:
            self._reveal(tpath or cpath)
        elif chosen is act_kb:
            self._add_selected_to_kb(sels)
        elif chosen is act_kb_set:
            self.ctx.choose_kb_dir(self)
        elif chosen is act_copy_t:
            QApplication.clipboard().setText(tpath)
            self.ctx.status_message("逐字稿路径已复制")
        elif chosen is act_copy_c:
            QApplication.clipboard().setText(cpath)
            self.ctx.status_message("评论路径已复制")
        elif chosen is act_url:
            QDesktopServices.openUrl(QUrl(it.url))
        elif chosen is act_copy_url:
            QApplication.clipboard().setText(it.url)
            self.ctx.status_message("原文链接已复制")

    def _add_selected_to_kb(self, items: list[Item]) -> None:
        """把选中的作品归档：已导出的文件直接复制；没导出的现场渲染一份。"""
        entries: list[dict] = []
        for it in items:
            f = self.files.get(it.item_id, {})
            tp, cp = f.get("transcript", ""), f.get("comments", "")
            if tp and Path(tp).is_file():
                entries.append({"path": tp, "item": it, "name": Path(tp).name})
            elif (it.transcript or "").strip():
                # 没导出文件 → 现场渲染成一份 .md 落到临时目录再归档
                p = self._render_temp_md(it)
                if p:
                    entries.append({"path": p, "item": it, "name": p.name})
            if cp and Path(cp).is_file():
                entries.append({"path": cp, "item": it, "name": Path(cp).name})
        if not entries:
            QMessageBox.information(
                self, "没有可归档的内容",
                "选中的条目既没有导出文件，也没有转录正文。\n"
                "请先完成一次转录，或导出后再试。")
            return
        self.ctx.add_paths_to_kb(entries, self)

    def _render_temp_md(self, it: Item) -> Path | None:
        """把一条作品的 Markdown 渲染到临时文件，供归档使用。"""
        try:
            from .fileops import to_file_url  # noqa: F401  (保持导入一致性)
            ts = ""
            if it.segments:
                from types import SimpleNamespace
                from ..asr.engine import format_timestamped
                ns = SimpleNamespace(segments=json.loads(it.segments))
                ts = format_timestamped(ns)
            comments = self.ctx.pipeline.comments_of(it.platform, it.item_id)
            opts = (self.ctx.task.output_opts if self.ctx.task else None) \
                or OutputOptions()
            md = render_item_markdown(it, ts, comments or None, opts)
            tmp_dir = ROOT / "data" / ".kb_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            from ..export.obsidian import safe_filename
            p = tmp_dir / f"{safe_filename(it.title or it.item_id)}.md"
            p.write_text(md, encoding="utf-8")
            return p
        except Exception:  # noqa: BLE001
            return None


# ================================================================ 小工具
def _make_scroll():
    from PyQt5.QtWidgets import QScrollArea
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return sa


def _wrap(layout: QVBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w


# ================================================================ 主窗口
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MediaHarvest · 视频采集与文案提取")
        self.resize(1240, 820)

        cfg = _load_cfg()
        self.theme = cfg.get("theme", "light")
        if self.theme not in ("light", "dark"):
            self.theme = "light"

        self.cm = CookieManager()
        self.pipeline = Pipeline(DB_PATH, cfg.get("media_dir") or MEDIA_DIR)
        self.task: Task | None = None
        self.cfg: dict = {}
        self.items: list[Item] = []
        self.control: ProcessControl | None = None
        self.processing = False
        self.collecting = False

        self._build()
        self._apply_theme()
        self.goto_key("setup")
        QTimer.singleShot(400, self.startup_check)

    # ---------------------------------------------------------- 构建
    def _build(self) -> None:
        central = QWidget()
        cl = QHBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        cl.addWidget(self._build_sidebar())

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.stack = QStackedWidget()
        self.page_setup = SetupPage(self)
        self.page_preview = PreviewPage(self)
        self.page_run = RunPage(self)
        self.page_docs = DocumentsPage(self)
        self.page_files = FilesPage(self, self.theme)
        self.page_history = HistoryPage(self, self.theme)
        self.pages = {
            "setup": self.page_setup,
            "preview": self.page_preview,
            "run": self.page_run,
            "docs": self.page_docs,
            "files": self.page_files,
            "history": self.page_history,
        }
        for key in NAV_KEYS:
            self.stack.addWidget(self.pages[key])
        rl.addWidget(self.stack, 1)

        self.status = self.statusBar()
        self.label_status = QLabel("就绪")
        self.status.addWidget(self.label_status)
        rl.addWidget(self.status)

        cl.addWidget(right, 1)
        self.setCentralWidget(central)

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(208)
        v = QVBoxLayout(bar)
        v.setContentsMargins(12, 16, 12, 12)
        v.setSpacing(4)

        brand = QLabel("MediaHarvest")
        brand.setObjectName("brandTitle")
        sub = QLabel("视频采集 · 文案提取")
        sub.setObjectName("brandSub")
        v.addWidget(brand)
        v.addWidget(sub)
        v.addSpacing(12)
        v.addWidget(divider(self.theme))
        v.addSpacing(8)

        self.nav_buttons: dict[str, QPushButton] = {}
        for key, icon, title, _desc in NAV_ITEMS:
            b = QPushButton(f"  {icon}   {title}")
            b.setProperty("nav", "item")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(_desc)
            b.clicked.connect(lambda _c, k=key: self.goto_key(k))
            self.nav_buttons[key] = b
            v.addWidget(b)

        v.addStretch(1)
        v.addWidget(divider(self.theme))
        v.addSpacing(8)

        self.btn_theme = QPushButton()
        self.btn_theme.setProperty("role", "iconbtn")
        self.btn_theme.setToolTip("在浅色 / 深色主题之间切换")
        self.btn_theme.clicked.connect(self.toggle_theme)
        v.addWidget(self.btn_theme)

        self.btn_cookie2 = QPushButton("Cookie 配置")
        self.btn_cookie2.setProperty("role", "iconbtn")
        self.btn_cookie2.clicked.connect(lambda: self.open_cookie_dialog())
        v.addWidget(self.btn_cookie2)
        return bar

    # ---------------------------------------------------------- 主题
    def _apply_theme(self) -> None:
        t = tokens(self.theme)
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss(t))
        self.btn_theme.setText("🌙 深色模式" if not t.is_dark else "☀ 浅色模式")

    def toggle_theme(self) -> None:
        self.theme = "dark" if self.theme == "light" else "light"
        self._apply_theme()
        _save_cfg({"theme": self.theme})
        self.status_message(f"已切换到{'深色' if self.theme == 'dark' else '浅色'}主题")

    # ---------------------------------------------------------- 导航
    def goto(self, index: int) -> None:
        """按顺序索引跳转（保持向后兼容）。"""
        if 0 <= index < len(NAV_KEYS):
            self.goto_key(NAV_KEYS[index])

    def goto_key(self, key: str) -> None:
        if key not in self.pages:
            return
        self.stack.setCurrentWidget(self.pages[key])
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        # 离开/进入时按需刷新，保证数据是新的
        if key == "files":
            self._sync_files_page()
        elif key == "history":
            self.page_history.reload()

    def status_message(self, text: str) -> None:
        self.label_status.setText(text)

    # ---------------------------------------------------------- 目录联动
    def set_output_dir(self, path: str) -> None:
        """「文件」页或配置页改了文稿目录 → 两边同步 + 落盘。"""
        if hasattr(self, "page_setup"):
            self.page_setup.edit_output.setText(path)
        self.page_docs.out_dir = path
        self.page_files.configure(path, self.page_files.media_dir)
        _save_cfg({"output_dir": path})
        self.status_message(f"文稿目录已设为 {path}")

    def set_media_dir(self, path: str) -> None:
        """中间文件目录改动 → 同步、更新 pipeline、落盘。"""
        if hasattr(self, "page_setup"):
            self.page_setup.edit_media_dir.setText(path)
        self.pipeline.set_media_dir(path)
        self.page_files.configure(self.page_files.doc_dir, path)
        _save_cfg({"media_dir": path})
        self.status_message(f"音视频中间目录已设为 {path}")

    def _sync_files_page(self) -> None:
        doc = self.page_setup.edit_output.text().strip()
        media = self.page_setup.edit_media_dir.text().strip()
        self.page_files.configure(doc, media)
        self.page_files.set_items(
            self.items or self.pipeline.list_items())

    # ---------------------------------------------------------- 外部打开
    def open_transcript(self, platform: str, item_id: str) -> None:
        """「历史」页双击 → 切到结果页并定位到那一条。"""
        items = self.pipeline.list_items()
        target = next((i for i in items if i.platform == platform
                       and i.item_id == item_id), None)
        if target is None:
            QMessageBox.information(self, "找不到", "这条记录已经不在数据库里了。")
            return
        self.items = items[:400]
        self.page_docs.set_items(self.items, self.page_docs.out_dir)
        for r, it in enumerate(self.items):
            if it.platform == platform and it.item_id == item_id:
                self.page_docs.table.selectRow(r)
                self.page_docs.show_detail(it)
                break
        self.goto_key("docs")

    def open_cookie_dialog(self, platform: str = "") -> None:
        dlg = CookieDialog(self.cm, self, focus_platform=platform)
        dlg.exec_()
        self.page_setup.update_banner()

    # ---------------------------------------------------------- 知识库
    def kb_dir(self) -> str:
        """当前知识库目录（空=未设置）。"""
        return str(_load_cfg().get("kb_dir", "") or "").strip()

    def choose_kb_dir(self, parent: QWidget | None = None) -> str:
        """让用户挑一个知识库目录，并落盘。返回选中的路径（取消则空）。"""
        cur = self.kb_dir()
        d = QFileDialog.getExistingDirectory(
            parent or self, "选择知识库目录（归档的文稿会复制到这里）",
            cur or str(ROOT))
        if not d:
            return ""
        _save_cfg({"kb_dir": d})
        self.status_message(f"知识库目录已设为 {d}")
        return d

    def add_paths_to_kb(self, entries: list[dict],
                        parent: QWidget | None = None) -> None:
        """把一批文件归档到知识库。

        entries: [{"path":..., "item": Item|None, "name":...}]
        没有设置知识库目录时先引导用户设置。
        """
        if not entries:
            QMessageBox.information(parent or self, "没有选中",
                                    "先选中要归档的文件（可多选）。")
            return
        kb = self.kb_dir()
        if not kb:
            QMessageBox.information(
                parent or self, "先设置知识库目录",
                "还没有设置知识库目录。\n\n"
                "接下来会打开目录选择框，选一个长期存放文稿的位置"
                "（比如你的 Obsidian 库目录）。")
            kb = self.choose_kb_dir(parent)
            if not kb:
                return

        flat = bool(_load_cfg().get("kb_flat", False))
        res = knowledge.add_files(kb, entries, flat=flat)
        if not res.copied:
            detail = "\n".join(f"· {p or '（空）'}：{r}"
                              for p, r in res.skipped[:8]) or "无有效文件"
            QMessageBox.warning(parent or self, "归档失败",
                                f"没有文件被复制。\n\n{detail}")
            return

        self.status_message(f"{res.summary()} → {kb}")
        open_it = QMessageBox.question(
            parent or self, "已归档",
            f"{res.summary()}。\n\n知识库目录：{kb}\n"
            f"（按「平台/作者」自动分了子文件夹）\n\n"
            f"是否现在打开知识库目录查看？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if open_it == QMessageBox.Yes:
            _open_safe(parent or self, open_path, kb)

    def startup_check(self) -> None:
        miss = []
        for key, spec in PLATFORMS.items():
            if not spec.need_cookie:
                continue
            ok, msg = self.cm.quick_check(key)
            if not ok:
                miss.append(f"• {spec.label}：{msg}")
        if not miss:
            return
        ret = QMessageBox.question(
            self, "尚未配置 Cookie",
            "以下平台还没有可用的登录 Cookie，相关采集功能会受限：\n\n"
            + "\n".join(miss)
            + "\n\n现在就去配置（会打开 Cookie 配置窗口）？\n"
              "登录过期后随时可以重新用 EditThisCookie 导出粘贴。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ret == QMessageBox.Yes:
            self.open_cookie_dialog()

    # ---------------------------------------------------------- 阶段 1：采集
    def start_collect(self, cfg: dict) -> None:
        self.cfg = cfg
        self.task = Task(
            name=f"{cfg['platform']}-{cfg['mode']}",
            mode=cfg["mode"],
            platform=cfg["platform"],
            query=normalize_query(cfg["platform"], cfg["query"]),
            max_count=cfg["max_count"],
            comment_limit=cfg.get("comment_limit", 20),
            fetch_comments=cfg["fetch_comments"],
            do_download=cfg["do_download"],
            do_transcribe=cfg["do_transcribe"],
            keep_media=cfg.get("keep_media", True),
            output_dir=cfg["output_dir"],
            media_dir=cfg.get("media_dir", ""),
            model_size=cfg.get("model", "small"),
            device=cfg.get("device", "auto"),
            compute_type=cfg.get("compute_type", "auto"),
            beam_size=int(cfg.get("beam_size", 5)),
            cpu_threads=int(cfg.get("cpu_threads", 0)),
            concurrency=int(cfg.get("concurrency", 1)),
            pipeline_mode=cfg.get("pipeline_mode", "overlap"),
            output_opts=cfg.get("output_opts") or OutputOptions(),
        )
        cookie = self.cm.to_header(cfg["platform"]) if get(
            cfg["platform"]).need_cookie else ""
        self.page_run.clear_log()
        self.page_run.set_controllable(False)
        self.goto_key("run")
        self.page_run.label_stage.setText("正在采集…")
        self.page_run.log_stage(
            f"开始采集 · 平台 {get(cfg['platform']).label} · 模式 {MODE_LABEL.get(cfg['mode'], cfg['mode'])}")
        self.page_run.log_line(
            f"目标：{cfg['query'] or '（手动链接）'}　最多 {cfg['max_count']} 个作品",
            "muted")
        if cfg.get("fetch_comments"):
            self.page_run.log_line(
                "本次会抓取评论（条数按「选择作品」页逐条设置的来）", "info")
        self.page_run.start_timing()
        self.collecting = True
        self.page_docs.set_files({}, cfg["output_dir"])

        self.control = ProcessControl()
        self.page_run.set_queue([])
        self._cw = CollectWorker(self.pipeline, self.task, cookie=cookie,
                                 proxy=cfg["proxy"], urls=cfg["urls"],
                                 control=self.control)
        self._cw.progress.connect(self.page_run.on_progress)
        self._cw.done.connect(self.on_collected)
        self._cw.failed.connect(self.on_collect_failed)
        self._cw.start()

    def on_collected(self, items: list) -> None:
        self.collecting = False
        self.page_run.stop_timing()
        self.items = items
        if not items:
            self.page_run.log_error("没有采集到任何内容")
            QMessageBox.information(self, "没有结果",
                                    "没有采集到任何内容。\n"
                                    "可能原因：目标不存在 / cookie 失效 / 被风控 / 关键词无结果。")
            self.goto_key("setup")
            return
        self.page_run.log_ok(f"采集完成，共 {len(items)} 条，进入「选择作品」")
        self.page_preview.set_items(items)
        self._apply_conc_for_items(items)
        self.goto_key("preview")

    def _apply_conc_for_items(self, items: list) -> None:
        """采集完成后，按真实素材时长校准并发建议（配置页只能按显存估）。"""
        prev = self.page_setup.spin_conc.value()
        n, avg = self.page_setup._suggest_conc(
            self.page_setup.combo_model.currentData(),
            self.page_setup.combo_compute.currentData() or "float16")
        if n != prev:
            self.page_setup.spin_conc.setValue(n)
            tip = (f"素材平均 {avg:.0f} 秒，并发 {prev} → {n}"
                   if avg else f"按显存把并发调整为 {n}")
            self.page_run.log_warn(f"⚙️ 已校准并发：{tip}")

    def on_collect_failed(self, msg: str) -> None:
        was_processing = self.processing
        self.collecting = False
        self.processing = False
        self.page_run.stop_timing()
        self.page_run.set_controllable(False)
        self.page_run.log_error(f"❌ 失败：{msg}")
        QMessageBox.critical(self, "失败", msg)
        self.goto_key("run" if was_processing else "setup")

    # ---------------------------------------------------------- 阶段 2/3：处理
    def start_process(self, items: list[Item]) -> None:
        self.items = items
        for it in items:
            self.pipeline.select(it.platform, it.item_id, True)
        self.goto_key("run")
        self.page_run.clear_log()
        opts = (self.task.output_opts if self.task else OutputOptions())
        self.page_run.log_stage(f"开始处理 {len(items)} 个作品")
        self.page_run.log_line(
            f"内容模式：{opts.mode_label()}　模型：{self.cfg.get('model', 'small')}"
            f"　设备：{self.cfg.get('device', 'auto')}", "muted")
        self.page_run.log_line(
            f"输出目录：{self.cfg.get('output_dir', '')}", "muted")
        if self.cfg.get("keep_media"):
            self.page_run.log_line("转录完成后保留音视频中间文件", "muted")
        else:
            self.page_run.log_line("转录完成后自动删除音视频中间文件", "muted")
        self._gpu_state_hint()
        self.page_run.set_done(False, "开始下载与转录…")
        self.page_run.start_timing()
        self.page_run.set_queue(items)
        self.page_run.set_controllable(True)

        self.processing = True
        self.control = ProcessControl()
        self._pw = ProcessWorker(
            self.pipeline, items, self.task, cookie=self._cookie(),
            proxy=self.cfg.get("proxy", ""),
            model_size=self.cfg.get("model", "small"),
            device=self.cfg.get("device", "auto"), control=self.control)
        self._pw.progress.connect(self.page_run.on_progress)
        self._pw.item_state.connect(self.page_run.mark_item)
        self._pw.done.connect(self.on_processed)
        self._pw.exported.connect(self.on_exported)
        self._pw.failed.connect(self.on_collect_failed)
        self._pw.start()

    def _gpu_state_hint(self) -> None:
        """显示一下转录设备（CUDA 初始化只能在 QApplication 之前做）。"""
        if not self.cfg.get("do_transcribe"):
            return
        try:
            if (FasterWhisperEngine.cuda_available()
                    and not FasterWhisperEngine.cuda_disabled):
                self.page_run.log_stage("ℹ️ 将使用显卡（CUDA）转录")
            else:
                self.page_run.log_warn("ℹ️ 将使用 CPU 转录（显卡不可用或被禁用）")
        except Exception:  # noqa: BLE001
            pass

    def _cookie(self) -> str:
        key = self.cfg.get("platform", "")
        if not key or not get(key).need_cookie:
            return ""
        return self.cm.to_header(key)

    def on_processed(self, items: list) -> None:
        ok = sum(1 for i in items if i.status == "done")
        fail = sum(1 for i in items if i.status == STATUS_FAILED)
        skip = sum(1 for i in items if i.status == "skipped")
        self.page_docs.set_items(items, self.cfg.get("output_dir", ""))
        extra = f"，跳过 {skip} 条" if skip else ""
        summary = f"处理完成：成功 {ok} 条，失败 {fail} 条{extra}，正在导出…"
        self.page_run.set_done(fail == 0, summary)
        self.page_run.log_stage("开始导出文件…")
        self.page_run.set_controllable(False)

    # ---------------------------------------------------------- 阶段 4：导出
    def on_exported(self, paths: dict) -> None:
        self.processing = False
        self.page_run.stop_timing()
        out_dir = paths.get("root") or self.cfg.get("output_dir", "")
        self.page_docs.set_files(paths.get("files", {}), out_dir)
        self.page_docs.set_items(self.items, out_dir)
        self.page_run.set_done(True, "全部完成，已导出到输出目录")
        self.page_run.log_ok(f"导出完成 → {out_dir}")
        self.page_run.bar.setRange(0, 100)
        self.page_run.bar.setValue(100)
        self.page_run.label_pct.setText("100/100")
        # 中间文件与产物都刷新一次，然后切到结果页
        self._sync_files_page()
        self.goto_key("docs")
