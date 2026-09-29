"""B站链路实测：cookie 校验 -> UP主主页抓取 -> 评论抓取。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.core.cookie_manager import CookieManager
from app.core.verify import verify
from app.crawler.bilibili import BilibiliCrawler

cm = CookieManager()

print("=== 1. cookie 校验 ===")
t0 = time.time()
ok, msg = verify(cm, "bilibili")
print(f"  B站: {ok} | {msg}  ({time.time()-t0:.2f}s)")

okd, msgd = verify(cm, "douyin")
print(f"  抖音: {okd} | {msgd}")

if not ok:
    sys.exit("B站 cookie 不通，后续跳过")

print("\n=== 2. UP主主页抓取 ===")
c = BilibiliCrawler(cookie=cm.to_header("bilibili"))
print("  登录检查:", c.check_login())

for mid in ["946974", "546195"]:
    try:
        items = c.fetch_creator(mid, max_count=5)
        print(f"\n  mid={mid} 抓到 {len(items)} 条")
        for it in items[:5]:
            print(f"    - [{it.item_id}] {it.title[:40]} | 播放 {it.view_count} | {it.publish_time}")
        if items:
            break
    except Exception as exc:
        print(f"  mid={mid} 失败: {exc}")

print("\n=== 3. 主题搜索 ===")
try:
    items = c.fetch_topic("AI 工具", max_count=3)
    print(f"  搜到 {len(items)} 条")
    for it in items:
        print(f"    - {it.title[:40]} | UP: {it.author}")
except Exception as exc:
    print(f"  搜索失败: {exc}")

print("\n=== 4. 评论抓取 ===")
try:
    # 用一个已知 aid 测试（取上面抓到的第一条转换）
    if items:
        aid = c.resolve_aid(items[0].item_id)
        print(f"  BV={items[0].item_id} -> aid={aid}")
        comments = c.fetch_comments(items[0].item_id, max_count=5)
        print(f"  aid={aid} 评论 {len(comments)} 条")
        for cc in comments[:3]:
            print(f"    - {cc.user}: {cc.content[:40]}")
except Exception as exc:
    print(f"  评论失败: {exc}")
