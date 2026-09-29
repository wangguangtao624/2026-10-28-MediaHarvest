"""中文文本规范化：繁体转简体 + 简繁混排清理。

为什么需要单独一层
------------------
Whisper 在中文上**经常输出繁体**，而且是随机的——同一段音频，有的段落出简体，
有的段落出繁体，甚至一个句子里混着来。原因：

1. Whisper 的多语言训练数据里，繁体语料占比不低，尤其港台内容；
2. `initial_prompt` 只能「引导」不能「约束」。实测即使写了
   「请使用简体中文输出」，长音频里仍会有 15%~30% 的段落飘回繁体，
   因为每个 segment 是独立解码的，prompt 的影响会随窗口滑走。

所以**不能只靠 prompt，必须做确定性后处理**。这里用 OpenCC 的 t2s 转换，
它是查表转换（不是模型），结果稳定、可预期、零幻觉。

为什么是 opencc-python-reimplemented 而不是 opencc
--------------------------------------------------
`opencc` 那个包要编译 C++ 扩展，Windows 上常装不上（需要 MSVC 工具链）。
`opencc-python-reimplemented` 是纯 Python 重写，pip 直接装，词表一致。

顺带在这里做两件相关的清理：
- `normalize_punct()`：把 Whisper 偶尔吐出的全角英文标点/多余空格收拾干净；
- 空行和首尾空白标准化。
"""

from __future__ import annotations

import re
from functools import lru_cache

# OpenCC 转换器是线程安全的（内部只读查表），但构造有成本（要加载词表），
# 所以缓存住。多个转录线程会并发调 convert()，lru_cache 保证只建一次。
_OPENCC_AVAILABLE: bool | None = None


@lru_cache(maxsize=4)
def _converter(config: str = "t2s"):
    """按配置拿一个 OpenCC 转换器。

    t2s  = Traditional → Simplified（我们只要这个）
    t2s.json 用的词表较大，能正确处理「著/着」「髮/发」这类一词多形。
    """
    try:
        import opencc
    except ImportError:  # pragma: no cover
        return None
    try:
        return opencc.OpenCC(config)
    except Exception:  # noqa: BLE001
        return None


def has_opencc() -> bool:
    """opencc 是否可用（界面/测试里用来提示降级行为）。"""
    global _OPENCC_AVAILABLE
    if _OPENCC_AVAILABLE is None:
        _OPENCC_AVAILABLE = _converter("t2s") is not None
    return _OPENCC_AVAILABLE


# 兜底映射：opencc 不可用时，至少把最常见的高频繁体字换掉。
# 不求全（那要几千行词表），只覆盖「一眼就看出是繁体」的字。
_FALLBACK_MAP = str.maketrans({
    "這": "这", "個": "个", "們": "们", "來": "来", "說": "说", "時": "时",
    "會": "会", "對": "对", "為": "为", "麼": "么", "樣": "样", "後": "后",
    "開": "开", "關": "关", "發": "发", "現": "现", "點": "点", "還": "还",
    "讓": "让", "從": "从", "經": "经", "過": "过", "題": "题", "實": "实",
    "際": "际", "應": "应", "該": "该", "種": "种", "類": "类", "問": "问",
    "題": "题", "覺": "觉", "得": "得", "東": "东", "西": "西", "車": "车",
    "錢": "钱", "買": "买", "賣": "卖", "價": "价", "貴": "贵", "錯": "错",
    "測": "测", "試": "试", "識": "识", "語": "语", "言": "言", "學": "学",
    "習": "习", "課": "课", "書": "书", "寫": "写", "讀": "读", "聽": "听",
    "聲": "声", "視": "视", "頻": "频", "網": "网", "絡": "络", "連": "连",
    "線": "线", "機": "机", "電": "电", "腦": "脑", "網": "网", "體": "体",
    "區": "区", "園": "园", "國": "国", "際": "际", "團": "团", "隊": "队",
    "員": "员", "師": "师", "醫": "医", "藥": "药", "廠": "厂", "產": "产",
    "業": "业", "務": "务", "圖": "图", "畫": "画", "樂": "乐", "藝": "艺",
    "術": "术", "館": "馆", "廳": "厅", "樓": "楼", "門": "门", "窗": "窗",
    "豔": "艳", "麗": "丽", "華": "华", "鳳": "凤", "龍": "龙", "馬": "马",
    "鳥": "鸟", "魚": "鱼", "蟲": "虫", "貓": "猫", "狗": "狗", "寶": "宝",
    "貝": "贝", "財": "财", "貨": "货", "購": "购", "費": "费", "賺": "赚",
    "賠": "赔", "贏": "赢", "輸": "输", "勝": "胜", "負": "负", "強": "强",
    "弱": "弱", "優": "优", "劣": "劣", "壞": "坏", "舊": "旧", "新": "新",
    "熱": "热", "冷": "冷", "溫": "温", "暖": "暖", "陰": "阴", "陽": "阳",
    "雲": "云", "霧": "雾", "風": "风", "雨": "雨", "雪": "雪", "霜": "霜",
    "樹": "树", "葉": "叶", "花": "花", "草": "草", "鳥": "鸟", "獸": "兽",
    # 高频虚词/助词
    "與": "与", "將": "将", "卻": "却", "僅": "仅", "並": "并", "則": "则",
    "雖": "虽", "既": "既", "若": "若", "爾": "尔", "麼": "么", "喲": "哟",
    "呢": "呢", "嗎": "吗", "吧": "吧", "啊": "啊", "呀": "呀", "哦": "哦",
    "喔": "喔", "耶": "耶", "嘿": "嘿", "唉": "唉", "咦": "咦", "哇": "哇",
})


