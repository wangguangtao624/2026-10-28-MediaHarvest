"""MediaHarvest 命令行入口。

示例：
    # B站 博主主页（走已保存的 cookie）
    python run.py --platform bilibili --mode creator --query 946974 --output D:/out --max 10

    # 抖音 博主主页
    python run.py --platform douyin --mode creator --query "https://www.douyin.com/user/MS4w..." --output D:/out

    # Telegram 频道（需要代理）
    python run.py --platform telegram --mode creator --query @durov --proxy http://127.0.0.1:7897 --output D:/out

    # 本地文件夹（跳过下载，直接转录）
    python run.py --platform local --mode creator --query "D:/videos" --output D:/out

    # 手动链接
    python run.py --mode manual --urls "https://www.bilibili.com/video/BVxxx" --output D:/out

    # 生成 Obsidian 格式（含 frontmatter 与 #标签）
    python run.py --platform bilibili --mode creator --query 946974 --output D:/out \
        --content obsidian

    # 抓评论（每条 20 条）并生成纯文本逐字稿
    python run.py --platform douyin --mode manual --urls "<链接>" --output D:/out \
        --content plain --comments --comment-limit 20

    # 只检查 cookie 通不通
    python run.py --platform douyin --check-login

Cookie 用 EditThisCookie 导出保存：
    python run.py --platform douyin --save-cookie "<粘贴的 JSON 或 cookie 字符串>"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.core.cookie_manager import CookieManager  # noqa: E402
from app.core.models import (  # noqa: E402
    OutputOptions, Task, MODE_CREATOR, MODE_TOPIC, MODE_MANUAL,
)
from app.core.verify import verify  # noqa: E402
from app.crawler.registry import PLATFORMS, get  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402


def on_progress(msg: str, cur: int, total: int) -> None:
    print(f"  ({cur}/{total}) {msg}" if total else f"  {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="MediaHarvest — 视频信息采集与逐字稿工作台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="支持平台：" + ", ".join(f"{k}({v.label})" for k, v in PLATFORMS.items()))
    ap.add_argument("--platform", default="bilibili", choices=list(PLATFORMS))
    ap.add_argument("--mode", choices=[MODE_CREATOR, MODE_TOPIC, MODE_MANUAL],
                    default=MODE_MANUAL)
    ap.add_argument("--query", default="", help="博主主页/UID/频道名/文件夹路径")
    ap.add_argument("--urls", nargs="*", default=[], help="manual 模式的视频链接")
    ap.add_argument("--output", default="", help="结果导出目录（可任意指定）")
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--model", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"])
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--proxy", default="", help="代理，Telegram 必填，如 http://127.0.0.1:7897")
    ap.add_argument("--comments", action="store_true", help="同时抓取评论")
    ap.add_argument("--comment-limit", type=int, default=20,
                    help="每个作品抓多少条评论（默认 20）")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--no-transcribe", action="store_true")
    # --- 性能 ---
    ap.add_argument("--compute-type", default="auto",
                    choices=["auto", "float16", "int8_float16", "int8", "float32"],
                    help="计算精度；图灵及更早显卡用 float16 最快（实测快 1.5x）")
    ap.add_argument("--beam", type=int, default=5, choices=[1, 5],
                    help="1=贪心最快（实测再快 30%%），5=默认更准")
    ap.add_argument("--cpu-threads", type=int, default=0,
                    help="CPU 线程数；GPU 模式下实测 12 比默认快 20%%，0=自动")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="并发转录实例数（受显存限制，small+float16 建议 2）")
    ap.add_argument("--pipeline", default="overlap", choices=["overlap", "serial"],
                    help="overlap=下载与转录并行（快）；serial=逐条（稳）")
    # --- 内容输出（三选一，对应界面上的「生成内容」）---
    ap.add_argument("--content", default="timestamped",
                    choices=["timestamped", "plain", "obsidian"],
                    help="生成内容的模式：timestamped=带时间戳逐字稿(默认) / "
                         "plain=纯文本逐字稿 / obsidian=Obsidian 格式(含 frontmatter+标签)")
    ap.add_argument("--no-comment-file", action="store_true",
                    help="评论不另存为独立文件")
    ap.add_argument("--comment-sort", default="like", choices=["like", "time"],
                    help="评论排序：like=按点赞(默认) / time=按时间")
    ap.add_argument("--check-login", action="store_true", help="只检测连通性")
    ap.add_argument("--save-cookie", metavar="TEXT", help="保存 cookie（EditThisCookie 导出的 JSON）")
    args = ap.parse_args()

    cm = CookieManager()
    spec = get(args.platform)

    # ---- cookie 子命令 ----
    if args.save_cookie:
        try:
            n = cm.save(args.platform, args.save_cookie)
        except Exception as exc:  # noqa: BLE001
            print(f"保存失败：{exc}")
            return 2
        print(f"已保存 {n} 条 {spec.label} cookie")
        ok, msg = verify(cm, args.platform)
        print(f"连通性：{'✅' if ok else '❌'} {msg}")
        return 0 if ok else 1

    # ---- 连通性检查 ----
    if args.check_login:
        if spec.need_cookie:
            ok, msg = verify(cm, args.platform)
        elif spec.need_proxy:
            ok, msg = get(args.platform).factory(proxy=args.proxy).check_login()
        else:
            ok, msg = True, "该平台无需登录"
        print(f"{spec.label} 连通性：{'✅' if ok else '❌'} {msg}")
        return 0 if ok else 1

    # ---- 参数校验 ----
    if not args.output:
        print("错误：请用 --output 指定导出目录")
        return 2
    cookie = cm.to_header(args.platform) if spec.need_cookie else ""
    if spec.need_proxy and not args.proxy:
        print(f"错误：{spec.label} 需要代理，请用 --proxy http://127.0.0.1:7897")
        return 2
    if args.mode == MODE_MANUAL:
        if not args.urls:
            print("错误：manual 模式需要 --urls 提供至少一条链接")
            return 2
    else:
        if not args.query:
            print("错误：请用 --query 指定采集目标")
            return 2
        if spec.need_cookie:
            ok, msg = cm.quick_check(args.platform)
            if not ok:
                print(f"错误：{spec.label} cookie 不可用 — {msg}")
                print(f"请用：python run.py --platform {args.platform} --save-cookie \"<粘贴内容>\"")
                return 2

    # ⚠️ 内容输出只剩三选一：其余段落开关由 OutputOptions.apply_mode() 按
    # mode 自动推导（详见 models.py 的说明）。关键词标签在所有模式下强制开启。
    opts = OutputOptions(
        mode=args.content,
        out_comments_file=not args.no_comment_file,
        comment_sort=args.comment_sort,
    )

    task = Task(
        name=args.query or "manual",
        mode=args.mode,
        platform=args.platform,
        query=args.query,
        max_count=args.max,
        comment_limit=args.comment_limit,
        fetch_comments=args.comments,
        do_download=not args.no_download,
        do_transcribe=not args.no_transcribe,
        output_dir=args.output,
        model_size=args.model,
        device=args.device,
        compute_type=args.compute_type,
        beam_size=args.beam,
        cpu_threads=args.cpu_threads,
        concurrency=args.concurrency,
        pipeline_mode=args.pipeline,
        output_opts=opts,
    )
    pipe = Pipeline(ROOT / "data" / "mediaharvest.db",
                    ROOT / "data" / "media", progress=on_progress)

    print("== 1. 采集 ==")
    try:
        items = pipe.collect(task, cookie=cookie, proxy=args.proxy, urls=args.urls)
    except Exception as exc:  # noqa: BLE001
        print(f"采集失败：{exc}")
        return 1
    if not items:
        print("未采集到任何作品，结束。")
        return 1
    print(f"采集到 {len(items)} 条：")
    for i, it in enumerate(items[:10], 1):
        print(f"  {i}. {it.title[:45]} | {it.author} | {it.duration}s")

    for it in items:
        it.selected = True

    print("\n== 2. 下载与转录 ==")
    print(f"  模式 {task.pipeline_mode} / 模型 {task.model_size} / "
          f"精度 {task.compute_type} / beam {task.beam_size} / "
          f"并发 {task.concurrency}")
    items = pipe.process(items, task, cookie=cookie, proxy=args.proxy,
                         model_size=args.model, device=args.device)

    print("\n== 3. 导出 ==")
    paths = pipe.export(items, task)
    print(f"导出完成 → {paths.get('root')}")
    for k in ("csv", "json", "summary"):
        if paths.get(k):
            print(f"  {k}: {paths[k]}")
    print(f"\n  内容模式：{task.output_opts.mode_label()}")
    if task.output_opts.mode == OutputOptions.MODE_OBSIDIAN:
        print("  提示：把整个输出目录拖进 Obsidian 库即可按标签分类，"
              "frontmatter 的属性面板可直接筛选。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
