"""定位段错误：ctranslate2/CUDA 在不同线程里的行为对照。

用法：python -u test_cuda_thread.py <模式>
模式：main / qthread / split
- main     : 主线程里 import+load+infer（基线，已知可用）
- qthread  : 全部放进 QThread（复现崩溃）
- split    : 主线程 import+load，QThread 里只做推理
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODE = sys.argv[1] if len(sys.argv) > 1 else "main"
AUDIO = "data/media/7667307851375527653.mp4"

print(f"=== 模式：{MODE} ===", flush=True)


def do_load():
    from app.asr.engine import FasterWhisperEngine
    e = FasterWhisperEngine(model_size="small", device="auto")
    t0 = time.time()
    e.load()
    print(f"  加载完成 {time.time()-t0:.1f}s 设备={e._resolved_device}", flush=True)
    return e


def do_infer(e):
    t0 = time.time()
    r = e.transcribe(AUDIO)
    print(f"  推理完成 {time.time()-t0:.1f}s 字数={len(r.text)}", flush=True)
    print(f"  首句：{r.text[:40]}", flush=True)


if MODE == "main":
    e = do_load()
    do_infer(e)
    print("OK", flush=True)

elif MODE == "qthread":
    from PyQt5.QtCore import QThread
    from PyQt5.QtWidgets import QApplication

    class W(QThread):
        def run(self):
            e = do_load()
            do_infer(e)
            print("OK", flush=True)

    app = QApplication(sys.argv)
    w = W()
    w.finished.connect(app.quit)
    w.start()
    app.exec_()

elif MODE == "warm":
    # 主线程先用 tiny 模型把 CUDA/ctranslate2 初始化掉，
    # 之后让 QThread 自己加载 small —— 看还会不会崩
    from app.asr.engine import FasterWhisperEngine
    warm = FasterWhisperEngine(model_size="tiny", device="cuda")
    t0 = time.time()
    warm.load()
    print(f"  预热完成 {time.time()-t0:.1f}s", flush=True)
    warm.unload()

    from PyQt5.QtCore import QThread
    from PyQt5.QtWidgets import QApplication

    class W(QThread):
        def run(self):
            e = do_load()
            do_infer(e)
            print("OK", flush=True)

    app = QApplication(sys.argv)
    w = W()
    w.finished.connect(app.quit)
    w.start()
    app.exec_()

elif MODE == "split":
    # 主线程把 ctranslate2 import 进来并加载模型，再交给 QThread 推理
    e = do_load()
    from PyQt5.QtCore import QThread
    from PyQt5.QtWidgets import QApplication

    class W(QThread):
        def run(self):
            do_infer(e)
            print("OK", flush=True)

    app = QApplication(sys.argv)
    w = W()
    w.finished.connect(app.quit)
    w.start()
    app.exec_()
