"""小红书解析容错测试（不需要联网，不需要 cookie）。

覆盖实测踩到的三个真实坑：
1. interact_info 的计数是**字符串**，还可能带中文单位（'7.6万' / '1.2亿'）。
   旧代码 int('7.6万') → ValueError，整个详情解析崩。
2. video.media.stream 的档位 key 是 **EF4/EF5/EF6/EF7**，不是 h264/h265/av1。
   旧代码写死 key 列表 → 一个都找不到 → 拿不到视频地址。
3. video.media_v2 是一个 **JSON 字符串**（不是 dict）。
   旧代码对它调 .get() → 'str' object has no attribute 'get' → 崩。

以及「图文/视频一视同仁」这轮新增的图文链路：
4. 图集详情 image_list 取图（url_default → url → url_pre → info_list 兜底）。
5. tag_list → 原生话题标签。
6. 平台标记（#xxx[话题]#）对文件名 / 关键词 / 正文的污染清洗。
7. 图文作品渲染成 Markdown：类型行、话题行、图片段、正文段。

跑法：
  env -u PYTHONPATH -u PYTHONHOME <venv>/python test_xhs_parse.py
"""

from __future__ import annotations

import json
import sys

from app.crawler.xiaohongshu import XiaohongshuCrawler as X
from app.core.cookie_manager import CookieManager
from app.core.models import Item, OutputOptions
from app.export.obsidian import extract_keywords, safe_filename, strip_platform_markup
from app.export.writer import (
    _clean_body,
    _parse_images,
    _split_tags,
    render_item_markdown,
)

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        FAILS.append(label)


print("=" * 62)
print("【1】计数解析 _num —— 真实返回值形态都要吃得下")
print("=" * 62)
cases = [
    (1234, 1234), ("1234", 1234), ("7.6万", 76000), ("1.2亿", 120000000),
    ("3.5w", 35000), ("12k", 12000), ("2,345", 2345), ("+88", 88),
    ("1234.0", 1234), (1234.0, 1234), ("", 0), (None, 0),
    ("abc", 0), (True, 0), ({"x": 1}, 0),
]
for raw, want in cases:
    got = X._num(raw)
    check(got == want, f"{raw!r:>12} → {got}（期望 {want}）")

print()
print("=" * 62)
print("【2】视频地址挑选 —— EF 档位 / JSON 字符串 media_v2")
print("=" * 62)

# 真实结构（实测 2026-09）：EF4~EF7，weight 越大越清晰
real = {"video": {"media": {"stream": {
    "EF4": [{"master_url": "http://s/259.mp4", "weight": 62}],
    "EF5": [
        {"master_url": "http://s/114.mp4", "weight": 62},
        {"master_url": "http://s/115.mp4", "weight": 70},   # 最清晰
    ],
    "EF6": [],
    "EF7": [],
}}}}
check(X._pick_stream(real) == "http://s/115.mp4",
      "EF 档位里挑出 weight 最大的（115.mp4）")

# media_v2 是 JSON 字符串 —— 旧代码在这里崩
mv2 = json.dumps({"stream": {"EF5": [
    {"master_url": "http://mv2/fallback.mp4", "weight": 70}]}})
check(X._pick_stream({"video": {"media_v2": mv2}}) == "http://mv2/fallback.mp4",
      "media_v2 为 JSON 字符串时不崩且能取到地址")

check(X._pick_stream({"video": {"media_v2": "{坏 json"}}) == "",
      "media_v2 是坏 JSON 时安全返回空串")

# 老结构仍要兼容
check(X._pick_stream({"video": {"media": {"stream": {
    "h264": [{"master_url": "http://x/h.mp4", "weight": 100}]}}}})
    == "http://x/h.mp4", "h264 老结构仍然兼容")

# 元素直接是字符串
check(X._pick_stream({"video": {"media": {"stream": {
    "EF4": ["http://x/plain.mp4"]}}}}) == "http://x/plain.mp4",
      "档位元素是纯字符串 URL 也能取到")

check(X._pick_stream({"video": {}}) == "", "空结构返回空串不抛异常")
check(X._pick_stream({}) == "", "完全没有 video 字段也不抛异常")

