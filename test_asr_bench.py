"""ASR 性能与精度基准：为「把显卡用满」找最优配置。

为什么要**每组配置独立子进程**：
ctranslate2 在进程内切换 device/compute_type 时，若 CUDA 上下文已经初始化过，
偶发段错误（退出码 139）。第一版单进程跑到底就是这么崩的 —— 前两组结果正常，
第三组（int8_float16）直接静默退出，日志断在那里没有任何报错。
改成每组 spawn 一个子进程后，崩溃只影响那一组，其余组照常出结果。

用法：
    python test_asr_bench.py             # 跑全部
    python test_asr_bench.py quick       # 只对比 compute_type
    python test_asr_bench.py one <idx>   # 内部用：跑单组
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

MEDIA = ROOT / "data" / "media"
VENV_PY = Path(sys.executable)

# ---------------------------------------------------------------- 组定义
# (标签, device, compute_type, beam_size, cpu_threads)
CASES: list[tuple[str, str, str, int, int]] = [
    ("cuda/int8/beam5",         "cuda", "int8",        5, 0),
    ("cuda/float16/beam5",      "cuda", "float16",     5, 0),
    ("cuda/int8_float16/beam5", "cuda", "int8_float16", 5, 0),
    ("cuda/float16/beam1",      "cuda", "float16",     1, 0),
    ("cuda/float16/beam5/cpu4", "cuda", "float16",     5, 4),
    ("cuda/float16/beam5/cpu12", "cuda", "float16",    5, 12),
    ("cpu/int8/beam5",          "cpu",  "int8",        5, 0),
]

QUICK_IDX = [0, 1, 2]


def pick_audio(min_sec: float = 20.0) -> tuple[Path | None, float]:
    try:
        import av
    except Exception:  # noqa: BLE001
        return None, 0.0
    best: tuple[float, Path] | None = None
    for p in sorted(MEDIA.glob("*")):
        if p.suffix.lower() not in (".mp4", ".mkv", ".webm", ".mp3", ".m4a",
                                    ".wav", ".flv", ".mov"):
            continue
        try:
            c = av.open(str(p))
            d = float(c.duration) / 1_000_000
            c.close()
        except Exception:  # noqa: BLE001
            continue
        if d >= min_sec and (best is None or d > best[0]):
            best = (d, p)
    return (best[1], best[0]) if best else (None, 0.0)


def child_run(audio: Path, size: str, device: str, compute: str,
              beam: int, cpu_threads: int) -> dict | None:
    """把单组配置丢进子进程跑，输出末行 JSON。崩了返回 None。"""
    code = r'''
import json, sys, time
from pathlib import Path
sys.path.insert(0, r"%s")
import app.asr.engine as E
E.warmup_cuda()
from faster_whisper import WhisperModel

audio, size, device, compute, beam, cpu_threads = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4],
    int(sys.argv[5]), int(sys.argv[6]))

kw = {"device": device, "compute_type": compute}
if cpu_threads > 0:
    kw["cpu_threads"] = cpu_threads

t0 = time.time()
m = WhisperModel(size, **kw)
t_load = time.time() - t0

t1 = time.time()
segs, info = m.transcribe(
    audio, language="zh", beam_size=beam, vad_filter=True,
    vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 400},
    initial_prompt="以下是简体中文的普通话内容，请使用简体中文输出。")
texts = [(s.text or "").strip() for s in segs]
t_run = time.time() - t1

dur = float(getattr(info, "duration", 0.0) or 0.0)
text = "".join(texts)
print("@@RESULT@@" + json.dumps({
    "load_s": round(t_load, 2),
    "run_s": round(t_run, 2),
    "audio_s": round(dur, 1),
    "rtf": round(t_run / dur, 4) if dur else 0,
    "x_speed": round(dur / t_run, 2) if t_run else 0,
    "chars": len(text),
    "segs": len(texts),
    "head": text[:70],
    "tail": text[-45:],
}, ensure_ascii=False))
''' % str(ROOT)

    cmd = [str(VENV_PY), "-c", code, str(audio), size, device, compute,
           str(beam), str(cpu_threads)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    print(f"      ✗ 子进程无结果（退出码 {r.returncode}）")
    err = (r.stderr or "").strip().splitlines()
    for e in err[-3:]:
        print("        " + e[:150])
    return None


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    audio, dur = pick_audio()
    if audio is None:
        print("❌ data/media 下没有 >=20 秒的音视频，无法做基准")
        return 1

    import ctranslate2
    size = "small" if Path(sys.executable).exists() and _cached("small") else "tiny"
    if not _cached(size):
        print(f"❌ {size} 模型未下载，先跑一次转录自动下载")
        return 1

    print(f"基准素材：{audio.name}  时长 {dur:.1f}s")
    print(f"显卡：{ctranslate2.get_cuda_device_count()} × CUDA，"
          f"可用精度 {sorted(ctranslate2.get_supported_compute_types('cuda'))}")
    print(f"模型：{size}\n")

    idxs = QUICK_IDX if mode == "quick" else range(len(CASES))
    results = []
    for i in idxs:
        label, device, compute, beam, th = CASES[i]
        print(f"  ▶ {label} …", flush=True)
        r = child_run(audio, size, device, compute, beam, th)
        if r is None:
            continue
        r["label"] = label
        r["device"] = device
        r["compute"] = compute
        r["beam"] = beam
        r["cpu_threads"] = th
        results.append(r)
        print(f"    ✓ 转录 {r['run_s']:.1f}s  加载 {r['load_s']:.1f}s  "
              f"RTF={r['rtf']:.4f}  加速 {r['x_speed']:.1f}x  字数 {r['chars']}")

    if not results:
        print("\n❌ 没有任何一组跑成功")
        return 1

    base = next((r for r in results if r["label"] == "cuda/int8/beam5"), None)
    print(f"{'配置':<26} {'转录(s)':>8} {'RTF':>8} {'实时倍速':>9} {'字数':>6}")
    print("-" * 70)
    for r in results_sorted:
        print(f"{r['label']:<26} {r['run_s']:>8.1f} {r['rtf']:>8.4f} "
              f"{r['x_speed']:>8.1f}x {r['chars']:>6}")
    if base:
        print("-" * 70)
        print(f"相对 int8 基准提速：{base['rtf'] / results_sorted[0]['rtf']:.2f}x")

    fp = ROOT / "out" / "asr_bench.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    print(f"\n明细 → {fp}")
    return 0


def _cached(size: str) -> bool:
    import app.asr.engine as E
    return E.is_model_cached(size)


if __name__ == "__main__":
    raise SystemExit(main())
