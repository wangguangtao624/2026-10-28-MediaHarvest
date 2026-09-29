# 导出格式说明 · Obsidian 友好

导出目录结构（`输出目录/任务名/`）：

```
<任务名>/
  transcripts/           每个作品一篇笔记（Markdown）
  comments/              评论独立笔记（勾选「评论另存为独立文件」时才有）
  SUMMARY.md             汇总速览（含需要人工确认的条目）
  index.csv              表格总览，可直接喂 Excel
  index.json             全量结构化数据（含标签）
```

> **已下架**：早先版本的 `INDEX.md` 索引页（MOC）与 `board.canvas` 看板
> 已按用户要求移除，不再生成。分类入口改由 Obsidian 自身的
> 「标签树 + 属性面板 + 搜索」承担，比一份静态 MOC 更灵活。

---

## 一、怎么接进 Obsidian

1. 把整个输出目录**放进你的 Obsidian 库**（Vault）里。
   推荐放在库内的固定子目录，例如 `库/素材库/`。
2. 打开 Obsidian 的「标签」面板，就能看到按 `platform/` `author/`
   `topic/` 分级折叠的标签树 —— 点任意标签即筛出对应笔记。
3. 也可用属性面板按 `platform` / `author` / `captured` 排序筛选。

---

## 二、每篇笔记的格式

### YAML frontmatter（属性面板可见、可筛选）

```yaml
---
title: 视频标题
platform: bilibili
author: 某某UP主
url: "https://..."
duration: "11:13"
duration_sec: 673
published: 2026-09-20
views: 123456
likes: 8901
comments: 42
media_kind: video
engine: faster-whisper(small/cuda/float16)
status: done
captured: 2026-09-27
tags:
  - mediaharvest
  - platform/bilibili
  - status/done
  - author/某某UP主
  - type/video
  - topic/影像
  - topic/手机
---
```

在 Obsidian 里可以直接这样筛：

```
tag:#platform/bilibili
tag:#author/某某UP主
tag:#topic/影像
```

### 标签体系（层级标签，能做树形分类）

| 前缀 | 含义 | 例子 |
|---|---|---|
| `platform/` | 来源平台 | `platform/douyin` |
| `author/` | 作者 | `author/某某UP主` |
| `topic/` | 从逐字稿自动抽取的主题 | `topic/影像` |
| `type/` | 视频 / 图文 | `type/video` |
| `status/` | 处理结果 | `status/done` |
| `mediaharvest` | 总量标签，一键筛出本工具产出的全部笔记 | — |

标签值**不带 `#`**（写成 `#topic/影像` 的是正文尾部的关键词行，frontmatter 里只写纯文本）。

⚠️ **关键词标签在三种模式下都会生成**（用户明确要求：「所有生成的文稿里
都需要包含关键词作为标签」），不再提供关掉的开关：

- **Obsidian 模式**：关键词进 frontmatter 的 `tags`（`topic/xxx`）；
- **带时间戳 / 纯文本模式**：没有 frontmatter，关键词以文档末尾一行
  `**关键词**：#xxx  #yyy` 的形式附上，保证任何一份文稿都能看到关键词。

自动主题标签用「高频 2~4 字连续汉字片段」抽取（词频 ≥ 2 才入选），
不引额外依赖、不联网；嫌多可以在 Obsidian 里直接删。

### 正文结构

```markdown
# 标题

| 项 | 值 |      ← 元信息用表格，扫起来比列表快
| --- | --- |
| 平台 | bilibili |

<!-- more -->        ← Obsidian 折叠点（仅 Obsidian 模式），之后的内容默认收起

## 逐字稿（带时间戳）    ← 仅带时间戳 / Obsidian 模式
[00:00 -> 00:03] 这台手机本来应该叫做 X400 Pro

## 纯文本
（无时间戳的整段文字）

## 原简介 / 文案
...

## 评论（42 条，按点赞）
1. **网友A**（👍30，2026-09-21）
   讲得很清楚
```

---

## 三、生成内容的三种模式（配置页「生成内容」）

改版后不再是一堆独立勾选，而是**三选一**，每种模式内部已经配齐
「一份正常文稿该有的东西」，不需要再做组合决策：

| 模式 | 产出 |
|---|---|
| **带时间戳的逐字稿** | 时间戳逐字稿 + 纯文本（方便复制）+ 原简介 + 评论 + 文末关键词标签 |
| **纯文本逐字稿** | 纯文本逐字稿 + 原简介 + 评论 + 文末关键词标签（无时间戳、无 frontmatter） |
| **Obsidian 格式的逐字稿** | frontmatter + 标签 + 折叠点 + 时间戳 + 纯文本 + 原简介 + 评论 |

以前的勾选组合太自由，能勾出「只导纯文本 + 评论但不要逐字稿」这种
没人想要的结果，所以现在收敛成三种语义清晰的模式。

---

## 四、命令行对应参数

```bash
# 带时间戳的逐字稿（默认）
python run.py --platform bilibili --mode creator --query 946974 \
  --output D:/素材库

# 纯文本逐字稿
python run.py --platform bilibili --mode creator --query 946974 \
  --output D:/素材库 --content plain

# Obsidian 格式（含 frontmatter + 标签 + 折叠点）
python run.py --platform bilibili --mode creator --query 946974 \
  --output D:/素材库 --content obsidian

# 抓评论（每条 20 条）并按时间排序
python run.py --mode manual --urls "https://..." \
  --output D:/素材库 --content obsidian --comments --comment-limit 20 \
  --comment-sort time
```

可用 `.obsidian` 的「属性」视图按 `platform` / `author` 等字段排序，
或配合 Dataview 插件做看板：

```dataview
TABLE platform, author, duration, captured
FROM #mediaharvest
WHERE contains(file.tags, "topic/影像")
SORT captured DESC
```
