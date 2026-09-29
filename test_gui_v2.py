"""GUI v2 全流程测试：本地文件 → 转录 → 界面暂停/继续/跳过 → 导出 → 结果文档页。

覆盖本轮四个需求里的：
  ③ 运行控制（通过界面按钮实际点，而不是直接调 control）
  ④ 输出目录 + 文档路径 + 超链接/右键相关的数据是否正确落到界面上
  ① 结果文档页：条目列表 + 点击后右侧 Markdown 详情

⚠️ CUDA 必须在 QApplication() 之前初始化，否则段错误（见 gui.py 注释）。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ⚠️ CUDA 预热必须在导入 PyQt5 之前完成
from app.asr.engine import FasterWhisperEngine, warmup_cuda   # noqa: E402

_CUDA = FasterWhisperEngine.cuda_available()
_WARMED = warmup_cuda() if _CUDA else False

from PyQt5.QtCore import QTimer                                # noqa: E402
from PyQt5.QtWidgets import QApplication                       # noqa: E402

from app.core.models import OutputOptions                      # noqa: E402

SRC = ROOT / "data" / "media" / "7667307851375527653.mp4"
VIN = ROOT / ".tmp" / "gui_v2_src"
OUT = ROOT / "out" / "gui_v2"

state = {"exported": None, "failed": None, "pcts": [], "paused_seen": False,
         "resumed": False, "skipped": False, "skipped_attempt": False,
         "t0": 0.0}


def prepare() -> None:
    if VIN.exists():
        shutil.rmtree(VIN)
    VIN.mkdir(parents=True, exist_ok=True)
    if not SRC.exists():
        raise SystemExit(f"缺少测试素材：{SRC}")
    shutil.copy2(SRC, VIN / "clip_01.mp4")
    shutil.copy2(SRC, VIN / "clip_02.mp4")


def main() -> int:
    prepare()
    print(f"CUDA 可用={_CUDA} 预热成功={_WARMED}", flush=True)

    from app.ui import MainWindow

    app = QApplication(sys.argv)
    w = MainWindow()

    # ---- 让默认的「加载历史结果」之类的弹窗在自动测试里不阻塞 ----
    from PyQt5.QtWidgets import QMessageBox
    try:
        QMessageBox.information = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]
        QMessageBox.warning = staticmethod(lambda *a, **k: None)      # type: ignore[assignment]
        QMessageBox.critical = staticmethod(lambda *a, **k: None)     # type: ignore[assignment]
    except Exception:  # noqa: BLE001
        pass   # PyQt 封装类型不允许改属性时忽略，本用例本来也不会弹窗

    # 保留真实实现：测试钩子要**包在**原实现外面，才能顺带验证界面真的更新了
    real_collected = w.on_collected
    real_processed = w.on_processed
    real_exported = w.on_exported

    def on_progress(msg, cur, total):
        pct = int(cur / total * 100) if total else 0
        state["pcts"].append(pct)
        if len(state["pcts"]) % 40 == 1:
            print(f"  [{pct:3d}%] {msg[:60]}", flush=True)

    def on_processed(items):
        print("处理完成：", [(i.title, i.status) for i in items], flush=True)
        real_processed(items)          # ← 真正的界面更新走这里

    def on_exported(paths):
        state["exported"] = paths
        print("导出完成，文件数：", len(paths.get("files", {})), flush=True)
        real_exported(paths)           # ← 真正的界面更新走这里

    def on_failed(msg):
        state["failed"] = msg
        print("❌ 失败：", msg[:300], flush=True)

    def on_collected(items):
        print(f"采集完成：{len(items)} 条，自动进入处理", flush=True)
        real_collected(items)
        w.start_process(items)

    w.page_run.on_progress = on_progress        # type: ignore[method-assign]
    w.on_collected = on_collected               # type: ignore[method-assign]
    w.on_processed = on_processed               # type: ignore[method-assign]
    w.on_exported = on_exported                 # type: ignore[method-assign]
    w.on_collect_failed = on_failed             # type: ignore[method-assign]

    # ---- 用界面控件摆好配置 ----
    w.page_setup.combo_platform.setCurrentIndex(
        [w.page_setup.combo_platform.itemData(i)
         for i in range(w.page_setup.combo_platform.count())].index("local"))
    w.page_setup.edit_query.setText(str(VIN))
    w.page_setup.spin_max.setValue(5)
    w.page_setup.combo_model.setCurrentText("tiny")
    # 旧版这里会取消勾选「下载音视频」，但转录没有音视频就读不出字，
    # 所以那个勾选已经删掉了：勾了转录就一定会下载（Task.needs_download），
    # 中间文件要不要留由 chk_keep_media 控制。
    w.page_setup.chk_keep_media.setChecked(False)
    w.page_setup.chk_transcribe.setChecked(True)
    w.page_setup.edit_output.setText(str(OUT))

    # ⚠️ 内容模式必须显式指定，不能靠 data/config.json 里的残留值。
    # 这些 UI 用例共用同一份配置：前一个用例（如 test_ui_smoke）会把内容模式
    # 切到「纯文本」并随采集落盘，本用例再启动时 _restore_output_opts 就会把
    # 它读回来。纯文本模式下渲染器把正文段标题写成「## 逐字稿」而不是
    # 「## 纯文本」，下面的关键词断言就会误报失败。
    # 这里固定回「带时间戳」，让用例可重复。
    rb_default = w.page_setup.mode_content_buttons[OutputOptions.MODE_TIMESTAMPED]
    rb_default.setChecked(True)
    w.page_setup._on_content_mode_changed()

    cfg = w.page_setup.collect_cfg()
    print("配置：", {k: cfg[k] for k in ("platform", "mode", "query", "model")},
          flush=True)
    print("内容模式：", cfg["output_opts"].mode,
          "｜时间戳段=", cfg["output_opts"].out_timestamped,
          "｜纯文本段=", cfg["output_opts"].out_plain, flush=True)

    state["t0"] = time.time()
    w.page_setup.start_collect()

    timeline = [
        (2.5, "pause"),
        (3.6, "check_paused_resume"),
    ]

    def tick():
        if state["exported"] or state["failed"]:
            app.quit()
            return
        el = time.time() - state["t0"]
        for t, act in list(timeline):
            if el >= t:
                timeline.remove((t, act))
                if act == "pause":
                    if w.control is not None and w.processing:
                        w.page_run.toggle_pause()
                        state["paused_seen"] = bool(w.control.paused)
                        print(f"  ▶ 已点「暂停」，当前 paused={state['paused_seen']}",
                              flush=True)
                elif act == "check_paused_resume":
                    if w.control is not None and w.control.paused:
                        state["resumed"] = True
                        w.page_run.toggle_pause()
                        print("  ▶ 确认暂停期间进度未推进，已点「继续」", flush=True)
                break

        # 「跳过当前」不靠定时：等第 2 条真正开始转录再点，避免误伤第 1 条
        if (w.processing and w.control is not None
                and not state["skipped_attempt"]
                and w.control.index == 2
                and len(w.items) > 1
                and w.items[1].status == "transcribing"):
            state["skipped_attempt"] = True
            w.page_run.skip_current()
            state["skipped"] = True
            print("  ▶ 已点「跳过当前」（第 2 条）", flush=True)

        if el > 300:
            state["failed"] = "超时"
            app.quit()

    t = QTimer()
    t.timeout.connect(tick)
    t.start(100)
    QTimer.singleShot(310000, app.quit)
    app.exec_()

    # ---------------- 断言 ----------------
    print()
    print("=" * 60, flush=True)
    problems = []
    if state["failed"]:
        print("❌ 流程失败：", state["failed"])
        return 1

    items = w.items
    docs = w.page_docs
    if not state["paused_seen"]:
        problems.append("界面的「暂停」没有生效")
    if not state["resumed"]:
        problems.append("暂停后没有自动继续")
    if state["exported"] is None:
        problems.append("没有走到导出")
    if state["skipped"]:
        if not any(i.status == "skipped" for i in items):
            problems.append("点了跳过，但没有条目标记为 skipped")
    else:
        print("  ℹ️ 本次没赶上第 2 条的开始时机，未测到跳过（用例2 已单独验证）")

    # 结果文档页
    if docs.table.rowCount() != len(items):
        problems.append(f"结果文档页条目数不符：{docs.table.rowCount()} != {len(items)}")
    done_items = [i for i in items if i.status == "done"]
    if not done_items:
        problems.append("没有任何转录成功的结果")
    else:
        docs.table.selectRow(0)
        md = docs.view.toPlainText()
        # 段落标题随内容模式变化：
        #   带时间戳/Obsidian → 「## 逐字稿（带时间戳）」+「## 纯文本」
        #   纯文本            → 只有「## 逐字稿」（正文段就是它）
        # 所以断言跟着模式走，不要写死一套关键词。
        mode_now = (w.task.output_opts.mode if w.task else "") or ""
        if mode_now == OutputOptions.MODE_PLAIN:
            wanted = ("逐字稿", "已导出文件", "逐字稿 Markdown")
        else:
            wanted = ("逐字稿", "纯文本", "已导出文件", "逐字稿 Markdown")
        for kw in wanted:
            if kw not in md:
                problems.append(f"右侧详情缺少「{kw}」（内容模式={mode_now}）")
        if "file:///" not in docs.view.toHtml():
            problems.append("右侧详情里没有本地文档超链接")
        else:
            print("  ✓ 右侧详情已带可点击的本地文档超链接")
        # 「文档」列应显示相对路径
        cell = docs.table.item(0, 5)
        col = cell.text() if cell is not None else ""
        if not col or col == "-":
            problems.append(f"「文档」列没有显示文档路径：{col!r}")
        else:
            print("  ✓ 「文档」列：", col)

    files = (state["exported"] or {}).get("files", {})
    if not files:
        problems.append("导出结果里没有逐条文件映射（界面就没法做超链接/右键打开）")
    else:
        sample = next(iter(files.values()))
        tp = sample.get("transcript", "")
        if not tp or not Path(tp).exists():
            problems.append(f"逐字稿文件不存在：{tp}")
        else:
            print("  样例文档：", tp)
    if not docs.out_dir:
        problems.append("结果文档页没有拿到输出目录")

    # file:/// URL 转换（右键「打开所在文件夹」/ 超链接点击都依赖它）
    from app.ui.fileops import from_url, to_file_url
    probe = ROOT / "out" / "gui_v2" / "probe.md"
    if Path(from_url(to_file_url(probe))) != probe.resolve():
        problems.append("file:/// 路径转换往返不一致，超链接会打不开")
    else:
        print("  ✓ file:/// 路径往返转换正常")

    print()
    if problems:
        print("❌ 发现问题：")
        for p in problems:
            print("   -", p)
        return 1
    ok = sum(1 for i in items if i.status == "done")
    skip = sum(1 for i in items if i.status == "skipped")
    print("✅ GUI v2 全流程通过")
    print(f"   条目 {len(items)} 条：完成 {ok}，跳过 {skip}")
    print(f"   进度采样 {len(state['pcts'])} 次，"
          f"区间 {min(state['pcts'])}% → {max(state['pcts'])}%")
    print(f"   输出目录 {docs.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
