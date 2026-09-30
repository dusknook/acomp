"""测量层 —— 把音频翻译成 AI 能读的东西。

人类验收靠耳朵和眼睛（看波形）。AI 没有这条通道。
所以这里不做波形、不做频谱图，只输出数字和文本：

    duration / peak_dbfs / rms_dbfs / 首尾静音 / 采样率 / 声道

这些是 AI 唯一能直接推理的反馈形式。
"""

from __future__ import annotations

import os
import re
import subprocess

import numpy as np

from . import audio
from .errors import AcompError

_FLOAT = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def _stats_range(path: str, start: float | None = None, dur: float | None = None) -> dict:
    """测文件的某个时间窗（或整文件）的 peak / rms。

    plan 阶段用它测「trim 之后的真实内容窗口」，而不是整素材 ——
    否则素材首尾的静音会把 RMS 拉低，预测从一开始就偏。
    """
    args = ["-hide_banner", "-nostats"]
    if start:
        args += ["-ss", str(start)]
    if dur:
        args += ["-t", str(dur)]
    args += ["-i", path, "-map", "0:a:0",
             "-af", "astats=metadata=0:reset=0", "-f", "null", "-"]
    proc = subprocess.run(
        [audio.ffmpeg_exe()] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        startupinfo=audio._STARTUPINFO,
    )
    txt = proc.stderr.decode("utf-8", errors="replace")
    out: dict[str, float] = {}
    for key, pat in (("peak_dbfs", r"Peak level dB:\s*(" + _FLOAT + r")"),
                     ("rms_dbfs", r"RMS level dB:\s*(" + _FLOAT + r")")):
        hits = re.findall(pat, txt)
        if hits:
            out[key] = float(hits[-1])
    if "peak_dbfs" not in out or "rms_dbfs" not in out:
        raise AcompError(
            code="MEASURE_FAILED",
            what_failed=f"astats 没返回 peak/rms：{path}"
                        + (f"（窗口 {start}+{dur}s）" if start or dur else ""),
            minimal_fix="确认窗口内有非静音内容；全静音的 RMS 是 -inf，无法解析",
            retryable=False,
            detail={"path": path, "stderr_tail": txt[-600:]},
        )
    return out


def _astats(path: str) -> dict[str, float]:
    """用 ffmpeg astats 拿整文件的 peak / rms。不进 Python 内存，多长都行。"""
    return _stats_range(path)