print()
print("=" * 62)
print("【3】封面提取 _first_image")
print("=" * 62)
check(X._first_image({"image_list": [{"url_default": "http://i/1.jpg"}]})
      == "http://i/1.jpg", "dict 形态取 url_default")
check(X._first_image({"image_list": ["http://i/2.jpg"]})
      == "http://i/2.jpg", "字符串形态直接返回")
check(X._first_image({"image_list": []}) == "", "空列表返回空串")
check(X._first_image({}) == "", "没有 image_list 也不崩")

print()
print("=" * 62)
print("【4】完整 detail card → Item 端到端（用真实响应结构）")
print("=" * 62)
card = {
    "note_id": "69f5fdeb000000002202bc06",
    "type": "video",
    "title": "5月 記錄",
    "desc": "正文",
    "time": 1790532000000,
    "user": {"nickname": "成为新一"},
    "interact_info": {            # ⚠️ 全部是字符串，含中文单位
        "liked_count": "7.6万",
        "comment_count": "1693",
        "collected_count": "645",
    },
    "video": {
        "media": {"stream": {
            "EF4": [{"master_url": "http://s/259.mp4", "weight": 62}],
            "EF5": [{"master_url": "http://s/115.mp4", "weight": 70}],
        }},
        "media_v2": json.dumps({"video_id": "1378937"}),
    },
    "image_list": [{"url_default": "http://i/cover.jpg"}],
}
c = X(cookie="a1=x; web_session=y")
item = c._card_to_item(card, note_id=card["note_id"], token="XSEC_TOKEN")
check(item is not None, "卡能被解析成 Item（旧代码这里抛异常）")
if item:
    check(item.title == "5月 記錄", f"标题正确：{item.title}")
    check(item.author == "成为新一", f"作者正确：{item.author}")
    check(item.like_count == 76000, f"'7.6万' → 76000（实得 {item.like_count}）")
    check(item.comment_count == 1693, f"评论数 1693（实得 {item.comment_count}）")
    check(item.media_kind == "video", "识别为视频")
    check(item.stream_url == "http://s/115.mp4",
          f"视频地址取最清晰档（实得 {item.stream_url}）")
    check(item.cover == "http://i/cover.jpg", "封面取得")
    check(getattr(item, "xsec_token", "") == "XSEC_TOKEN",
          "xsec_token 带上了（评论要用）")
    check(item.duration == 0, "无视频时长信息时为 0")

print()
print("=" * 62)
print("【5】图集笔记（type=normal）")
print("=" * 62)
pic = {
    "note_id": "pic001", "type": "normal", "title": "图片笔记",
    "user": {"nickname": "作者"}, "interact_info": {"liked_count": "99"},
    "image_list": [{"url_default": "http://i/p1.jpg"}],
}
pi = c._card_to_item(pic, note_id="pic001", token="t")
check(pi is not None and pi.media_kind == "image", "图集识别为 image")
check(pi is not None and pi.stream_url == "", "图集没有视频地址")
check(pi is not None and pi.like_count == 99, "图集点赞数解析正确")

print()
print("=" * 62)
print("【6】Cookie 管理器支持小红书")
print("=" * 62)
cm = CookieManager()
from app.core import cookie_manager as _cmmod
check("xiaohongshu" in _cmmod.TUTORIAL, "有小红书获取教程")
check("a1" in _cmmod.REQUIRED_KEYS.get("xiaohongshu", []),
      "必备字段含 a1")
check("web_session" in _cmmod.REQUIRED_KEYS.get("xiaohongshu", []),
      "必备字段含 web_session")
check("小红书" in CookieManager.tutorial("xiaohongshu"), "教程文案是中文小红书教程")

# 校验器已注册
from app.core.verify import VERIFIERS
check("xiaohongshu" in VERIFIERS, "小红书校验器已注册")

print()
print("=" * 62)
print("【7】图集取图 _collect_images —— 四种 URL 字段兜底")
print("=" * 62)

