"""界面设计系统：配色令牌 + QSS 生成。

为什么要单独一个文件
--------------------
以前样式是散在各处的 `setStyleSheet("color:#666;")` —— 换个主题要全局搜，
而且「#666 在浅色底上还行、在深色底上就看不见」。这里把颜色收进一套
**语义化令牌**（surface / text / border / accent…），再由令牌生成 QSS，
切换明暗主题只是换一份令牌值，业务代码一行都不用动。

配色参考了主流 Agent / 编辑器产品的做法：
- 不用纯黑纯白。深色底用 #16161A 一类的深灰，浅色底用 #FFFFFF + #F7F7F8 分层，
  长时间盯着不累；
- 强调色只用一个（靛蓝 #4F6BED / 深色下提亮到 #7C93F5），
  其余用中性灰阶撑起层级，避免"哪里都花"；
- 层级靠「背景深浅 + 0.5px 细边」表达，而不是靠加阴影；
- 圆角统一 3 档（小控件 6 / 卡片 10 / 大容器 14），间距统一 4 的倍数。

用法
----
    from .theme import tokens, build_qss
    app.setStyleSheet(build_qss(tokens("dark")))

控件上要局部变色时，优先用语义类名而不是硬编码颜色：
    label.setProperty("role", "muted")   # → 次级文字色
    按钮/标签加 property 后记得 style().unpolish/polish 一次让 QSS 重新匹配。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Tokens:
    """一套主题的全部颜色令牌。字段名即语义，别在业务代码里写死色值。"""

    # 背景层级：从最底（窗口）到最上（浮层）
    bg: str              # 窗口底
    surface: str         # 卡片/面板
    surface_alt: str     # 次级面板（侧边栏、表头）
    surface_hover: str   # 悬停态
    surface_active: str  # 选中态

    # 文字层级
    text: str            # 正文
    text_muted: str      # 次级说明
    text_faint: str      # 占位符/禁用
    text_on_accent: str  # 强调色底上的文字

    # 描边
    border: str          # 常规 0.5px 细边
    border_strong: str   # 悬停/聚焦

    # 强调与语义色
    accent: str
    accent_hover: str
    accent_soft: str     # 强调色的浅底（选中行底色、徽标底）
    # ⚠️ 坑：chip_bg 曾经在 main_window 的日志渲染里被直接引用，
    # 但 Tokens 里根本没定义 → 槽函数 AttributeError → 日志面板整个不显示
    # （PyQt5 槽内异常在 Windows 上是静默的，只表现为「啥也没发生」）。
    # 这里补上：日志行交替底色 / 等宽徽章底，只比面板色差一档，别抢眼。
    chip_bg: str

    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str

    # 状态色（队列行用）
    running: str         # 下载中/转录中

    # 其它
    shadow: str
    scroll: str
    scroll_hover: str

    is_dark: bool

    def as_dict(self) -> dict:
        return asdict(self)

    def local_status(self, status: str) -> str:
        """作品状态 → 颜色。表格里没有 QSS 属性选择器可用，
        只能给单元格直接上色，所以这里集中一份映射，别在界面里写死。"""
        return {
            "done": self.success,
            "failed": self.danger,
            "skipped": self.text_faint,
            "downloading": self.running,
            "transcribing": self.running,
            "downloaded": self.running,
        }.get(status, self.text_muted)


# ------------------------------------------------------------------ 浅色
LIGHT = Tokens(
    bg="#F5F6F8",
    surface="#FFFFFF",
    surface_alt="#F8F9FB",
    surface_hover="#EFF1F5",
    surface_active="#E7EAF4",

    text="#1F2329",
    text_muted="#646A73",
    text_faint="#9BA1AA",
    text_on_accent="#FFFFFF",

    border="#E6E8EC",
    border_strong="#CFD3DA",

    accent="#4F6BED",
    accent_hover="#3D57D6",
    accent_soft="#EEF1FE",
    chip_bg="#F2F4F8",

    success="#1A7F37",
    success_soft="#EDF7F0",
    warning="#8A5D00",
    warning_soft="#FFF9E8",
    danger="#C62828",
    danger_soft="#FDF0F0",

    running="#1976D2",

    shadow="rgba(0,0,0,0.06)",
    scroll="#D8DAE0",
    scroll_hover="#C0C4CC",

    is_dark=False,
)

# ------------------------------------------------------------------ 深色
# ⚠️ 深色模式的「柔和底色」踩过的坑：
# 早先用的 success_soft=#1A2E22 / warning_soft=#33290F / danger_soft=#3A1D1D
# 都是**高饱和的深色**，铺在深灰面板上非常扎眼——一片暗绿/暗黄/暗红的色块，
# 和整体中性灰的主题完全不搭，用户反馈「提示框背景颜色太深，和主题不对应」。
#
# 正确的做法是：深色底上的柔和色应该**只比面板底色亮一点点**、并且大幅
# **降低饱和度**，靠左侧的强调色文字来表达语义，而不是靠整块底色。
DARK = Tokens(
    bg="#17171B",
    surface="#1E1E24",
    surface_alt="#23232A",
    surface_hover="#2A2A33",
    surface_active="#2E3048",

    text="#E7E8EC",
    text_muted="#9CA2AC",
    text_faint="#6E737C",
    text_on_accent="#FFFFFF",

    border="#2E2E37",
    border_strong="#41414C",

    accent="#8A9EF7",
    accent_hover="#A0B0F9",
    accent_soft="#252A42",
    chip_bg="#24242B",

    success="#5ED68C",
    success_soft="#1D2A22",
    warning="#E8C155",
    warning_soft="#2B2618",
    danger="#F08C8C",
    danger_soft="#2E2022",

    running="#6FB0F5",

    shadow="rgba(0,0,0,0.35)",
    scroll="#3A3A45",
    scroll_hover="#4C4C5A",

    is_dark=True,
)

THEMES = {"light": LIGHT, "dark": DARK}

# 统一间距/圆角刻度，QSS 里直接引用
RADIUS_SM = 6
RADIUS_MD = 10
RADIUS_LG = 14


def tokens(name: str = "light") -> Tokens:
    return THEMES.get((name or "light").lower(), LIGHT)


def is_dark(name: str) -> bool:
    return tokens(name).is_dark


def build_qss(t: Tokens) -> str:
    """由令牌生成整套 QSS。

    只写「全局 + 分区覆盖」，不针对单个控件写死色值，
    这样换主题时本文件是唯一需要改的地方。
    """
    return f"""
