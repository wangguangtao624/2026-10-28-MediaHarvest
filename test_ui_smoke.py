"""离屏烟测：按 gui.py 的 CUDA-先于-PyQt5 顺序构造主窗口，检查关键接线。

跑法（必须用 venv 的 python，并清掉 PYTHONPATH/PYTHONHOME）：
    cd mediaharvest
    env -u PYTHONPATH -u PYTHONHOME QT_QPA_PLATFORM=offscreen <venv>/python test_ui_smoke.py

⚠️ 为什么这个测试值得单独存在
------------------------------
本项目有个很难查的坑：**PyQt5 槽函数里抛未捕获异常 = 进程静默退出**，
Windows 上既没有 traceback 也没有输出，只有退出码 127 或 139。
本次就踩了一次：构建「生成内容」卡片时先建了单选按钮、后建提示标签，
按钮 setChecked(True) 触发 toggled → 槽里读 self.label_content（还不存在）
→ AttributeError → 进程直接死，查了很久。

所以这个烟测的价值不只是「能不能构造」，而是**在构造路径上把
所有信号槽都踩一遍**，一旦有类似的「槽里抛异常」立刻能被发现。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ---- 顺序铁律：CUDA 先于 PyQt5 ----
# 直接照抄 gui.py 的预热逻辑：光 import ctranslate2 但不真正跑一次，
# 之后 import PyQt5 仍会以退出码 127 静默死掉（无 traceback、无输出）。
try:
    from app.asr.engine import FasterWhisperEngine, warmup_cuda
    if FasterWhisperEngine.cuda_available():
        try:
            if not warmup_cuda():
                FasterWhisperEngine.cuda_disabled = True
        except Exception as exc:  # noqa: BLE001
            print("预热异常:", exc)
            FasterWhisperEngine.cuda_disabled = True
    else:
        FasterWhisperEngine.cuda_disabled = True
except Exception as exc:  # noqa: BLE001
    print("engine 导入失败:", exc)

from PyQt5.QtWidgets import QApplication  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

app = QApplication([])

from app.ui.main_window import MainWindow  # noqa: E402

w = MainWindow()
print("MainWindow 构建成功")

s = w.page_setup
print("内容模式:", s.content_mode())
print("高级区默认展开?", s._adv.is_expanded())
print("高级区摘要:", s._adv.label_summary.text())

cfg = s.collect_cfg()
print("output_opts.mode =", cfg["output_opts"].mode)
print("out_timestamped =", cfg["output_opts"].out_timestamped)
print("out_plain =", cfg["output_opts"].out_plain)
print("keywords_as_tags =", cfg["output_opts"].keywords_as_tags)
print("已移除 spin_comments?", not hasattr(s, "spin_comments"))
print("已移除 chk_out_timestamp?", not hasattr(s, "chk_out_timestamp"))
print("已移除 chk_canvas?", not hasattr(s, "chk_canvas"))
print("有 chk_comments?", hasattr(s, "chk_comments"))

# 切换内容模式 → 配置跟着变
s.mode_content_buttons["plain"].setChecked(True)
print("切到纯文本后 out_timestamped =", s.collect_cfg()["output_opts"].out_timestamped)

p = w.page_preview
print("预览页列:", p.COLS)
print("面板默认隐藏?", not p.panel.isVisible())

# 用假的 items 灌一次，验证条数单元格
from app.core.models import Item  # noqa: E402
fake = [Item(platform="bilibili", item_id="BV1", title="标题A", author="张三",
             duration=125, view_count=1000, comment_count=200),
        Item(platform="bilibili", item_id="BV2", title="标题B", comment_limit=77,
             comment_count=50)]
p.set_items(fake)
print("第 0 行抓取条数格 =", p.table.item(0, 6).text())
print("第 1 行抓取条数格 =", p.table.item(1, 6).text())
p._set_limit(fake[0], 88)
p._refresh_limit_cell(0)
print("设 88 后第 0 行 =", p.table.item(0, 6).text())

# 主题切换
w.toggle_theme()
print("切深色后 theme =", w.theme)
w.toggle_theme()
print("切回后 theme =", w.theme)

# 历史页
w.goto_key("history")
w.page_history.reload()
print("历史页概览卡:", list(w.page_history.stat_labels))
# ---- 新增：配置页的外部工具 / 知识库设置 ----
s = w.page_setup
print("有编辑器路径输入框?", hasattr(s, "edit_editor"))
print("有知识库目录输入框?", hasattr(s, "edit_kb"))
print("有平铺复选框?", hasattr(s, "chk_kb_flat"))
s._detect_editor()
print("编辑器探测提示:", s.label_editor.text()[:60])
s.edit_editor.setText("")
s._commit_editor()          # 落盘空值 = 走自动探测
s._detect_editor()
print("清空后探测提示:", s.label_editor.text()[:60])
print("AUTO-DETECT SMOKE OK")

# ---- 新增：知识库归档模块（写到临时目录，绝不动真实知识库）----
import tempfile  # noqa: E402
from app.export import knowledge  # noqa: E402
tmp = Path(tempfile.mkdtemp(prefix="kb_smoke_"))
src_file = tmp / "逐字稿.md"
src_file.write_text("# 测试\n内容", encoding="utf-8")
res = knowledge.add_files(str(tmp / "kb"), [
    {"path": str(src_file), "item": None, "name": "逐字稿.md"},
])
print("归档结果:", res.summary())
print("KB OK")

print("ALL SMOKE OK")

# ---- 日志渲染与筛选 ----
r = w.page_run
r.clear_log()
r.log_stage("开始处理 3 个作品")
r.log_line("[1] 标题一 → 下载中", "info")
r.log_ok("[1] 标题一 → ✓ 已完成")
r.log_warn("[2] 标题二 → 疑似纯音乐")
r.log_error("[3] 标题三 → 失败：超时")
assert len(r.log_entries) == 5, len(r.log_entries)
full = r.log.toPlainText()
print("日志条数:", len(r.log_entries))
print("日志含时间戳?", ":" in full)

# 只留错误
for i in range(r.combo_log_level.count()):
    if r.combo_log_level.itemData(i) == "error":
        r.combo_log_level.setCurrentIndex(i)
only_err = r.log.toPlainText()
assert "失败：超时" in only_err
assert "已完成" not in only_err, only_err
print("按级别筛选 OK（只剩错误）")

# 恢复全部
for i in range(r.combo_log_level.count()):
    if r.combo_log_level.itemData(i) == "all":
        r.combo_log_level.setCurrentIndex(i)
assert "已完成" in r.log.toPlainText()
print("恢复全部 OK")

# 队列 → 日志
r.set_queue(fake)
r.mark_item(0, "done")
assert any("[1]" in e["text"] and "已完成" in e["text"] for e in r.log_entries)
print("队列状态写入日志 OK")
print("LOG SMOKE OK")

# ---- 历史页四个标签都能渲染 ----
h = w.page_history
h.reload()
print("历史页标签数:", h.stack.count())
assert h.stack.count() == 4
for idx in range(4):
    h.switch_tab(idx)
    h.stack.setCurrentIndex(idx)
print("八个概览卡都有值?", all(lb.text() not in ("", "—")
      for lb in h.stat_labels.values()))
for k, lb in h.stat_labels.items():
    print(f"   {k} = {lb.text()}")
print("转录表列:", h.COLS)

# ---- 新增：平台分布是树、可展开、子节点可点 ----
print("平台分布是树控件?", hasattr(h, "plat_tree"))
print("平台行数:", h.plat_tree.topLevelItemCount())
if h.plat_tree.topLevelItemCount():
    root = h.plat_tree.topLevelItem(0)
    print("平台行文案:", root.text(0))
    print("有占位子节点（懒加载标记）?", root.childCount() == 1
          and root.child(0).text(0) == "加载中…")
    # 触发展开 → 子节点应被填成真实作品（或「还没有」提示）
    root.setExpanded(True)
    print("展开后子节点数:", root.childCount())
    if root.childCount():
        sub = root.child(0)
        print("  第一个子节点:", sub.text(0).strip()[:30],
              "| kind =", (sub.data(0, Qt.UserRole) or {}).get("kind"))

# ---- 新增：一键清中间文件的扫描（只扫不删，安全） ----
files, total = h._scan_media()
print(f"扫描到可清理中间文件 {len(files)} 个，共 {h._mb(total)}")
assert all(f.suffix.lower() not in h.DOC_EXT for f in files), \
    "文稿类文件绝不能进清理列表！"
print("清理列表不含文稿 OK")

# ---- 新增：最近动态行可双击 ----
print("最近动态可点行数:", h.recent_col.count() - 1)

# ---- 本轮新增：转录记录页左右分栏就地预览 ----
print("有左右分栏 tr_split?", hasattr(h, "tr_split"))
assert hasattr(h, "tr_split"), "转录页应有 QSplitter 分栏"
print("分栏左右尺寸:", h.tr_split.sizes())
for name in ("pv", "label_pv_title", "label_pv_meta", "label_pv_path",
             "btn_pv_editor", "btn_pv_folder", "btn_pv_copy", "btn_pv_kb"):
    assert hasattr(h, name), f"预览面板缺控件 {name}"
print("预览面板控件齐全（pv / 标题 / 元信息 / 路径 / 4 个按钮）")

# 空态：没选行时不应崩
h._show_preview(None)
print("空态预览标题:", h.label_pv_title.text()[:20])

# 取一条真实记录，走一遍 _show_preview，正文应被渲染进 pv
rows = h.ctx.pipeline.db.list_transcripts(limit=1)
if rows:
    d = rows[0]
    h._show_preview(d)
    txt = h.pv.toPlainText()
    print("预览正文长度:", len(txt))
    assert len(txt) > 0, "选中记录后预览框应有正文"
    print("预览标题:", h.label_pv_title.text()[:30])
    print("路径标签:", h.label_pv_path.text()[:60])
    # 有正文时 4 个按钮应该可用
    print("按钮可用?", h.btn_pv_editor.isEnabled(),
          h.btn_pv_folder.isEnabled(), h.btn_pv_copy.isEnabled())
else:
    print("（库里没有转录记录，跳过正文渲染断言）")
print("TR PREVIEW SMOKE OK")

# ---- 本轮新增：平台下拉由 registry 生成，且含小红书 ----
from app.crawler import registry as _reg
plats = [p.key for p in _reg.platform_list()]
print("registry 平台:", plats)
assert "xiaohongshu" in plats, "小红书应注册在平台清单里"
print("小红书在 registry 清单里 OK")

# 平台筛选下拉应跟着 registry 走，而不是手写
if hasattr(h, "combo_plat"):
    items = [h.combo_plat.itemData(i) for i in range(h.combo_plat.count())]
    items = [i for i in items if i]
    print("平台筛选下拉项:", items)
    assert "xiaohongshu" in items, "平台筛选下拉应含小红书（从 registry 生成）"
    print("平台筛选下拉含小红书 OK")

print("HISTORY SMOKE OK")

# ---- 本轮新增：配置页「文件」页已下线 + 目录/外部工具收进高级区 ----
print("导航项数:", len(w.nav_buttons))
assert "files" not in w.nav_buttons, "「文件」不应在导航栏"
print("「文件」已从导航栏下线 OK")
print("高级区折叠标题:", s._adv.label_summary.text()[:80])
assert hasattr(s, "edit_editor") and hasattr(s, "edit_kb"), \
    "外部工具与知识库输入框应存在（收进高级区）"
print("外部工具/知识库输入框存在 OK（已收进高级设置）")
print("CONFIG SMOKE OK")