# 实测：image_list 每张图是 dict，url_default 才是真正可用的，
# url 常常是空串，url_pre 是预览小图（有真人露脸的小红书尤其明显）。
# 旧代码只读 url → 拿到空串 → 图片全丢。
pic_card = {
    "note_id": "pic777", "type": "normal", "title": "红烧肉教程",
    "user": {"nickname": "厨子"},
    "interact_info": {"liked_count": "1.2万"},
    "tag_list": [
        {"id": "t1", "name": "红烧肉", "type": "topic"},
        {"id": "t2", "name": "家常菜", "type": "topic"},
    ],
    "image_list": [
        # ① 正常：url_default 可用
        {"url_default": "http://i/1.jpg", "url": "", "url_pre": "http://i/1_p.jpg",
         "width": 1440, "height": 1920},
        # ② url_default 缺失，只剩 url
        {"url": "http://i/2.jpg", "width": 1080, "height": 1440},
        # ③ 只剩 url_pre（预览图）
        {"url_pre": "http://i/3_p.jpg", "width": 720, "height": 960},
        # ④ 都缺，靠 info_list[].url 兜底
        {"info_list": [{"url": "http://i/4.jpg"}], "width": 800, "height": 600},
        # ⑤ 完全取不到 → 应被丢弃，不产生坏图
        {"width": 0, "height": 0},
    ],
}
imgs = X._collect_images(pic_card)
check(len(imgs) == 4, f"五种形态里取到 4 张可用图（实得 {len(imgs)}）")
if imgs:
    check(imgs[0]["url"] == "http://i/1.jpg", "① url_default 优先")
    check(imgs[1]["url"] == "http://i/2.jpg", "② url_default 缺失时回退 url")
    check(imgs[2]["url"] == "http://i/3_p.jpg", "③ 再回退 url_pre")
    check(imgs[3]["url"] == "http://i/4.jpg", "④ 最后回退 info_list[].url")
    check(imgs[0].get("w") == 1440 and imgs[0].get("h") == 1920,
          f"① 尺寸带上了（实得 {imgs[0].get('w')}×{imgs[0].get('h')}）")
    check(all(i.get("url") for i in imgs), "没有任何一张图的 url 是空串")

# 去重：同一张图出现在两个字段里只能收一次
dup = {"image_list": [
    {"url_default": "http://i/same.jpg", "url": "http://i/same.jpg"},
    {"url_default": "http://i/other.jpg"},
]}
check(len(X._collect_images(dup)) == 2, "同一张图重复出现只收一次")

# 纯字符串形态（老接口）
check(X._collect_images({"image_list": ["http://i/s.jpg"]})[0]["url"] == "http://i/s.jpg",
      "image_list 元素是纯字符串也能取到")
check(X._collect_images({}) == [], "没有 image_list 返回空列表不抛异常")

print()
print("=" * 62)
print("【8】话题标签 _collect_tags —— tag_list[].name")
print("=" * 62)
check(X._collect_tags(pic_card) == "红烧肉,家常菜",
      f"tag_list 取到原生话题（实得 {X._collect_tags(pic_card)!r}）")
# 去重保序
duptag = {"tag_list": [
    {"name": "红烧肉"}, {"name": "家常菜"}, {"name": "红烧肉"}, {"name": ""},
]}
check(X._collect_tags(duptag) == "红烧肉,家常菜", "话题去重保序、空名丢弃")
check(X._collect_tags({}) == "", "没有 tag_list 返回空串不抛异常")

print()
print("=" * 62)
print("【9】图集端到端 _card_to_item —— images / tags / cover 都落上")
print("=" * 62)
pi = c._card_to_item(pic_card, note_id="pic777", token="tk")
check(pi is not None and pi.media_kind == "image", "图集识别为 image")
if pi:
    check(pi.image_count == 4, f"image_count = 4（实得 {pi.image_count}）")
    check(pi.cover == "http://i/1.jpg", f"封面取第一张（实得 {pi.cover}）")
    check(pi.tags == "红烧肉,家常菜", f"tags 落库（实得 {pi.tags!r}）")
    parsed = json.loads(pi.images or "[]")
    check(isinstance(parsed, list) and len(parsed) == 4,
          "images 是合法 JSON 数组且 4 张")
    check(pi.stream_url == "", "图集没有视频地址")
    check(pi.like_count == 12000, f"'1.2万' → 12000（实得 {pi.like_count}）")

