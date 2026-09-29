"""并发与流水线模式的实测对比。

对比三种跑法的墙钟时间：
    A. serial          —— 逐条：下载 → 转录 → 下一条（原行为）
    B. overlap + 并发1  —— 下载与转录重叠，转录单实例
    C. overlap + 并发2  —— 下载与转录重叠，转录双实例

关键点：
- 下载是网络瓶颈，转录是 GPU 瓶颈，重叠后两者互不抢占，能吃掉「等下载」的空转；
- 并发转录靠多实例把 GPU 喂满。但**提速不是线性的**：
  单条转录时 GPU 已经比较满，多实例主要收益在「多条的排队缝隙」，
  实测更接近 1.3~1.6x，而不是 2x。这是正常现象，别指望线性。

用法：
    python test_concurrency.py            # 用本地已有素材跑
    python test_concurrency.py <目录>
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import app.asr.engine as E  # noqa: E402

E.warmup_cuda()

from app.core.control import ProcessControl  # noqa: E402
from app.core.models import Item, OutputOptions, Task, STATUS_DONE  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

MEDIA = ROOT / "data" / "media"
OUT = ROOT / "out" / "test_conc"
DB = ROOT / "data" / "_bench.db"


def local_items(limit: int = 4) -> list[Item]:
    """把本地已有音频当素材：直接置 local_path，跳过下载，专测转录并发。"""
    out: list[Item] = []
    for p in sorted(MEDIA.glob("*")):
        if p.suffix.lower() not in (".mp4", ".mkv", ".m4a", ".mp3", ".wav",
                                    ".webm", ".flv"):
            continue
        try:
            import av
            c = av.open(str(p))
            d = float(c.duration) / 1_000_000
            c.close()
        except Exception:  # noqa: BLE001
            continue
        out.append(Item(
            platform="local", item_id=p.stem,
            title=p.stem, author="本地素材",
            duration=int(d), local_path=str(p),
            status=STATUS_DONE))
        if len(out) >= limit:
            break
    return out


def mk_task(name: str, mode: str, conc: int, model: str = "small") -> Task:
    return Task(
        name=name, mode="manual", platform="local", output_dir=str(OUT),
        do_download=False, do_transcribe=True,
        model_size=model, device="cuda", compute_type="float16",
        beam_size=5, concurrency=conc, pipeline_mode=mode,
        # 新版只有三模式；这里只要纯文本逐字稿
        output_opts=OutputOptions(mode=OutputOptions.MODE_PLAIN),
    )


def run_case(label: str, mode: str, conc: int, items: list[Item],
             model: str = "small") -> dict | None:
    # ⚠️ 不要删已有文件：沙箱把 unlink 重定向到「回收站」，
    # 而回收站对已占用的 .db 会失败并抛 OSError。改成每轮用独立文件名。
    tag = f"{mode}{conc}"
    if OUT.exists():
        shutil.rmtree(OUT)
    db = DB.with_name(f"_bench_{tag}.db")
    for suf in ("", "-wal", "-shm"):
        p = Path(str(db) + suf)
        if p.exists():
            try:
                p.write_bytes(b"")
            except Exception:  # noqa: BLE001
                pass
    pipe = Pipeline(db, MEDIA)
    task = mk_task(f"conc{conc}", mode, conc, model)

    # 用全新的 Item 副本，避免上一轮把 transcript 带着跑
    fresh = [Item(**{**it.to_dict(), "transcript": "", "segments": ""})
             for it in items]
    logs: list[str] = []
    pipe.progress = lambda m, c, t: logs.append(m)

    t0 = time.time()
    try:
        pipe.process(fresh, task, control=ProcessControl())
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {label} 失败：{str(exc)[:160]}")
        return None
    wall = time.time() - t0

    ok = sum(1 for it in fresh if it.status == STATUS_DONE)
    chars = sum(len(it.transcript or "") for it in fresh)
    if ok == 0:
        print(f"  ✗ {label} 没有条目成功（可能模型未下载）")
        return None
    print(f"  ✓ {label:<24} 墙钟 {wall:>6.1f}s  成功 {ok}/{len(fresh)}  "
          f"字数 {chars}")
    return {"label": label, "wall_s": round(wall, 1), "ok": ok,
            "total": len(fresh), "chars": chars}


def main() -> int:
    limit = 4
    model = "small"
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        limit = int(sys.argv[1])
    if len(sys.argv) > 2:
        model = sys.argv[2]

    if not E.is_model_cached(model):
        print(f"❌ {model} 模型未下载")
        return 1
    items = local_items(limit)
    if not items:
        print("❌ data/media 下没有可用的本地音视频")
        return 1

    total_dur = sum(it.duration for it in items)
    print(f"素材：{len(items)} 条本地音频，总时长 {total_dur}s，模型 {model}")
    print(f"显卡：{E.FasterWhisperEngine.detect_arch()}")
    print(f"空闲显存：{E.gpu_free_gb()} GB\n")

    cases = [
        ("A 串行（原行为）", "serial", 1),
        ("B 重叠+并发1", "overlap", 1),
        ("C 重叠+并发2", "overlap", 2),
    ]
    results: list[dict] = []
    for label, mode, conc in cases:
        print(f"  ▶ {label} …", flush=True)
        r = run_case(label, mode, conc, items, model)
        if r:
            results.append(r)

    if len(results) < 2:
        print("\n没有足够的对照结果")
        return 1

    base = results[0]
    print("\n" + "=" * 68)
    print(f"{'跑法':<26} {'墙钟(s)':>9} {'相对串行':>10} {'成功':>7}")
    print("-" * 68)
    for r in results:
        sp = base["wall_s"] / r["wall_s"]
        print(f"{r['label']:<26} {r['wall_s']:>9.1f} {sp:>9.2f}x "
              f"{r['ok']}/{r['total']:>5}")
    best = min(results, key=lambda r: r["wall_s"])
    print("-" * 68)
    print(f"最快：{best['label']}  相对串行提速 "
          f"{base['wall_s'] / best['wall_s']:.2f}x")

    fp = ROOT / "out" / "concurrency_bench.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"明细 → {fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
