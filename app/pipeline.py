"""流水线编排：采集 → 勾选 → 下载 → 转录 → 导出。

GUI 和命令行共用这一层，保证行为一致。

运行控制（暂停/前进/后退/选择）通过 `ProcessControl` 注入：GUI 传真实对象，
CLI 不传则用 NullControl，行为与以前完全一致。

两种执行模式
------------
serial（默认，最稳）
    逐条走：下载 → 转录 → 下一条。实现简单，暂停粒度清晰。

overlap（重叠，快）
    下载与转录**重叠**：下载线程先抢跑几条，转录线程从队列里取已完成下载的
    文件开始转。下载是 IO/网络瓶颈、转录是 GPU 瓶颈，两者互不抢占，
    实测能把「等下载」的时间基本吃掉。
    并发度由 `task.concurrency` 控制转录实例数（多实例并行吃满 GPU）。

⚠️ 并发下的一个硬约束：CUDA 上下文只能在主线程初始化（见 gui.py）。
   多个转录实例共享同一个进程的 CUDA 上下文是安全的；但**每个实例各自
   持有一份模型权重**，所以并发数受显存限制，由 suggest_concurrency() 定档。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .core.control import (
    ACTION_BACK, ACTION_JUMP, ACTION_NONE, ACTION_SKIP, ACTION_STOP,
    NullControl, ProcessControl,
)
from .core.db import Database
from .core.models import (
    Comment, Item, OutputOptions, Task,
    MODE_CREATOR, MODE_TOPIC, MODE_MANUAL,
    STATUS_PENDING, STATUS_QUEUED, STATUS_DOWNLOADING, STATUS_DOWNLOADED,
    STATUS_TRANSCRIBING, STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED,
)
from .crawler.registry import build, get, normalize_query
from .core.urlutil import clean_url, extract_urls
from .downloader import DownloadAborted, Downloader
from .asr.engine import FasterWhisperEngine, MODEL_APPROX_GB, format_timestamped
from .export.writer import Exporter

ProgressCb = Callable[[str, int, int], None]   # (消息, 当前, 总数)
# 步骤内进度：(已完成字节, 总字节)。总字节为 0 表示未知长度
ByteCb = Callable[[int, int], None]
# 0~1 的完成度回调
FracCb = Callable[[float], None]
# 条目状态变化：(下标0起, 条目, 状态)
ItemCb = Callable[[int, Item, str], None]

# 各平台直连下载时要带的 Referer。
# 这些 CDN 都有防盗链：Referer 不对就返回一个小文件或 403，
# 而不是干脆报错 —— 表现是「下载成功但文件只有几 KB / 打不开」。
# ⚠️ 以前这里没做映射，全都吃 downloader 里写死的 douyin 默认值，
# 结果小红书/哔哩哔哩的直链要么被拦、要么拿到错误内容。
PLATFORM_REFERER = {
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "bilibili": "https://www.bilibili.com/",
    "telegram": "https://web.telegram.org/",
}


class Pipeline:
    def __init__(self, db_path: str | Path, media_dir: str | Path,
                 progress: ProgressCb | None = None,
                 on_item: ItemCb | None = None) -> None:
        self.db = Database(db_path)
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.progress = progress or (lambda msg, cur, total: None)
        self.on_item = on_item or (lambda idx, item, status: None)
        # 本次运行下载过的中间文件：item_id → 路径（「文件」页用）
        self.media_seen: dict[str, str] = {}

    def set_media_dir(self, path: str | Path) -> None:
        """切换中间文件目录（用户在界面上改了音视频输出目录时调用）。"""
        if not path:
            return
        p = Path(path)
        if p.resolve() == self.media_dir.resolve():
            return
        self.media_dir = p
        self.media_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 进度 ----------
    def _prog(self, i: int, total: int, frac: float, msg: str) -> None:
        """整体进度条：把「第 i 条 / 共 total 条」和「条内完成度 frac」合成一格。"""
        frac = 0.0 if frac < 0 else min(1.0, max(0.0, frac))
        self.progress(msg, int((i - 1 + frac) * 100), max(1, total) * 100)

    def _byte_cb(self, i: int, total: int, label: str) -> ByteCb:
        """把字节进度转成进度条回调。"""
        def cb(got: int, size: int) -> None:
            mb, smb = got / 1048576, size / 1048576
            if size > 0:
                frac = got / size
                self._prog(i, total, frac, f"[{i}/{total}] {label} "
                                           f"{mb:.1f}/{smb:.1f} MB（{frac*100:.0f}%）")
            else:
                self._prog(i, total, 0.0, f"[{i}/{total}] {label} 已下载 {mb:.1f} MB")
        return cb

    # ---------- 1. 采集 ----------
    def collect(self, task: Task, cookie: str = "", proxy: str = "",
                urls: list[str] | None = None,
                control: ProcessControl | None = None) -> list[Item]:
        ctl = control or NullControl()
        spec = get(task.platform)
        crawler = build(task.platform, cookie=cookie, proxy=proxy)
        # 用户粘的通常是一整段「分享文案」（标题+话题+链接+引导语），
        # 每行最多取一个真链接，否则整段话会被当 URL 请求
        urls = extract_urls("\n".join(urls or []))
        query = normalize_query(task.platform, clean_url(task.query))

        if task.platform == "local":
            self.progress(f"扫描目录 {query} …", 0, 100)
            items = build("local").fetch_creator(query, task.max_count)
        elif task.mode == MODE_MANUAL:
            if not urls:
                raise ValueError(
                    "没有识别到任何链接。\n"
                    "可以粘贴纯链接，也可以直接粘贴 App 复制出来的一整段分享文案"
                    "（会自动抠出里面的 http 链接），一行一条。")
            if task.platform == "douyin" and hasattr(crawler, "fetch_manual"):
                items = crawler.fetch_manual(urls or [])
            else:
                items = self._collect_manual(urls or [], task.platform, cookie, proxy)
        elif task.mode == MODE_CREATOR:
            self.progress(f"抓取 {spec.label} {query} 的作品列表…", 0, 100)
            items = crawler.fetch_creator(query, task.max_count)
            self.progress(f"列表获取完成，共 {len(items)} 条", 50, 100)
        elif task.mode == MODE_TOPIC:
            self.progress(f"按主题「{query}」搜索…", 0, 100)
            items = crawler.fetch_topic(query, task.max_count)
            self.progress(f"搜索完成，共 {len(items)} 条", 50, 100)
        else:
            raise ValueError(f"{spec.label} 不支持模式：{task.mode}")

        if not items:
            return []

        # ⚠️ 小红书必须补一次详情，否则图文作品导出是空壳。
        # 原因：搜索/主页接口**只返回标题和统计**，正文 desc、图片列表、
        # 话题标签只在详情接口里。实测搜「红烧肉」13/20 条是图文，
        # 列表接口的 desc 全是空串 —— 不补详情就没法生成 Markdown。
        if task.platform == "xiaohongshu" and hasattr(crawler, "enrich_items"):
            n_img = sum(1 for i in items if i.media_kind == "image")

            def _on_enrich(idx: int, total: int, name: str) -> None:
                self.progress(
                    f"补全详情 {idx}/{total}：{name[:24]}", 
                    int(idx * 100 / max(total, 1)), 100)

            self.progress(
                f"共 {len(items)} 条（{n_img} 条图文），正在补全正文与图片…",
                0, 100)
            try:
                crawler.enrich_items(
                    items, on_progress=_on_enrich,
                    should_stop=lambda: ctl.stopped)
            except Exception as exc:  # noqa: BLE001
                # 补详情失败不能让整批采集失败，列表数据照样能用
                self.progress(f"补全详情中断（不影响已获取的列表）：{str(exc)[:80]}",
                              100, 100)
            else:
                # 有多少条没补到正文要说清楚：静默少数据会让用户以为
                # 「这条图文本来就没有正文」，实际多半是被限流了。
                nf = int(getattr(crawler, "last_enrich_failed", 0) or 0)
                if nf:
                    why = str(getattr(crawler, "last_enrich_error", "") or "")
                    hint = f"（{why[:40]}）" if why else ""
                    self.progress(
                        f"有 {nf}/{len(items)} 条没取到正文{hint}。"
                        f"可稍后重试采集，或勾选『保留音视频』后重新跑一次。",
                        100, 100)

        self.db.upsert_items(items)
        self.db.save_task(task)

        # 评论（可选，失败不影响主流程）
        if task.fetch_comments:
            self._collect_comments(crawler, items, task.comment_limit, ctl)

        self.progress(f"采集完成，共 {len(items)} 条，等待勾选", 100, 100)
        return items

    def _collect_manual(self, urls: list[str], platform: str,
                        cookie: str, proxy: str) -> list[Item]:
        """手动链接模式：用 yt-dlp 逐个解析，多数平台不需要 cookie。"""
        import yt_dlp

        opts: dict[str, object] = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "proxy": proxy or "", "skip_download": True,
            "playlistend": 5,
            "extract_flat": False,
        }
        if cookie:
            opts["cookiefile"] = self._cookie_file(cookie)

        items: list[Item] = []
        n = len(urls)
        for i, url in enumerate(urls, 1):
            self._prog(i, n, 0.0, f"解析链接 {i}/{n}（跟进重定向…）")
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info is None:
                        continue
                    if info.get("_type") == "playlist":
                        entries = [e for e in (info.get("entries") or []) if e]
                        for e in entries[:20]:
                            items.append(self._info_to_item(e, platform))
                        continue
                    items.append(self._info_to_item(info, platform))
            except Exception as exc:  # noqa: BLE001
                items.append(Item(
                    platform=platform, item_id=f"manual_{i}",
                    title=f"（解析失败）{url[:50]}", url=url,
                    status=STATUS_FAILED, error=str(exc)[:300],
                ))
            self._prog(i, n, 1.0, f"解析完成 {i}/{n}")
            time.sleep(0.3)
        return items

    def _cookie_file(self, cookie: str) -> str:
        path = self.media_dir / "_cookies.txt"
        lines = ["# Netscape HTTP Cookie File"]
        for part in cookie.replace("\n", ";").split(";"):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            lines.append(f".douyin.com\tTRUE\t/\tFALSE\t0\t{k.strip()}\t{v.strip()}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    @staticmethod
    def _info_to_item(info: dict, platform: str) -> Item:
        return Item(
            platform=platform,
            item_id=str(info.get("id", "")),
            title=str(info.get("title", "")),
            author=str(info.get("uploader") or info.get("channel") or ""),
            author_id=str(info.get("uploader_id") or ""),
            url=str(info.get("webpage_url") or info.get("url") or ""),
            duration=int(info.get("duration") or 0),
            view_count=int(info.get("view_count") or 0),
            like_count=int(info.get("like_count") or 0),
            comment_count=int(info.get("comment_count") or 0),
            publish_time=str(info.get("upload_date") or ""),
            desc=str(info.get("description") or "")[:2000],
            cover=str(info.get("thumbnail") or ""),
            status=STATUS_PENDING,
        )

    def _fetch_comments_for(self, items: list[Item], task: Task, cookie: str,
                            proxy: str, control: ProcessControl) -> None:
        """处理前按作品抓评论（用户已在「选择作品」页设好每条要抓多少）。

        抓不到就跳过，绝不让评论拖垮整个转录任务。
        """
        if not task.fetch_comments or not items:
            return
        try:
            spec = get(task.platform)
        except KeyError:
            return
        if spec.factory is None:
            return
        try:
            crawler = spec.factory(cookie=cookie, proxy=proxy)
        except Exception:  # noqa: BLE001
            return
        # 只处理被勾选的条目（用户可能在预览页取消了部分勾选）
        targets = [it for it in items if it.selected] or items
        self._collect_comments(crawler, targets, task.comment_limit, control)

    def _collect_comments(self, crawler: object, items: list[Item], limit: int,
                          control: ProcessControl) -> None:
        """抓评论。

        ⚠️ 条数是**逐条可覆盖**的：Item.comment_limit > 0 时用它的值，
        否则回退到任务级的 limit（配置页的默认值）。
        这样用户可以在「选择作品」页对不同作品设不同条数——
        热门视频多抓点、长尾的抓前几条看看风向。
        """
        n = len(items)
        for i, it in enumerate(items, 1):
            # 同上：这里只判断「已停止」，避免吞掉 skip/back/jump 等一次性指令
            if control.stopped:
                return
            per = it.comment_limit if it.comment_limit and it.comment_limit > 0 \
                else limit
            self._prog(i, n, 0.0, f"抓取评论 {i}/{n}：{it.title[:20]}（{per} 条）")
            try:
                cs = crawler.fetch_comments(it.item_id, max_count=per)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            if not cs:
                continue
            for c in cs:
                c.item_id = it.item_id
            self.db.save_comments(it.platform, cs)
            it.comment_count = max(int(it.comment_count or 0), len(cs))
            self.db.update_item(it.platform, it.item_id,
                                comment_count=it.comment_count)
            self._prog(i, n, 1.0, f"评论 {i}/{n} 完成（{len(cs)} 条）")

    # ---------- 2. 勾选 ----------
    def list_items(self, platform: str | None = None) -> list[Item]:
        return self.db.get_items(platform)

    def select(self, platform: str, item_id: str, selected: bool) -> None:
        self.db.set_selected(platform, item_id, selected)

    # ---------- 3. 下载 + 转录 ----------
    def process(self, items: list[Item], task: Task, cookie: str = "",
                proxy: str = "", model_size: str = "small",
                device: str = "auto",
                control: ProcessControl | None = None) -> list[Item]:
        """按 task.pipeline_mode 选择执行策略。"""
        ctl = control or NullControl()
        model_size = task.model_size or model_size
        device = task.device or device

        downloader = self._make_downloader(task, cookie, proxy)

        # ⚠️ 评论抓取放在这里，而不是只在采集阶段做。
        # 原因：用户希望「先看到有哪些作品，再决定每个作品抓多少评论」。
        # 采集阶段还看不到列表、设不了条数；等到了「选择作品」页，
        # 用户按作品逐个设好 comment_limit，点「开始处理」时才真正去抓。
        # 每条用自己的 comment_limit，回退到任务级默认值。
        self._fetch_comments_for(items, task, cookie, proxy, ctl)

        # 模型下载放在循环外，并给出字节级进度。
        engine = self._make_engine(task, model_size, device)
        engines: list[FasterWhisperEngine] = [engine]

        if task.do_transcribe:
            gb = MODEL_APPROX_GB.get(model_size, 0)

            def _model_dl(f: float) -> None:
                self.progress(
                    f"首次使用 {model_size} 模型，正在下载（约 {gb:.2f} GB）… "
                    f"{f*100:.0f}%", int(f * 100), 100)

            try:
                engine.load(on_progress=_model_dl)
            except Exception as exc:  # noqa: BLE001
                self.progress(f"模型加载失败：{str(exc)[:120]}", 0, 1)
                raise

        # 并发转录：额外实例（各自独立模型实例，共享同进程 CUDA 上下文）
        n_conc = 1
        if task.do_transcribe and task.concurrency and task.concurrency > 1:
            n_conc = int(task.concurrency)
            resolved = engine._resolved_compute or "float16"
            self.progress(
                f"并发转录：{n_conc} 个实例（{model_size}/{resolved}）", 0, 1)
            for k in range(n_conc - 1):
                extra = self._make_engine(task, model_size, device)
                try:
                    extra.load()
                    engines.append(extra)
                except Exception as exc:  # noqa: BLE001
                    # 显存不够就降并发，不要整个任务失败
                    self.progress(
                        f"⚠️ 第 {k+2} 个转录实例加载失败（{str(exc)[:60]}），"
                        f"并发降为 {len(engines)}", 0, 1)
                    break
            n_conc = len(engines)

        try:
            if task.pipeline_mode == "overlap" and n_conc >= 1 and task.do_download:
                self._run_overlap(items, task, downloader, engines, ctl)
            else:
                self._run_serial(items, task, downloader, engine, ctl)
        finally:
            for e in engines:
                e.unload()
        return items

    def _make_downloader(self, task: Task, cookie: str, proxy: str) -> Downloader:
        domain = {"bilibili": ".bilibili.com", "douyin": ".douyin.com"}.get(
            task.platform, "")
        # 中间文件目录允许每次任务单独指定（界面上的「音视频目录」）
        if task.media_dir:
            self.set_media_dir(task.media_dir)
        return Downloader(self.media_dir, cookie=cookie, proxy=proxy,
                          cookie_domain=domain or ".bilibili.com",
                          download_video=False)

    def _make_engine(self, task: Task, model_size: str,
                     device: str) -> FasterWhisperEngine:
        return FasterWhisperEngine(
            model_size=model_size, device=device,
            compute_type=getattr(task, "compute_type", "auto") or "auto",
            beam_size=getattr(task, "beam_size", 5) or 5,
            cpu_threads=getattr(task, "cpu_threads", 0) or 0)

    # ---------------- 中间音视频的处理 ----------------
    def _note_media(self, it: Item, path: str) -> None:
        """记一笔「这个中间文件是哪来的」，方便「文件」页展示来源。"""
        self.media_seen[it.item_id] = str(path)

    def _maybe_drop_media(self, it: Item, task: Task, i: int, total: int) -> None:
        """转录完成后按用户选择决定中间音视频的去留。

        注意两点：
        1. 只删**我们自己下载到 media_dir 里的**文件，绝不碰用户的本地素材
           （local 平台的文件是用户自己的，删了就是事故）；
        2. 删除失败不算任务失败，记个 warning 照常往下走。
        """
        keep = getattr(task, "keep_media", True)
        src = it.local_path or ""
        if keep or not src:
            return
        try:
            p = Path(src).resolve()
            media_root = self.media_dir.resolve()
            # 只清理本项目 media_dir 下的文件
            if not str(p).lower().startswith(str(media_root).lower()):
                return
            if p.exists() and p.is_file():
                size = p.stat().st_size
                p.unlink()
                self.media_seen.pop(it.item_id, None)
                it.local_path = ""
                self.db.update_item(it.platform, it.item_id, local_path="")
                self._prog(i, total, 1.0,
                           f"[{i}/{total}] 已删除中间文件（省 {size/1048576:.1f} MB）")
        except Exception as exc:  # noqa: BLE001
            self.progress(f"[{i}/{total}] 中间文件删除失败（不影响结果）："
                          f"{str(exc)[:60]}", i, total)

    # ---------------- 串行（保持原语义，最稳） ----------------
    def _run_serial(self, items: list[Item], task: Task, downloader: Downloader,
                    engine: FasterWhisperEngine, ctl: ProcessControl) -> None:
        total = len(items)
        i = 0
        while i < total:
            it = items[i]

            jump = ctl.take_jump()
            if jump is not None:
                i = min(jump, total - 1)
                continue

            ctl.begin_item(i + 1, total, it.title[:28])

            # 注意：这里**不能**用 ctl.checkpoint() 做「预览」——一次性的
            # skip/back/jump 指令取出来就消费掉了，会被白白吞掉（实测导致
            # 「点跳过没反应，反而把下一条也中止了」）。所以先只处理 stop，
            # 其余指令一律交给 _run_item 内部的 checkpoint 真正执行。
            if ctl.stopped:
                break

            action = self._run_item(it, i + 1, total, task, downloader,
                                    engine, ctl)
            if action == ACTION_STOP:
                break
            if action == ACTION_SKIP:
                it.status = STATUS_SKIPPED
                self.db.update_item(it.platform, it.item_id,
                                    status=STATUS_SKIPPED)
                self.on_item(i, it, STATUS_SKIPPED)
                self._prog(i + 1, total, 1.0, f"[{i+1}/{total}] 已跳过")
                i += 1
                continue
            if action == ACTION_BACK:
                i = max(0, i - 1)
                self._prog(i + 1, total, 0.0,
                           f"[{i+1}/{total}] 回退重做：{items[i].title[:24]}")
                continue
            if action == ACTION_JUMP:
                j = ctl.take_jump()
                i = min(j, total - 1) if j is not None else i + 1
                continue
            i += 1

    # ---------------- 重叠：下载 ‖ 转录 ----------------
    def _run_overlap(self, items: list[Item], task: Task, downloader: Downloader,
                     engines: list[FasterWhisperEngine],
                     ctl: ProcessControl) -> None:
        """下载线程 + 转录线程池并发。

        为什么能提速：
        - 下载吃网络（yt-dlp 解析 + 拉流），转录吃 GPU，两者基本不抢资源；
        - 串行时 GPU 在下载期间完全空闲，这部分时间在 overlap 下被填满。
        实测在 20 条短素材场景下整体耗时约降到串行的 55%~70%（视素材长短）。

        为什么不做「并发下载」：同一平台并发请求容易触发风控（尤其抖音），
        收益也不如把 GPU 喂满，所以下载保持单线程顺序。
        """
        total = len(items)
        dl_q: "queue.Queue[tuple[int, Item] | None]" = queue.Queue()
        result_lock = threading.Lock()
        stop_flag = threading.Event()
        done_count = {"n": 0}

        # 已经下好的先直接入队，跳过下载
        pre_done: list[tuple[int, Item]] = []

        def _download_loop() -> None:
            i = 0
            while i < total:
                if stop_flag.is_set():
                    break
                it = items[i]
                # 只查「是否已停止」，不要用 checkpoint() 预览：
                # 它会把 skip/back/jump 这类一次性指令消费掉，导致用户点了没反应。
                if ctl.stopped:
                    stop_flag.set()
                    break
                if it.media_kind == "image" or (
                        it.local_path and Path(it.local_path).exists()):
                    dl_q.put((i, it))
                    i += 1
                    continue
                # 与串行路径同一套判断：要转录就必须先下音频视频
                if not task.needs_download(it):
                    dl_q.put((i, it))
                    i += 1
                    continue
                try:
                    ctl.begin_item(i + 1, total, it.title[:28])
                    ctl.set_phase("download")
                    it.status = STATUS_DOWNLOADING
                    self.db.update_item(it.platform, it.item_id,
                                        status=STATUS_DOWNLOADING)
                    self.on_item(i, it, STATUS_DOWNLOADING)
                    if it.stream_url:
                        path = downloader.download_direct(
                            it.stream_url, it.item_id,
                            referer=PLATFORM_REFERER.get(it.platform, ""),
                            on_progress=self._byte_cb(i + 1, total, "下载"),
                            checkpoint=lambda: ctl.checkpoint() != ACTION_NONE)
                    else:
                        path = downloader.download(
                            it.url, it.item_id,
                            on_progress=self._byte_cb(i + 1, total, "下载"),
                            checkpoint=lambda: ctl.checkpoint() != ACTION_NONE)
                    it.local_path = path
                    self.db.update_item(it.platform, it.item_id,
                                        local_path=path, status=STATUS_DOWNLOADED)
                    self._note_media(it, path)
                except DownloadAborted:
                    it.status = STATUS_PENDING
                    self.db.update_item(it.platform, it.item_id,
                                        status=STATUS_PENDING)
                except Exception as exc:  # noqa: BLE001
                    it.error = str(exc)[:300]
                    it.status = STATUS_FAILED
                    self.db.update_item(it.platform, it.item_id,
                                        status=STATUS_FAILED, error=it.error)
                    self.on_item(i, it, STATUS_FAILED)
                    self._prog(i + 1, total, 1.0,
                               f"[{i+1}/{total}] 下载失败：{str(exc)[:50]}")
                dl_q.put((i, it))
                i += 1
            for _ in engines:
                dl_q.put(None)

        def _transcribe_loop(eng: FasterWhisperEngine) -> None:
            while True:
                got = dl_q.get()
                if got is None:
                    return
                idx, it = got
                if stop_flag.is_set():
                    continue
                self._transcribe_one(idx, total, it, task, eng, ctl,
                                     result_lock, done_count)

        threads = [threading.Thread(target=_download_loop, name="dl", daemon=True)]
        threads += [threading.Thread(target=_transcribe_loop, args=(e,),
                                     name=f"asr{k}", daemon=True)
                    for k, e in enumerate(engines)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _transcribe_one(self, i: int, total: int, it: Item, task: Task,
                        engine: FasterWhisperEngine, ctl: ProcessControl,
                        lock: threading.Lock, counter: dict) -> None:
        """并发路径下的单条转录（只做转录，下载已由下载线程完成）。"""
        if it.media_kind == "image":
            with lock:
                it.status = STATUS_DONE
                self.db.update_item(it.platform, it.item_id,
                                    status=STATUS_DONE, warning=it.warning)
                self.on_item(i, it, STATUS_DONE)
                counter["n"] += 1
                n_img = it.image_count or 0
                self._prog(counter["n"], total, 1.0,
                           f"[{counter['n']}/{total}] 图文作品"
                           f"（{n_img} 张图），跳过转录")
            return
        if it.status == STATUS_FAILED:
            with lock:
                counter["n"] += 1
                self._prog(counter["n"], total, 1.0,
                           f"[{counter['n']}/{total}] 下载失败，跳过转录")
            return
        if not task.do_transcribe:
            with lock:
                it.status = STATUS_DONE
                self.db.update_item(it.platform, it.item_id, status=STATUS_DONE)
                self.on_item(i, it, STATUS_DONE)
                counter["n"] += 1
            return

        src = it.local_path
        if not src or not Path(src).exists():
            with lock:
                it.status = STATUS_FAILED
                it.error = "本地音视频不存在，无法转录"
                self.db.update_item(it.platform, it.item_id,
                                    status=STATUS_FAILED, error=it.error)
                self.on_item(i, it, STATUS_FAILED)
                counter["n"] += 1
            return

        with lock:
            it.status = STATUS_TRANSCRIBING
            self.db.update_item(it.platform, it.item_id,
                                status=STATUS_TRANSCRIBING)
            self.on_item(i, it, STATUS_TRANSCRIBING)
            self._prog(counter["n"] + 1, total, 0.0,
                       f"转录中：{it.title[:24]}")

        try:
            result = engine.transcribe(
                src,
                on_progress=lambda f: self._prog(
                    counter["n"] + 1, total, f,
                    f"转录中 {counter['n'] + 1}/{total} {f*100:.0f}%"),
                checkpoint=lambda: ctl.checkpoint() != ACTION_NONE)
        except Exception as exc:  # noqa: BLE001
            with lock:
                it.status = STATUS_FAILED
                it.error = str(exc)[:300]
                self.db.update_item(it.platform, it.item_id,
                                    status=STATUS_FAILED, error=it.error)
                self.on_item(i, it, STATUS_FAILED)
                counter["n"] += 1
            return

        with lock:
            it.transcript = result.text
            it.segments = json.dumps(result.segments, ensure_ascii=False)
            it.asr_engine = f"{result.engine}({result.model})"
            it.warning = result.warning
            it.status = STATUS_DONE
            self.db.update_item(
                it.platform, it.item_id,
                transcript=result.text, segments=it.segments,
                asr_engine=it.asr_engine, warning=result.warning,
                status=STATUS_DONE, error="")
            self.on_item(i, it, STATUS_DONE)
            counter["n"] += 1
            self._prog(counter["n"], total, 1.0,
                       f"[{counter['n']}/{total}] 完成：{it.title[:24]}")

        # 转录完成 → 中间文件使命结束，按用户选择决定去留（锁外做 IO）
        self._maybe_drop_media(it, task, i + 1, total)

    # ---------------- 单条（串行用，保持原有控制语义） ----------------
    def _run_item(self, it: Item, i: int, total: int, task: Task,
                  downloader: Downloader, engine: FasterWhisperEngine,
                  ctl: ProcessControl) -> str:
        """处理单条。返回中止原因（ACTION_*）；正常跑完返回 ACTION_NONE。"""
        aborted = ACTION_NONE

        def checkpoint() -> bool:
            nonlocal aborted
            a = ctl.checkpoint()
            if a != ACTION_NONE:
                aborted = a
                return True
            return False

        try:
            if it.media_kind == "image":
                # 图文作品没有音轨，不下载也不转录，但正文/图片/话题
                # 已经在采集阶段补全并落库了，导出时照样能生成完整 Markdown。
                n_img = it.image_count or 0
                detail = (f"{n_img} 张图" if n_img else "无图")
                if it.desc:
                    detail += f"、正文 {len(it.desc)} 字"
                else:
                    detail += "、正文未取到"
                self._prog(i, total, 1.0,
                           f"[{i}/{total}] 图文作品（{detail}），跳过转录")
                it.status = STATUS_DONE
                self.db.update_item(it.platform, it.item_id,
                                    status=STATUS_DONE, warning=it.warning)
                self.on_item(i - 1, it, STATUS_DONE)
                return ACTION_NONE

            # ⚠️ 修正：以前是「不勾选下载就不下」，但转录没有音视频可读，
            # 结果转出 0 个字。正确的判断是「这一条需不需要音视频文件」，
            # 由 Task.needs_download() 统一决定（要转录就必须下）。
            need = task.needs_download(it)
            if not need:
                if it.local_path and Path(it.local_path).exists():
                    self.db.update_item(it.platform, it.item_id,
                                        status=STATUS_DOWNLOADED)
            else:
                self._prog(i, total, 0.0, f"[{i}/{total}] 开始下载：{it.title[:24]}")
                ctl.set_phase("download")
                it.status = STATUS_DOWNLOADING
                self.db.update_item(it.platform, it.item_id, status=STATUS_DOWNLOADING)
                self.on_item(i - 1, it, STATUS_DOWNLOADING)
                if it.stream_url:
                    path = downloader.download_direct(
                        it.stream_url, it.item_id,
                        referer=PLATFORM_REFERER.get(it.platform, ""),
                        on_progress=self._byte_cb(i, total, "下载"),
                        checkpoint=checkpoint)
                else:
                    path = downloader.download(
                        it.url, it.item_id,
                        on_progress=self._byte_cb(i, total, "下载"),
                        checkpoint=checkpoint)
                it.local_path = path
                self.db.update_item(it.platform, it.item_id,
                                    local_path=path, status=STATUS_DOWNLOADED)
                self._note_media(it, path)

            if task.do_transcribe:
                src = it.local_path
                if not src or not Path(src).exists():
                    raise RuntimeError("本地音视频不存在，无法转录")
                self._prog(i, total, 0.0, f"[{i}/{total}] 开始转录：{it.title[:24]}")
                ctl.set_phase("transcribe")
                it.status = STATUS_TRANSCRIBING
                self.db.update_item(it.platform, it.item_id, status=STATUS_TRANSCRIBING)
                self.on_item(i - 1, it, STATUS_TRANSCRIBING)
                result = engine.transcribe(
                    src,
                    on_progress=lambda f, _i=i, _t=total: self._prog(
                        _i, _t, f, f"[{_i}/{_t}] 转录中 {f*100:.0f}%"),
                    checkpoint=checkpoint)
                it.transcript = result.text
                it.segments = json.dumps(result.segments, ensure_ascii=False)
                it.asr_engine = f"{result.engine}({result.model})"
                it.warning = result.warning
                if result.warning:
                    self.progress(f"[{i}/{total}] {result.warning}", i, total)
                self.db.update_item(
                    it.platform, it.item_id,
                    transcript=result.text, segments=it.segments,
                    asr_engine=it.asr_engine, warning=result.warning,
                    status=STATUS_DONE, error="")
                it.status = STATUS_DONE
                # 转录完成 → 中间文件使命结束，按用户选择决定去留
                self._maybe_drop_media(it, task, i, total)
            else:
                self.db.update_item(it.platform, it.item_id, status=STATUS_DONE)
                it.status = STATUS_DONE

        except DownloadAborted:
            return aborted if aborted != ACTION_NONE else ACTION_SKIP
        except Exception as exc:  # noqa: BLE001
            it.error = str(exc)[:300]
            self.db.update_item(it.platform, it.item_id,
                                status=STATUS_FAILED, error=it.error)
            it.status = STATUS_FAILED
            self.on_item(i - 1, it, STATUS_FAILED)
            self._prog(i, total, 1.0, f"[{i}/{total}] 失败：{str(exc)[:60]}")
            return ACTION_NONE

        if aborted != ACTION_NONE:
            fields: dict[str, Any] = {"status": STATUS_PENDING}
            if it.transcript:
                fields["transcript"] = it.transcript
                fields["segments"] = it.segments or ""
                fields["asr_engine"] = it.asr_engine
            self.db.update_item(it.platform, it.item_id, **fields)
            it.status = STATUS_PENDING
            self.on_item(i - 1, it, STATUS_PENDING)
            return aborted

        it.status = STATUS_DONE
        self.on_item(i - 1, it, STATUS_DONE)
        return ACTION_NONE

    # ---------- 4. 导出 ----------
    def export(self, items: list[Item], task: Task,
               control: ProcessControl | None = None) -> dict[str, Any]:
        """导出到 task.output_dir。

        返回：
            {"root": str, "csv": str, "json": str, "summary": str,
             "files": {item_id: {"transcript": 路径, "comments": 路径}}}

        ⚠️ 索引页（moc）与 Canvas 看板（canvas）已按用户要求下架，
        不再出现在返回值里。
        """
        ctl = control or NullControl()
        opts = getattr(task, "output_opts", None) or OutputOptions()
        exporter = Exporter(task.output_dir, task.name, opts)
        comments_map: dict[str, list[dict]] = {}
        n = len(items)
        for i, it in enumerate(items, 1):
            if ctl.stopped:
                break
            self._prog(i, n, 0.5, f"导出 {i}/{n}：{it.title[:20]}")
            ts = ""
            if it.segments:
                try:
                    from types import SimpleNamespace
                    ns = SimpleNamespace(segments=json.loads(it.segments))
                    ts = format_timestamped(ns)
                except Exception:  # noqa: BLE001
                    ts = ""
            cs = self.db.get_comments(it.platform, it.item_id)
            if cs:
                comments_map[it.item_id] = cs
            exporter.write_comments(it, cs)
            exporter.write_transcript(it, ts, cs or None)
        paths = exporter.write_index(items, comments_map)
        self.progress(f"已导出到 {exporter.root}", n * 100, max(1, n) * 100)
        out: dict[str, Any] = {k: str(v) for k, v in paths.items()}
        out["root"] = str(exporter.root)
        out["files"] = exporter.file_map()
        return out

    # ---------- 工具 ----------
    def comments_of(self, platform: str, item_id: str) -> list[dict]:
        return self.db.get_comments(platform, item_id)
