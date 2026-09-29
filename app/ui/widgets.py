"""可复用的小控件：说明按钮、卡片、小节标题……

抽出来的原因：界面里要用到的地方太多（模型、设备、精度、并发、中间文件…），
每个地方手写一遍 QSS 和对话框既啰嗦又容易走样。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QTextBrowser,
    QToolButton, QVBoxLayout, QWidget,
)

from .theme import repolish, tokens


class InfoButton(QPushButton):
    """一个「?」，点开是说明卡。

    比单纯 tooltip 好在：内容长也不怕（tooltip 长了会飘、会超时消失），
    而且能选中复制。

    尺寸注意：圆钮要放得下一个字符 + 2px 边框，17px 在部分字体下会把
    「?」的上下切掉，所以用 18px 并把 padding 压到 0。
    """

    def __init__(self, title: str, html: str, parent: QWidget | None = None,
                 theme: str = "light") -> None:
        super().__init__("?", parent)
        self._title = title
        self._html = html
        self._theme = theme
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(title)
        self.setProperty("role", "ghost")
        t = tokens(theme)
        self.setStyleSheet(
            f"QPushButton{{border:1px solid {t.border_strong};border-radius:9px;"
            f"color:{t.text_muted};font-size:11px;font-weight:700;"
            f"padding:0;margin:0;background:transparent;min-height:0;}}"
            f"QPushButton:hover{{border-color:{t.accent};color:{t.accent};}}")
        self.clicked.connect(self._show)

    def update_html(self, title: str, html: str) -> None:
        self._title, self._html = title, html
        self.setToolTip(title)

    def _show(self) -> None:
        dlg = _InfoDialog(self._title, self._html, self.window())
        dlg.exec_()


class _InfoDialog(QDialog):
    def __init__(self, title: str, html: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)

        head = QLabel(title)
        head.setProperty("role", "h2")
        lay.addWidget(head)

        body = QTextBrowser()
        body.setOpenExternalLinks(True)
        body.setHtml(f"<div style='line-height:1.65'>{html}</div>")
        lay.addWidget(body, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("知道了")
        btn.setProperty("role", "primary")
        btn.clicked.connect(self.accept)
        row.addWidget(btn)
        lay.addLayout(row)


class Card(QFrame):
    """卡片容器：现代界面的基本分层单元（浅底 + 细边 + 大圆角）。"""

    def __init__(self, parent: QWidget | None = None, role: str = "card") -> None:
        super().__init__(parent)
        self.setProperty("role", role)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(10)


class SectionTitle(QWidget):
    """小节标题：左侧一条强调色竖线 + 标题 +（可选）说明按钮。

    参数顺序刻意设计成 (text, theme, info)：
    大部分调用点只关心「标题 + 说明」，theme 用默认值即可。
    """

    def __init__(self, text: str, theme: str = "light",
                 info: tuple[str, str] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        bar = QFrame()
        bar.setFixedSize(3, 14)
        t = tokens(theme)
        bar.setStyleSheet(f"background:{t.accent};border-radius:2px;")
        row.addWidget(bar)

        lb = QLabel(text)
        lb.setProperty("role", "h2")
        row.addWidget(lb)
        if info:
            # ⓘ 必须紧跟标题，不能放在 addStretch 之后——
            # 那样它会被推到卡片最右边，看着像个孤立的零件。
            row.addWidget(InfoButton(info[0], info[1], self, theme))
        row.addStretch(1)


def labeled(label: str, widget: QWidget, width: int = 76,
            theme: str = "light") -> QWidget:
    """「标签 + 控件」一行，统一标签宽度避免参差不齐。"""
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    lb = QLabel(label)
    lb.setFixedWidth(width)
    lb.setProperty("role", "muted")
    row.addWidget(lb)
    row.addWidget(widget, 1)
    return w


def hint(text: str, theme: str = "light", role: str = "muted") -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(True)
    lb.setProperty("role", role)
    return lb


def divider(theme: str = "light") -> QFrame:
    f = QFrame()
    f.setProperty("role", "divider")
    f.setFixedHeight(1)
    return f


class Collapsible(QWidget):
    """可折叠区块：一行标题按钮 + 一块默认收起的内容。

    为什么要有这个
    --------------
    配置页里「选什么模型 / CPU 还是 GPU / 计算精度 / 解码策略 / 执行模式 /
    并发 / CPU 线程」这些属于**高级选项**：默认值已经调好，普通用户根本不用
    碰；但一旦把所有下拉框平铺出来，页面就显得又多又吓人。

    它们的共同点是「不知道就别改，改了也不会更好」。所以收进折叠区：
    默认只显示一行「高级设置（默认即可）」，想看细节或者真要调的人点一下展开。

    交互细节：
    - 用 QToolButton 的 arrowType 做三角指示，不用自己画图标；
    - 标题行右侧带一句「当前：xxx」的摘要，收起时也能一眼看到当前配置，
      不必展开才知道自己选了什么；
    - 展开/收起要同时控制内容和摘要的显隐。
    """

    def __init__(self, title: str, theme: str = "light",
                 summary: str = "", expanded: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._summary_text = summary

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        t = tokens(theme)
        self.toggle = QToolButton()
        self.toggle.setObjectName("collapsibleToggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle.setStyleSheet(
            f"QToolButton#collapsibleToggle{{"
            f"border:1px dashed {t.border};border-radius:8px;"
            f"background:{t.surface_alt};color:{t.text_muted};"
            f"padding:7px 10px;text-align:left;font-size:12px;}}"
            f"QToolButton#collapsibleToggle:hover{{"
            f"border-color:{t.accent};color:{t.accent};}}")
        self.toggle.toggled.connect(self._on_toggle)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(self.toggle, 1)
        self.label_summary = QLabel(summary)
        self.label_summary.setProperty("role", "faint")
        self.label_summary.setStyleSheet(f"color:{t.text_faint};font-size:11px;")
        head.addWidget(self.label_summary)
        head.addStretch(0)
        col.addLayout(head)

        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(2, 10, 2, 2)
        self.body.setSpacing(10)
        self.content.setVisible(expanded)
        col.addWidget(self.content)

    def _on_toggle(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(on)

    def set_summary(self, text: str) -> None:
        """收起状态下也能看到当前配置摘要。"""
        self._summary_text = text
        self.label_summary.setText(text)

    def add(self, widget) -> None:
        """往折叠区里加一行（QLayout 也可以）。"""
        if hasattr(widget, "count") and not isinstance(widget, QWidget):
            self.body.addLayout(widget)
        else:
            self.body.addWidget(widget)

    def is_expanded(self) -> bool:
        return self.toggle.isChecked()


def apply_role(widget: QWidget, role: str) -> None:
    """给控件挂语义类名并让它立即生效（Qt 不会自动重算 QSS）。"""
    widget.setProperty("role", role)
    repolish(widget)
