"""选项说明文案：模型 / 设备 / 精度 / 解码……

用户原话：「识别模型有好几种，我都不知道怎么用，需要加一下备注说明」
「运行设备这块有 Auto、CUDA、CPU，我也不太懂怎么选」。

所以这里不只是塞一句 tooltip，而是给每个选项配三样东西：
    what  —— 它到底是什么（一句话）
    how   —— 什么时候选它（决策依据）
    cost  —— 代价/风险（显存、速度、准确率）

界面在选项旁放一个 ⓘ 按钮，点开就是这张卡；同时给下拉框挂 tooltip。
文案全部用「人话」，不堆术语，把本机实测数字带上，用户才有判断依据。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 识别模型
# key → (展示名, 体积GB, 速度倍数, 准确度, 说明)
MODEL_INFO: dict[str, dict] = {
    "tiny": {
        "label": "tiny · 极快",
        "size": 0.08,
        "speed": "最快（约 20x 实时）",
        "quality": "★☆☆☆☆",
        "what": "最小的模型，速度极快但准确率低。",
        "how": "只用在「先跑通试试看」或者纯粹想知道视频大概在讲什么。",
        "cost": "中文错字很多（会把「跳过了一代」听成「跳过雷蛋」），"
                "不适合做正式逐字稿。",
    },
    "base": {
        "label": "base · 快",
        "size": 0.15,
        "speed": "很快（约 14x 实时）",
        "quality": "★★☆☆☆",
        "what": "比 tiny 好一点，仍然属于「快速粗糙」档。",
        "how": "长视频只要大意、对错字宽容时用。",
        "cost": "专有名词、人名基本会错。",
    },
    "small": {
        "label": "small · 推荐默认",
        "size": 0.49,
        "speed": "快（约 9x 实时）",
        "quality": "★★★★☆",
        "what": "速度和准确率平衡得最好的一档，中文口播基本可用。",
        "how": "绝大多数场景直接用这个。本项目的默认值。",
        "cost": "极专业的术语偶尔还是会错，但整体可读性很好。",
    },
    "medium": {
        "label": "medium · 更准",
        "size": 1.53,
        "speed": "较慢（约 4x 实时）",
        "quality": "★★★★★",
        "what": "准确率明显再上一档。",
        "how": "要出正式稿、内容里专业名词多、并且能接受多等一会儿时。",
        "cost": "显存占用约 2.6GB；速度约为 small 的一半。",
    },
    "large-v3": {
        "label": "large-v3 · 最准",
        "size": 3.09,
        "speed": "最慢（约 1.5x 实时）",
        "quality": "★★★★★",
        "what": "Whisper 系列里准确率最高、语种支持最全的模型。",
        "how": "做出版级文稿、或有大量方言/口音时才值得。",
        "cost": "首次要下载约 3GB；float16 下单实例约占 4.2GB 显存，"
                "6GB 显卡同时只能跑 1 个，长视频会等很久。",
    },
}

MODEL_ORDER = ["tiny", "base", "small", "medium", "large-v3"]


def model_tip(key: str) -> str:
    """下拉框 tooltip：一行讲清「是什么 + 选它的场景」。"""
    info = MODEL_INFO.get(key)
    if not info:
        return ""
    return (f"{info['label']}\n"
            f"体积 {info['size']:.2f} GB ｜ 速度 {info['speed']} ｜ 准确度 {info['quality']}\n"
            f"{info['what']}\n"
            f"什么时候用：{info['how']}\n"
            f"代价：{info['cost']}")


def model_detail(key: str) -> str:
    """说明卡里的富文本正文。"""
    info = MODEL_INFO.get(key)
    if not info:
        return ""
    return (
        f"<p style='margin:0 0 8px'><b>{info['label']}</b></p>"
        f"<table cellspacing='0' cellpadding='3'>"
        f"<tr><td style='color:#888'>模型体积</td><td>{info['size']:.2f} GB</td></tr>"
        f"<tr><td style='color:#888'>速&nbsp;&nbsp;&nbsp;&nbsp;度</td><td>{info['speed']}</td></tr>"
        f"<tr><td style='color:#888'>准确度</td><td>{info['quality']}</td></tr>"
        f"</table>"
        f"<p style='margin:8px 0 4px'>{info['what']}</p>"
        f"<p style='margin:4px 0'><b>什么时候用</b>：{info['how']}</p>"
        f"<p style='margin:4px 0'><b>代价</b>：{info['cost']}</p>"
    )


def recommend_model() -> str:
    """按本机显存猜一个默认模型（显存大就允许更大的）。"""
    try:
        from ..asr.engine import gpu_free_gb
        free = gpu_free_gb()
    except Exception:  # noqa: BLE001
        free = 0.0
    if free <= 0:
        return "small"          # CPU 上别用大的，太慢
    if free >= 6.0:
        return "medium"
    if free >= 2.5:
        return "small"
    if free >= 1.0:
        return "base"
    return "tiny"


# ---------------------------------------------------------------- 运行设备
DEVICE_INFO: dict[str, dict] = {
    "auto": {
        "label": "auto · 自动（推荐）",
        "what": "有可用显卡就用显卡（CUDA），没有就退回 CPU。",
        "how": "不用想，直接用这个。",
        "cost": "无。显卡不可用时它会自动降级，不会报错。",
    },
    "cuda": {
        "label": "cuda · 强制用显卡",
        "what": "强制走 NVIDIA 显卡。",
        "how": "你确定想用显卡，并且希望「显卡有问题时直接报错」而不是"
               "偷偷退回 CPU 慢慢跑。",
        "cost": "显卡不可用时任务会失败（这通常正是你想要的——"
                "早点发现问题，而不是等半小时才发现一直在用 CPU）。",
    },
    "cpu": {
        "label": "cpu · 强制用处理器",
        "what": "只用 CPU 跑，不碰显卡。",
        "how": "显卡被别的程序占满了（比如在跑游戏/另一个模型），"
               "或者显卡在出问题需要绕过时。",
        "cost": "慢一个数量级：实测同一段素材，显卡 76 秒 / CPU 344 秒（约 4.5 倍差距）。"
                "真要用 CPU，建议同时把「识别模型」降到 base 或 tiny。",
    },
}


def device_tip(key: str) -> str:
    info = DEVICE_INFO.get(key)
    if not info:
        return ""
    return (f"{info['label']}\n{info['what']}\n"
            f"什么时候用：{info['how']}\n代价：{info['cost']}")


def device_detail(key: str) -> str:
    info = DEVICE_INFO.get(key)
    if not info:
        return ""
    return (
        f"<p style='margin:0 0 8px'><b>{info['label']}</b></p>"
        f"<p style='margin:4px 0'>{info['what']}</p>"
        f"<p style='margin:4px 0'><b>什么时候用</b>：{info['how']}</p>"
        f"<p style='margin:4px 0'><b>代价</b>：{info['cost']}</p>"
    )


# ---------------------------------------------------------------- 计算精度
COMPUTE_INFO: dict[str, dict] = {
    "auto": {
        "label": "自动（推荐）",
        "what": "按显卡架构自动挑最优精度。",
        "how": "直接用。20 系及更早会选 float16，30 系及以后会选 int8_float16。",
        "cost": "无。",
    },
    "float16": {
        "label": "float16 · 半精度",
        "what": "半精度浮点，图灵（20 系）及更早显卡的最快选择。",
        "how": "你是 16/20 系显卡（本机 RTX 2060 就属于这类）。",
        "cost": "显存占用是 int8 的约 1.8 倍（small 模型约 1.2GB）。",
    },
    "int8_float16": {
        "label": "int8_float16 · 混合",
        "what": "权重用 int8、计算用 float16，安培（30 系）及以上的最优解。",
        "how": "30 系 / 40 系显卡，想省显存又要快。",
        "cost": "在 20 系及更早上反而更慢（没有 INT8 硬件单元），别选。",
    },
    "int8": {
        "label": "int8 · 整型量化",
        "what": "全整型量化，最省显存。",
        "how": "显存特别紧张、必须牺牲速度换空间时。",
        "cost": "实测在 RTX 2060 上比 float16 慢 1.5 倍（115 秒 vs 76 秒），"
                "网上教程推荐 int8 是因为它们默认你用 30 系以上。",
    },
    "float32": {
        "label": "float32 · 全精度",
        "what": "单精度浮点，精度最高、最占显存。",
        "how": "几乎不用。只在排查「结果异常」时用来对照。",
        "cost": "最慢、最占显存，日常完全没必要。",
    },
}


def compute_tip(key: str) -> str:
    info = COMPUTE_INFO.get(key)
    if not info:
        return ""
    return (f"{info['label']}\n{info['what']}\n"
            f"什么时候用：{info['how']}\n代价：{info['cost']}")


def compute_detail(key: str) -> str:
    info = COMPUTE_INFO.get(key)
    if not info:
        return ""
    return (
        f"<p style='margin:0 0 8px'><b>{info['label']}</b></p>"
        f"<p style='margin:4px 0'>{info['what']}</p>"
        f"<p style='margin:4px 0'><b>什么时候用</b>：{info['how']}</p>"
        f"<p style='margin:4px 0'><b>代价</b>：{info['cost']}</p>"
    )


# ---------------------------------------------------------------- 其它选项
MISC_TIPS: dict[str, str] = {
    "max_count": (
        "本次采集最多拿回多少个作品（按平台给出的顺序从新到旧）。\n"
        "只影响采集列表的长度，不决定下载多少——下载哪个由下一步「选择作品」决定。\n"
        "先少采几条试试水，确认平台和 cookie 都通了，再放开数量。"),
    "beam": (
        "解码策略。beam5 会同时保留 5 条候选路径再挑最好的，更准但更慢；"
        "beam1 是贪心解码，实测再快约 30%，代价是同音字（尤其人名、专有名词）"
        "更容易错。要出正式稿用 beam5，批量粗筛用 beam1。"),
    "pipeline": (
        "下载受网络限制、转录受显卡限制，两者互不抢资源。\n"
        "「重叠」让下载和转录同时进行，把「等下载」的时间吃掉，实测最快；\n"
        "「串行」逐条来，暂停/跳过的粒度最清晰，出问题最好排查。"),
    "concurrency": (
        "同时跑几个转录实例，靠显存换吞吐。每个实例各持一份模型权重。\n"
        "⚠️ 实测：素材平均时长 < 60 秒时，并发 2 反而比并发 1 慢"
        "（短素材的重启/VAD 固定开销占主导）。长素材才有收益。"),
    "cpu_threads": (
        "影响音频前后处理（静音检测、特征提取），这部分跑在 CPU 上。\n"
        "实测给足线程能再快约 20%：默认 76.4s ／ 4 线程 72.7s ／ 12 线程 62.9s。\n"
        "0 表示交给底层自己决定。"),
    "keep_media": (
        "下载的音视频是「中间文件」——转录出文字之后就完成了使命。\n"
        "勾选保留：硬盘里会留下 mp4/m4a（方便以后重转、或想看点看原片）；\n"
        "不勾选：转录完成后自动删除，只留 Markdown 文稿，磁盘占用最小。\n"
        "无论哪种，都能在「文件」页看到它们放在哪。"),
    "comments": (
        "评论按作品逐个抓取。B站 与抖音都支持；抖音的评论接口需要 a_bogus 签名，"
        "本项目已内置。\n"
        "抓多少条**不在这里定**：到下一步「选择作品」页里，"
        "每个作品旁边单独填数字（或者双击作品，在弹出的评论预览里填）。\n"
        "这样做是因为不同作品的评论价值差别很大——热门视频值得多抓，"
        "长尾的抓前几条看看风向就够了。"),
}

MISC_DETAILS: dict[str, str] = {
    "keep_media": (
        "<p style='margin:0 0 8px'><b>要不要保留音视频</b></p>"
        "<p style='margin:4px 0'>提取文案必须先有音视频——这是绕不开的一步。"
        "下载下来的文件属于「中间产物」，转成文字之后就没用了。</p>"
        "<p style='margin:4px 0'><b>勾选保留</b>：文件留在中间目录里，"
        "方便以后重新转录（比如换个大模型再跑一遍）、或者你想留一份原片。</p>"
        "<p style='margin:4px 0'><b>不勾选</b>：转录完成的瞬间自动删除该文件，"
        "磁盘上只留下 Markdown 文稿。适合「只要文案」的场景。</p>"
        "<p style='margin:4px 0'>两种方式下，文件放在哪、还剩哪些，"
        "都能在左侧「文件」页里直接看到。</p>"
    ),
}
