"""运行控制语义测试（不联网、不用 GPU，纯逻辑验证）。

用假的引擎 + 假的条目，实测四种控制指令是否真的生效：
    暂停 → 进度确实停住，继续后从原位置接着跑（不丢进度）
    跳过当前 → 当前条目标记 skipped，直接处理下一条
    重做上一条 → 回到上一条从头再来
    跳转 → 直接处理指定的第 N 条
    停止 → 整个任务收尾，不吞异常
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.control import ProcessControl          # noqa: E402
from app.core.models import Item, Task, MODE_CREATOR  # noqa: E402
from app import pipeline as pl                       # noqa: E402

TMP = ROOT / ".tmp" / "control_test"
TMP.mkdir(parents=True, exist_ok=True)

LOG: list[tuple[str, int]] = []
CALLS: dict[str, int] = {}
LOCK = threading.Lock()


class FakeEngine:
    """按「步」推进的假转录器，方便精确观察暂停/中止时机。"""

    def __init__(self, model_size="small", device="auto", compute_type="auto",
                 beam_size=5, cpu_threads=0, num_workers=1) -> None:
        self.model_size = model_size
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads

    @staticmethod
    def cuda_available() -> bool:
        return False

    def load(self, on_progress=None) -> None:
        return

    def unload(self) -> None:
        return

    def transcribe(self, audio_path, on_progress=None, checkpoint=None):
        from app.asr.engine import TranscriptResult

        name = Path(audio_path).stem
        with LOCK:
            CALLS[name] = CALLS.get(name, 0) + 1
            call = CALLS[name]
        steps = 40
        for s in range(1, steps + 1):
            if checkpoint is not None and checkpoint():
                with LOCK:
                    LOG.append((f"{name}#{call}", -s))   # 负数表示被中止
                return TranscriptResult(
                    text=f"{name} 部分结果", segments=[], language="zh",
                    duration=1.0, engine="fake", model="fake")
            if on_progress:
                on_progress(s / steps)
            time.sleep(0.02)
            with LOCK:
                LOG.append((f"{name}#{call}", s))
        return TranscriptResult(
            text=f"{name} 完整结果", segments=[{"start": 0, "end": 1, "text": "ok"}],
            language="zh", duration=1.0, engine="fake", model="fake")


def make_items(names: list[str]) -> list[Item]:
    out = []
    for n in names:
        f = TMP / f"{n}.mp4"
        f.write_bytes(b"x" * 2048)
        out.append(Item(platform="local", item_id=f"id_{n}", title=n,
                        local_path=str(f), url=str(f), duration=10))
    return out


def last(name: str, call: int = 1) -> int:
    with LOCK:
        vals = [s for (n, s) in LOG if n == f"{name}#{call}"]
    return vals[-1] if vals else 0


def wait_for(pred, timeout: float = 15.0, what: str = "") -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    print(f"  ⚠️ 等待超时：{what}")
    return False


def fresh_db(name: str) -> Path:
    p = TMP / f"{name}.db"
    if p.exists():
        p.unlink()
    return p


def run_case(items: list[Item], task: Task, driver, title: str) -> bool:
    LOG.clear()
    CALLS.clear()
    ctl = ProcessControl()
    pipe = pl.Pipeline(fresh_db(title), TMP / "media")
    orig = pl.FasterWhisperEngine
    pl.FasterWhisperEngine = FakeEngine  # type: ignore[misc,assignment]
    ok = False
    try:
        th = threading.Thread(
            target=lambda: pipe.process(items, task, control=ctl), daemon=True)
        th.start()
        ok = driver(ctl, items)
        ctl.stop()
        th.join(timeout=30)
        if th.is_alive():
            print(f"  ✗ {title}：处理线程未能结束")
            return False
    finally:
        pl.FasterWhisperEngine = orig  # type: ignore[misc]
    return ok


def test_pause_resume_skip() -> bool:
    print("\n[用例1] 暂停 / 继续 / 跳过当前")
    items = make_items(["A", "B"])
    # 本用例断言的是「串行」处理顺序（A 停住 → 跳过 → 再跑 B），
    # 因此显式固定 serial 模式，避免受默认 overlap/并发影响。
    task = Task(name="t1", mode=MODE_CREATOR, platform="local",
                output_dir=str(TMP / "out"), pipeline_mode="serial",
                concurrency=1)

    def driver(ctl: ProcessControl, items: list[Item]) -> bool:
        if not wait_for(lambda: last("A") >= 3, what="A 开始转录"):
            return False
        ctl.pause()
        time.sleep(0.25)
        at_pause = last("A")
        time.sleep(0.4)
        if last("A") != at_pause:
            print(f"  ✗ 暂停没生效：进度从 {at_pause} 走到了 {last('A')}")
            return False
        print(f"  ✓ 暂停生效，进度停在 {at_pause} 步")

        ctl.resume()
        if not wait_for(lambda: last("A") > at_pause, what="继续后进度恢复"):
            return False
        print(f"  ✓ 继续生效，进度恢复到 {last('A')} 步（未从 0 重来）")

        ctl.skip_current()
        if not wait_for(lambda: items[0].status == "skipped", what="A 被跳过"):
            return False
        print("  ✓ 跳过生效，A 状态 =", items[0].status)
        if not wait_for(lambda: last("B") >= 40, timeout=40.0, what="B 转录完成"):
            return False
        print(f"  ✓ 已自动推进到 B 并跑完（{last('B')} 步）")
        # 乐观日志打点（第 40 步）早于流水线落库 STATUS_DONE，故再等一下状态
        if not wait_for(lambda: items[1].status == "done",
                        what="B 状态落为 done"):
            return False
        return True

    ok = run_case(items, task, driver, "t_pause_skip")
    print("  结果：", "通过" if ok else "失败")
    return ok


def test_back_and_jump() -> bool:
    print("\n[用例2] 重做上一条 / 跳转到指定条目 / 停止")
    items = make_items(["A", "B", "C"])

    task = Task(name="t2", mode=MODE_CREATOR, platform="local",
                output_dir=str(TMP / "out"), pipeline_mode="serial",
                concurrency=1)

    def driver(ctl: ProcessControl, items: list[Item]) -> bool:
        # 等 B 跑到一半，点「后退」→ 应该回到 A 重做（A 出现第 2 次调用）
        if not wait_for(lambda: last("B") >= 5, what="B 开始转录"):
            return False
        ctl.back()
        if not wait_for(lambda: last("A", 2) >= 3, what="A 被重做"):
            return False
        print(f"  ✓ 后退生效，A 开始第 2 次处理（进度 {last('A', 2)} 步）")

        # 等 A 第 2 次跑完进入 B，再点「跳转」到第 3 条
        if not wait_for(lambda: last("B", 2) >= 3, what="B 第 2 次开始"):
            return False
        ctl.jump_to(2)
        if not wait_for(lambda: last("C") >= 3, what="跳到 C"):
            return False
        print(f"  ✓ 跳转生效，已直接处理第 3 条 C（进度 {last('C')} 步）")

        ctl.stop()
        return True

    ok = run_case(items, task, driver, "t_back_jump")
    print("  结果：", "通过" if ok else "失败")
    return ok


def main() -> int:
    results = [test_pause_resume_skip(), test_back_and_jump()]
    print()
    print("=" * 56)
    if all(results):
        print("✅ 运行控制全部通过（暂停/继续/跳过/后退/跳转/停止）")
        return 0
    print("❌ 存在失败用例")
    return 1


if __name__ == "__main__":
    sys.exit(main())
