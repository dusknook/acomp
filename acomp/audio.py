"""ffmpeg 封装层。

所有音频操作都是 headless 的纯函数：给入参，出文件。
没有播放、没有实时预览、没有波形窗口 —— AI 用不上，而且慢。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache

import numpy as np

from .errors import AcompError

_STARTUPINFO = None
if os.name == "nt":
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    """定位 ffmpeg。优先级：环境变量 > imageio_ffmpeg 内置 > PATH。"""
    env = os.environ.get("ACOMP_FFMPEG")
    if env and os.path.exists(env):
        return env
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    p = shutil.which("ffmpeg")
    if p:
        return p
    raise AcompError(
        code="FFMPEG_MISSING",
        what_failed="找不到 ffmpeg 可执行文件",
        minimal_fix="pip install imageio-ffmpeg，或把 ffmpeg 加进 PATH，"
                    "或设置环境变量 ACOMP_FFMPEG 指向 ffmpeg.exe",
        retryable=False,
    )


def run(args: list[str], *, purpose: str) -> subprocess.CompletedProcess:
    exe = ffmpeg_exe()
    proc = subprocess.run(
        [exe] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=_STARTUPINFO,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise AcompError(
            code="FFMPEG_FAILED",
            what_failed=f"{purpose} 失败（exit {proc.returncode}）",
            minimal_fix="看 detail.stderr_tail 定位；修正入参后重跑。"
                        "若入参未变，重跑不会改变结果。",
            retryable=False,
            detail={"purpose": purpose, "stderr_tail": err[-1200:]},
        )
    return proc


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
_AUDIO_RE = re.compile(r"Stream #\d+:\d+.*?Audio:\s*(.+)")


@lru_cache(maxsize=512)
def media_info(path: str) -> dict:
    """解析 ffmpeg 输出得到时长/采样率/声道。没有 ffprobe 也能用。"""
    if not os.path.exists(path):
        raise AcompError(
            code="SRC_MISSING",
            what_failed=f"源文件不存在：{path}",
            minimal_fix="检查 segments[].src 路径（相对 project.yml 所在目录解析）",
            retryable=False,
            detail={"path": path},
        )
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=_STARTUPINFO,
    )
    txt = proc.stderr.decode("utf-8", errors="replace")

    duration = None
    m = _DUR_RE.search(txt)
    if m:
        h, mm, ss = m.groups()
        duration = int(h) * 3600 + int(mm) * 60 + float(ss)

    sr = None
    ch = None
    m = _AUDIO_RE.search(txt)
    if m:
        desc = m.group(1)
        ms = re.search(r"(\d+)\s*Hz", desc)
        if ms:
            sr = int(ms.group(1))
        mc = re.search(r"Hz,\s*([^,]+),", desc)
        if mc:
            token = mc.group(1).strip()
            ch = {"mono": 1, "stereo": 2}.get(token)
            if ch is None:
                mc2 = re.search(r"(\d+)\s*channels", token)
                ch = int(mc2.group(1)) if mc2 else None
    if "Audio:" not in txt:
        raise AcompError(
            code="NO_AUDIO_STREAM",
            what_failed=f"文件里没有音轨：{path}",
            minimal_fix="确认是音频文件；若是视频，先抽音轨",
            retryable=False,
            detail={"path": path},
        )
    return {"duration": duration, "sample_rate": sr, "channels": ch,
            "path": path, "size": os.path.getsize(path)}


def decode_pcm(path: str, channels: int = 2) -> tuple[np.ndarray, int, float]:
    """解码成 float32 PCM，返回 (samples[n, ch], sample_rate, duration)。

    不做重采样（保留原生采样率），这样 peak/rms 才是准确的。
    """
    info = media_info(path)
    proc = run(
        ["-v", "error", "-i", path, "-map", "0:a:0",
         "-f", "f32le", "-ac", str(channels), "-"],
        purpose=f"解码 {os.path.basename(path)}",
    )
    data = np.frombuffer(proc.stdout, dtype="<f4")
    if data.size == 0:
        raise AcompError(
            code="DECODE_EMPTY",
            what_failed=f"解码出 0 个采样：{path}",
            minimal_fix="文件可能是空的或编码损坏；重新导出该段",
            retryable=False,
            detail={"path": path},
        )
    samples = data.reshape(-1, channels)
    sr = info.get("sample_rate")
    duration = info.get("duration")
    if not sr or not duration:
        raise AcompError(
            code="MEDIA_INFO_INCOMPLETE",
            what_failed=f"拿不到采样率或时长：{path}（sr={sr}, dur={duration}）",
            minimal_fix="用标准 wav/mp3 重新导出该段",
            retryable=False,
            detail={"path": path},
        )
    # 交叉校验：采样数 / 时长 应约等于采样率
    derived = samples.shape[0] / duration
    if sr and abs(derived - sr) / sr > 0.02:
        sr = int(round(derived))
    return samples, sr, duration


def build_segment(seg, src_path: str, dst_path: str, proj) -> None:
    """按 IR 把一段源音频加工成规范化中间文件。"""
    info = media_info(src_path)
    src_dur = info["duration"] or 0.0
    start = seg.trim_start
    end = seg.trim_end if seg.trim_end is not None else src_dur
    body = max(0.0, end - start)

    f: list[str] = []
    if start > 0 or seg.trim_end is not None:
        end_arg = "" if seg.trim_end is None else f":end={seg.trim_end}"
        f.append(f"atrim=start={start}{end_arg}")
        f.append("asetpts=PTS-STARTPTS")
    if seg.gain_db:
        f.append(f"volume={seg.gain_db}dB")
    if seg.fade_in > 0:
        f.append(f"afade=t=in:st=0:d={seg.fade_in}")
    if seg.fade_out > 0:
        st = max(0.0, body - seg.fade_out)
        f.append(f"afade=t=out:st={st}:d={seg.fade_out}")
    if seg.pad_before > 0:
        f.append(f"adelay={int(round(seg.pad_before * 1000))}:all=1")
    if seg.pad_after > 0:
        f.append(f"apad=pad_dur={seg.pad_after}")

    sr = proj.audio["sample_rate"]
    ch = proj.audio["channels"]
    args = ["-v", "error", "-y", "-i", src_path]
    if f:
        args += ["-af", ",".join(f)]
    args += ["-ar", str(sr), "-ac", str(ch), "-c:a", "pcm_s16le", dst_path]
    run(args, purpose=f"构建段落 {seg.id}")


def concat_segments(wavs: list[str], crossfades: list[float], dst_path: str, proj,
                    post: str | None = None) -> None:
    """把中间段落拼成 master。

    crossfades[i] 是 wavs[i] 与 wavs[i+1] 之间的交叉淡化时长。
    post 是拼接之后追加的滤镜链（母带增益 / 限幅），例如 "volume=2dB,alimiter=..."。
    """
    if not wavs:
        raise AcompError(
            code="NO_SEGMENTS",
            what_failed="没有可拼接的段落（segments 为空，或全部 enabled: false）",
            minimal_fix="至少启用一段；或把某段的 enabled 改成 true",
            retryable=False,
        )
    sr = proj.audio["sample_rate"]
    ch = proj.audio["channels"]
    args = ["-v", "error", "-y"]
    for w in wavs:
        args += ["-i", w]

    if len(wavs) == 1 and not post:
        args += ["-c", "copy", dst_path]
        run(args, purpose="输出单段 master")
        return

    chain: list[str] = []
    if len(wavs) == 1:
        last = "[0:a]"
    elif any(c > 0 for c in crossfades):
        prev = "[0:a]"
        for i, cf in enumerate(crossfades):
            nxt = f"[a{i}]" if i < len(crossfades) - 1 else "[out]"
            chain.append(f"{prev}[{i + 1}:a]acrossfade=d={max(0.0, cf)}:c1=tri:c2=tri{nxt}")
            prev = f"[a{i}]"
        last = "[out]"
    else:
        chain.append(f"concat=n={len(wavs)}:v=0:a=1[out]")
        last = "[out]"

    maplabel = last
    if post:
        chain.append(f"{last}{post}[final]")
        maplabel = "[final]"
    args += ["-filter_complex", ";".join(chain), "-map", maplabel]

    args += ["-ar", str(sr), "-ac", str(ch), "-c:a", "pcm_s16le", dst_path]
    run(args, purpose="拼接 master")


@lru_cache(maxsize=1)
def has_encoder(name: str) -> bool:
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=_STARTUPINFO,
    )
    return name.encode() in proc.stdout


def encode_mp3(src_wav: str, dst_mp3: str, cfg: dict) -> None:
    if not has_encoder("libmp3lame"):
        raise AcompError(
            code="ENCODER_MISSING",
            what_failed="当前 ffmpeg 没有 libmp3lame 编码器",
            minimal_fix="换一个带 libmp3lame 的 ffmpeg 构建，"
                        "或把 output.mp3 整段删掉只出 wav",
            retryable=False,
        )
    args = [
        "-v", "error", "-y", "-i", src_wav,
        "-codec:a", "libmp3lame", "-b:a", cfg["bitrate"],
        "-ar", str(cfg["sample_rate"]),
    ]
    if cfg.get("joint_stereo", True):
        args += ["-joint_stereo", "1"]
    args += [dst_mp3]
    run(args, purpose="编码 mp3")
