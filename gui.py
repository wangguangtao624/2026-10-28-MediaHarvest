"""GUI 入口：python gui.py

⚠️ 文件里的 import 顺序不能改，改了程序会段错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _init_cuda_before_pyqt() -> None:
    """必须在导入 PyQt5 **之前**完成 CUDA 初始化。

    实测（Windows 11 + RTX 2060 + PyQt5 5.15 + ctranslate2/CUDA）：

      import PyQt5 → 再初始化 CUDA → 退出码 139（进程直接死，无异常、无 traceback）
      先初始化 CUDA → 再 import PyQt5 → 一切正常，子线程随意调 GPU

    连 QApplication() 都不用创建，光 import PyQt5.QtWidgets 就足以把后续
    CUDA 初始化搞崩。具体是 Qt 加载哪几个 DLL 与 CUDA 冲突没查到底，
    但结论可稳定复现，所以预热必须放在所有 PyQt5 import 之前。

    代价：启动时多花 2~3 秒（加载 75MB 的 tiny 模型然后释放）。
    """
    try:
        from app.asr.engine import FasterWhisperEngine, warmup_cuda
    except Exception:  # noqa: BLE001
        return

    if not FasterWhisperEngine.cuda_available():
        return
    try:
        if warmup_cuda():
            return
    except Exception as exc:  # noqa: BLE001
        print(f"[MediaHarvest] 显卡预热异常：{exc}", file=sys.stderr)
    # 预热失败：此时再让后台线程去初始化 CUDA 就是崩溃，直接锁 CPU
    print("[MediaHarvest] 显卡不可用，已回退到 CPU 转录。", file=sys.stderr)
    FasterWhisperEngine.cuda_disabled = True


_init_cuda_before_pyqt()

# ↓↓↓ 从这里开始才能安全导入 PyQt5
try:
    from PyQt5.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    sys.exit("未安装 PyQt5，请先 pip install PyQt5")


def main() -> int:
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFont
    from PyQt5.QtWidgets import QApplication

    from app.ui import MainWindow

    # 高分屏下不设这两个，界面会被系统拉伸得发虚
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("MediaHarvest")
    # Fusion 是跨平台一致的基础样式，配色全部由 theme.py 的 QSS 接管。
    # 不显式给字体的话，Fusion 在部分中文 Windows 上会退到衬线字体，
    # 界面会显得很"老"。
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
