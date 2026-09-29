"""本地 ASR 引擎（faster-whisper，MIT 许可）。

之所以不直接用 VideoCaptioner：它是 GPL-3.0，会污染整个项目许可。
faster-whisper 是 MIT，FunASR 是 Apache-2.0，可以放心整合。

Windows 注意：pip 装的 nvidia-*-cu12 把 DLL 放在 site-packages/nvidia/*/bin，
Python 3.8+ 不再从 PATH 找 DLL，必须在 import ctranslate2 之前注册目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_NVIDIA_SUBS = ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime",
                "cufft", "curand", "cusparse", "cusolver")


def setup_cuda_dll_dirs() -> list[str]:
    """注册 NVIDIA DLL 目录。返回已注册的路径。"""
    if sys.platform != "win32":
        return []
    registered: list[str] = []
    try:
        import site
        roots = [Path(p) for p in site.getsitepackages()]
        user = site.getusersitepackages()
        if isinstance(user, str):
            roots.append(Path(user))
    except Exception:  # noqa: BLE001
        roots = []
    roots.append(Path(sys.prefix) / "Lib" / "site-packages")

    seen: set[str] = set()
    for root in roots:
        nv = root / "nvidia"
        if not nv.is_dir():
            continue
        for sub in _NVIDIA_SUBS:
            bin_dir = nv / sub / "bin"
            if not bin_dir.is_dir():
                continue
            key = str(bin_dir).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                os.add_dll_directory(str(bin_dir))
                registered.append(str(bin_dir))
            except (OSError, AttributeError):
                pass
    if registered:
        os.environ["PATH"] = os.pathsep.join(registered) + os.pathsep + os.environ.get("PATH", "")
    return registered


setup_cuda_dll_dirs()

# 模型缓存放项目内（避免撑爆 C 盘），并复用之前已下载的模型
_DEFAULT_CACHE = Path(__file__).resolve().parents[2] / ".cache" / "huggingface"
_DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(_DEFAULT_CACHE))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# 实测：huggingface.co 在本机直连超时，hf-mirror.com 镜像直连可用（0.85s）
# 只有用户没自己指定时才套用镜像
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402

from ..core import zhconv  # noqa: E402


@dataclass
class TranscriptResult:
    text: str
    segments: list[dict[str, Any]]
    language: str
    duration: float
    engine: str
    model: str
    speech_ratio: float = 1.0   # 有效人声时长 / 音频总时长
    warning: str = ""           # 疑似纯音乐 / 无清晰人声等提示


# 判定「基本没有人声」的阈值：人声占比低于这个数就提示用户
LOW_SPEECH_RATIO = 0.08


MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3")
# 大致体积，仅用于界面提示（GB）
MODEL_APPROX_GB = {
    "tiny": 0.08, "base": 0.15, "small": 0.5, "medium": 1.5, "large-v3": 3.1,
}

# ---------------------------------------------------------------- 性能档位
# 实测（RTX 2060 6GB，算力 7.5，small 模型，673s 中文素材）：
#     int8       RTF 0.118   8.5x
#     float16    RTF 0.086  11.6x   ← 最快
# 原因：图灵架构（7.5）**没有 INT8 Tensor Core**，int8 拿不到硬件红利，
# 反而多了一层反量化开销；float16 才是这张卡的原生最优精度。
# 所以「2070/2060/1660 等图灵及更早」一律推 float16；
# 安培（8.0+）及以上 int8_float16 才能反超。
COMPUTE_CHOICES = (
    ("auto", "自动（按显卡架构选最优）"),
    ("float16", "float16（最快，图灵及更早显卡推荐）"),
    ("int8_float16", "int8_float16（安培/30系以上更快）"),
    ("int8", "int8（兼容性最好，速度一般）"),
    ("float32", "float32（最慢，精度参考用）"),
)

# 显存占用估算（GB，按模型大小 + 精度），用来决定并发档位
_MODEL_VRAM_GB = {
    "tiny": 0.3, "base": 0.5, "small": 1.2, "medium": 2.6, "large-v3": 4.2,
}
_COMPUTE_VRAM_FACTOR = {
    "float32": 2.0, "float16": 1.0, "int8_float16": 0.55, "int8": 0.5,
}


def vram_estimate_gb(model_size: str, compute_type: str = "float16") -> float:
    """估算单实例显存占用（GB）。"""
    base = _MODEL_VRAM_GB.get(model_size, 1.5)
    return round(base * _COMPUTE_VRAM_FACTOR.get(compute_type, 1.0), 2)


def gpu_free_gb() -> float:
    """当前空闲显存（GB）。取不到就返回 0，调用方按保守处理。"""
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        line = (r.stdout or "").strip().splitlines()
        if line:
            return round(int(line[0].strip()) / 1024, 2)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def suggest_concurrency(model_size: str, compute_type: str = "float16",
                        reserve_gb: float = 0.8,
                        avg_duration_sec: float = 0.0) -> int:
    """按可用显存**和素材长度**建议并发实例数（至少 1）。

    为什么还要看素材长度（实测踩过的坑）：
    用 4 条素材（10s / 28s / 111s / 261s）对比过 ——
        串行          38.0s
        重叠 + 并发1   30.6s   ← 最快
        重叠 + 并发2   32.8s   ← 反而更慢
    因为短素材转录只要 1~2 秒，此时「模型调度 + VAD」的固定开销占主导，
    多开一个实例纯属互相干扰。所以：
        - 平均时长 < 60s  → 封顶 1 个实例（除非条数特别多）
        - 平均时长 >= 60s → 才按显存往上加
    """
    free = gpu_free_gb()
    if free <= 0:
        return 1
    per = vram_estimate_gb(model_size, compute_type)
    if per <= 0:
        return 1
    by_vram = max(1, min(4, int((free - reserve_gb) // per)))
    if avg_duration_sec and avg_duration_sec < 60:
        # 短素材：并发没有收益，保持 1
        return 1
    return by_vram


def model_dir(model_size: str) -> Path:
    """该模型在本地的缓存目录（不存在即未下载）。"""
    return _DEFAULT_CACHE / "hub" / f"models--Systran--faster-whisper-{model_size}"


def is_model_cached(model_size: str) -> bool:
    d = model_dir(model_size)
    if not d.is_dir():
        return False
    # 快照目录里有 model.bin 才算完整
    snaps = d / "snapshots"
    return any((s / "model.bin").exists() for s in snaps.iterdir()) if snaps.is_dir() else False


def cached_models() -> list[str]:
    return [m for m in MODEL_SIZES if is_model_cached(m)]


def _make_hub_tqdm(on_frac):
    """把 huggingface_hub 的 tqdm 换成回调。

    官方 snapshot_download 只在终端打 tqdm 条，GUI 里用户完全看不到进度，
    下 large-v3（3GB）时会以为程序卡死。这里用 tqdm_class 把字节数转发出来。
    """
    class _HubProgress:
        def __init__(self, *args, **kwargs):
            self.total = kwargs.get("total") or 0
            self.n = kwargs.get("initial") or 0
            self.desc = kwargs.get("desc") or ""

        def update(self, n=1):
            try:
                self.n += int(n)
            except (TypeError, ValueError):
                return
            if self.total and on_frac:
                on_frac(min(1.0, self.n / self.total))

        def refresh(self, *a, **k):
            pass

        def close(self, *a, **k):
            pass

        def set_description(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _HubProgress


def ensure_model(model_size: str, on_progress=None) -> Path:
    """确保模型已下载，返回本地路径。失败给出可操作的错误信息。

    on_progress(frac): 0~1 的下载进度。
    """
    if is_model_cached(model_size):
        return model_dir(model_size)
    try:
        from huggingface_hub import snapshot_download
        path = snapshot_download(
            f"Systran/faster-whisper-{model_size}",
            allow_patterns=["*.bin", "*.json", "*.txt"],
            tqdm_class=_make_hub_tqdm(on_progress) if on_progress else None,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"下载 {model_size} 模型失败：{str(exc)[:200]}\n"
            f"本机直连 huggingface.co 不通，已自动改用 hf-mirror.com 镜像；"
            f"仍失败可手动设置环境变量 HF_ENDPOINT 后重试。") from exc
    return Path(path)


_CUDA_WARMED = False


def warmup_cuda(on_progress=None) -> bool:
    """在主线程里把 CUDA 初始化掉。返回是否成功。

    ⚠️ 这是个实测出来的坑，别删：
    Windows 下如果在 QThread 里**第一次**初始化 ctranslate2/CUDA，进程直接
    段错误（退出码 139），界面表现为「一点开始处理就整个崩掉」。
    而只要主线程先加载过一次模型（哪怕只是 75MB 的 tiny），
    之后子线程自己加载任何模型都正常。

    实测对照：
      全程 QThread            → 退出码 139（崩）
      主线程加载+子线程推理   → 正常
      主线程预热 tiny + 子线程加载 small → 正常（2.5s 预热）
    """
    global _CUDA_WARMED
    if _CUDA_WARMED:
        return True
    if not FasterWhisperEngine.cuda_available():
        return False
    try:
        e = FasterWhisperEngine(model_size="tiny", device="cuda")
        e.load(on_progress=on_progress)
        e.unload()
        _CUDA_WARMED = True
    except Exception:  # noqa: BLE001
        _CUDA_WARMED = False
    return _CUDA_WARMED


class FasterWhisperEngine:
    """faster-whisper 本地引擎，支持 GPU（CUDA）与 CPU 回退。"""

    name = "faster-whisper"

    # 由 GUI 入口设置：Qt 环境下 CUDA 预热失败时为 True，强制走 CPU，
    # 否则后台线程一初始化 CUDA 就段错误
    cuda_disabled = False

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "auto", language: str = "zh",
                 beam_size: int = 5, cpu_threads: int = 0,
                 num_workers: int = 1) -> None:
        self.model_size = model_size
        self.language = language
        self.device = device
        self.compute_type = compute_type
        self.beam_size = max(1, int(beam_size))
        self.cpu_threads = max(0, int(cpu_threads))
        self.num_workers = max(1, int(num_workers))
        self._model = None
        self._resolved_device = ""
        self._resolved_compute = ""

    @staticmethod
    def detect_arch() -> tuple[int, str]:
        """返回 (算力主版本, 显卡名)。取不到返回 (0, '')。"""
        try:
            import subprocess
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=compute_cap,name",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5)
            line = (r.stdout or "").strip().splitlines()
            if not line:
                return 0, ""
            cap, _, name = line[0].partition(",")
            return int(float(cap.strip().split(".")[0])), name.strip()
        except Exception:  # noqa: BLE001
            return 0, ""

    @staticmethod
    def cuda_available() -> bool:
        try:
            import ctranslate2
            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            return False

    def resolve_compute(self, device: str) -> str:
        """把 auto 解析成具体精度。

        规则来自实测（见文件头 COMPUTE_CHOICES 注释）：
        - 图灵(7)及更早：float16 最快（无 INT8 Tensor Core，int8 反而慢）
        - 安培(8)/爱达(9)：int8_float16 更快
        - 取不到显卡信息时保守用 float16（比 int8 稳且快）
        """
        if self.compute_type != "auto":
            return self.compute_type
        if device == "cpu":
            return "int8"
        major, _ = self.detect_arch()
        if major >= 8:
            return "int8_float16"
        return "float16"

    def _resolve_device(self) -> tuple[str, str]:
        device = self.device
        if device == "auto":
            device = "cuda" if (self.cuda_available()
                                and not self.cuda_disabled) else "cpu"
        if device == "cuda" and self.cuda_disabled:
            # 用户显式选了 cuda，但预热失败过：硬切 CPU，别去送死
            device = "cpu"
        return device, self.resolve_compute(device)

    def load(self, on_progress=None) -> None:
        """加载模型。on_progress(frac) 只在需要下载模型时有回调。"""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        if not is_model_cached(self.model_size):
            ensure_model(self.model_size, on_progress)   # 首次使用时自动下载

        device, ctype = self._resolve_device()
        self._resolved_device, self._resolved_compute = device, ctype

        # cpu_threads 影响前后处理（VAD、特征提取）；GPU 上设太高会跟 CUDA
        # 抢线程，实测 4 是甜点，0 表示交给 ctranslate2 自己定。
        extra: dict[str, Any] = {}
        if self.cpu_threads > 0:
            extra["cpu_threads"] = self.cpu_threads
        if self.num_workers > 1:
            extra["num_workers"] = self.num_workers

        try:
            self._model = WhisperModel(self.model_size, device=device,
                                       compute_type=ctype, **extra)
        except Exception as exc:  # noqa: BLE001
            if device == "cuda":
                # GPU 加载失败（显存不足 / 缺少库）时自动回退 CPU
                self._resolved_device = "cpu"
                self._resolved_compute = "int8"
                fb: dict[str, Any] = {}
                if self.cpu_threads > 0:
                    fb["cpu_threads"] = self.cpu_threads
                self._model = WhisperModel(self.model_size, device="cpu",
                                           compute_type="int8", **fb)
            else:
                raise RuntimeError(f"加载 faster-whisper 模型失败: {exc}") from exc

    def unload(self) -> None:
        """显式释放模型（尤其 CUDA 显存）。

        转录跑在后台线程里，如果不主动释放，CUDA 上下文会一直挂着；
        界面退出时偶发段错误，主动释放能显著降低概率。
        """
        self._model = None
        try:
            import gc
            gc.collect()
        except Exception:  # noqa: BLE001
            pass

    def transcribe(self, audio_path: str | Path, on_progress=None,
                   checkpoint=None) -> TranscriptResult:
        """转录。

        on_progress(frac): 按已识别到的时间轴推进，0~1。
        checkpoint(): 每个语音段调用一次。返回 True 表示**中止本次转录**
            （用户点了跳过/后退/停止）。暂停也是在这里阻塞实现的，
            所以暂停期间进度会停在原地而不是继续跑。
            已识别出的段落会保留在中止时返回的结果里。
        """
        import time

        self.load()
        segments_iter, info = self._model.transcribe(
            str(audio_path),
            language=self.language or None,
            # beam_size=1 是贪心解码：实测比 beam5 再快约 30%，代价是同音字
            # 错误略增（人名/专有名词容易错）。批量转录对话类内容够用。
            beam_size=self.beam_size,
            # 单条音频不需要「多条候选」重排，关掉能省一点时间
            best_of=1 if self.beam_size <= 1 else 5,
            temperature=0.0,
            condition_on_previous_text=False,   # 避免长音频累积幻觉
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 400,
            },
            # Whisper 遇到中文常输出繁体，先用提示词引导……
            initial_prompt=(
                "以下是一段简体中文普通话内容。请务必使用简体中文转写，"
                "不要输出繁体字。"),
        )
        segments: list[dict[str, Any]] = []
        texts: list[str] = []
        speech_sec = 0.0
        audio_len = float(getattr(info, "duration", 0.0) or 0.0)
        for seg in segments_iter:
            if checkpoint is not None and checkpoint():
                break
            # 进度按「已识别到的时间点 / 音频总长」推进，比按段数准得多
            if on_progress and audio_len > 0:
                try:
                    on_progress(min(1.0, float(seg.end) / audio_len))
                except Exception:  # noqa: BLE001
                    pass
            text = (seg.text or "").strip()
            # ⚠️ 别把阈值调低：实测说唱/带 BGM 的视频 no_speech_prob 会系统性
            # 偏高到 0.69，但人声是实打实有的。之前设 0.6 会把整段歌词全砍掉。
            # 这里只在「模型几乎确定没说话」时才丢段。
            nsp = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
            if not text or nsp > 0.95:
                continue
            # ⚠️ 光靠 initial_prompt 不够：长音频里 prompt 的影响会随解码窗口
            # 滑走，实测仍有 15%~30% 的段落飘回繁体，甚至同一句话里简繁混排。
            # 所以每段都做一次确定性的繁→简转换（opencc 查表，无幻觉）。
            text = zhconv.clean_text(text)
            if not text:
                continue
            speech_sec += max(0.0, float(seg.end) - float(seg.start))
            segments.append({
                "start": round(float(seg.start), 2),
                "end": round(float(seg.end), 2),
                "text": text,
            })
            texts.append(text)

        duration = getattr(info, "duration", 0.0) or 0.0
        ratio = (speech_sec / duration) if duration > 0 else 1.0

        warning = ""
        if duration > 0 and ratio < LOW_SPEECH_RATIO:
            warning = (
                f"⚠️ 未检测到清晰人声：音频总长 {duration:.0f} 秒，"
                f"有效语音仅占 {ratio*100:.1f}%。该素材可能以背景音乐为主、"
                f"无人声旁白，或音量过低。建议换用 small 及以上模型重试，"
                f"或确认原视频是否本来就没有配音。")

        return TranscriptResult(
            text=zhconv.to_simplified("\n".join(texts)),
            segments=segments,
            language=getattr(info, "language", "") or "",
            duration=duration,
            engine=self.name,
            model=f"{self.model_size}/{self._resolved_device}/{self._resolved_compute}",
            speech_ratio=round(ratio, 4),
            warning=warning,
        )


def format_timestamped(result: TranscriptResult) -> str:
    """把带时间戳的片段渲染成可读文本。"""
    lines = []
    for seg in result.segments:
        mm1, ss1 = divmod(int(seg["start"]), 60)
        mm2, ss2 = divmod(int(seg["end"]), 60)
        lines.append(f"[{mm1:02d}:{ss1:02d} -> {mm2:02d}:{ss2:02d}] {seg['text']}")
    return "\n".join(lines)