/* ============ 全局 ============ */
QWidget {{
    background: {t.bg};
    color: {t.text};
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
}}

QMainWindow, QDialog {{ background: {t.bg}; }}

QToolTip {{
    background: {t.surface};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 6px 9px;
}}

/* ============ 语义类名（业务代码用 property 挂） ============ */
QLabel[role="muted"]   {{ color: {t.text_muted}; }}
QLabel[role="faint"]   {{ color: {t.text_faint}; }}
QLabel[role="h1"]      {{ font-size: 17px; font-weight: 600; color: {t.text}; }}
QLabel[role="h2"]      {{ font-size: 14px; font-weight: 600; color: {t.text}; }}
QLabel[role="accent"]  {{ color: {t.accent}; }}
QLabel[role="danger"]  {{ color: {t.danger}; }}
QLabel[role="success"] {{ color: {t.success}; }}
/* 行内状态提示（无边框无底色，只换文字色）：
   用来标「ⓘ 编辑器已就绪 / ✗ 路径不存在」这类一句话反馈，
   跟上面带色条的 warning/info 框区分开——那些是成块的提示区，
   这些是紧跟在输入框下面的一行小字。 */
QLabel[role="ok"]   {{ color: {t.success}; }}
QLabel[role="warn"] {{ color: {t.warning}; }}

/* 提示框：不再用「整块高饱和底色」，改成
   「一层几乎看不出的浅底 + 左侧 3px 强调色竖条」。
   底色只负责把这一块和周围区分开，语义靠左边那道色条和文字颜色表达，
   这样深浅两套主题下都不会出现「一大片暗黄/暗绿」的突兀色块。 */
QLabel[role="warning"] {{
    color: {t.warning};
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-left: 3px solid {t.warning};
    border-radius: {RADIUS_SM}px;
    padding: 8px 11px;
}}
QLabel[role="info"] {{
    color: {t.text_muted};
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-left: 3px solid {t.accent};
    border-radius: {RADIUS_SM}px;
    padding: 8px 11px;
}}
QLabel[role="banner"] {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: {RADIUS_MD}px;
    padding: 9px 12px;
}}
QLabel[role="banner-ok"] {{
    background: {t.surface_alt};
    color: {t.success};
    border: 1px solid {t.border};
    border-left: 3px solid {t.success};
    border-radius: {RADIUS_MD}px;
    padding: 9px 12px;
}}
QLabel[role="banner-bad"] {{
    background: {t.surface_alt};
    color: {t.danger};
    border: 1px solid {t.border};
    border-left: 3px solid {t.danger};
    border-radius: {RADIUS_MD}px;
    padding: 9px 12px;
}}
QLabel[role="banner-info"] {{
    background: {t.surface_alt};
    color: {t.text_muted};
    border: 1px solid {t.border};
    border-left: 3px solid {t.accent};
    border-radius: {RADIUS_MD}px;
    padding: 9px 12px;
}}

