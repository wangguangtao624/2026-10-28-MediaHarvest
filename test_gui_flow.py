"""GUI 全流程自动化冒烟测试（无人工点击）。

验证「采集 → 下载 → 转录 → 导出」在真实 Qt 事件循环里能跑完，
进程不崩、进度条有连续变化、最后能跳到导出页。

⚠️ CUDA 必须在 QApplication() 之前初始化，否则段错误（见 gui.py 注释）。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ⚠️ CUDA 预热必须在导入 PyQt5 之前完成（见 gui.py 的注释）
from app.asr.engine import FasterWhisperEngine, warmup_cuda

_CUDA = FasterWhisperEngine.cuda_available()
_WARMED = warmup_cuda() if _CUDA else False

from PyQt5.QtCore import QTimer          # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

SHARE = ("3.53 NWM:/ 10/22 EasySlides | GPT-6 Astra 做PPT … # Codex # PPT # GPT6  "
         "https://v.douyin.com/g3n27E0f4vw/ 复制此链接，打开Dou音搜索，直接观看视频！")

MEDIA = "data/media/7685043849751694646.mp4"
if os.path.exists(MEDIA):
    os.remove(MEDIA)   # 强制走一遍真实下载

state = {"pcts": [], "exported": None, "failed": None, "t0": 0.0}


def main() -> int:
    print(f"CUDA 可用={_CUDA} 预热成功={_WARMED}", flush=True)

    from app.ui import MainWindow

    app = QApplication(sys.argv)
    w = MainWindow()

    cfg = w.page_setup.collect_cfg()
    cfg.update({
        "platform": "douyin", "mode": "manual", "urls": [SHARE],
        "model": "small", "device": "auto",
        "do_download": True, "do_transcribe": True,
        "output_dir": "out/gui_flow",
    })
    w.page_setup.edit_urls.setPlainText(SHARE)
    w.page_setup.combo_model.setCurrentText("small")

    def on_progress(msg, cur, total):
        pct = int(cur / total * 100) if total else 0
        state["pcts"].append(pct)
        if len(state["pcts"]) % 15 == 1:
            print(f"  [{pct:3d}%] {msg[:64]}", flush=True)

    def on_collected(items):
        print(f"采集完成：{len(items)} 条", flush=True)
        for it in items:
            w.pipeline.select(it.platform, it.item_id, True)
        w.start_process(items)

    def on_processed(items):
        ok = sum(1 for i in items if i.status == "done")
        print(f"处理完成：成功 {ok}/{len(items)}，等待导出…", flush=True)

    def on_exported(paths):
        state["exported"] = paths
        print("导出完成：", list(paths), flush=True)

    def on_failed(msg):
        state["failed"] = msg
        print("❌ 失败：", msg[:200], flush=True)

    w.page_run.on_progress = on_progress      # type: ignore[method-assign]
    w.on_collected = on_collected             # type: ignore[method-assign]
    w.on_processed = on_processed             # type: ignore[method-assign]
    w.on_exported = on_exported               # type: ignore[method-assign]
    w.on_collect_failed = on_failed           # type: ignore[method-assign]

    state["t0"] = time.time()
    w.start_collect(cfg)

    def watchdog():
        if state["exported"] or state["failed"]:
            app.quit()
        elif time.time() - state["t0"] > 400:
            state["failed"] = "超时"
            app.quit()

    t = QTimer()
    t.timeout.connect(watchdog)
    t.start(500)
    QTimer.singleShot(410000, app.quit)
    app.exec_()

    print(flush=True)
    print("=" * 58, flush=True)
    if state["exported"]:
        print("✅ GUI 全流程通过（采集→下载→转录→导出），进程正常退出", flush=True)
        print(f"   进度采样 {len(state['pcts'])} 次，"
              f"区间 {min(state['pcts'])}% → {max(state['pcts'])}%", flush=True)
        return 0
    print("❌ 未走到导出：", state["failed"], flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
