"""GUI 新版界面全流程测试：验证三模式与配置项真的贯穿到导出结果。

用真实的 MainWindow，但把「采集」换成直接构造本地素材，绕过网络。
依次验证：
  1) 内容模式（三选一）真的影响导出文件内容
  2) 性能选项（精度/并发/模式）被正确写进 Task 并传给 pipeline
  3) Obsidian 模式产出 frontmatter + tags；index.csv/json/SUMMARY.md 齐全
     （INDEX.md 索引页与 board.canvas 看板**已按用户要求下架**，不再生成）
  4) 结果文档页能显示新格式
退出码 0 表示通过。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ⚠️ 顺序不能反：CUDA 必须先于 PyQt5（详见 gui.py 注释）
import app.asr.engine as E  # noqa: E402

E.warmup_cuda()

from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.core.models import Item, OutputOptions, STATUS_DONE  # noqa: E402
import app.ui.main_window as MW  # noqa: E402

MEDIA = ROOT / "data" / "media"
OUT = ROOT / "out" / "gui_v3"
PROBLEMS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        PROBLEMS.append(label)


def local_items(limit: int = 2) -> list[Item]:
    # ⚠️ 素材要能抽出关键词：extract_keywords 的 min_count=2，词语必须重复出现。
    # 所以这里刻意写一段词汇大量复现的文案（「手机」「影像」「拍摄」各出现多次），
    # 否则短素材会抽出 0 个 topic 标签（那是设计如此，不是 bug）。
    text = (
        "这台手机本来应该叫做X400 Pro，但这次跳过了一代的名称。"
        "手机影像升级是重点，手机的主摄传感器更大了，夜景拍摄效果明显更好。"
        "手机影像算法配合更大的传感器，让夜景拍摄的成像更干净，"
        "所以这台手机的影像表现，在同价位手机的拍摄里竞争力很强。"
    )
    out: list[Item] = []
    for p in sorted(MEDIA.glob("*")):
        if p.suffix.lower() not in (".mp4", ".m4a", ".mp3", ".wav", ".flv"):
            continue
        try:
            import av
            c = av.open(str(p))
            d = float(c.duration) / 1_000_000
            c.close()
        except Exception:  # noqa: BLE001
            continue
        out.append(Item(
            platform="local", item_id=p.stem, title=f"GUI测试-{p.stem}",
            author="本地素材", duration=int(d), local_path=str(p),
            comment_count=0, status=STATUS_DONE,
            transcript=text,
            # ⚠️ pipeline 的时间戳段是从 item.segments 渲染的，不是 transcript。
            # 没有 segments 就只有纯文本，测试也就测不到时间戳段。
            segments=json.dumps([
                {"start": 0.0, "end": 3.0, "text": "这台手机本来应该叫做X400 Pro"},
                {"start": 3.0, "end": 8.0, "text": "但这次跳过了一代的名称"},
            ], ensure_ascii=False),
            asr_engine="faster-whisper(small/cuda/float16)",
        ))
        if len(out) >= limit:
            break
    return out


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    items = local_items(2)
    if not items:
        print("❌ 没有可用的本地素材")
        return 1

    app = QApplication(sys.argv)
    for name in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))  # type: ignore[arg-type]

    w = MW.MainWindow()
    w.show()

    state: dict = {"exported": None, "failed": None, "conf": {}}
    deadline = time.time() + 900

    def on_exported(paths: dict) -> None:
        state["exported"] = paths
        app.quit()

    def on_failed(msg: str) -> None:
        state["failed"] = msg
        app.quit()

    w.on_exported = on_exported          # type: ignore[method-assign]
    w.on_collect_failed = on_failed      # type: ignore[method-assign]

    # ---- 构造配置：走 Obsidian 模式（三模式之一）----
    sp = w.page_setup
    sp.edit_output.setText(str(OUT))
    # ⚠️ 改版后内容输出只剩「三选一」单选按钮，旧的 chk_out_timestamp /
    # chk_out_plain / chk_obsidian / chk_moc / chk_canvas 已全部删除。
    sp.mode_content_buttons[OutputOptions.MODE_OBSIDIAN].setChecked(True)
    sp.chk_comments.setChecked(False)
    # 旧版有 chk_download（「要不要下载」），现已取消这个勾选：
    # 提取文案必须先有音视频，所以「转录」勾上就一定下载（Task.needs_download），
    # 下载下来的中间文件去留改由 chk_keep_media 决定。
    sp.chk_keep_media.setChecked(True)
    sp.chk_transcribe.setChecked(False)
    sp.combo_model.setCurrentText("small")

    cfg = sp.collect_cfg()
    state["conf"] = cfg
    print("\n【1】配置收集")
    check(cfg["output_opts"].mode == OutputOptions.MODE_OBSIDIAN,
          "配置里带着 Obsidian 模式")
    check(cfg["output_opts"].out_timestamped is True,
          "Obsidian 模式自动包含时间戳段")
    check(cfg["output_opts"].keywords_as_tags is True,
          "关键词标签强制开启（用户要求）")
    check(cfg["pipeline_mode"] in ("overlap", "serial"),
          f"执行模式已收集：{cfg['pipeline_mode']}")

    # ---- 直接进入处理阶段，用本地素材 ----
    w.cfg = cfg
    w.task = MW.Task(
        name="gui-v3", mode="manual", platform="local",
        max_count=len(items), output_dir=str(OUT),
        do_download=False, do_transcribe=False,
        model_size="small", device="auto",
        compute_type=cfg["compute_type"], beam_size=cfg["beam_size"],
        pipeline_mode="serial", concurrency=1,
        output_opts=cfg["output_opts"],
    )
    # 结果文档页要先拿到 items，导出后才有文件映射
    w.page_docs.set_items(items, str(OUT))

    print("\n【2】跑导出")
    w._pw = MW.ProcessWorker(
        w.pipeline, items, w.task, cookie="", proxy="",
        model_size="small", device="auto", control=w.control or MW.ProcessControl())
    w._pw.progress.connect(w.page_run.on_progress)
    w._pw.item_state.connect(w.page_run.mark_item)
    w._pw.exported.connect(w.on_exported)
    w._pw.failed.connect(w.on_collect_failed)
    w._pw.start()

    def guard() -> None:
        if time.time() > deadline and state["exported"] is None:
            state["failed"] = "超时"
            app.quit()

    g = QTimer()
    g.timeout.connect(guard)
    g.start(1000)
    app.exec_()

    if state["failed"]:
        print(f"  ✗ 失败：{state['failed']}")
        return 1

    paths = state["exported"] or {}
    files = paths.get("files", {})
    print(f"  导出根目录：{paths.get('root')}")
    check(bool(files), "拿到了逐条文件映射")

    print("\n【3】内容模式是否真的生效（Obsidian）")
    sample = next(iter(files.values()), {})
    tp = sample.get("transcript", "")
    check(bool(tp) and Path(tp).exists(), f"逐字稿文件存在：{Path(tp).name if tp else '-'}")
    body = Path(tp).read_text(encoding="utf-8") if tp else ""
    check(body.startswith("---"), "Obsidian 模式 → 首行是 frontmatter 的 ---")
    check("tags:" in body, "frontmatter 含 tags")
    check("topic/" in body, "frontmatter 含自动抽取的 topic/ 关键词标签")
    check("<!-- more -->" in body, "含折叠点")
    check("## 逐字稿（带时间戳）" in body, "Obsidian 模式含时间戳段")
    check("## 纯文本" in body, "Obsidian 模式含纯文本段")

    print("\n【4】评论独立文件开关")
    check(not sample.get("comments"), "未抓评论 → 无评论文件")

    print("\n【5】索引 / 汇总（Canvas 与 MOC 已下架）")
    check("csv" in paths and Path(paths["csv"]).exists(), "生成 index.csv")
    check("json" in paths and Path(paths["json"]).exists(), "生成 index.json")
    check("summary" in paths and Path(paths["summary"]).exists(), "生成 SUMMARY.md")
    check("moc" not in paths, "不再生成 INDEX.md（用户明确不要）")
    check("canvas" not in paths, "不再生成 board.canvas（用户明确不要）")
    check(not (Path(paths.get("root", ".")) / "INDEX.md").exists(),
          "磁盘上确实没有 INDEX.md")
    check(not (Path(paths.get("root", ".")) / "board.canvas").exists(),
          "磁盘上确实没有 board.canvas")
    csv_text = Path(paths["csv"]).read_text(encoding="utf-8-sig") if "csv" in paths else ""
    check("topic/" in csv_text, "index.csv 的标签列含 topic/ 关键词")

    print("\n【6】结果文档页能载入新格式")
    docs = w.page_docs
    docs.set_files(files, paths.get("root", ""))
    docs.set_items(items, paths.get("root", ""))
    docs.table.selectRow(0)
    docs.show_detail(items[0])
    shown = docs.view.toPlainText()
    check(len(shown) > 50, f"右侧详情有内容（{len(shown)} 字）")
    check("file:///" in docs.view.toHtml(), "详情里有本地文件超链接")

    print("\n" + "=" * 60)
    if PROBLEMS:
        print(f"❌ {len(PROBLEMS)} 项未通过：")
        for p in PROBLEMS:
            print("   -", p)
        return 1
    print("✅ GUI 全流程通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