/* 卡片容器：现代界面靠「浅底 + 细边 + 大圆角」分层，不靠阴影 */
QFrame[role="card"] {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {RADIUS_LG}px;
}}
QFrame[role="panel"] {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: {RADIUS_MD}px;
}}
QFrame[role="divider"] {{
    background: {t.border};
    border: none;
    max-height: 1px;
}}

/* ============ 左侧导航栏 ============ */
QFrame#sidebar {{
    background: {t.surface_alt};
    border: none;
    border-right: 1px solid {t.border};
}}
QLabel#brandTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {t.text};
}}
QLabel#brandSub {{
    font-size: 11px;
    color: {t.text_faint};
}}
QPushButton[nav="item"] {{
    text-align: left;
    padding: 9px 12px;
    border: none;
    border-radius: {RADIUS_MD}px;
    background: transparent;
    color: {t.text_muted};
    font-size: 13px;
}}
QPushButton[nav="item"]:hover {{
    background: {t.surface_hover};
    color: {t.text};
}}
QPushButton[nav="item"]:checked {{
    background: {t.accent_soft};
    color: {t.accent};
    font-weight: 600;
}}
QPushButton[nav="item"]:disabled {{ color: {t.text_faint}; }}

/* 分段切换（「文件」页的 文稿/中间文件/全部） */
QPushButton[nav="tab"] {{
    padding: 6px 14px;
    border: 1px solid {t.border};
    border-radius: 999px;
    background: {t.surface};
    color: {t.text_muted};
    font-size: 12px;
}}
QPushButton[nav="tab"]:hover {{ border-color: {t.border_strong}; color: {t.text}; }}
QPushButton[nav="tab"]:checked {{
    background: {t.accent_soft};
    border-color: {t.accent};
    color: {t.accent};
    font-weight: 600;
}}

/* ============ 按钮 ============ */
QPushButton {{
    background: {t.surface};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 7px 14px;
    min-height: 20px;
}}
QPushButton:hover  {{ background: {t.surface_hover}; border-color: {t.border_strong}; }}
QPushButton:pressed {{ background: {t.surface_active}; }}
QPushButton:disabled {{ color: {t.text_faint}; background: {t.surface_alt}; }}

QPushButton[role="primary"] {{
    background: {t.accent};
    color: {t.text_on_accent};
    border: none;
    font-weight: 600;
    padding: 9px 22px;
}}
QPushButton[role="primary"]:hover   {{ background: {t.accent_hover}; }}
QPushButton[role="primary"]:disabled {{ background: {t.surface_active}; color: {t.text_faint}; }}

QPushButton[role="danger"] {{
    background: {t.danger_soft};
    color: {t.danger};
    border: 1px solid transparent;
}}
QPushButton[role="danger"]:hover {{ border-color: {t.danger}; }}

QPushButton[role="ghost"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {t.text_muted};
    padding: 4px 8px;
}}
QPushButton[role="ghost"]:hover {{ background: {t.surface_hover}; color: {t.text}; }}

/* ============ 输入类 ============ */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t.surface};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    padding: 6px 9px;
    selection-background-color: {t.accent};
    selection-color: {t.text_on_accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {t.accent};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled,
QSpinBox:disabled {{
    background: {t.surface_alt};
    color: {t.text_faint};
}}
QLineEdit[readOnly="true"] {{ background: {t.surface_alt}; color: {t.text_muted}; }}

QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {t.text_muted};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {t.surface};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    selection-background-color: {t.accent_soft};
    selection-color: {t.accent};
    outline: none;
    padding: 4px;
}}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 18px;
    border: none;
    background: transparent;
}}

/* ============ 勾选 / 单选 ============ */
QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QCheckBox::indicator {{
    border: 1px solid {t.border_strong};
    border-radius: 4px;
    background: {t.surface};
}}
QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
QCheckBox::indicator:checked {{
    background: {t.accent};
    border-color: {t.accent};
}}
QRadioButton::indicator {{
    border: 1px solid {t.border_strong};
    border-radius: 8px;
    background: {t.surface};
}}
QRadioButton::indicator:checked {{
    background: {t.accent};
    border: 4px solid {t.surface};
    outline: 1px solid {t.accent};
}}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t.text_faint}; }}

/* ============ 分组框（改造成卡片风） ============ */
QGroupBox {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {RADIUS_LG}px;
    margin-top: 14px;
    padding: 14px 14px 12px 14px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 2px 6px;
    color: {t.text_muted};
    background: {t.surface};
    font-size: 12px;
}}