print()
print("=" * 62)
print("【10】平台标记清洗 —— 文件名 / 关键词的污染源")
print("=" * 62)
dirty = "学着做！#红烧肉[话题]# #超级下饭的[话题]# #一起学做菜[话题]# 展开全文"
cleaned = strip_platform_markup(dirty)
check("[话题]#" not in cleaned and "[话题]" not in cleaned,
      f"话题标记被洗净（实得 {cleaned!r}）")
check("展开全文" not in cleaned, "「展开全文」按钮文案被去掉")
check("红烧肉" in cleaned, "标签里的正文词保留")

# 文件名：实测旧输出「学着做！#红烧肉[话题]# #超级下饭的[话题]#.md」又长又脏
fn = safe_filename(dirty + ".md")
check("[话题]#" not in fn and "[" not in fn,
      f"文件名不含残留标记（实得 {fn!r}）")
check(len(fn) <= 80, f"文件名长度受控（实得 {len(fn)}）")
check(fn and fn != "untitled", "清洗后文件名非空")

# 关键词：实测旧输出含 topic/话题、topic/饭的 这类碎片
# ⚠️ 这里刻意让「红烧肉」重复出现 3 次：extract_keywords 默认 min_count=2，
# 只出现一次的短语抽不出来（这是设计如此，短文案宁缺勿滥）。
kws = extract_keywords(
    "红烧肉[话题]# 家常菜[话题]# 五花肉[话题]# 红烧肉做法 红烧肉教程 展开 全文")
check("话题" not in kws, f"关键词不含「话题」碎片（实得 {kws}）")
check("全文" not in kws and "展开" not in kws, f"不含按钮碎片（实得 {kws}）")
check("红烧肉" in kws, f"重复出现的正文词能抽出来（实得 {kws}）")
check(all(t not in kws for t in ("标记", "的标", "开的")),
      f"不含标记切出的碎片（实得 {kws}）")

print()
print("=" * 62)
print("【11】图文正文清洗 _clean_body")
print("=" * 62)
raw_desc = ("今天做个红烧肉，巨香！\n\n"
            "#红烧肉[话题]# #家常菜[话题]# #下饭菜[话题]# #超级下饭的[话题]#\n"
            "\n"
            "记得少放糖。")
washed = _clean_body(raw_desc)
check("[话题]#" not in washed, f"正文里的 [话题]# 洗净（实得 {washed!r}）")
check("记得少放糖" in washed, "正文有效内容保留")
check("巨香" in washed, "首段正文保留")
# 整行 3 个以上纯标签 → 整行删掉（上方已有「话题」行）
tag_line = "#红烧肉 #家常菜 #下饭菜"
check(_clean_body("开头\n\n" + tag_line + "\n\n结尾").strip()
      == "开头\n\n结尾", "纯标签行被整行删除")
check(_clean_body("") == "", "空正文返回空串")

print()
print("=" * 62)
print("【12】图文渲染成 Markdown —— 一视同仁的落地形态")
print("=" * 62)
img_item = Item(
    platform="xiaohongshu", item_id="pic777", title="红烧肉教程",
    author="厨子", status="done", media_kind="image", duration=0,
    desc="今天做个红烧肉，巨香！",
    images=json.dumps([
        {"url": "http://i/1.jpg", "w": 1440, "h": 1920},
        {"url": "http://i/2.jpg", "w": 1080, "h": 1440},
    ], ensure_ascii=False),
    image_count=2,
    tags="红烧肉,家常菜",
)
opts = OutputOptions(mode=OutputOptions.MODE_OBSIDIAN)
md = render_item_markdown(img_item, "", None, opts)

