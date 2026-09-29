"""核心数据模型。

设计要点：
- 一切以 Item（作品）为中心，转录、评论都挂在 Item 上
- Item.status 贯穿整条流水线，UI 靠它展示进度
- 用户勾选过的 Item 才进入下载/转录阶段（selected 字段）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# 作品状态流转
STATUS_PENDING = "pending"        # 已采集，等待用户勾选
STATUS_QUEUED = "queued"          # 已勾选，排队待处理
STATUS_DOWNLOADING = "downloading"
STATUS_DOWNLOADED = "downloaded"
STATUS_TRANSCRIBING = "transcribing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"        # 用户取消勾选

ALL_STATUSES = (
    STATUS_PENDING, STATUS_QUEUED, STATUS_DOWNLOADING, STATUS_DOWNLOADED,
    STATUS_TRANSCRIBING, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED,
)

# 采集模式
MODE_CREATOR = "creator"   # 指定博主主页
MODE_TOPIC = "topic"       # 主题搜索
MODE_MANUAL = "manual"     # 手动粘贴链接（无需 cookie 的兜底模式）


@dataclass
class Item:
    """一个作品（视频/图文）。"""
    platform: str
    item_id: str
    title: str = ""
    author: str = ""
    author_id: str = ""
    url: str = ""
    duration: int = 0
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    publish_time: str = ""
    desc: str = ""
    cover: str = ""
    status: str = STATUS_PENDING
    selected: bool = False       # 用户在预览页是否勾选
    local_path: str = ""         # 下载后的本地音视频路径
    stream_url: str = ""         # 直连下载地址（有它就不用 yt-dlp，抖音靠这个）
    transcript: str = ""         # 逐字稿全文
    segments: str = ""           # 带时间戳片段（JSON 字符串）
    asr_engine: str = ""
    error: str = ""
    warning: str = ""        # 非致命提示，比如「疑似纯音乐、没检出人声」
    media_kind: str = "video"   # video / image（图文作品没有人声轨，不能转录）
    # ---- 图文作品专用（视频作品这些字段为空）----
    # 小红书的图集正文只存在于详情接口，列表接口不给，所以必须单独落库。
    # images 是 JSON 数组字符串：[{"url": "...", "w": 1440, "h": 1920}, ...]
    images: str = ""
    image_count: int = 0
    tags: str = ""              # 平台原生的 #话题# 标签，逗号分隔
    comment_limit: int = -1     # 本条要抓多少评论；-1=用任务级默认值
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Any) -> "Item":
        data = dict(row)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Comment:
    """一条评论。"""
    item_id: str
    user: str = ""
    content: str = ""
    like_count: int = 0
    publish_time: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class OutputOptions:
    """「生成什么内容」的选项。

    ⚠️ 2026-09 改版：从 10 个独立勾选精简为**三种互斥模式**。
    原来的勾选组合太自由，能勾出「只导纯文本+评论但不要逐字稿」这种
    没人想要的结果，用户也说不清自己到底要哪个。

    现在只有三选一（`MODE_*`），每种模式内部已经把「一个正常文稿该有的
    东西」都配齐了，用户不需要再做组合决策：

        带时间戳 → 时间戳逐字稿 + 纯文本（方便复制）+ 简介 + 评论
        纯文本   → 纯文本逐字稿 + 简介 + 评论
        Obsidian → frontmatter + 标签 + 时间戳 + 纯文本 + 简介 + 评论
                   且折叠点、双链、属性面板全部到位

    「关键词标签」在**所有模式**下都会生成（用户明确要求：
    「所有生成的文稿里都需要包含关键词作为标签」），所以它不再是一个
    可关闭的开关。纯文本模式下没有 frontmatter，标签以正文尾部的
    `#tag` 形式附在文档最后，保证「任何一份导出的文稿都能看到关键词」。

    已废弃的字段（export_moc / export_canvas）保留在 dataclass 里只为
    兼容旧 config.json 与旧任务记录，实际不再产生任何输出。
    """

    MODE_TIMESTAMPED = "timestamped"
    MODE_PLAIN = "plain"
    MODE_OBSIDIAN = "obsidian"

    # --- 三选一 ---
    mode: str = "timestamped"

    # --- 内容段落开关（由 mode 自动推导，界面上不再单独暴露）---
    out_transcript: bool = True      # 输出逐字稿正文
    out_timestamped: bool = True     # 带时间戳那一份
    out_plain: bool = True           # 纯文本那一份
    out_desc: bool = True            # 带上原简介/文案

    # --- 评论 ---
    out_comments: bool = True        # 评论是否写进文档
    out_comments_file: bool = True   # 是否额外单独导出评论文件
    comment_sort: str = "like"       # like=按点赞 / time=按时间

    # --- 标签 ---
    keywords_as_tags: bool = True    # 关键词当标签（用户要求：始终开启）
    auto_tags: bool = True           # 兼容旧字段，等价于 keywords_as_tags

    # --- 兼容旧版字段（不再产生输出）---
    obsidian: bool = False           # 由 mode 推导
    export_canvas: bool = False      # deprecated
    export_moc: bool = False         # deprecated

    def __post_init__(self) -> None:
        self.apply_mode()

    def apply_mode(self) -> None:
        """按 mode 把各段落开关设成该模式应有的样子。"""
        m = self.mode if self.mode in (
            self.MODE_TIMESTAMPED, self.MODE_PLAIN, self.MODE_OBSIDIAN
        ) else self.MODE_TIMESTAMPED
        self.mode = m
        if m == self.MODE_PLAIN:
            self.out_transcript = True
            self.out_timestamped = False
            self.out_plain = True
            self.obsidian = False
        elif m == self.MODE_OBSIDIAN:
            self.out_transcript = True
            self.out_timestamped = True
            self.out_plain = True
            self.obsidian = True
        else:                                    # timestamped
            self.out_transcript = True
            self.out_timestamped = True
            self.out_plain = True
            self.obsidian = False
        # 标签在所有模式下都要（用户明确要求）
        self.keywords_as_tags = True
        self.auto_tags = True

    def mode_label(self) -> str:
        return {
            self.MODE_TIMESTAMPED: "带时间戳的逐字稿",
            self.MODE_PLAIN: "纯文本逐字稿",
            self.MODE_OBSIDIAN: "Obsidian 格式的逐字稿",
        }.get(self.mode, self.mode)

    def any_content(self) -> bool:
        """至少要输出一种内容，否则生成的文件是空壳。"""
        return bool(self.out_transcript or self.out_timestamped
                    or self.out_plain or self.out_desc)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "OutputOptions":
        d = dict(d or {})
        # 旧配置没有 mode：按旧的勾选组合反推一个最接近的模式
        if "mode" not in d:
            if d.get("obsidian"):
                d["mode"] = cls.MODE_OBSIDIAN
            elif d.get("out_plain") and not d.get("out_timestamped"):
                d["mode"] = cls.MODE_PLAIN
            else:
                d["mode"] = cls.MODE_TIMESTAMPED
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class Task:
    """一次采集任务。"""
    name: str
    mode: str
    platform: str
    query: str = ""
    max_count: int = 20            # 采集多少个作品
    comment_limit: int = 20        # 每个作品抓多少条评论（与 max_count 解耦）
    fetch_comments: bool = False
    do_download: bool = True
    do_transcribe: bool = True
    output_dir: str = ""           # Markdown / CSV / JSON 等成品的输出根目录
    media_dir: str = ""            # 音视频中间文件的存放目录（空=用默认）
    # 中间文件处理
    keep_media: bool = True        # 转录完成后是否保留音视频；False=转完即删
    # 性能相关
    model_size: str = "small"
    device: str = "auto"
    compute_type: str = "auto"     # auto / float16 / int8 / int8_float16
    beam_size: int = 5             # 1=贪心最快，5=默认
    cpu_threads: int = 0           # 0=交给 ctranslate2 自己定
    concurrency: int = 1           # 并发转录实例数
    pipeline_mode: str = "serial"  # serial / overlap（下载与转录重叠）
    # 内容输出
    output_opts: OutputOptions = field(default_factory=OutputOptions)
    created_at: float = field(default_factory=time.time)
    task_id: int | None = None

    def needs_download(self, item: "Item") -> bool:
        """这一条到底要不要下载。

        关键修正：以前「不勾选下载」就不下，但转录又必须有音视频，
        于是转出 0 个字。正确语义是——
            **只要要转录，就必须先下载**（有本地文件可复用的除外），
        do_download 只决定「转完要不要留着」这件事由 keep_media 表达。
        """
        if item.media_kind == "image":
            return False                      # 图文作品没人声轨，下了也没用
        if item.local_path and Path(item.local_path).exists():
            return False                      # 上次已经下好了，直接复用
        if item.stream_url:
            return True                       # 有直连地址，随时可下
        return bool(item.url)                 # 有页面链接也能下
