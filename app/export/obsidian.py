"""Obsidian 友好的 Markdown 输出。

为什么要单独一层：
- Obsidian 靠 **YAML frontmatter** 做元数据（属性面板）、靠 **#标签** 做分类、
  靠 **[[双链]]** 做关联、靠 **Canvas** 做可视化看板、靠 **MOC** 做索引。
  普通 Markdown 导出到 Obsidian 里就是「一堆散文件」，搜不到、分不了类。
- 这里把「怎么写才能被 Obsidian 正确识别并分类」的规则集中到一处，
  界面预览与磁盘导出共用，避免两套格式走偏。

设计约定（写死，别随意改）：
- frontmatter 用 --- 包裹，字段名全小写英文（Obsidian 属性面板兼容最好）
- tags 用列表形式，值是**不带 # 的纯文本**（带 # 会被字面渲染成正文）
- 标签统一带层级前缀：platform/xxx、author/xxx、topic/xxx、status/xxx
  层级标签在 Obsidian 里能折叠树形分类，比扁平标签好用
- 正文里的链接用标准 Markdown，不用 [[ ]]：这些文件不在用户库里，
  双链会变成「未创建的空笔记」，反而污染图谱
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Obsidian 标签不允许空格和这些字符，统一替换
_TAG_BAD = re.compile(r"[^\w\u4e00-\u9fff/+-]", re.UNICODE)
# 文件名的非法字符（Windows 更严）
_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]')


def clean_tag(text: str, limit: int = 24) -> str:
    """把任意文本变成合法 Obsidian 标签。"""
    t = (text or "").strip().replace(" ", "-")
    t = _TAG_BAD.sub("", t)
    t = re.sub(r"-{2,}", "-", t).strip("-/+")
    return t[:limit]


def strip_platform_markup(text: str) -> str:
    """去掉平台特有的标记，避免污染文件名与关键词抽取。

    小红书的正文里到处都是这种标记：
        #红烧肉[话题]#  #超级下饭的[话题]#  @某人  展开全文
    如果直接丢给 extract_keywords，会切出「话题」「开的」「的标」这类
    碎片，跑出来的标签全是噪声；用在文件名上则会变得又长又脏
    （实测：「学着做！#红烧肉[话题]# #超级下饭的[话题]#.md」）。
    """
    if not text:
        return ""
    s = text
    # #xxx[话题]# —— 只留 xxx
    s = re.sub(r"#([^#\[\]]{1,30})\[话题\]#", r"\1 ", s)
    # 其他形式的话题/地点/好物标记
    s = re.sub(r"#([^#\[\]]{1,30})\[(?:地点|好物|商品|品牌|歌手)\]#", r"\1 ", s)
    # 裸露的 [话题]# / [话题] 残留
    s = re.sub(r"\[(?:话题|地点|好物|商品|品牌|歌手|标记)\]#?", " ", s)
    # @某人
    s = re.sub(r"@[\w\u4e00-\u9fff\-]{1,30}", " ", s)
    # 展开 / 展开全文 这类按钮文案
    s = re.sub(r"(展开全文|收起全文|展开|收起|全文)", " ", s)
    return s


def safe_filename(name: str, limit: int = 80) -> str:
    """文件名：去平台标记、去非法字符、去首尾点空格、截断。"""
    n = name or "untitled"
    # ⚠️ 标题常直接来自抓取结果，会带着「#xxx[话题]#」这种标记。
    # 不洗掉的话文件名会变成「学着做！#红烧肉[话题]# #超级下饭的[话题]#.md」，
    # 又长又脏还占字符配额。这里先清标记再落盘。
    n = strip_platform_markup(n)
    n = re.sub(r"#\s+", " ", n)          # 清洗后残留的孤立 #
    n = re.sub(r"\s+", " ", n)
    n = _FILENAME_BAD.sub("_", n.strip())
    n = n.strip("_ ")
    n = n.rstrip(". ")
    return (n[:limit] or "untitled")


# ---------------------------------------------------------------- 关键词抽取
# 中文没有空格，不能按词切。这里用「高频 2~4 字连续片段」的朴素做法：
# 对分类够用（能分出「手机」「摄影」「AI」这类主题），且零依赖、不联网。
_STOP = set("""
的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
自己 这 那 这个 那个 我们 你们 他们 什么 怎么 因为 所以 但是 如果 可以 这个
就是 还是 然后 现在 时候 已经 可能 应该 这样 那样 一下 一些 大家 非常 真的
其实 觉得 知道 想要 需要 问题 地方 东西 事 让 给 对 从 被 把 与 或 但 而 及
然后 这种 那种 一定 一直 出来 起来 过来 下来 进去 出去 现在 今天 明天 昨天
the a an and or but if of to in on at for with is are was were be been
this that these those it its you your we our they their he she his her
""".split())

# ⚠️ 平台水印 / 标记词的碎片：小红书正文里的「#xxx[话题]#」标签被
# 分词后会产生「话题」「标记」「展开」这类无意义碎片，实测跑出来的
# 标签里混进了 topic/话题、topic/饭的、topic/人小孩儿 这种垃圾。
# 这里单独列一张表，在抽取前后都过一遍。
_STOP_MARKUP = set("""
话题 标记 展开 全文 收起 编辑 分享 收藏 点赞 评论 关注 置顶 作者
视频 图文 笔记 图片 封面 正文 标题 简介 内容 详情 链接 来源
的 了 是 在 有 和 就 不 都 也 很 我 你 他 她 它 们 这 那 其
""".split())


def extract_keywords(text: str, top: int = 5, min_len: int = 2,
                     max_len: int = 4, min_count: int = 2) -> list[str]:
    """从中文文本抽高频短语当标签。

    刻意做得简单可控：只统计 2~4 字的连续汉字片段出现次数，
    过滤停用词与纯数字，取前 top 个。不引入 jieba 等额外依赖。

    min_count 默认 2（不是 3）：一条几十秒的短视频逐字稿也就一两百字，
    阈值定 3 会让大部分素材一个标签都抽不出来（实测只出得来单个词）。
    宁可多给几个候选，用户在 Obsidian 里删标签比补标签容易。
    """
    if not text:
        return []
    # 先洗掉平台标记，否则「[话题]#」会切出满屏碎片词
    text = strip_platform_markup(text)
    zh = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    counter: dict[str, int] = {}
    for chunk in zh:
        for n in range(min_len, max_len + 1):
            for i in range(len(chunk) - n + 1):
                w = chunk[i:i + n]
                if w in _STOP or w in _STOP_MARKUP:
                    continue
                counter[w] = counter.get(w, 0) + 1

    # 排序要点：先比频次，再比长度（长的信息量更大）
    items = sorted(counter.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    picked: list[str] = []
    for w, c in items:
        if c < min_count:
            break
        # 碎片过滤：单独出现一次又被切出来的短词，多半是「超级下饭的」
        # 里切出的「饭的」这种无意义片段。要求它至少出现 2 次，
        # 或者本身足够长（3 字以上），否则丢掉。
        if c < 2 and len(w) < 3:
            continue
        # 词尾是常见虚词的多半是切歪的（「饭的」「学做」里 …的/…做）
        if len(w) <= 3 and w[-1] in "的了是在和与及以及":
            continue
        # 被已选中的更长词包含则跳过：选了「手机影像」就不再要「手机」
        if any(w in p and w != p for p in picked):
            continue
        # 已选中的短词若被当前更长的词包含，替换掉（长词更具体）
        picked = [p for p in picked if not (p in w and p != w)]
        picked.append(w)
        if len(picked) >= top:
            break
    return picked


# ---------------------------------------------------------------- frontmatter
def _yaml_scalar(v: object) -> str:
    """YAML 值转义：含冒号/引号/换行的一律加引号。"""
    s = "" if v is None else str(v)
    s = s.replace("\r", " ").replace("\n", " ").strip()
    if s == "" or re.search(r'[:#\[\]{}",&*?|<>=!%@`\'"]', s) or s[0] in "-? ":
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def build_frontmatter(fields: dict[str, object],
                      tags: list[str]) -> str:
    """生成 YAML frontmatter 块（含结尾空行）。"""
    lines = ["---"]
    for k, v in fields.items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}:")
            for x in v:
                lines.append(f"  - {_yaml_scalar(x)}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    if tags:
        lines.append("tags:")
        for t in dict.fromkeys(tags):     # 去重保序
            lines.append(f"  - {_yaml_scalar(t)}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- Canvas
def canvas_json(nodes: list[dict], edges: list[dict] | None = None) -> str:
    """生成 Obsidian Canvas 文件内容（.canvas 就是 JSON）。

    Obsidian 的 Canvas 支持 file 类型节点，双击能直接打开对应笔记，
    所以这里用 type=file + file=相对路径 的方式把文档铺成看板。
    """
    import json

    payload = {"nodes": nodes, "edges": edges or []}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def canvas_file_node(node_id: str, rel_path: str, x: int, y: int,
                     w: int = 320, h: int = 120, color: str = "") -> dict:
    node: dict = {
        "id": node_id, "type": "file", "file": rel_path,
        "x": x, "y": y, "width": w, "height": h,
    }
    if color:
        node["color"] = color
    return node


def canvas_text_node(node_id: str, text: str, x: int, y: int,
                     w: int = 320, h: int = 80, color: str = "") -> dict:
    node: dict = {
        "id": node_id, "type": "text", "text": text,
        "x": x, "y": y, "width": w, "height": h,
    }
    if color:
        node["color"] = color
    return node


def truncate(text: str, limit: int) -> str:
    """按显示宽度截断（中文算 2 宽），避免把长标题塞爆。"""
    if not text:
        return ""
    out, width = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + w > limit:
            return "".join(out) + "…"
        out.append(ch)
        width += w
    return "".join(out)