check("## 图片（2 张）" in md, "有「图片（2 张）」段")
check("### 图 1" in md and "### 图 2" in md, "逐张列出 图1 / 图2")
check("![图 1](http://i/1.jpg)" in md, "图片用 Markdown 图片语法")
check("<sub>1440×1920</sub>" in md, "图片带宽高标注")
check("## 正文" in md, "图文正文段标题是「正文」而不是「原简介」")
check("**话题**：#红烧肉  #家常菜" in md, f"有话题行（实得片段见下）")
check("图文（2 张图）" in md, "元信息表显示「图文（2 张图）」")
# 图文没有时长：frontmatter 里 duration 整项被省略（空值不写），
# 只留一个 duration_sec: 0，避免 Obsidian 属性面板显示一个空字符串。
check("\nduration:" not in md, "图文 frontmatter 不写空的 duration 项")
check("duration_sec: 0" in md, "图文 frontmatter 的 duration_sec 为 0")
check("image_count: 2" in md, "frontmatter 带 image_count")
check("source_tags:" in md, "frontmatter 带 source_tags")
check("type/image" in md, "Obsidian 标签里有 type/image")

# 视频作品仍走封面单张，不该出现「图片（N 张）」段
vid_item = Item(
    platform="xiaohongshu", item_id="vid1", title="视频笔记",
    author="UP", status="done", media_kind="video", duration=30,
    transcript="这是视频逐字稿内容。",
    images=json.dumps([{"url": "http://i/c.jpg", "w": 1080, "h": 1440}],
                      ensure_ascii=False),
    image_count=1,
)
md_v = render_item_markdown(vid_item, "[00:00 -> 00:30] 这是视频逐字稿内容。",
                            None, opts)
check("## 图片（1 张）" not in md_v, "视频不出现「图片（N 张）」段")
check("## 封面" in md_v, "视频封面单独一段")
check("## 纯文本" in md_v, "视频有纯文本段")
check("## 逐字稿（带时间戳）" in md_v, "视频有时间戳段")
check("| 类型 | 视频 |" in md_v, "视频的类型行是「视频」")

# 图文无正文时给提示，而不是留个空文档
empty_img = Item(platform="xiaohongshu", item_id="e1", title="空图文",
                 status="done", media_kind="image", images="[]", image_count=0,
                 desc="")
md_e = render_item_markdown(empty_img, "", None, opts)
check("[!info]" in md_e, "图文无正文时给出 [!info] 提示")

# 图文没有逐字稿：不该出现「（本次未抓取评论，或抓取失败）」以外的转录空段
check("逐字稿（带时间戳）" not in md, "图文没有时间戳逐字稿段")

print()
print("=" * 62)
print("【13】HTTP 461 的错误文案 —— 真实原因在响应体里，不能一律说账号异常")
print("=" * 62)


class _FakeResp:
    """只给 _as_json 需要的两个东西：状态码 + 文本。"""

    def __init__(self, code: int, text: str) -> None:
        self.status_code = code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _err_of(code: int, text: str) -> str:
    try:
        X._as_json(_FakeResp(code, text), "/test")
        return ""          # 没抛异常（不该发生在这几个用例里）
    except Exception as exc:  # noqa: BLE001
        return str(exc)


# ⚠️ 实测（2026-09）坑：小红书用 461 表示「请求被挑战/限流」，
# 但**响应体仍然是合法 JSON**，真实原因在 body 的 code/msg 里。
# 旧代码一看到 461 就抛「账号存在异常」，用户会跑去重导 cookie，白折腾。
e_soft = _err_of(461, '{"code":0,"success":true,"data":{}}')
check("限流" in e_soft or "频繁" in e_soft,
      f"461+空data → 说限流（实得 {e_soft[:40]}…）")
check("账号存在异常" not in e_soft, "不再误导成「账号异常」")
# 文案里出现「重新导出 cookie」这几个字没关系（我们正是在说**不需要**），
# 关键是不能出现「请重新导出」这种引导用户去做的措辞。
check("请重新导出" not in e_soft and "重新从浏览器导出" not in e_soft,
      "不再引导用户去重新导出 cookie")

e_biz = _err_of(461, '{"code":300031,"msg":"当前笔记暂时无法浏览","data":{}}')
check("300031" in e_biz and "当前笔记暂时无法浏览" in e_biz,
      f"461+业务码 → 报真实业务码（实得 {e_biz[:60]}）")

check("406" in _err_of(406, '{"code":0}'), "406 仍是签名/过密的文案")
# 正常响应不能被拦
check(_err_of(200, '{"code":0,"data":{"items":[]}}') == "",
      "HTTP 200 正常放行")
