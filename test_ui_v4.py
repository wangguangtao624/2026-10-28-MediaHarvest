"""本轮改版的专项测试：中间文件语义 + 目录分离 + 选项说明。

覆盖用户这一轮提的四个点：
1. 「不勾下载就转出 0 个字」——现在只要转录就必须下载（needs_download）；
2. 转录完可以选保留还是删掉中间文件（keep_media）；
3. 文稿目录与音视频目录分离，且都能在界面上改；
4. 模型 / 设备 / 精度每个选项都有说明文案，且会随选项切换更新。

跑法：
    env -u PYTHONPATH -u PYTHONHOME <venv>/Scripts/python.exe test_ui_v4.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "test_ui_v4"

_fail = 0


def check(ok: bool, label: str) -> None:
    global _fail
    print(f"  {'✓' if ok else '✗'} {label}")
    if not ok:
        _fail += 1


# ---------------------------------------------------------------- 1. 语义
def test_needs_download() -> None:
    print("\n【1】「要转录就必须先下载」的语义")
    from app.core.models import Item, STATUS_DONE, Task

    task = Task(name="t", mode="creator", platform="bilibili",
                do_download=False, do_transcribe=True)

    # 有直连地址 → 必须下
    it = Item(platform="bilibili", item_id="1", url="https://x/1",
              stream_url="https://x/1.mp4")
    check(task.needs_download(it) is True,
          "有 stream_url 时 needs_download=True（哪怕 do_download=False）")

    # 有页面链接 → 也要下
    it2 = Item(platform="bilibili", item_id="2", url="https://x/2")
    check(task.needs_download(it2) is True, "有页面链接时也要下载")

    # 本地已有文件 → 复用，不重复下
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "a.mp4"
        f.write_bytes(b"x" * 16)
        it3 = Item(platform="local", item_id="3", url="x", local_path=str(f))
        check(task.needs_download(it3) is False,
              "本地已有文件时不再下载（复用旧的）")

    # 图文作品 → 没人声轨，不下
    it4 = Item(platform="douyin", item_id="4", url="https://x/4",
               media_kind="image")
    check(task.needs_download(it4) is False, "图文作品不下载（无法转录）")
    _ = STATUS_DONE


# ---------------------------------------------------------------- 2. keep_media
def test_keep_media() -> None:
    print("\n【2】转录后中间文件的去留（keep_media）")
    from app.core.models import Item, Task
    from app.pipeline import Pipeline

    OUT.mkdir(parents=True, exist_ok=True)
    media_dir = OUT / "media_del"
    if media_dir.exists():
        for p in media_dir.iterdir():
            p.unlink()
    media_dir.mkdir(parents=True, exist_ok=True)

    pl = Pipeline(OUT / "t.db", media_dir)

    # --- 保留：文件必须还在 ---
    f_keep = media_dir / "keep_me.mp4"
    f_keep.write_bytes(b"x" * 1024)
    it = Item(platform="bilibili", item_id="k1", local_path=str(f_keep))
    task_keep = Task(name="k", mode="creator", platform="bilibili",
                     keep_media=True)
    pl._maybe_drop_media(it, task_keep, 1, 1)
    check(f_keep.exists(), "keep_media=True → 文件保留")

    # --- 删除：文件应被清掉，且 local_path 归零 ---
    f_del = media_dir / "del_me.mp4"
    f_del.write_bytes(b"x" * 2048)
    it2 = Item(platform="bilibili", item_id="k2", local_path=str(f_del))
    task_del = Task(name="k", mode="creator", platform="bilibili",
                    keep_media=False)
    pl._maybe_drop_media(it2, task_del, 1, 1)
    check(not f_del.exists(), "keep_media=False → 文件已删除")
    check(it2.local_path == "", "删除后 local_path 已清空（数据库也同步）")

    # --- 关键安全约束：绝不能删 media_dir 之外的文件 ---
    with tempfile.TemporaryDirectory() as outside:
        guard = Path(outside) / "user_own_video.mp4"
        guard.write_bytes(b"x" * 4096)
        it3 = Item(platform="local", item_id="k3", local_path=str(guard))
        pl._maybe_drop_media(it3, task_del, 1, 1)
        check(guard.exists(),
              "media_dir 之外的文件（用户自己的素材）绝不被删")

    # --- 删除失败不能炸任务 ---
    it4 = Item(platform="bilibili", item_id="k4",
               local_path=str(media_dir / "not_exists.mp4"))
    try:
        pl._maybe_drop_media(it4, task_del, 1, 1)
        check(True, "文件不存在时静默跳过，不抛异常")
    except Exception as e:  # noqa: BLE001
        check(False, f"删除不存在的文件时抛了异常：{e}")


# ---------------------------------------------------------------- 3. 目录分离
def test_dirs() -> None:
    print("\n【3】文稿目录与音视频目录分离")
    from app.core.models import Task
    from app.pipeline import Pipeline

    doc = OUT / "docs_only"
    media = OUT / "media_only"
    doc.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)

    pl = Pipeline(OUT / "t.db", OUT / "unused_media")
    check(pl.media_dir == OUT / "unused_media", "初始 media_dir 就是传入值")

    task = Task(name="d", mode="creator", platform="bilibili",
                output_dir=str(doc), media_dir=str(media))
    pl._make_downloader(task, "", "")
    check(pl.media_dir == media, "任务带 media_dir 时，pipeline 会切到它")

    # 产出目录与中间目录是不同的两个路径
    check(str(doc) != str(media), "两个目录互相独立，不会混在一起")

    # 切回默认值也不报错
    pl.set_media_dir("")
    check(pl.media_dir == media, "set_media_dir('') 时不改动（防误清空）")


# ---------------------------------------------------------------- 4. 说明文案
def test_hints() -> None:
    print("\n【4】模型 / 设备 / 精度的说明文案")
    from app.ui import hints

    # 每个模型都必须有完整的 what/how/cost
    for key in hints.MODEL_ORDER:
        info = hints.MODEL_INFO.get(key)
        ok = bool(info and info.get("what") and info.get("how")
                  and info.get("cost") and info.get("label"))
        check(ok, f"模型 {key} 有 label/what/how/cost")
        tip = hints.model_tip(key)
        check(len(tip) > 40 and "什么时候用" in tip,
              f"模型 {key} 的 tooltip 含「什么时候用」")
        detail = hints.model_detail(key)
        check("<b>" in detail and len(detail) > 80,
              f"模型 {key} 的说明卡有富文本正文")

    for key in ("auto", "cuda", "cpu"):
        info = hints.DEVICE_INFO.get(key)
        check(bool(info and info.get("what") and info.get("how")
                   and info.get("cost")),
              f"设备 {key} 有 what/how/cost")
        check("什么时候用" in hints.device_tip(key),
              f"设备 {key} 的 tooltip 含「什么时候用」")

    for key in ("auto", "float16", "int8_float16", "int8", "float32"):
        check(key in hints.COMPUTE_INFO, f"精度 {key} 有说明")
        check("代价" in hints.compute_tip(key), f"精度 {key} 讲了代价")

    # 实测数字必须写进去（用户才信「本机该选哪个」）
    check("76" in hints.COMPUTE_INFO["float16"]["what"]
          or "76" in hints.COMPUTE_INFO["int8"]["cost"],
          "精度说明里带了本机实测数字（float16 76s / int8 115s）")
    check("344" in hints.DEVICE_INFO["cpu"]["cost"],
          "CPU 设备说明里带了实测对比（344 秒）")
    check("500" in hints.MISC_TIPS["cpu_threads"]
          or "62.9" in hints.MISC_TIPS["cpu_threads"],
          "CPU 线程说明里带了实测数字")

    rec = hints.recommend_model()
    check(rec in hints.MODEL_ORDER, f"按显存推荐的默认模型合法：{rec}")


# ---------------------------------------------------------------- 5. 界面接线
def test_ui_wiring() -> None:
    print("\n【5】界面接线（无头构建主窗口）")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from app.ui import MainWindow

    w = MainWindow()

    # 「文件」页已按用户要求从侧边栏下线，但页面对象仍留在 pages 注册表里
    # （内部做知识库归档/目录同步用），只是不再出现在导航栏。
    check(set(w.pages) == {"setup", "preview", "run", "docs", "files",
                           "history"},
          "六个页面对象都在（含下线的文件页）")
    check(len(w.nav_buttons) == 5, "侧边栏只有 5 个导航项（文件页已下线）")
    check("files" not in w.nav_buttons, "「文件」不在导航里")

    # 逐页切换不应该报错
    ok_nav = True
    for k in w.pages:
        try:
            w.goto_key(k)
        except Exception as e:  # noqa: BLE001
            ok_nav = False
            print(f"    切换 {k} 失败：{e}")
    check(ok_nav, "六个页面都能正常切换")

    # 主题切换
    before = w.theme
    w.toggle_theme()
    check(w.theme != before, f"主题可切换：{before} → {w.theme}")
    w.toggle_theme()
    check(w.theme == before, "再切一次能切回来")

    sp = w.page_setup
    cfg = sp.collect_cfg()
    check("keep_media" in cfg, "配置里有 keep_media（保留音视频）")
    check("media_dir" in cfg, "配置里有 media_dir（音视频中间目录）")
    check("output_dir" in cfg, "配置里有 output_dir（Markdown 目录）")
    check(cfg["output_dir"] != cfg["media_dir"],
          "两个目录在配置里是分开的两项")
    check(cfg["do_transcribe"] is True and cfg["do_download"] is True,
          "勾了转录时 do_download 也是 True（保证有音视频可转）")

    # 说明按钮的文案会随下拉框切换而更新
    sp.combo_model.setCurrentIndex(0)   # tiny
    t1 = sp.combo_model.toolTip()
    sp.combo_model.setCurrentIndex(4)   # large-v3
    t2 = sp.combo_model.toolTip()
    check(t1 != t2, "换模型时说明文案跟着变")
    check("large-v3" in t2 or "最准" in t2, "large-v3 的说明正确")

    sp.combo_device.setCurrentIndex(2)  # cpu
    check("CPU" in sp.combo_device.toolTip()
          or "处理器" in sp.combo_device.toolTip(),
          "设备的说明随选项更新")

    # 历史页能拉到库里的概览
    w.goto_key("history")
    st = w.page_history.stat_labels
    # 2026-09 改版：历史页从 5 张卡扩到 8 张（新增 中间文件 / 失败 / 任务 计数）
    check(len(st) == 8, "历史页有 8 个概览卡")
    check(all(lb.text() for lb in st.values()), "概览卡都有数字")

    # 文件页能列出条目
    w.goto_key("files")
    check(hasattr(w.page_files, "entries"), "文件页有文件列表数据")
    check(w.page_files.tab_buttons["doc"].isChecked(),
          "文件页默认停在「Markdown 文稿」")

    # 输出目录可改，且三处同步
    w.set_output_dir(str(OUT / "sync_doc"))
    check(sp.edit_output.text() == str(OUT / "sync_doc"),
          "改目录后配置页输入框同步")
    check(w.page_docs.out_dir == str(OUT / "sync_doc"),
          "改目录后结果页同步")

    w.set_media_dir(str(OUT / "sync_media"))
    check(str(w.pipeline.media_dir) == str(OUT / "sync_media"),
          "改中间目录后 pipeline 同步")
    _ = time, app


# ---------------------------------------------------------------- 主流程
def main() -> int:
    print("=" * 62)
    print("本轮改版专项测试：中间文件语义 / 目录分离 / 选项说明")
    print("=" * 62)

    test_needs_download()
    test_keep_media()
    test_dirs()
    test_hints()
    test_ui_wiring()

    print("\n" + "=" * 62)
    if _fail:
        print(f"❌ 有 {_fail} 项没通过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
