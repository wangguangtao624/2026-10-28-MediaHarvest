"""评论抓取端到端回归：B站 + 抖音，走真实 pipeline（含入库与评论数回填）。

抖音评论必须带 a_bogus 签名，不带签名时接口返回 HTTP 200 + 空 body，
本用例就是用来防这个回归的。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.cookie_manager import CookieManager          # noqa: E402
from app.core.models import Task, MODE_CREATOR, MODE_MANUAL  # noqa: E402
from app.pipeline import Pipeline                          # noqa: E402

TMP = ROOT / ".tmp" / "comments_test"
TMP.mkdir(parents=True, exist_ok=True)


def fresh(name: str) -> Pipeline:
    db = TMP / f"{name}.db"
    if db.exists():
        db.unlink()
    return Pipeline(db, TMP / name / "media")


def case_bilibili(cm: CookieManager) -> tuple[bool, str]:
    pipe = fresh("bili")
    task = Task(name="bili", mode=MODE_CREATOR, platform="bilibili",
                query="946974", max_count=2, comment_limit=20,
                fetch_comments=True)
    items = pipe.collect(task, cookie=cm.to_header("bilibili"))
    if not items:
        return False, "没采集到作品"
    total = 0
    for it in items:
        cs = pipe.comments_of(it.platform, it.item_id)
        total += len(cs)
        if cs:
            top = cs[0]
            print(f"    {it.item_id} ← {len(cs)} 条，最高赞："
                  f"{top['user']}「{str(top['content'])[:26]}」")
    if not total:
        return False, "一条评论都没抓到"
    return True, f"{len(items)} 个作品共 {total} 条评论"


def case_douyin(cm: CookieManager) -> tuple[bool, str]:
    pipe = fresh("dy")
    links = [
        "https://www.douyin.com/video/7685043849751694646",
        "https://www.douyin.com/video/7690025870215384354",
    ]
    task = Task(name="dy", mode=MODE_MANUAL, platform="douyin",
                query="", max_count=20, comment_limit=20, fetch_comments=True)
    items = pipe.collect(task, cookie=cm.to_header("douyin"), urls=links)
    ok_items = [i for i in items if i.item_id.isdigit()]
    if not ok_items:
        return False, "抖音作品没解析出来"
    total = 0
    for it in ok_items:
        cs = pipe.comments_of(it.platform, it.item_id)
        total += len(cs)
        if cs:
            print(f"    {it.item_id} ← {len(cs)} 条，最高赞："
                  f"{cs[0]['user']}「{str(cs[0]['content'])[:26]}」")
    if not total:
        return False, ("一条评论都没抓到（抖音评论接口变了吧？先用 "
                       "test_probe_abogus 的思路确认签名是否还有效）")
    return True, f"{len(ok_items)} 个作品共 {total} 条评论"


def main() -> int:
    cm = CookieManager()
    print("=" * 58)
    results = []
    for name, fn in (("B站", case_bilibili), ("抖音", case_douyin)):
        print(f"\n[{name} 评论]")
        try:
            ok, msg = fn(cm)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"异常：{str(exc)[:200]}"
        print(f"  {'✅' if ok else '❌'} {msg}")
        results.append(ok)
    print()
    print("=" * 58)
    if all(results):
        print("✅ 两平台评论抓取全部通过")
        return 0
    print("❌ 有平台未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