# 461 但响应体不是 JSON：不能崩，要给兜底文案
check("461" in _err_of(461, "<html>error</html>"),
      "461 且非 JSON 时有兜底文案、不抛解析异常")

print()
print("=" * 62)
print("【14】code -100 会话失效 —— 别把它报成「没搜到」")
print("=" * 62)
# ⚠️ -100 的坑在于它**不像失败**：HTTP 200，只是 data 空。
# 旧代码在 fetch_topic 里 `if data.get("code") != 0: break`，
# 于是返回空列表 → 界面显示「搜索完成，共 0 条」→
# 用户以为这个主题没内容，实际是 cookie 不能用了。
check(X._session_dead({"code": -100, "data": {}}), "识别 -100 为会话失效")
check(not X._session_dead({"code": 0, "data": {}}), "code=0 不算失效")
check(not X._session_dead({"code": 300031}), "业务错误码不算失效")
check(not X._session_dead({}), "空响应不算失效")

# 搜索遇到 -100 必须抛出带「重新导出 cookie」的明确指引
c5 = X(cookie="a1=x; web_session=y")
c5._request_json = lambda *a, **k: {   # type: ignore[method-assign]
    "code": -100, "success": True, "msg": "登录已过期", "data": {}}
try:
    c5.fetch_topic("红烧肉", 5)
    check(False, "搜索遇到 -100 应当抛出错误")
except RuntimeError as exc:
    msg = str(exc)
    check("登录态已失效" in msg, f"明确说登录态失效（实得 {msg[:40]}）")
    check("重新" in msg and "cookie" in msg,
          f"给出「重新导出 cookie」的指引（实得 {msg[:40]}）")
# 主页同理
try:
    c5.fetch_creator("somebody", 5)
    check(False, "主页遇到 -100 应当抛出错误")
except RuntimeError as exc:
    check("登录态已失效" in str(exc), "主页也识别 -100")

print()
print("=" * 62)
print("【15】限流退避重试 —— 只对 461/406 重试，其他错误立即抛")
print("=" * 62)
c2 = X(cookie="a1=x; web_session=y")
# 可重试的（限流类）
check(c2._is_retryable(RuntimeError("HTTP 461 请求被拒")), "461 可重试")
check(c2._is_retryable(RuntimeError("接口 406 请求过密")), "406 可重试")
check(c2._is_retryable(RuntimeError("触发了限流")), "「限流」字样可重试")
# 不可重试的（重试多少次都一样，别浪费用户时间）
check(not c2._is_retryable(RuntimeError("-100 登录已过期")), "登录过期不重试")
check(not c2._is_retryable(RuntimeError("没有识别到任何链接")), "参数错误不重试")
check(not c2._is_retryable(ValueError("bad json")), "解析错误不重试")

# 重试次数用尽后要抛出最后一个错误，而不是静默返回
calls = {"n": 0}


def _always_461(*a, **k):
    calls["n"] += 1
    raise RuntimeError("HTTP 461 请求被拒")


c3 = X(cookie="a1=x; web_session=y")
c3._post_json = _always_461      # type: ignore[method-assign]
c3._sleep = lambda s=0.8: None   # type: ignore[method-assign]
try:
    c3._request_json("/x", payload={}, tries=3)
    check(False, "重试用尽后应当抛出异常")
except RuntimeError as exc:
    check("461" in str(exc), f"重试用尽后抛出原始错误（实得 {str(exc)[:40]}）")
    check(calls["n"] == 3, f"确实重试了 3 次（实得 {calls['n']}）")

# 不可重试的错误应当一次就抛，不要白等
calls2 = {"n": 0}


def _always_login(*a, **k):
    calls2["n"] += 1
    raise RuntimeError("-100 登录已过期")


c4 = X(cookie="a1=x; web_session=y")
c4._post_json = _always_login      # type: ignore[method-assign]
c4._sleep = lambda s=0.8: None     # type: ignore[method-assign]
try:
    c4._request_json("/x", payload={}, tries=3)
    check(False, "不可重试的错误也应当抛出")
except RuntimeError:
    check(calls2["n"] == 1, f"不可重试的错误只请求 1 次（实得 {calls2['n']}）")

print()
print("=" * 62)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 小红书解析容错全部通过")
print("=" * 62)
