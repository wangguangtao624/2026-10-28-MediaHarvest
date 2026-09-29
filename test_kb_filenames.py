"""知识库归档的文件名回归测试。

为什么单独写这个
----------------
真实事故：长标题的逐字稿归档到知识库后**扩展名丢了**。
用户反馈「存到知识库里面的这个文件不太对啊？应该是 Markdown 的文件啊，
格式怎么不太对呢？」

根因：`knowledge._safe_segment()` 对整串文件名（含 `.md`）做 `s[:80]`，
中文标题 80 字符早就超过一个视频标题的长度，于是 `.md` 被一并切掉，
落地成 80 个字符的无后缀文件 —— Obsidian 不认、双击打不开。

这个测试把「任何长度、任何语言的标题，后缀都必须保住」钉死，
并且覆盖 Windows 单段路径 255 字节上限。

跑法：
    cd mediaharvest
    env -u PYTHONPATH -u PYTHONHOME <venv>/python test_kb_filenames.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.models import Item                      # noqa: E402
from app.export import knowledge                      # noqa: E402

FAIL = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        FAIL.append(msg)


print("【1】后缀必须保住（各种长度 / 语言 / 奇怪字符）")
CASES = [
    ("短标题.md", ".md"),
    ("a.md", ".md"),
    ("Codex联动Obsidian，搭建超强知识库，手把手教程 用Codex+Obsidian_搭建可以"
     "“自生长”的知识库_帮你把信息的利用效率_直接拉高到next level.md", ".md"),
    ("9个Obsidian必装插件，让AI帮你干活更顺手 Obsidian使用体验感拉满，编辑更顺手、"
     "文件更好找、散落的资料能一键搬进知识库，还能自动生成模板文件、汇.md", ".md"),
    ("标题里带.gitignore这种点.md", ".md"),
    ("BUILD 2024.12.31 版本.md", ".md"),
    ("x" * 400 + ".md", ".md"),
    ("评论：某视频.md", ".md"),
    ("没有后缀的散文件", ""),
    ("canvas画布.canvas", ".canvas"),
    ("字幕.srt", ".srt"),
    ("data.json", ".json"),
]
for raw, want in CASES:
    got = knowledge.safe_filename(raw)
    check(got.endswith(want) if want else "." not in got,
          f"{len(raw):3d} 字符 → 后缀 {got[-8:]!r}（期望 {want!r}）")

print()
print("【2】长度受控：Windows 单段 <255 字节")
for raw in ("中" * 500 + ".md", "a" * 500 + ".md", "😀" * 200 + ".md"):
    got = knowledge.safe_filename(raw)
    nb = len(got.encode("utf-8"))
    check(nb < 255, f"'{raw[0]}'×{len(raw)} → {nb} 字节（<255）")

print()
print("【3】非法字符被替换，但后缀还在")
for raw, want_ext in [('标题:含*非法?字符.md', ".md"),
                      ('带"引号"和<尖括号>.md', ".md"),
                      ("带/斜杠\\反斜杠.md", ".md")]:
    got = knowledge.safe_filename(raw)
    check(got.endswith(want_ext) and not any(
        c in got for c in ':*?"<>|\\/'), f"{raw!r} → {got!r}")

print()
print("【3b】斜杠属于标题内容，不能被当路径分隔符吃掉前半段")
for raw in ("路径/子路径/视频标题.md", "带/斜杠.md"):
    got = knowledge.safe_filename(raw)
    check(got.endswith(".md") and len(got) >= len(raw) - 3,
          f"{raw!r} → {got!r}（保留全部字符）")

print()
print("【4】目录名（平台/作者）单独限长，不受文件名逻辑影响")
check(knowledge._safe_segment("douyin") == "douyin", "平台名保持原样")
check(len(knowledge._safe_segment("作者" * 100)) <= 60, "超长作者名被截到 ≤60")

print()
print("【5】端到端：真拷贝一次，确认落地的文件能按 .md 打开")
tmp = Path(tempfile.mkdtemp(prefix="kb_test_"))
src = tmp / "source.md"
src.write_text("# 标题\n\n正文内容", encoding="utf-8")
long_title = ("Codex联动Obsidian，搭建超强知识库，手把手教程 用Codex+Obsidian_"
              "搭建可以“自生长”的知识库_帮你把信息的利用效率_直接拉高到next level")
it = Item(platform="douyin", item_id="123", title=long_title, author="Xuan酱")
res = knowledge.add_files(str(tmp / "kb"),
                          [{"path": str(src), "item": it,
                            "name": f"{long_title}.md"}])
check(res.ok, f"归档成功：{res.summary()}")
if res.copied:
    dst = Path(res.copied[0][1])
    check(dst.exists(), f"目标文件存在：{dst.name[:40]}…")
    check(dst.suffix == ".md", f"后缀是 .md（实际 {dst.suffix!r}）")
    check(dst.read_text(encoding="utf-8").startswith("# 标题"),
          "内容完整（能读回原文）")
    rel = dst.relative_to(tmp / "kb")
    check(rel.parts[0] == "douyin" and rel.parts[1] == "Xuan酱",
          f"目录结构正确：{rel}")

print()
print("【6】重名不覆盖：连拷三次应有 (2)、(3)")
for _ in range(2):
    knowledge.add_files(str(tmp / "kb"),
                        [{"path": str(src), "item": it,
                          "name": f"{long_title}.md"}])
names = sorted(p.name for p in (tmp / "kb" / "douyin" / "Xuan酱").iterdir())
check(len(names) == 3, f"3 个文件共存，未覆盖（实际 {len(names)} 个）")
check(all(n.endswith(".md") for n in names), "三个文件后缀都是 .md")
for n in names:
    print(f"     · {n[-30:]}")

print()
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项")
    for m in FAIL:
        print("   -", m)
    sys.exit(1)
print("✅ 知识库文件名全部通过")
