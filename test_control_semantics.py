"""运行控制语义测试（串行 + 并发两种场景）。

这轮改动引入了并发转录（overlap 模式），控制器必须同时满足：
- 串行：一次性指令取走即生效，不残留到下一轮
- 并发：一条指令要让**所有**转录线程都收到（否则被无关线程吞掉）
- 暂停：阻塞式生效，恢复后从原位置继续，不丢进度
- 停止：状态型，永久生效

之所以单独写这个测试文件：并发下「指令可见性」的坑非常隐蔽 ——
最初实现是取走即清空，实测 4 个线程一起取时指令凭空消失，
界面上点了「跳过」但没反应。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.control import ACTION_NONE, ProcessControl  # noqa: E402

PROBLEMS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        PROBLEMS.append(label)


def t1_concurrent_visibility() -> None:
    print("\n【1】并发下指令对所有线程可见")
    ctl = ProcessControl()
    seen: list[int] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        for _ in range(500):
            a = ctl.checkpoint()
            if a != ACTION_NONE:
                with lock:
                    seen.append(n)
                return
            time.sleep(0.001)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    time.sleep(0.05)
    ctl.skip_current()
    for t in ts:
        t.join(timeout=3)
    check(len(seen) == 4, f"4 个线程全部收到指令（实得 {len(seen)}）")
    check(all(not t.is_alive() for t in ts), "所有线程都退出了")


def t2_no_stale_command() -> None:
    print("\n【2】指令过期后不残留（关键：无人读取也要失效）")
    ctl = ProcessControl()
    ctl.skip_current()
    time.sleep(1.0)
    got = ctl.checkpoint()
    check(got == ACTION_NONE, f"1 秒后取到的是 {got}（应为 none）")


def t3_serial_immediate() -> None:
    print("\n【3】串行场景：指令下达后立刻可取到")
    ctl = ProcessControl()
    ctl.skip_current()
    check(ctl.checkpoint() == "skip", "紧接着 checkpoint 拿到 skip")


def t4_take_limit() -> None:
    print("\n【4】取满次数后自动失效")
    ctl = ProcessControl()
    ctl.skip_current()
    got = [ctl.checkpoint() for _ in range(5)]
    check(got[:4] == ["skip"] * 4, f"前 4 次都是 skip（实得 {got[:4]}）")
    check(got[4] == ACTION_NONE, f"第 5 次是 none（实得 {got[4]}）")


def t5_pause_blocks() -> None:
    print("\n【5】暂停阻塞、恢复后继续")
    ctl = ProcessControl()
    ctl.pause()
    got: list[str] = []
    t = threading.Thread(target=lambda: got.append(ctl.checkpoint()))
    t.start()
    time.sleep(0.3)
    blocked = t.is_alive()
    ctl.resume()
    t.join(timeout=2)
    check(blocked, "暂停期间 checkpoint 确实阻塞住了")
    check(got == [ACTION_NONE], f"恢复后返回 none（实得 {got}）")
    check(ctl.paused is False, "paused 状态已复位")


def t6_toggle_pause() -> None:
    print("\n【6】暂停开关往返")
    ctl = ProcessControl()
    check(ctl.toggle_pause() is True, "第一次切换 → 暂停")
    check(ctl.paused is True, "paused 属性为真")
    check(ctl.toggle_pause() is False, "第二次切换 → 继续")
    check(ctl.paused is False, "paused 属性为假")


def t7_stop_persistent() -> None:
    print("\n【7】停止是持久状态")
    ctl = ProcessControl()
    ctl.stop()
    time.sleep(0.5)
    check(ctl.checkpoint() == "stop", "0.5 秒后仍是 stop")
    check(ctl.checkpoint() == "stop", "再取一次还是 stop")
    check(ctl.stopped is True, "stopped 属性为真")


def t8_jump() -> None:
    print("\n【8】跳转目标只被取走一次")
    ctl = ProcessControl()
    ctl.jump_to(3)
    check(ctl.take_jump() == 3, "第一次 take_jump 得到 3")
    check(ctl.take_jump() is None, "第二次 take_jump 得到 None")
    check(ctl.checkpoint() == "jump", "checkpoint 能拿到 jump 指令")


def t9_skip_clears_pause() -> None:
    print("\n【9】暂停中下达指令会自动解除暂停")
    ctl = ProcessControl()
    ctl.pause()
    ctl.skip_current()
    got: list[str] = []
    t = threading.Thread(target=lambda: got.append(ctl.checkpoint()))
    t.start()
    t.join(timeout=2)
    check(got == ["skip"], f"暂停中下达 skip 后能立即取到（实得 {got}）")


def t10_no_cross_item_leak() -> None:
    """回归：跳过 A 的指令不能把下一条 B 也一起中止。

    真实踩坑：宽限期本是为了让并发线程共享指令，但串行时 A 被跳过后
    B 立刻开始，B 的第一轮 checkpoint 仍然落在宽限期内，
    于是把同一条 skip 又消费了一次 —— A 跳过、B 跟着被中止。
    修法：引入「代次」，begin_item() 一调用旧指令立即作废。
    """
    print("\n【10】指令不跨条目泄漏（跳过 A 不影响 B）")
    ctl = ProcessControl()

    # A 正在处理（代次 0）
    ctl.begin_item(1, 2, "A")
    ctl.skip_current()
    got_a = ctl.checkpoint()
    check(got_a == "skip", f"A 取到 skip（实得 {got_a}）")

    # 进入 B：代次 +1，上一条 skip 必须立即作废
    ctl.begin_item(2, 2, "B")
    got_b = ctl.checkpoint()
    check(got_b == ACTION_NONE,
          f"B 不该再看到 A 的 skip（实得 {got_b}）")

    # 反过来也要成立：B 自己的指令必须有效
    ctl.skip_current()
    got_b2 = ctl.checkpoint()
    check(got_b2 == "skip", f"B 自己下的 skip 生效（实得 {got_b2}）")


def main() -> int:
    for fn in (t1_concurrent_visibility, t2_no_stale_command,
               t3_serial_immediate, t4_take_limit, t5_pause_blocks,
               t6_toggle_pause, t7_stop_persistent, t8_jump,
               t9_skip_clears_pause, t10_no_cross_item_leak):
        fn()
    print("\n" + "=" * 58)
    if PROBLEMS:
        print(f"❌ {len(PROBLEMS)} 项未通过：")
        for p in PROBLEMS:
            print("   -", p)
        return 1
    print("✅ 运行控制语义全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
