"""验证「生成内容勾选项」+「Obsidian 格式」是否正确落地。

不依赖网络、不跑 ASR：直接构造 Item 走 exporter，检查产出的 Markdown
是否符合 Obsidian 的语法约定，以及各勾选项是否真的生效。

关键断言：
1. frontmatter 必须是合法 YAML 且能被解析回字段
2. tags 必须是列表、不带 #、含层级前缀（platform/、author/、topic/）
3. 关闭「时间戳」时正文不得出现 [mm:ss -> mm:ss]
4. 关闭「纯文本」时不得出现「## 纯文本」段
5. 关掉评论写入时正文不得有「## 评论」段
6. INDEX.md / board.canvas 的生成与内容
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.models import Item, OutputOptions, STATUS_DONE  # noqa: E402
from app.core import zhconv  # noqa: E402
from app.export.obsidian import extract_keywords  # noqa: E402
from app.export.writer import Exporter, build_tags, render_item_markdown  # noqa: E402


def _s2t_converter():
    """拿一个 s2t 转换器（仅调试用；判定繁体用 zhconv.to_simplified 反查即可）。"""
    try:
        import opencc
        return opencc.OpenCC("s2t")
    except Exception:  # noqa: BLE001
        return None


OUT = ROOT / "out" / "test_export"

SAMPLE_TEXT = (
    "这台手机本来应该叫做X400 Pro，但这次跳过了一代的名称并且加了尾锥，"
    "变成了X500 Pro Max。我觉得这次的影像升级是重点，主摄传感器更大了，"
    "夜景拍摄效果明显更好。手机的续航也很重要，电池容量提升了，"
    "配合影像算法，这台手机在手机市场里竞争力很强。"
)


def _item(i: int = 1) -> Item:
    return Item(
        platform="bilibili", item_id=f"BV{i:04d}",
        title=f"测试作品 / 第 {i} 条：带*非法*字符?",
        author="某某UP主", url=f"https://www.bilibili.com/video/BV{i:04d}",
        duration=673, view_count=123456, like_count=8901, comment_count=42,
        publish_time="2026-09-20", desc="这是原简介，包含 #话题 和 http://x.cn",
        status=STATUS_DONE, transcript=SAMPLE_TEXT,
        asr_engine="faster-whisper(small/cuda/float16)",
        segments=json.dumps([
            {"start": 0.0, "end": 3.5, "text": "这台手机本来应该叫做X400 Pro"},
            {"start": 3.5, "end": 8.2, "text": "但这次跳过了一代的名称并且加了尾锥"},
        ], ensure_ascii=False),
    )


COMMENTS = [
    {"user": "网友A", "content": "讲得很清楚", "like_count": 30,
     "publish_time": "2026-09-21"},
    {"user": "网友B", "content": "影像确实强", "like_count": 120,
     "publish_time": "2026-09-22"},
]


def check(cond: bool, msg: str, problems: list[str]) -> None:
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        problems.append(msg)


def parse_frontmatter(md: str) -> dict:
    """极简 YAML frontmatter 解析（只认本工具写出的那一小撮语法）。"""
    m = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    if not m:
        return {}
    out: dict = {}
    cur_key = ""
    for line in m.group(1).splitlines():
        if line.startswith("  - "):
            out.setdefault(cur_key, []).append(line[4:].strip().strip('"'))
        elif ":" in line:
            k, _, v = line.partition(":")
            cur_key = k.strip()
            v = v.strip()
            if v:
                out[cur_key] = v.strip('"')
    return out


def main() -> int:
    problems: list[str] = []
    if OUT.exists():
        shutil.rmtree(OUT)

    print("\n【1】关键词抽取")
    kws = extract_keywords(SAMPLE_TEXT, top=6)
    print(f"  抽到：{kws}")
    check(len(kws) > 0, "能从中文正文抽出关键词", problems)
    check(all(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", k) for k in kws),
          "关键词都是 2~4 个汉字", problems)

    print("\n【2】标签体系")
    it = _item()
    tags = build_tags(it, SAMPLE_TEXT)
    print(f"  tags = {tags}")
    check(any(t.startswith("platform/") for t in tags), "含 platform/ 层级标签", problems)
    check(any(t.startswith("author/") for t in tags), "含 author/ 层级标签", problems)
    check(any(t.startswith("topic/") for t in tags), "含自动 topic/ 标签", problems)
    check(all(" " not in t and "#" not in t for t in tags),
          "标签不含空格和 #", problems)

    print("\n【3】frontmatter 合法性")
    # ⚠️ frontmatter 只在 Obsidian 模式写。默认模式是 timestamped（不写 frontmatter），
    # 旧版测试漏了这句显式 mode，导致 4 项误判失败。
    md = render_item_markdown(it, "[00:00 -> 00:03] 这台手机本来应该叫做X400 Pro",
                              COMMENTS,
                              OutputOptions(mode=OutputOptions.MODE_OBSIDIAN))
    fm = parse_frontmatter(md)
    print(f"  frontmatter 字段：{sorted(fm.keys())}")
    check("title" in fm and "tags" in fm, "frontmatter 含 title 与 tags", problems)
    check(isinstance(fm.get("tags"), list) and len(fm.get("tags", [])) >= 3,
          "tags 是 YAML 列表且多于 3 个", problems)
    check(md.count("---\n") >= 2, "frontmatter 用 --- 正确闭合", problems)
    check("<!-- more -->" in md, "含 Obsidian 折叠点 <!-- more -->", problems)

    print("\n【4】三种内容模式（互斥）")
    exp = Exporter(OUT, "full", OutputOptions(mode=OutputOptions.MODE_TIMESTAMPED))
    p = exp.write_transcript(it, "[00:00 -> 00:03] 这台手机本来应该叫做X400 Pro",
                             COMMENTS)
    body = p.read_text(encoding="utf-8")
    check("## 纯文本" in body, "带时间戳模式：有「纯文本」段", problems)
    check("## 逐字稿（带时间戳）" in body, "带时间戳模式：有「带时间戳」段", problems)
    check("## 原简介 / 文案" in body, "带时间戳模式：有「原简介」段", problems)
    check("## 评论（2 条" in body, "带时间戳模式：有「评论」段且条数正确", problems)
    check("[00:00 -> 00:03]" in body, "带时间戳模式：时间戳存在", problems)
    check("**关键词**：" in body, "带时间戳模式：文末有关键词标签", problems)
    check("00: 00" not in body, "时间戳冒号没被加空格（normalize_punct 回归）", problems)

    # 纯文本模式：没有时间戳，但有正文 + 简介 + 评论 + 关键词
    o_plain = OutputOptions(mode=OutputOptions.MODE_PLAIN)
    body2 = Exporter(OUT, "plain", o_plain).write_transcript(
        it, "[00:00 -> 00:03] xxx", COMMENTS).read_text("utf-8")
    check("---" not in body2.split("\n")[0], "纯文本模式不写 frontmatter", problems)
    check("## 逐字稿" in body2, "纯文本模式含正文", problems)
    check("[00:00 -> 00:03]" not in body2, "纯文本模式无时间戳", problems)
    check("## 原简介" in body2, "纯文本模式含简介", problems)
    check("## 评论" in body2, "纯文本模式含评论", problems)
    check("**关键词**：" in body2, "纯文本模式文末也有关键词标签", problems)

    # Obsidian 模式：frontmatter + topic/ 标签 + 折叠点
    o_obs = OutputOptions(mode=OutputOptions.MODE_OBSIDIAN)
    body3 = Exporter(OUT, "obs", o_obs).write_transcript(
        it, "[00:00 -> 00:03] xxx", COMMENTS).read_text("utf-8")
    check(body3.startswith("---"), "Obsidian 模式首行是 frontmatter", problems)
    check("topic/" in body3, "Obsidian 模式 frontmatter 含 topic/ 标签", problems)
    check("<!-- more -->" in body3, "Obsidian 模式含折叠点", problems)

    print("\n【5】模式互斥：段落开关由 mode 自动推导")
    check(OutputOptions(mode="plain").out_timestamped is False,
          "plain 模式关掉时间戳段", problems)
    check(OutputOptions(mode="timestamped").out_timestamped is True,
          "timestamped 模式开时间戳段", problems)
    check(OutputOptions(mode="obsidian").obsidian is True,
          "obsidian 模式打开 obsidian 标志", problems)
    check(OutputOptions(mode="plain").obsidian is False,
          "plain 模式不打开 obsidian 标志", problems)
    # 关键词标签在所有模式都强制开启（用户明确要求）
    for m in ("timestamped", "plain", "obsidian"):
        check(OutputOptions(mode=m).keywords_as_tags is True,
              f"{m} 模式强制开启关键词标签", problems)
    # 非法 mode 回落到默认
    check(OutputOptions(mode="不存在的模式").mode == "timestamped",
          "非法 mode 回落到 timestamped", problems)

    print("\n【5b】旧配置反推模式")
    check(OutputOptions.from_dict({"obsidian": True}).mode == "obsidian",
          "旧配置 obsidian=True → obsidian 模式", problems)
    check(OutputOptions.from_dict(
        {"out_plain": True, "out_timestamped": False}).mode == "plain",
        "旧配置只要纯文本 → plain 模式", problems)
    check(OutputOptions.from_dict({}).mode == "timestamped",
          "旧配置无信息 → timestamped 模式", problems)

    print("\n【6】评论独立文件开关")
    e7 = Exporter(OUT, "cmt-file", OutputOptions(out_comments_file=True))
    cp = e7.write_comments(it, COMMENTS)
    check(cp is not None and Path(cp).exists(), "开：评论独立文件已生成", problems)
    e8 = Exporter(OUT, "no-cmt-file", OutputOptions(out_comments_file=False))
    check(e8.write_comments(it, COMMENTS) is None, "关：不生成评论独立文件", problems)

    print("\n【7】索引页 / Canvas 已下架（用户明确不要）")
    items = [_item(1), _item(2), _item(3)]
    e9 = Exporter(OUT, "index-test", OutputOptions())
    for n, x in enumerate(items):
        e9.write_transcript(x, f"[00:00 -> 00:03] 第{n}条")
    paths = e9.write_index(items)
    check("csv" in paths and paths["csv"].exists(), "生成 index.csv", problems)
    check("json" in paths and paths["json"].exists(), "生成 index.json", problems)
    check("summary" in paths and paths["summary"].exists(), "生成 SUMMARY.md", problems)
    check("moc" not in paths, "不再生成 INDEX.md（moc）", problems)
    check("canvas" not in paths, "不再生成 board.canvas", problems)
    check(not (e9.root / "INDEX.md").exists(), "磁盘上确实没有 INDEX.md", problems)
    check(not (e9.root / "board.canvas").exists(), "磁盘上确实没有 board.canvas", problems)

    print("\n【8】文件名安全")
    check("/" not in p.name and "?" not in p.name and "*" not in p.name,
          f"文件名去掉了非法字符：{p.name}", problems)

    print("\n【9】繁体转简体")
    # ⚠️ 判定「还有没有繁体」不能靠手写字表（写错一个字表就把简体误判成繁体），
    # 要反过来问 OpenCC：把这个字按 s2t 转一次，如果**变了**说明它本来就是简体，
    # 如果**没变**且它本身是汉字，才可能是繁体残留。
    s2t = _s2t_converter()
    han = re.compile(r"[\u4e00-\u9fff]")

    def _trad_left(text: str) -> list[str]:
        """找出文本里所有「疑似繁体」的字（OpenCC s2t 不改变它 = 它已是繁体/共通字）。

        为了不把「一」「是」这种简繁同形字算进来，只报「t2s 会把它变成别的字」的字。
        """
        bad: list[str] = []
        for ch in dict.fromkeys(text):
            if not han.fullmatch(ch):
                continue
            # t2s 把它改了 → 它原来是繁体
            if zhconv.to_simplified(ch) != ch:
                bad.append(ch)
        return bad

    trad = _item(9)
    trad.title = "測試視頻 繁體標題"
    trad.author = "測試作者"
    trad.transcript = "這是一個測試。我們來看看手機影像的表現。"
    trad.desc = "這是原簡介，軟體開發與網路連線。"
    for mode in ("timestamped", "plain", "obsidian"):
        md = render_item_markdown(
            trad, "[00:00 -> 00:05] 這是一個測試。", None,
            OutputOptions(mode=mode))
        bad = _trad_left(md)
        check(not bad, f"{mode} 模式全文无繁体（残留：{bad}）", problems)

    # 反向校验：确认素材原本确实是繁体（否则测试等于没测）
    raw_bad = _trad_left(trad.title + trad.author + trad.transcript + trad.desc)
    check(len(raw_bad) >= 3,
          f"素材本身含大量繁体（共 {len(raw_bad)} 种：{raw_bad[:8]}…），测试有效",
          problems)
    _ = s2t  # 保留给调试用

    print("\n" + "=" * 62)
    if problems:
        print(f"❌ {len(problems)} 项未通过：")
        for x in problems:
            print("   -", x)
        return 1
    print("✅ 全部通过")
    print(f"\n产出目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
