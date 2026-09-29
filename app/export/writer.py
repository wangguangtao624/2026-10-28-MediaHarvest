"""导出层：把库里的数据落地到用户指定的目录。

产出结构（输出目录可任意指定）：
    <output_dir>/<任务名>/
        transcripts/<标题>.md     逐字稿（按勾选项决定含哪些段落）
        comments/<标题>.md        评论（勾选「单独成文件」时才生成）
        index.csv                 作品总览，可直接喂给 Excel / 分析脚本
        index.json                全量结构化数据
        SUMMARY.md                汇总速览
        INDEX.md                  索引页（MOC，Obsidian 分类入口）
        board.canvas              Obsidian Canvas 看板（勾选时生成）

Obsidian 适配（勾选「Obsidian 格式」时生效）：
    - 每个笔记头部写 YAML frontmatter，Obsidian 属性面板直接可筛
    - tags 用层级标签（platform/、author/、topic/、status/）做树形分类
    - 正文用 <!-- more --> 标记折叠点
    - INDEX.md 把作品按平台/作者/标签汇总成入口页

界面上的「结果文档」页直接复用 `render_item_markdown()`，
保证「界面上看到的」和「导出到磁盘的」是同一份内容，不会两套逻辑走偏。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from ..core.models import Item, OutputOptions
from ..core import zhconv
from .obsidian import (
    build_frontmatter, canvas_file_node, canvas_json, canvas_text_node,
    clean_tag, extract_keywords, safe_filename, truncate,
)


def _fmt_dur(sec: int) -> str:
    """秒 → m:ss（Obsidian 里比裸秒数好读）。"""
    if not sec:
        return "0:00"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}" if m else f"0:{s:02d}"


def _parse_images(item: Item) -> list[dict]:
    """把 item.images（JSON 字符串）解析成列表，容错处理。"""
    raw = getattr(item, "images", "") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for d in data:
        if isinstance(d, dict) and isinstance(d.get("url"), str) and d["url"]:
            out.append(d)
        elif isinstance(d, str) and d.startswith("http"):
            out.append({"url": d, "w": 0, "h": 0})
    return out


def _split_tags(item: Item) -> list[str]:
    raw = getattr(item, "tags", "") or ""
    return [t for t in (x.strip() for x in raw.split(",")) if t]


def _clean_body(text: str) -> str:
    """清洗正文里的平台标记，让 Markdown 读起来干净。

    ⚠️ 小红书正文尾部常拖着一大串这样的东西：
        ...巨香！#howto搞定0失败美食[话题]# #红烧肉[话题]# #家常菜[话题]#
    这些标签已经在上方「**话题**：」单独列出来了，正文里再留一遍
    纯属噪音。这里把 `#xxx[话题]#` 洗成 `#xxx`（保留可读性），
    再把孤立的 `[话题]#` 残渣清掉，并压掉多余空行。
    """
    if not text:
        return ""
    s = text
    # #xxx[话题]# → #xxx
    s = re.sub(r"#([^#\[\]]{1,30})\[(?:话题|地点|好物|商品|品牌|歌手)\]#?",
               r"#\1", s)
    # 残留的 [话题]# / [话题]
    s = re.sub(r"\[(?:话题|地点|好物|商品|品牌|歌手|标记)\]#?", "", s)
    # 一行里堆了 3 个以上纯话题标签 → 整行删掉（上方已有话题行）
    lines: list[str] = []
    for ln in s.split("\n"):
        stripped = ln.strip()
        if re.fullmatch(r"(#[^\s#]{1,30}\s*){3,}", stripped):
            continue
        # tab 占位（小红书正文里常用来做空行）
        lines.append(ln.replace("\t", "").rstrip())
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _body_text(item: Item) -> str:
    """这条作品「可用来抽关键词」的正文。

    ⚠️ 图文作品没有逐字稿，正文就是 desc。以前只取 transcript，
    图文作品永远抽不出关键词，标签里只有 platform/status 那几个。
    """
    parts = [item.transcript or "", getattr(item, "desc", "") or "",
             getattr(item, "tags", "") or ""]
    return "\n".join(p for p in parts if p)


def build_tags(item: Item, text: str,
               opts: OutputOptions | None = None) -> list[str]:
    """生成本条笔记的标签。

    ⚠️ 关键词标签在**所有模式**下都会生成（用户明确要求：「所有生成的文稿里
    都需要包含关键词作为标签」），所以这里不再看 opts.auto_tags 开关，
    只要有正文就抽。

    ⚠️ 标签里的作者名也要过简繁转换：作者名来自抓取页面（B 站港台号、
    YouTube 等常是繁体），frontmatter 的 `author:` 字段转了但
    `author/xxx` 标签没转的话，同一份文档里会出现「作者: 测试作者」+
    「author/測試作者」这种半繁半简的割裂（实测踩到）。
    """
    opts = opts or OutputOptions()
    tags = [
        "mediaharvest",
        f"platform/{clean_tag(zhconv.to_simplified(item.platform))}",
        f"status/{clean_tag(item.status)}",
    ]
    if item.author:
        tags.append(f"author/{clean_tag(zhconv.to_simplified(item.author))}")
    tags.append("type/image" if item.media_kind == "image" else "type/video")
    # ⚠️ 图文作品的「正文」是 desc 不是 transcript，这里传进来的 text
    # 可能只有 transcript（空的）。用 _body_text 兜底合并所有文本源，
    # 否则图文作品的 topic/ 标签永远抽不出来。
    merged = "\n".join(x for x in (text or "", _body_text(item)) if x)
    # 平台原生话题标签优先（作者自己打的，比抽取准）
    native = _split_tags(item)
    for kw in native:
        t = clean_tag(zhconv.to_simplified(kw))
        if t and len(t) >= 2:
            tags.append(f"topic/{t}")
    for kw in extract_keywords(merged):
        t = clean_tag(kw)
        if t and len(t) >= 2:
            tags.append(f"topic/{t}")
    # 去重保序
    return list(dict.fromkeys(tags))


def keyword_tags(item: Item, text: str) -> list[str]:
    """只要「关键词」类的标签（正文尾部用，不带 platform/status 这些内部前缀）。"""
    out: list[str] = []
    # 平台原生话题放最前面（作者自己打的，最贴合内容）
    for kw in _split_tags(item):
        t = clean_tag(zhconv.to_simplified(kw))
        if t and len(t) >= 2:
            out.append(t)
    merged = "\n".join(x for x in (text or "", _body_text(item)) if x)
    for kw in extract_keywords(merged):
        t = clean_tag(kw)
        if t and len(t) >= 2:
            out.append(t)
    return list(dict.fromkeys(out))


def render_item_markdown(item: Item, timestamped: str = "",
                         comments: list[dict] | None = None,
                         opts: OutputOptions | None = None) -> str:
    """把一个作品渲染成 Markdown。

    界面预览和磁盘导出共用这一份，避免两边显示不一致。
    段落由 opts.mode 决定（三种互斥模式，见 OutputOptions 文档）。
    """
    opts = opts or OutputOptions()
    opts.apply_mode()
    # ⚠️ 落库的 transcript 可能来自旧版本（含繁体），渲染前统一过一遍简体，
    # 保证「界面看到的」「导出到磁盘的」都是简体。
    plain = zhconv.clean_lines(item.transcript or "")
    ts_text = zhconv.clean_lines(timestamped) if timestamped else ""
    desc = zhconv.to_simplified(item.desc or "")
    full_text = "\n".join(x for x in (ts_text, plain) if x)

    # ⚠️ 标题/作者/平台也来自抓取页面，同样可能是繁体（B 站港台号、YouTube 等），
    # 正文转了但标题没转的话，文档看起来仍然"半繁半简"。统一在这里转换一次。
    title = zhconv.to_simplified(item.title or item.item_id or "")
    author = zhconv.to_simplified(item.author or "")
    platform = zhconv.to_simplified(item.platform or "")

    is_image = item.media_kind == "image"
    images = _parse_images(item)
    native_tags = _split_tags(item)

    body: list[str] = []
    is_obsidian = opts.mode == OutputOptions.MODE_OBSIDIAN

    # ---- Obsidian frontmatter（仅 Obsidian 模式）----
    if is_obsidian:
        fm: dict = {
            "title": title,
            "platform": platform,
            "author": author,
            "url": item.url,
            "duration": _fmt_dur(item.duration),
            "duration_sec": item.duration,
            "published": item.publish_time,
            "views": item.view_count,
            "likes": item.like_count,
            "comments": item.comment_count,
            "media_kind": item.media_kind,
            "engine": item.asr_engine,
            "status": item.status,
            "captured": _date_of(item.created_at),
        }
        if is_image:
            # 图文作品：时长没意义，换成图片张数
            fm["duration"] = ""
            fm["duration_sec"] = 0
            fm["image_count"] = item.image_count or len(images)
            if native_tags:
                fm["source_tags"] = native_tags
        body.append(build_frontmatter(fm, build_tags(item, full_text, opts)))

    body.append(f"# {title}")
    body.append("")

    # ---- 元信息表 ----
    kind_cn = "图文" if is_image else "视频"
    rows = [
        ("平台", platform),
        ("类型", f"{kind_cn}（{item.image_count or len(images)} 张图）"
         if is_image else kind_cn),
        ("作者", author or "-"),
    ]
    if not is_image:
        rows.append(("时长", _fmt_dur(item.duration)))
    rows += [
        ("发布", item.publish_time or "-"),
        ("播放", f"{item.view_count:,}" if item.view_count else "-"),
        ("点赞", f"{item.like_count:,}" if item.like_count else "-"),
        ("评论", f"{item.comment_count:,}" if item.comment_count else "-"),
        ("引擎", item.asr_engine or "-"),
    ]
    if item.url:
        rows.append(("原文", f"<{item.url}>"))
    body.append("| 项 | 值 |")
    body.append("| --- | --- |")
    for k, v in rows:
        body.append(f"| {k} | {v} |")
    body.append("")

    # ---- 平台原生话题标签 ----
    if native_tags:
        body.append("**话题**：" + "  ".join(f"#{t}" for t in native_tags))
        body.append("")

    if item.warning:
        body.append(f"> [!warning] {zhconv.to_simplified(item.warning)}")
        body.append("")
    if item.error:
        body.append(f"> [!failure] {zhconv.to_simplified(item.error)}")
        body.append("")

    # ---- 折叠点（仅 Obsidian）----
    if is_obsidian:
        body.append("<!-- more -->")
        body.append("")

    # ---- 图片（图文作品的核心内容）----
    # ⚠️ 用 Markdown 图片语法 ![](url)，Obsidian 打开会自动拉取渲染。
    # 视频作品通常只有一张封面，放在正文前当预览。
    if images:
        if is_image:
            body.append(f"## 图片（{len(images)} 张）")
            body.append("")
            for k, im in enumerate(images, 1):
                url = im.get("url") or ""
                w, h = im.get("w") or 0, im.get("h") or 0
                body.append(f"### 图 {k}")
                body.append("")
                body.append(f"![图 {k}]({url})")
                body.append("")
                if w and h:
                    body.append(f"<sub>{w}×{h}</sub>")
                    body.append("")
        else:
            # 视频：封面只放一张，别把封面当正文图
            body.append("## 封面")
            body.append("")
            body.append(f"![封面]({images[0].get('url', '')})")
            body.append("")

    # ---- 正文 / 逐字稿 ----
    # 带时间戳的模式：先给时间戳版（信息最全），再给纯文本（方便复制粘贴）
    if opts.out_timestamped and ts_text:
        body.append("## 逐字稿（带时间戳）")
        body.append("")
        body.append(ts_text)
        body.append("")

    if opts.out_plain and plain:
        # 纯文本模式下它就是主角，不加「（无时间戳）」这种废话
        heading = "## 逐字稿" if opts.mode == OutputOptions.MODE_PLAIN \
            else "## 纯文本"
        body.append(heading)
        body.append("")
        body.append(plain)
        body.append("")

    # 图文作品的正文就是 desc，用「正文」而不是「原简介」，
    # 因为它本来就是这条内容的主体，不是附注
    if opts.out_desc and desc:
        heading = "## 正文" if (is_image and not plain) else "## 原简介 / 文案"
        body.append(heading)
        body.append("")
        # ⚠️ 正文里往往拖着一长串「#xxx[话题]# #yyy[话题]#」，
        # 和上面「**话题**：」那行完全重复，读起来很脏。
        # 已经单独抽成话题行了，正文里就把这串标记洗掉。
        body.append(_clean_body(desc))
        body.append("")

    # 图文作品：上面什么都没输出的话给一句说明，别让文档看起来是坏的
    if is_image and not plain and not desc:
        body.append("> [!info] 图文作品")
        body.append("> 本次未取到正文（详情接口未返回或抓取被风控拦截），"
                    "可重试采集。")
        body.append("")

    # ---- 评论 ----
    if comments:
        ordered = _sort_comments(comments, opts.comment_sort)
        body.append(f"## 评论（{len(ordered)} 条"
                    f"{'，按点赞' if opts.comment_sort == 'like' else '，按时间'}）")
        body.append("")
        for i, c in enumerate(ordered, 1):
            who = zhconv.to_simplified(c.get("user") or "匿名")
            when = c.get("publish_time") or ""
            like = c.get("like_count", 0)
            meta = "，".join(x for x in (f"👍{like}" if like else "", when) if x)
            content = zhconv.clean_lines(c.get("content", "") or "")
            body.append(f"{i}. **{who}**" + (f"（{meta}）" if meta else ""))
            body.append(f"   {content}")
            body.append("")
    elif item.comment_count:
        body.append("## 评论")
        body.append("")
        body.append("（本次未抓取评论，或抓取失败）")
        body.append("")

    # ---- 关键词标签（非 Obsidian 模式贴在文末）----
    # 用户要求：所有生成的文稿里都要包含关键词作为标签。
    # Obsidian 模式已经在 frontmatter 的 tags 里了，这里只处理另外两种。
    if not is_obsidian:
        kws = keyword_tags(item, full_text)
        if kws:
            body.append("---")
            body.append("")
            body.append("**关键词**：" + "  ".join(f"#{k}" for k in kws))
            body.append("")

    return "\n".join(body)


def _sort_comments(comments: list[dict], mode: str) -> list[dict]:
    if mode == "time":
        return sorted(comments, key=lambda c: str(c.get("publish_time") or ""),
                      reverse=True)
    return sorted(comments, key=lambda c: int(c.get("like_count") or 0),
                  reverse=True)


def _date_of(ts: float) -> str:
    import time
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return ""


class Exporter:
    def __init__(self, output_dir: str | Path, task_name: str = "export",
                 opts: OutputOptions | None = None) -> None:
        self.opts = opts or OutputOptions()
        self.root = Path(output_dir) / safe_filename(task_name, 40)
        self.transcripts_dir = self.root / "transcripts"
        self.comments_dir = self.root / "comments"
        dirs = [self.root, self.transcripts_dir]
        if self.opts.out_comments_file:
            dirs.append(self.comments_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        # item_id -> {"transcript": 路径, "comments": 路径}
        self.files: dict[str, dict[str, str]] = {}
        # item_id -> 笔记相对 root 的路径（Canvas 用）
        self.rel: dict[str, str] = {}

    def write_transcript(self, item: Item, timestamped: str = "",
                         comments: list[dict] | None = None) -> Path:
        base = safe_filename(item.title or item.item_id)
        path = self.transcripts_dir / f"{base}.md"
        path.write_text(
            render_item_markdown(item, timestamped, comments, self.opts),
            encoding="utf-8")
        self.files.setdefault(item.item_id, {})["transcript"] = str(path)
        self.rel[item.item_id] = f"transcripts/{base}.md"
        return path

    def write_comments(self, item: Item, comments: list[dict]) -> Path | None:
        if not comments or not self.opts.out_comments_file:
            return None
        base = safe_filename(item.title or item.item_id)
        path = self.comments_dir / f"{base}.md"
        ordered = _sort_comments(comments, self.opts.comment_sort)
        # 标题/作者同样可能来自繁体页面，统一转简体（与正文保持一致）
        title = zhconv.to_simplified(item.title or item.item_id)
        author = zhconv.to_simplified(item.author or "")
        # 评论正文也参与关键词抽取：评论里常出现比正文更具体的话题词
        all_text = "\n".join(
            zhconv.clean_text(c.get("content", "") or "") for c in ordered)
        lines: list[str] = []
        if self.opts.mode == OutputOptions.MODE_OBSIDIAN:
            lines.append(build_frontmatter(
                {
                    "title": f"评论：{title}",
                    "platform": zhconv.to_simplified(item.platform),
                    "author": author,
                    "url": item.url,
                    "count": len(ordered),
                },
                ["mediaharvest", "kind/comments",
                 f"platform/{clean_tag(zhconv.to_simplified(item.platform))}"]
                + ([f"author/{clean_tag(author)}"] if author else [])
                + [f"topic/{clean_tag(k)}" for k in extract_keywords(all_text)
                   if clean_tag(k)],
            ))
        lines.append(f"# 评论：{title}")
        lines.append("")
        if item.url:
            lines.append(f"原文：<{item.url}>")
            lines.append("")
        for i, c in enumerate(ordered, 1):
            who = zhconv.to_simplified(c.get("user") or "匿名")
            like = c.get("like_count", 0)
            when = c.get("publish_time") or ""
            meta = "，".join(x for x in (f"👍{like}" if like else "", when) if x)
            content = zhconv.clean_lines(c.get("content", "") or "")
            lines.append(f"{i}. **{who}**" + (f"（{meta}）" if meta else ""))
            lines.append(f"   {content}")
            lines.append("")
        # 非 Obsidian 模式：关键词贴文末，保证「评论文件里也有标签」
        if self.opts.mode != OutputOptions.MODE_OBSIDIAN:
            kws = [clean_tag(k) for k in extract_keywords(all_text)]
            kws = list(dict.fromkeys(k for k in kws if k))
            if kws:
                lines.append("---")
                lines.append("")
                lines.append("**关键词**：" + "  ".join(f"#{k}" for k in kws))
                lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        self.files.setdefault(item.item_id, {})["comments"] = str(path)
        return path

    def file_map(self) -> dict[str, dict[str, str]]:
        """item_id -> {"transcript": 路径, "comments": 路径}，给界面做超链接用。"""
        return {k: dict(v) for k, v in self.files.items()}

    # ------------------------------------------------------------ 索引 / 看板
    def write_index(self, items: list[Item],
                    comments_map: dict[str, list[dict]] | None = None) -> dict[str, Path]:
        comments_map = comments_map or {}
        out: dict[str, Path] = {}

        csv_path = self.root / "index.csv"
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["平台", "ID", "类型", "标题", "作者", "时长(秒)", "图片数",
                        "播放", "点赞", "评论数", "发布时间", "链接", "状态",
                        "正文字数", "话题标签", "标签", "文稿文件", "评论文件"])
            for it in items:
                files = self.files.get(it.item_id, {})
                tags = build_tags(it, _body_text(it), self.opts)
                native = " ".join("#" + t for t in _split_tags(it))
                # 图文作品的字数算 desc（没有逐字稿），否则「正文字数」全是 0
                chars = (len(it.transcript or "") if it.media_kind != "image"
                         else len(getattr(it, "desc", "") or ""))
                w.writerow([
                    zhconv.to_simplified(it.platform), it.item_id,
                    "图文" if it.media_kind == "image" else "视频",
                    zhconv.to_simplified(it.title),
                    zhconv.to_simplified(it.author), it.duration,
                    it.image_count or 0,
                    it.view_count, it.like_count, it.comment_count,
                    it.publish_time, it.url, it.status, chars,
                    native, " ".join("#" + t for t in tags),
                    files.get("transcript", ""), files.get("comments", ""),
                ])
        out["csv"] = csv_path

        json_path = self.root / "index.json"
        payload = []
        for it in items:
            d = it.to_dict()
            # 结构化数据也跟着转简体，避免下游（Excel / 分析脚本）拿到繁体
            d["title"] = zhconv.to_simplified(it.title)
            d["author"] = zhconv.to_simplified(it.author)
            d["platform"] = zhconv.to_simplified(it.platform)
            d["desc"] = zhconv.to_simplified(it.desc)
            d["comments"] = comments_map.get(it.item_id, [])
            d["tags"] = build_tags(it, _body_text(it), self.opts)
            # 给下游一份解析好的图片列表，省得自己再 json.loads
            d["image_list"] = _parse_images(it)
            d["source_tags"] = _split_tags(it)
            payload.append(d)
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        out["json"] = json_path

        out["summary"] = self._write_summary(items)
        # ⚠️ 索引页（MOC）与 Canvas 看板已移除：用户明确说「做得太烂了，不需要」。
        # 保留 _write_moc / _write_canvas 方法本体只为兼容旧代码引用，
        # 不再主动调用，也不再写盘。
        return out

    def _write_summary(self, items: list[Item]) -> Path:
        done = [i for i in items if i.status == "done"]
        total_chars = sum(len(i.transcript or "") for i in items)
        lines = [
            f"# 汇总：{self.root.name}",
            "",
            f"- 作品总数：{len(items)}",
            f"- 已完成转录：{len(done)}",
            f"- 逐字稿总字数：{total_chars}",
            "",
            "## 清单",
            "",
        ]
        for i, it in enumerate(items, 1):
            flag = "x" if it.status == "done" else " "
            rel = self.rel.get(it.item_id, "")
            # 用相对路径的相对链接，Obsidian 里能直接点开
            title = zhconv.to_simplified(it.title or it.item_id).replace("|", "/")
            link = f"[[{rel[:-3]}\\|{title}]]" if rel else title
            lines.append(f"- [{flag}] {i}. {link} — "
                         f"{zhconv.to_simplified(it.author) or '-'}")
        warned = [i for i in items if i.warning]
        if warned:
            lines += ["", "## ⚠️ 需要人工确认", ""]
            for it in warned:
                lines.append(f"- {zhconv.to_simplified(it.title) or it.item_id}："
                             f"{zhconv.to_simplified(it.warning)}")
        path = self.root / "SUMMARY.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _write_moc(self, items: list[Item]) -> Path:
        """【已弃用】索引页（MOC）。

        ⚠️ 用户明确表示「索引页做得太烂了，不需要」，因此 write_index 不再
        调用本方法。保留方法体只为兼容可能的外部引用，不要重新启用；
        新代码请直接删掉这段（连 `_write_canvas` 一起）。
        """
        by_platform: dict[str, list[Item]] = {}
        by_author: dict[str, list[Item]] = {}
        by_topic: dict[str, list[Item]] = {}
        for it in items:
            by_platform.setdefault(it.platform, []).append(it)
            by_author.setdefault(it.author or "未知作者", []).append(it)
            for t in build_tags(it, it.transcript or "", self.opts):
                if t.startswith("topic/"):
                    by_topic.setdefault(t[len("topic/"):], []).append(it)

        lines: list[str] = []
        if self.opts.obsidian:
            lines.append(build_frontmatter(
                {
                    "title": f"索引：{self.root.name}",
                    "type": "moc",
                    "count": len(items),
                    "captured": _date_of(items[0].created_at) if items else "",
                },
                ["mediaharvest", "kind/moc"],
            ))
        lines.append(f"# 索引：{self.root.name}")
        lines.append("")
        lines.append(f"共 {len(items)} 条内容。下面按平台 / 作者 / 主题三个维度分类，"
                     f"点任意一条直接进入笔记。")
        lines.append("")

        def _section(title: str, groups: dict[str, list[Item]]) -> None:
            if not groups:
                return
            lines.append(f"## {title}")
            lines.append("")
            for key in sorted(groups, key=lambda k: (-len(groups[k]), k)):
                lines.append(f"### {key}（{len(groups[key])}）")
                lines.append("")
                for it in groups[key]:
                    rel = self.rel.get(it.item_id, "")
                    name = (it.title or it.item_id).replace("|", "/")
                    if rel:
                        lines.append(f"- [[{rel[:-3]}\\|{name}]] "
                                     f"`{_fmt_dur(it.duration)}`")
                    else:
                        lines.append(f"- {name} `{_fmt_dur(it.duration)}`")
                lines.append("")

        _section("按平台", by_platform)
        _section("按作者", by_author)
        if by_topic:
            _section("按主题", by_topic)

        path = self.root / "INDEX.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _write_canvas(self, items: list[Item]) -> Path | None:
        """【已弃用】Obsidian Canvas 看板。

        ⚠️ 用户明确表示「Canvas 看板做得太烂了，不需要」，write_index 不再调用。
        保留方法体仅为兼容外部引用，不要重新启用。
        """
        by_author: dict[str, list[Item]] = {}
        for it in items:
            by_author.setdefault(it.author or "未知作者", []).append(it)
        if not by_author:
            return None

        nodes: list[dict] = []
        col_w, card_h, gap = 360, 96, 16
        for ci, (author, group) in enumerate(
                sorted(by_author.items(), key=lambda kv: (-len(kv[1]), kv[0]))):
            x = ci * col_w
            head = canvas_text_node(
                f"head{ci}",
                f"**{truncate(author, 22)}**\n{len(group)} 条",
                x, -140, w=col_w - gap, h=100, color="4")
            nodes.append(head)
            for ri, it in enumerate(group):
                rel = self.rel.get(it.item_id, "")
                if not rel:
                    continue
                nodes.append(canvas_file_node(
                    f"f{ci}_{ri}", rel,
                    x, ri * (card_h + gap),
                    w=col_w - gap, h=card_h,
                    color="1" if it.status == "done" else "3"))
        if not nodes:
            return None
        path = self.root / "board.canvas"
        path.write_text(canvas_json(nodes), encoding="utf-8")
        return path
