"""转录/下载过程的运行控制。

GUI 线程只负责往这里**放指令**（暂停、前进、后退、跳转、停止），
处理线程在下载的每个数据块、转录的每一段上调用 `checkpoint()` 来**取指令**。
两边通过一把条件变量同步，不用 Queue，逻辑更直白。

为什么不让 GUI 直接去操作 workers：
- Qt 的信号槽在跨线程时是排队投递的，命令到达时机不可控；
- 暂停要求「立刻生效但又不丢进度」，必须在处理线程自己的循环里检查。

指令语义（和界面上三个按钮一一对应）：
    暂停 / 继续   暂停：就地挂起，恢复后从原位置继续，不丢已下载/已转录的部分
    跳过当前（前进）放弃当前条目，直接处理下一条；本条标记为「已跳过」
    重做上一条（后退）放弃当前条目，回到上一条从头再来（第一条则重做自己）
    跳到第 N 条（选择）放弃当前条目，直接处理队列里的第 N 条
    停止          放弃当前条目，整个任务收尾（已经处理的条目结果保留）
"""

from __future__ import annotations

import threading
import time

# checkpoint() 的返回值
ACTION_NONE = "none"      # 继续跑
ACTION_SKIP = "skip"      # 放弃当前条目，处理下一条
ACTION_BACK = "back"      # 放弃当前条目，回到上一条
ACTION_JUMP = "jump"      # 放弃当前条目，跳到 take_jump() 指定的下标
ACTION_STOP = "stop"      # 放弃当前条目，整个任务停止

# 一条一次性指令最多被取几次（并发转录线程数上限），取满即失效
_MAX_TAKERS = 4

# 一次「取指令」窗口能再延续多久（秒）。同一条目内的多个线程
# 只要都还在这一次窗口里，就能各自看到同一指令。
_GRACE = 0.35


class ProcessControl:
    """线程安全的运行控制器。处理线程调用 checkpoint()，GUI 调用其余方法。

    并发语义（overlap 模式下有多个转录线程同时 checkpoint）：
    - 「一次性指令」要在**同一条目的多个并发线程之间共享**：4 个线程一起
      转录时，点了「跳过」应该让它们都放弃当前条目，而不是被某一条吞掉。
    - 但指令**绝不能跨界到下一条目**：A 被跳过之后 B 才开始跑，
      B 不该再收到那条 skip（实测踩过：A 跳过，B 跟着也被中止了）。
    - 于是用「代次」(epoch) 来界定边界：
        指令下达时，记录下当前代次；之后只要 `begin_item` 被调用
        （说明进入了新条目），代次 +1，旧指令立即作废。
      同一条目内的并发线程共享同一代次，因此都能看到；新条目代次不同，
      自然看不到。
    - 再叠加时间戳过期，防止没人来取时指令一直残留。
    暂停/停止是**状态**不是一次性指令，天然对所有线程生效。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._paused = False
        self._stopped = False
        self._pending = ACTION_NONE      # 一次性指令
        self._pending_ts = 0.0           # 指令下达时间
        self._take_count = 0             # 已被取走次数
        self._epoch = 0                  # 当前条目代次
        self._pending_epoch = 0          # 指令下达时所属代次
        self._jump_index: int | None = None

        # 只读展示信息（GUI 用来画队列）
        self.index = 0
        self.total = 0
        self.label = ""
        self.phase = ""                  # download / transcribe / idle

    # ---------------- GUI 侧 ----------------
    def pause(self) -> None:
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def toggle_pause(self) -> bool:
        """返回切换后的状态：True 表示现在处于暂停。"""
        with self._cond:
            self._paused = not self._paused
            self._cond.notify_all()
            return self._paused

    @property
    def paused(self) -> bool:
        with self._cond:
            return self._paused

    @property
    def stopped(self) -> bool:
        with self._cond:
            return self._stopped

    def _issue(self, action: str) -> None:
        """下达一次性指令（内部）。绑在当前代次上，换条目即失效。"""
        self._pending = action
        self._pending_ts = time.monotonic()
        self._take_count = 0
        self._pending_epoch = self._epoch
        self._paused = False
        self._cond.notify_all()

    def skip_current(self) -> None:
        """前进：放弃当前条目。"""
        with self._cond:
            self._issue(ACTION_SKIP)

    def back(self) -> None:
        """后退：回到上一条重做。"""
        with self._cond:
            self._issue(ACTION_BACK)

    def jump_to(self, index: int) -> None:
        """选择：跳到指定下标。"""
        with self._cond:
            self._jump_index = max(0, int(index))
            self._issue(ACTION_JUMP)

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._issue(ACTION_STOP)

    # ---------------- 处理线程侧 ----------------
    def begin_item(self, index: int, total: int, label: str, phase: str = "") -> None:
        with self._cond:
            self.index, self.total, self.label, self.phase = index, total, label, phase
            # 进入新条目 → 代次 +1，上一轮的一次性指令立刻作废，
            # 避免「A 的跳过指令把 B 也一起中止」。
            if self._pending != ACTION_NONE:
                self._pending = ACTION_NONE
                self._take_count = 0
            self._epoch += 1

    def set_phase(self, phase: str) -> None:
        with self._cond:
            self.phase = phase

    def checkpoint(self) -> str:
        """取一次指令。暂停时在此阻塞，返回非 NONE 表示当前条目要中止。"""
        with self._cond:
            while True:
                if self._stopped:
                    return ACTION_STOP
                if self._pending != ACTION_NONE:
                    # ⚠️ 过期判断必须基于时间戳，不能靠「被读取时顺便清理」：
                    # 没人来读时指令会一直留着，等下一轮循环取到就变成鬼指令。
                    expired = (time.monotonic() - self._pending_ts) > _GRACE
                    # 跨代次（已进入新条目）的旧指令一律作废
                    stale = self._pending_epoch != self._epoch
                    if expired or stale or self._take_count >= _MAX_TAKERS:
                        self._pending = ACTION_NONE
                        self._take_count = 0
                        # 过期了就当没这回事，继续往下判断暂停状态
                    else:
                        action = self._pending
                        self._take_count += 1
                        return action
                if not self._paused:
                    return ACTION_NONE
                self._cond.wait(0.2)

    def take_jump(self) -> int | None:
        with self._cond:
            idx, self._jump_index = self._jump_index, None
            return idx


# 什么都不做（CLI / 测试用）
class NullControl(ProcessControl):
    def checkpoint(self) -> str:  # noqa: D102
        return ACTION_NONE
