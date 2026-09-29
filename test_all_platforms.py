"""四平台采集冒烟测试：B站 / 抖音 / Telegram / 本地文件。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.core.cookie_manager import CookieManager
from app.core.models import Item, Task, MODE_CREATOR, MODE_TOPIC
from app.pipeline import Pipeline

PROXY = "http://127.0.0.1:7897"
cm = CookieManager()
pipe = Pipeline("data/mediaharvest.db", "data/media",
                progress=lambda m, c, t: None)


def show(tag: str, items: list[Item]) -> None:
    print(f"  → {tag}: {len(items)} 条")
    for it in items[:3]:
        print(f"     [{it.platform}] {it.item_id} | {it.title[:38]} | {it.author}")


print("=== 1. B站 博主主页 ===")
t = Task(name="bili", mode=MODE_CREATOR, platform="bilibili",
         query="https://space.bilibili.com/946974", max_count=3)
try:
    show("bilibili/creator", pipe.collect(t, cookie=cm.to_header("bilibili")))
except Exception as exc:
    print("  失败:", str(exc)[:150])

print("\n=== 2. B站 主题搜索 ===")
t = Task(name="bili-topic", mode=MODE_TOPIC, platform="bilibili",
         query="AI 工具", max_count=3)
try:
    show("bilibili/topic", pipe.collect(t, cookie=cm.to_header("bilibili")))
except Exception as exc:
    print("  失败:", str(exc)[:150])

print("\n=== 3. 抖音 博主主页 ===")
from app.crawler.douyin import DouyinCrawler
sec = DouyinCrawler(cookie=cm.to_header("douyin")).fetch_sec_uid_from_homepage()
t = Task(name="dy", mode=MODE_CREATOR, platform="douyin",
         query=f"https://www.douyin.com/user/{sec}", max_count=3)
try:
    show("douyin/creator", pipe.collect(t, cookie=cm.to_header("douyin")))
except Exception as exc:
    print("  失败:", str(exc)[:150])

print("\n=== 4. 抖音 主题搜索（预期被风控拦截） ===")
t = Task(name="dy-t", mode=MODE_TOPIC, platform="douyin", query="AI", max_count=3)
try:
    show("douyin/topic", pipe.collect(t, cookie=cm.to_header("douyin")))
except Exception as exc:
    print("  如预期失败:", str(exc)[:110].replace("\n", " / "))

print("\n=== 5. Telegram 频道 ===")
t = Task(name="tg", mode=MODE_CREATOR, platform="telegram",
         query="@durov", max_count=3)
try:
    show("telegram/creator", pipe.collect(t, proxy=PROXY))
except Exception as exc:
    print("  失败:", str(exc)[:150])

print("\n=== 6. 本地文件 ===")
# 用 media 目录本身测试（可能为空，那就造一个）
media = Path("data/media")
media.mkdir(parents=True, exist_ok=True)
t = Task(name="local", mode=MODE_CREATOR, platform="local",
         query=str(media.resolve()), max_count=5)
try:
    show("local", pipe.collect(t))
except Exception as exc:
    print("  失败:", str(exc)[:150])

print("\n=== 7. 数据库回读 ===")
for p in ["bilibili", "douyin", "telegram", "local"]:
    rows = pipe.list_items(p)
    print(f"  {p}: {len(rows)} 条入库")