def to_simplified(text: str) -> str:
    """繁体 → 简体。这是本模块的核心函数。

    对已经是简体的文本是幂等操作（再转一次结果不变），
    所以可以放心地在流水线的多个位置重复调用。

    opencc 不可用时退化为小词表映射——虽然不完美，但至少常见繁体字能换掉，
    比原样输出繁体好。界面会在「高级设置」里提示实际使用了哪种方式。
    """
    if not text:
        return text
    conv = _converter("t2s")
    if conv is not None:
        try:
            return conv.convert(text)
        except Exception:  # noqa: BLE001
            pass
    return text.translate(_FALLBACK_MAP)


# 全角标点在中文正文里其实更自然，但 Whisper 偶尔会把中文句号写成
# 英文点、或者中英标点混排得很乱。这里只做「明显该修」的几件事。
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_STRIP_PUNCT_EDGE = re.compile(r"^[\s，。、；：！？,.;:!?]+|[\s，。、；：！？,.;:!?]+$")

# ⚠️ 冒号必须排除在「补空格」之外，否则时间戳 `00:00` 会被改成 `00: 00`。
# 这是个真实踩过的坑：normalize_punct 在渲染时间戳时被调用，
# 把每一行 [00:00 -> 00:05] 都打成了 [00: 00 -> 00: 05]，看起来很脏。
# 只处理逗号/分号/叹号/问号，且只在「后面紧跟非空白」时补一个空格。
_EN_PUNCT_GAP = re.compile(r"([,;!?])(?=[^\s,;:!?])")


def normalize_punct(text: str) -> str:
    """收拾标点与空白，但**不改变语义、不乱改中文标点**。

    只做三件事：
    1. 英文逗号/分号/叹号/问号后面补一个空格（中英混排时更顺眼）；
    2. 连续空格压成一个；
    3. 去掉首尾多余空白。

    注意**不碰冒号**：冒号大量出现在时间戳（00:00）、比例（16:9）、
    时钟里，补空格会破坏这些格式。
    """
    if not text:
        return text
    s = _MULTI_SPACE.sub(" ", text)
    s = _EN_PUNCT_GAP.sub(r"\1 ", s)
    return s.strip()


# 括号配对：Whisper 有时会吐出 "(字幕由...提供)" 这类元信息，去掉。
_BRACKET_NOISE = re.compile(
    r"[（(]\s*(字幕|翻译|校对|感谢观看|请订阅|点关注|music|音乐|掌声|笑声)"
    r"[^）)]*[）)]",
    re.IGNORECASE)


def strip_noise(text: str) -> str:
    """去掉模型爱加的括号注释（「(字幕由XX提供)」这类）。"""
    if not text:
        return text
    return _BRACKET_NOISE.sub("", text).strip()


def clean_text(text: str, *, punct: bool = True) -> str:
    """一站式清理：繁体转简体 → 去括号噪音 → 标点规整。

    转录结果落库前统一过一遍这个函数，保证导出、界面预览、标签、
    关键词全都是简体。
    """
    if not text:
        return text
    s = strip_noise(text)
    s = to_simplified(s)
    if punct:
        s = normalize_punct(s)
    return s


def clean_lines(text: str, *, punct: bool = True) -> str:
    """逐行清理并丢掉清空后的空行（转录结果按段落组织时用）。"""
    if not text:
        return text
    out = []
    for line in text.splitlines():
        c = clean_text(line, punct=punct)
        if c or not out:
            out.append(c)
    return "\n".join(out).strip()
