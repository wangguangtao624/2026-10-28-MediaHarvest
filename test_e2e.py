"""端到端：B站采集 -> 下载 -> 转录 -> 导出。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.core.cookie_manager import CookieManager
from app.core.models import Task, MODE_CREATOR
from app.pipeline import Pipeline

cm = CookieManager()
OUT = str((Path(__file__).parent / "out" / "e2e").resolve())

pipe = Pipeline("data/mediaharvest.db", "data/media",
                progress=lambda m, c, t: print(f"  [{c}/{t}] {m}"))

task = Task(name="e2e", mode=MODE_CREATOR, platform="bilibili",
            query="https://space.bilibili.com/946974", max_count=1,
            fetch_comments=True, output_dir=OUT)

print("=== 1. 采集 ===")
items = pipe.collect(task, cookie=cm.to_header("bilibili"))
print("  采集:", len(items))
if not items:
    sys.exit("采集为空，退出")

it = items[0]
print(f"  目标: {it.title} ({it.url})")

print("\n=== 2. 下载+转录 ===")
t0 = time.time()
pipe.process(items, task, cookie=cm.to_header("bilibili"),
             model_size="tiny", device="auto")
print(f"  耗时 {time.time()-t0:.1f}s")
print("  状态:", it.status)
print("  本地文件:", it.local_path)
print("  引擎:", it.asr_engine)
print("  逐字稿前 120 字:", (it.transcript or "")[:120])
print("  错误:", it.error[:200] if it.error else "(无)")

print("\n=== 3. 导出 ===")
paths = pipe.export(items, task)
for k, v in paths.items():
    print(f"  {k}: {v}")

print("\n=== 4. 评论 ===")
cs = pipe.db.get_comments("bilibili", it.item_id)
print(f"  评论 {len(cs)} 条")
for c in cs[:3]:
    print(f"    - {c.get('user')}: {str(c.get('content'))[:40]}")
