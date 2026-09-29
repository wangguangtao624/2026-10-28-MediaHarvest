"""逐段插桩，定位 GUI 流程里到底哪一步段错误。"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from app.ui import MainWindow

SHARE = "https://v.douyin.com/g3n27E0f4vw/"
MEDIA = "data/media/7685043849751694646.mp4"
if os.path.exists(MEDIA):
    os.remove(MEDIA)


def p(*a):
    print(f"[{time.time()-T0:5.1f}s]", *a, flush=True)


T0 = time.time()
app = QApplication(sys.argv)
w = MainWindow()
p("MainWindow 建好")

from app.asr.engine import warmup_cuda, FasterWhisperEngine

p("cuda_available =", FasterWhisperEngine.cuda_available())
p("开始预热（主线程）…")
ok = warmup_cuda()
p("预热结果 =", ok)
p("预热后直接子进程态检查 OK")

# 只跑处理阶段，不跑采集：手动造一条 Item
from app.core.models import Item, Task, MODE_MANUAL

cfg = {
    "platform": "douyin", "mode": MODE_MANUAL, "query": "",
    "urls": [SHARE], "max_count": 1, "proxy": "",
    "fetch_comments": False, "do_download": True,
    "do_transcribe": True, "model": "small", "device": "auto",
    "output_dir": "out/crash_locate",
}
w.cfg = cfg
w.task = Task(name="crash-locate", mode=MODE_MANUAL, platform="douyin",
              max_count=1, do_download=True, do_transcribe=True,
              output_dir=cfg["output_dir"])

from app.core.cookie_manager import CookieManager
from app.crawler.douyin import DouyinCrawler

cm = CookieManager()
p("开始解析作品…")
c = DouyinCrawler(cookie=cm.to_header("douyin"))
it = c.fetch_detail(c.resolve_aweme_id(SHARE))
p("作品解析完成:", it.item_id, "| 直连:", bool(it.stream_url))
w.pipeline.upsert_items([it])

p("→ 调用 start_process")
w.start_process([it])


def watchdog():
    p("watchdog 轮询… 状态:", w.page_run.label_stage.text()[:40])
    if time.time() - T0 > 200:
        app.quit()


t = QTimer()
t.timeout.connect(watchdog)
t.start(2000)
QTimer.singleShot(210000, app.quit)
p("进入事件循环")
app.exec_()
p("事件循环退出（正常）")