/* ============ 表格 / 树 ============ */
QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget, QListView {{
    background: {t.surface};
    alternate-background-color: {t.surface_alt};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_MD}px;
    gridline-color: {t.border};
    outline: none;
}}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{ padding: 5px 6px; }}
QTableWidget::item:selected, QTreeWidget::item:selected,
QListWidget::item:selected {{
    background: {t.accent_soft};
    color: {t.accent};
}}
QHeaderView::section {{
    background: {t.surface_alt};
    color: {t.text_muted};
    border: none;
    border-bottom: 1px solid {t.border};
    border-right: 1px solid {t.border};
    padding: 7px 6px;
    font-weight: 600;
}}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{ background: {t.surface_alt}; border: none; }}

/* ============ 进度条 ============ */
QProgressBar {{
    background: {t.surface_alt};
    border: none;
    border-radius: {RADIUS_SM}px;
    height: 8px;
    text-align: center;
    color: {t.text_muted};
}}
QProgressBar::chunk {{
    background: {t.accent};
    border-radius: {RADIUS_SM}px;
}}

/* ============ 滚动条 ============ */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t.scroll};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {t.scroll_hover}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {t.scroll};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {t.scroll_hover}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ============ 分隔 / 菜单 / 状态栏 ============ */
QSplitter::handle {{ background: {t.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QMenu {{
    background: {t.surface};
    color: {t.text};
    border: 1px solid {t.border};
    border-radius: {RADIUS_MD}px;
    padding: 5px;
}}
QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: {RADIUS_SM}px; }}
QMenu::item:selected {{ background: {t.accent_soft}; color: {t.accent}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

QStatusBar {{ background: {t.surface_alt}; color: {t.text_muted}; border-top: 1px solid {t.border}; }}
QStatusBar::item {{ border: none; }}

QTextBrowser {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {RADIUS_MD}px;
    padding: 12px;
}}

/* 运行日志：等宽字体 + 稍紧的行距，多任务并行时一屏能多看几行；
   日志正文由 HTML 着色（见 RunPage.log_line），这里只管容器。 */
QTextBrowser#runLog {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    border-radius: {RADIUS_MD}px;
    padding: 10px 12px;
    font-family: "Cascadia Mono", "Consolas", "JetBrains Mono", monospace;
    font-size: 12px;
}}

/* 折叠区的展开/收起按钮 */
QToolButton#collapsibleToggle {{
    text-align: left;
}}

/* 历史页的任务卡片：一整行摘要，比表格更容易扫 */
QFrame[role="taskcard"] {{
    background: {t.surface};
    border: 1px solid {t.border};
    border-radius: {RADIUS_MD}px;
}}
QFrame[role="taskcard"]:hover {{
    border-color: {t.border_strong};
}}
/* 最近动态里可双击跳转的行：平时和背景融为一体，
   悬停时给一点底色，让人意识到「这行是能点的」 */
QFrame[role="clickrow"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: {RADIUS_SM}px;
}}
QFrame[role="clickrow"]:hover {{
    background: {t.surface_hover};
    border-color: {t.border};
}}
/* 平台分布树的展开三角：默认太小、太浅，浅色主题下几乎看不见 */
QTreeWidget::branch {{
    background: transparent;
}}
QTreeWidget {{
    outline: 0;
}}
QTreeWidget::item {{
    padding: 3px 4px;
}}
QTreeWidget::item:selected {{
    background: {t.accent_soft};
    color: {t.accent};
}}
QLabel[role="mono"] {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 11px;
    color: {t.text_faint};
}}
QLabel[role="pill"] {{
    background: {t.surface_alt};
    border: 1px solid {t.border};
    color: {t.text_muted};
    border-radius: 999px;
    padding: 1px 8px;
    font-size: 11px;
}}

/* ============ 主题切换小按钮 ============ */
QPushButton[role="iconbtn"] {{
    background: transparent;
    border: 1px solid {t.border};
    border-radius: {RADIUS_SM}px;
    color: {t.text_muted};
    padding: 3px 9px;
    font-size: 12px;
}}
QPushButton[role="iconbtn"]:hover {{
    background: {t.surface_hover};
    border-color: {t.border_strong};
    color: {t.text};
}}

/* 概览数字（历史页） */
QLabel[role="stat"] {{ font-size: 20px; font-weight: 600; color: {t.text}; }}

/* 队列里正在跑的那一行 */
QLabel[role="chip"] {{
    background: {t.accent_soft};
    color: {t.accent};
    border-radius: 999px;
    padding: 2px 9px;
    font-size: 11px;
}}
"""


def repolish(widget) -> None:
    """改了 property 之后让 QSS 重新匹配（Qt 不认自动重算）。"""
    try:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()
    except Exception:  # noqa: BLE001
        pass