def _ebur128(path: str) -> dict[str, float | None]:
    """ITU-R BS.1770 综合响度（LUFS）+ 真峰值。

    RMS 是个粗糙的代用品：它对低频不加权、跟人耳听到的响度不是一回事。
    平台实际多按 LUFS 判，所以这里把真值也测出来。
    文件太短（< 3s）时 ffmpeg 不给 Summary，返回 None 而不是报错。
    """
    args = ["-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
            "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]
    proc = subprocess.run(
        [audio.ffmpeg_exe()] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        startupinfo=audio._STARTUPINFO,
    )
    txt = proc.stderr.decode("utf-8", errors="replace")
    out: dict[str, float | None] = {"integrated_lufs": None,
                                    "true_peak_dbfs": None, "lra": None}
    m = re.search(r"\bI:\s*(" + _FLOAT + r")\s*LUFS", txt)
    if m:
        out["integrated_lufs"] = float(m.group(1))
    p = re.findall(r"\bPeak:\s*(" + _FLOAT + r")\s*dBFS", txt)
    if p:
        out["true_peak_dbfs"] = float(p[-1])
    r = re.search(r"\bLRA:\s*(" + _FLOAT + r")\s*LU", txt)
    if r:
        out["lra"] = float(r.group(1))
    return out


def _window(path: str, start: float | None, dur: float) -> tuple[np.ndarray, int]:
    """解一小段窗口用于静音检测，避免整文件进内存。"""
    args = ["-v", "error"]
    if start is None:            # 结尾窗口
        args += ["-sseof", f"-{dur}"]
    else:
        args += ["-ss", str(start), "-t", str(dur)]
    args += ["-i", path, "-map", "0:a:0", "-f", "f32le", "-ac", "1", "-"]
    proc = subprocess.run(
        [audio.ffmpeg_exe()] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        startupinfo=audio._STARTUPINFO,
    )
    data = np.frombuffer(proc.stdout, dtype="<f4")
    info = audio.media_info(path)
    return data, info["sample_rate"] or 44100


def _edge_silence(path: str, duration: float, thr_db: float, window: float = 15.0) -> tuple[float, float]:
    thr = 10.0 ** (thr_db / 20.0)
    w = max(0.5, min(window, duration))

    head, sr = _window(path, 0.0, w)
    lead = w
    if head.size:
        above = np.flatnonzero(np.abs(head) > thr)
        if above.size:
            lead = float(above[0]) / sr
    if head.size and np.abs(head).max() <= thr:
        lead = float(head.size) / sr

    tail, sr2 = _window(path, None, w)
    trail = w
    if tail.size:
        above = np.flatnonzero(np.abs(tail) > thr)
        if above.size:
            trail = float(tail.size - above[-1] - 1) / sr2
    return round(max(0.0, lead), 4), round(max(0.0, trail), 4)


def measure_file(path: str, *, silence_threshold_db: float = -50.0,
                 with_lufs: bool = True) -> dict:
    """测一个音频文件，返回纯数字字典。"""
    info = audio.media_info(path)
    duration = info["duration"] or 0.0
    stats = _astats(path)
    lead, trail = _edge_silence(path, duration, silence_threshold_db)
    out = {
        "path": os.path.basename(path),
        "abs_path": os.path.abspath(path),
        "duration": round(duration, 3),
        "sample_rate": info["sample_rate"],
        "channels": info["channels"],
        "peak_dbfs": round(stats["peak_dbfs"], 2),
        "rms_dbfs": round(stats["rms_dbfs"], 2),
        "lead_silence": lead,
        "trail_silence": trail,
        "bytes": info["size"],
    }
    if with_lufs:
        lu = _ebur128(path)
        for k in ("integrated_lufs", "true_peak_dbfs", "lra"):
            v = lu.get(k)
            out[k] = None if v is None else round(v, 2)
        if out["peak_dbfs"] is not None and out["rms_dbfs"] is not None:
            out["crest_db"] = round(out["peak_dbfs"] - out["rms_dbfs"], 2)
    return out


def measure_window(path: str, start: float | None = None,
                   dur: float | None = None,
                   with_true_peak: bool = False) -> dict[str, float | None]:
    """测某个时间窗的 peak/rms —— plan 阶段预测用。

    真峰值（with_true_peak=True）是可选的：要对每个源文件跑一次 ebur128，
    7 段大约多 10s。MP3 编码后采样峰值会低估 0.2~0.4 dB，启用后预测更准。
    """
    s = _stats_range(path, start, dur)
    out: dict[str, float | None] = {
        "peak_dbfs": round(s["peak_dbfs"], 2),
        "rms_dbfs": round(s["rms_dbfs"], 2),
        "true_peak_dbfs": None,
    }
    if with_true_peak:
        out["true_peak_dbfs"] = _ebur128(path).get("true_peak_dbfs")
    return out


def measure_source_true_peak(path: str) -> float | None:
    """只测源文件的真峰值（ebur128 的 Peak 行）。plan 阶段预测 master 真峰值用。"""
    return _ebur128(path).get("true_peak_dbfs")


_ASR_MODEL = None


def _asr_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def asr_text(path: str, model_size: str = "base") -> str | None:
    """可选：把语音转成文本。没装 faster_whisper 就返回 None，绝不影响主流程。"""
    if not _asr_available():
        return None
    global _ASR_MODEL
    try:
        from faster_whisper import WhisperModel
        if _ASR_MODEL is None:
            _ASR_MODEL = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _ = _ASR_MODEL.transcribe(path, beam_size=1)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception:
        return None


def text_similarity(a: str, b: str) -> float:
    import difflib
    na = re.sub(r"\s+", " ", a.strip().lower())
    nb = re.sub(r"\s+", " ", b.strip().lower())
    if not na and not nb:
        return 1.0
    return round(difflib.SequenceMatcher(None, na, nb).ratio(), 4)
