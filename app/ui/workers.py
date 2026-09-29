"""后台任务：采集、处理都跑在 QThread 里，界面不卡。

⚠️ 三个坑，改这里之前先看：

1. 不要用 `finished` 当信号名 —— QThread 自己有一个 `finished`，
   在子类里定义同名 pyqtSignal 会把父类的遮蔽掉，导致拿不到「线程真正结束」的通知。
   所以这里统一叫 `done`。

2. 处理和导出必须在**同一个线程**里顺序执行。
   之前是处理线程 emit done 后立刻起一个导出线程，两个线程同时碰同一个 SQLite
   连接（连接是 check_same_thread=False 建的，当时还没加锁）→ 未定义行为，
   实测直接把进程干成段错误（Segmentation fault），表现就是「导出卡住」。

3. 暂停/前进/后退不要用信号传进线程 —— Qt 的跨线程信号是排队投递的，
   到达时机不可控，暂停必须立刻生效。所以控制指令走 `ProcessControl`
   （一把条件变量），线程在自己的循环里主动 `checkpoint()` 取指令。
"""

from __future__ import annotations

from typing import Any

from PyQt5.QtCore import QThread, pyqtSignal

from ..core.control import ProcessControl
from ..core.models import Item, Task


class CollectWorker(QThread):
    """采集阶段：拉列表，不下载。"""

    progress = pyqtSignal(str, int, int)
    done = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, pipeline: Any, task: Task, cookie: str = "",
                 proxy: str = "", urls: list[str] | None = None,
                 control: ProcessControl | None = None) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.task = task
        self.cookie = cookie
        self.proxy = proxy
        self.urls = urls
        self.control = control or ProcessControl()

    def run(self) -> None:
        try:
            self.pipeline.progress = lambda m, c, t: self.progress.emit(m, c, t)
            items = self.pipeline.collect(
                self.task, cookie=self.cookie, proxy=self.proxy,
                urls=self.urls, control=self.control)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc)[:500])
            return
        self.done.emit(items)


class ProcessWorker(QThread):
    """处理阶段：下载 + 转录 + 导出，全部在同一个线程里顺序跑完。"""

    progress = pyqtSignal(str, int, int)
    item_state = pyqtSignal(int, str, str)   # (下标, item_id, 状态)
    done = pyqtSignal(list)                  # 处理完成
    exported = pyqtSignal(dict)              # 导出完成
    failed = pyqtSignal(str)

    def __init__(self, pipeline: Any, items: list[Item], task: Task,
                 cookie: str = "", proxy: str = "", model_size: str = "small",
                 device: str = "auto", export_after: bool = True,
                 control: ProcessControl | None = None) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.items = items
        self.task = task
        self.cookie = cookie
        self.proxy = proxy
        self.model_size = model_size
        self.device = device
        self.export_after = export_after
        self.control = control or ProcessControl()

    def run(self) -> None:
        def _item_cb(idx: int, item: Item, status: str) -> None:
            self.item_state.emit(idx, item.item_id, status)

        try:
            self.pipeline.progress = lambda m, c, t: self.progress.emit(m, c, t)
            self.pipeline.on_item = _item_cb
            # 性能/内容选项都在 task 上，pipeline 自己取，不必在这里重复传参
            out = self.pipeline.process(
                self.items, self.task, cookie=self.cookie, proxy=self.proxy,
                model_size=self.model_size, device=self.device,
                control=self.control)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc)[:500])
            return
        self.done.emit(out)

        if not self.export_after:
            return
        try:
            self.progress.emit("正在导出文件…", 0, 100)
            paths = self.pipeline.export(self.items, self.task,
                                         control=self.control)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"导出失败：{str(exc)[:400]}")
            return
        self.exported.emit(paths)


class ExportWorker(QThread):
    """单独导出（目前只在「只导出不处理」的分支用，正常流程走 ProcessWorker）。"""

    progress = pyqtSignal(str, int, int)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, pipeline: Any, items: list[Item], task: Task,
                 control: ProcessControl | None = None) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.items = items
        self.task = task
        self.control = control or ProcessControl()

    def run(self) -> None:
        try:
            self.pipeline.progress = lambda m, c, t: self.progress.emit(m, c, t)
            paths = self.pipeline.export(self.items, self.task,
                                         control=self.control)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc)[:500])
            return
        self.done.emit(paths)


class VerifyWorker(QThread):
    """平台连通性校验（给配置页顶部提示条用）。"""

    done = pyqtSignal(bool, str)

    def __init__(self, fn: Any) -> None:
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            ok, msg = self.fn()
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)[:200]
        self.done.emit(ok, msg)
