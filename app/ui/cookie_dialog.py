"""Cookie 配置对话框：教程 + 粘贴 + 一键验通。

需求来源：cookie 不能写死在代码里，程序内可配置，配完立刻能验证连通性，
联不通就提示用户重新用 EditThisCookie 导出粘贴。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ..core.cookie_manager import CookieManager
from ..core.verify import verify

STYLE_OK = "color: #1a7f37; font-weight: bold;"
STYLE_BAD = "color: #c0392b; font-weight: bold;"
STYLE_WAIT = "color: #8a6d3b;"


class _VerifyWorker(QThread):
    """联网校验放在子线程里跑，避免界面卡死。"""

    done = pyqtSignal(bool, str)

    def __init__(self, cm: CookieManager, platform: str) -> None:
        super().__init__()
        self.cm = cm
        self.platform = platform

    def run(self) -> None:
        try:
            ok, msg = verify(self.cm, self.platform)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"校验异常：{str(exc)[:200]}"
        self.done.emit(ok, msg)


class _PlatformTab(QWidget):
    def __init__(self, cm: CookieManager, platform: str, label: str) -> None:
        super().__init__()
        self.cm = cm
        self.platform = platform
        self._worker: _VerifyWorker | None = None

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(
            "在这里粘贴 EditThisCookie 导出的 JSON，或浏览器里复制的 cookie 字符串")
        self.editor.setMinimumHeight(120)
        self.result = QLabel("")
        self.result.setWordWrap(True)

        tutorial = QTextBrowser()
        tutorial.setPlainText(cm.tutorial(platform))
        tutorial.setMaximumHeight(170)
        tutorial.setReadOnly(True)

        self.btn_verify = QPushButton("验证连通性")
        self.btn_save = QPushButton("保存")
        self.btn_clear = QPushButton("清除")
        self.btn_verify.clicked.connect(self.on_verify)
        self.btn_save.clicked.connect(self.on_save)
        self.btn_clear.clicked.connect(self.on_clear)

        row = QHBoxLayout()
        row.addWidget(self.btn_verify)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_clear)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(QLabel(f"【{label}】获取教程"))
        layout.addWidget(tutorial)
        layout.addWidget(QLabel("粘贴区"))
        layout.addWidget(self.editor)
        layout.addLayout(row)
        layout.addWidget(self.result)

        self.refresh()

    # ---------- 状态 ----------
    def refresh(self) -> None:
        ok, msg = self.cm.quick_check(self.platform)
        if ok:
            self.status.setText(f"当前状态：✅ {msg}")
            self.status.setStyleSheet(STYLE_OK)
        else:
            self.status.setText(f"当前状态：⚠️ {msg}")
            self.status.setStyleSheet(STYLE_BAD)

    # ---------- 动作 ----------
    def on_save(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            self._set_result(False, "粘贴区是空的")
            return
        try:
            n = self.cm.save(self.platform, text)
        except Exception as exc:  # noqa: BLE001
            self._set_result(False, f"保存失败：{exc}")
            return
        self.refresh()
        self._set_result(True, f"已保存 {n} 条 cookie，点「验证连通性」确认是否可用")

    def on_clear(self) -> None:
        self.cm.clear(self.platform)
        self.editor.clear()
        self.refresh()
        self._set_result(False, "已清除")

    def on_verify(self) -> None:
        text = self.editor.toPlainText().strip()
        if text:
            # 有粘贴内容就先存再验，避免用户忘了点保存
            try:
                self.cm.save(self.platform, text)
                self.refresh()
            except Exception:  # noqa: BLE001
                pass
        ok, msg = self.cm.quick_check(self.platform)
        if not ok:
            self._set_result(False, f"离线自检未通过：{msg}")
            return
        self.btn_verify.setEnabled(False)
        self._set_result(None, "正在验证连通性…")
        self._worker = _VerifyWorker(self.cm, self.platform)
        self._worker.done.connect(self.on_verify_done)
        self._worker.start()

    def on_verify_done(self, ok: bool, msg: str) -> None:
        self.btn_verify.setEnabled(True)
        self._set_result(ok, msg)

    def _set_result(self, ok: bool | None, msg: str) -> None:
        if ok is None:
            self.result.setStyleSheet(STYLE_WAIT)
        elif ok:
            self.result.setStyleSheet(STYLE_OK)
        else:
            self.result.setStyleSheet(STYLE_BAD)
        self.result.setText(msg)


class CookieDialog(QDialog):
    def __init__(self, cm: CookieManager, parent=None,
                 focus_platform: str = "") -> None:
        super().__init__(parent)
        self.cm = cm
        self.setWindowTitle("Cookie 配置")
        self.resize(680, 620)

        self.tabs = QTabWidget()
        self._tabs: dict[str, _PlatformTab] = {}
        # ⚠️ 平台清单必须从 registry 取，别手写。
        # 之前这里写死 bilibili/douyin，加了小红书后配置页就没有它的
        # cookie 输入窗口（历史页的平台下拉踩过同一个坑）。
        from ..crawler import registry as _reg
        for spec in _reg.platform_list():
            if not spec.need_cookie:
                continue          # telegram / local 不需要 cookie
            tab = _PlatformTab(cm, spec.key, spec.label)
            self._tabs[spec.key] = tab
            self.tabs.addTab(tab, spec.label)

        if focus_platform in self._tabs:
            self.tabs.setCurrentWidget(self._tabs[focus_platform])

        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        tip = QLabel(
            "Cookie 只保存在本机 data/cookies/ 目录，不会写进代码、不会上传。\n"
            "过期后重新用 EditThisCookie 导出粘贴、点一次「验证连通性」即可。")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        layout.addWidget(self.tabs)
        layout.addWidget(box)
