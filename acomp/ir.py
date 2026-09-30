"""IR —— 声明式工程表示层。

这是整个工具的核心，也是它区别于剪映/AU 的根本：

    人类工具：状态活在 GUI 里（哪个面板开着、时间轴拖到哪、选中了谁）。
              AI 看不见，只能截图猜。
    本工具：  状态就是这份 YAML。可写、可读、可 diff、可进 git、可批量生成。

render 是一个纯函数：IR + 源音频 -> 产物 + 测量报告。
同样的 IR 跑两次，结果逐字节一致。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .errors import AcompError, ValidationFailed

VERSION = 1

DEFAULT_AUDIO = {
    "sample_rate": 44100,
    "channels": 2,
}

DEFAULT_MEASURE = {
    "silence_threshold_db": -50.0,
}

DEFAULT_TARGETS: dict[str, Any] = {
    "total_duration": None,      # [min, max] 秒
    "peak_dbfs": [None, -3.0],
    # 真峰值（BS.1770-4）。mp3 编码后采样点之间的真实波形峰值会高于采样值，
    # 实测本项目高 0.2~0.4 dB —— 按采样峰值判"过了"的，按真峰值可能其实超了。
    # 交付验收以真峰值为准，写 true_peak_dbfs: [null, -3.0] 启用。
    "true_peak_dbfs": None,
    "rms_dbfs": [-23.0, -18.0],
    # 综合响度（LUFS）。默认不判 —— RMS 与 LUFS 不是同一个量，
    # 只有确认平台按 LUFS 验收时才写，例如 integrated_lufs: [-23.0, -16.0]
    "integrated_lufs": None,
    "lead_silence": [None, 0.25],
    "trail_silence": [None, 0.25],
}

SEGMENT_KEYS = {
    "id", "src", "trim", "gain_db", "fade_in", "fade_out",
    "pad_before", "pad_after", "crossfade_next", "enabled",
    "expect_text", "note",
}

# targets.segment_checks 允许写哪些项（即 metrics 里的字段名）
SEGMENT_CHECK_NAMES = ("duration", "peak_dbfs", "rms_dbfs",
                       "integrated_lufs", "true_peak_dbfs",
                       "lead_silence", "trail_silence")

SEGMENT_DEFAULTS = {
    "gain_db": 0.0,
    "fade_in": 0.0,
    "fade_out": 0.0,
    "pad_before": 0.0,
    "pad_after": 0.0,
    "crossfade_next": 0.0,
    "enabled": True,
    "expect_text": None,
}


class Range:
    """[min, max] 区间，任一端可为 None 表示不设限。"""

    __slots__ = ("lo", "hi")

    def __init__(self, lo: float | None, hi: float | None):
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"区间上下限倒置：{lo} > {hi}")
        self.lo = lo
        self.hi = hi

    def holds(self, v: float) -> bool:
        if self.lo is not None and v < self.lo:
            return False
        if self.hi is not None and v > self.hi:
            return False
        return True

    def describe(self) -> str:
        lo = "-inf" if self.lo is None else f"{self.lo:g}"
        hi = "+inf" if self.hi is None else f"{self.hi:g}"
        return f"[{lo}, {hi}]"

    def __repr__(self) -> str:
        return f"Range({self.lo}, {self.hi})"


@dataclass
class Segment:
    index: int
    id: str
    src: str
    trim_start: float = 0.0
    trim_end: float | None = None
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    pad_before: float = 0.0
    pad_after: float = 0.0
    crossfade_next: float = 0.0
    enabled: bool = True
    expect_text: str | None = None
    note: str | None = None

    def effective_duration(self, src_duration: float | None) -> float | None:
        """不知道源时长时返回 None（plan 阶段还没探测）。"""
        if src_duration is None:
            return None
        end = self.trim_end if self.trim_end is not None else src_duration
        body = max(0.0, end - self.trim_start)
        return body + self.pad_before + self.pad_after


@dataclass
class Project:
    path: str
    base_dir: str
    name: str
    out_dir: str
    audio: dict
    measure: dict
    targets: dict[str, Range | None]
    segments: list[Segment]
    output: dict
    segment_checks: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    def resolve(self, p: str) -> str:
        if os.path.isabs(p):
            return os.path.normpath(p)
        return os.path.normpath(os.path.join(self.base_dir, p))

    @property
    def active_segments(self) -> list[Segment]:
        return [s for s in self.segments if s.enabled]

    @property
    def master_path(self) -> str:
        return os.path.join(self.out_dir, self.output["master"])


def _num(v: Any, path: str, issues: list[dict], *, allow_negative: bool = False,
         default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        issues.append({
            "path": path, "severity": "error",
            "what": f"期望数字，实际是 {type(v).__name__}：{v!r}",
            "fix": f"把 {path} 改成数字，例如 {default}",
        })
        return default
    if not allow_negative and v < 0:
        issues.append({
            "path": path, "severity": "error",
            "what": f"不允许负数：{v}",
            "fix": f"把 {path} 改成 >= 0 的值",
        })
        return default
    return float(v)


def _range(v: Any, path: str, issues: list[dict]) -> Range | None:
    if v is None:
        return None
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        issues.append({
            "path": path, "severity": "error",
            "what": f"区间必须是 [min, max] 两项，任一项可为 null；实际：{v!r}",
            "fix": f"把 {path} 写成 [min, max]，例如 [-23.0, -18.0] 或 [null, -3.0]",
        })
        return None
    lo, hi = v
    for name, x in (("min", lo), ("max", hi)):
        if x is not None and (isinstance(x, bool) or not isinstance(x, (int, float))):
            issues.append({
                "path": f"{path}.{name}", "severity": "error",
                "what": f"期望数字或 null，实际：{x!r}",
                "fix": f"把 {path}.{name} 改成数字或 null",
            })
            return None
    lo = None if lo is None else float(lo)
    hi = None if hi is None else float(hi)
    if lo is not None and hi is not None and lo > hi:
        issues.append({
            "path": path, "severity": "error",
            "what": f"区间上下限倒置：{lo} > {hi}",
            "fix": f"对调 {path} 的两项，或修正数值",
        })
        return None
    return Range(lo, hi)


def load(path: str) -> Project:
    if not os.path.exists(path):
        raise AcompError(
            code="IR_NOT_FOUND",
            what_failed=f"工程文件不存在：{path}",
            minimal_fix="检查路径；或用 acomp init <name> 生成一份模板",
            retryable=False,
            detail={"path": path},
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise AcompError(
            code="IR_PARSE_FAILED",
            what_failed=f"YAML 解析失败：{e}",
            minimal_fix="修正 YAML 语法（常见是缩进不一致或冒号后缺空格）后重跑",
            retryable=False,
            detail={"path": path},
        )
    if not isinstance(raw, dict):
        raise AcompError(
            code="IR_PARSE_FAILED",
            what_failed="工程文件顶层必须是 mapping（key: value 结构）",
            minimal_fix="顶层写成 version: / project: / segments: 这样的结构",
            retryable=False,
            detail={"path": path},
        )
    return parse(raw, path)


def parse(raw: dict, path: str = "<memory>") -> Project:
    """把原始 dict 归一化成 Project。不做 IO，纯函数，便于测试。"""
    issues: list[dict] = []
    base_dir = os.path.dirname(os.path.abspath(path)) if path != "<memory>" else os.getcwd()

    ver = raw.get("version")
    if ver is None:
        issues.append({
            "path": "version", "severity": "error",
            "what": "缺少 version",
            "fix": "顶层加一行 version: 1",
        })
    elif ver != VERSION:
        issues.append({
            "path": "version", "severity": "error",
            "what": f"不支持的 version：{ver!r}（本工具支持 {VERSION}）",
            "fix": f"把 version 改成 {VERSION}",
        })

    proj_raw = raw.get("project") or {}
    if not isinstance(proj_raw, dict):
        issues.append({
            "path": "project", "severity": "error",
            "what": f"project 必须是 mapping，实际：{type(proj_raw).__name__}",
            "fix": "写成 project:\\n  name: xxx",
        })
        proj_raw = {}
    default_name = os.path.splitext(os.path.basename(path))[0] if path != "<memory>" else "untitled"
    name = proj_raw.get("name") or default_name
    out_dir_rel = proj_raw.get("out_dir") or "./out"

    audio = dict(DEFAULT_AUDIO)
    audio_raw = raw.get("audio") or {}
    if not isinstance(audio_raw, dict):
        issues.append({
            "path": "audio", "severity": "error",
            "what": f"audio 必须是 mapping，实际：{type(audio_raw).__name__}",
            "fix": "写成 audio:\\n  sample_rate: 44100",
        })
        audio_raw = {}
    sr = _num(audio_raw.get("sample_rate", DEFAULT_AUDIO["sample_rate"]), "audio.sample_rate", issues)
    if sr not in (8000, 16000, 22050, 32000, 44100, 48000, 88200, 96000):
        issues.append({
            "path": "audio.sample_rate", "severity": "warn",
            "what": f"非常见采样率：{sr}",
            "fix": "通常用 44100 或 48000",
        })
    audio["sample_rate"] = int(sr)
    audio["channels"] = int(_num(audio_raw.get("channels", 2), "audio.channels", issues))
    if audio["channels"] not in (1, 2):
        issues.append({
            "path": "audio.channels", "severity": "error",
            "what": f"channels 只能是 1 或 2，实际：{audio['channels']}",
            "fix": "改成 1（单声道）或 2（立体声）",
        })

    measure = dict(DEFAULT_MEASURE)
    measure_raw = raw.get("measure") or {}
    if isinstance(measure_raw, dict):
        measure["silence_threshold_db"] = _num(
            measure_raw.get("silence_threshold_db", -50.0),
            "measure.silence_threshold_db", issues, allow_negative=True)

    targets: dict[str, Range | None] = {}
    targets_raw = raw.get("targets") or {}
    if not isinstance(targets_raw, dict):
        issues.append({
            "path": "targets", "severity": "error",
            "what": f"targets 必须是 mapping，实际：{type(targets_raw).__name__}",
            "fix": "写成 targets:\\n  peak_dbfs: [null, -3.0]",
        })
        targets_raw = {}
    targets_raw = dict(targets_raw)
    # segment_checks 不是区间，是「段落级要查哪几项」，单独处理
    seg_checks_raw = targets_raw.pop("segment_checks", None)
    seg_checks: tuple[str, ...] = ()
    if seg_checks_raw is not None:
        if not isinstance(seg_checks_raw, list):
            issues.append({
                "path": "targets.segment_checks", "severity": "error",
                "what": f"必须是列表，实际：{type(seg_checks_raw).__name__}",
                "fix": "写成 segment_checks: [peak_dbfs, rms_dbfs]；写 [] 关闭所有段落级检查",
            })
        else:
            known = set(SEGMENT_CHECK_NAMES)
            bad = [x for x in seg_checks_raw if x not in known]
            if bad:
                issues.append({
                    "path": "targets.segment_checks", "severity": "error",
                    "what": f"未知的检查项：{bad}",
                    "fix": f"可用项：{', '.join(sorted(known))}",
                })
            # 允许 []：ONE_SHOT_MIXED 模式下段落是混合产物，关掉所有段落级检查
            seg_checks = tuple(str(x) for x in seg_checks_raw)

    unknown_t = set(targets_raw) - set(DEFAULT_TARGETS)
    for k in sorted(unknown_t):
        issues.append({
            "path": f"targets.{k}", "severity": "warn",
            "what": f"未知的 target：{k}",
            "fix": f"可用项：{', '.join(sorted(DEFAULT_TARGETS))}；打错字不会生效",
        })
    for k, default in DEFAULT_TARGETS.items():
        targets[k] = _range(targets_raw.get(k, default), f"targets.{k}", issues)

    segs_raw = raw.get("segments")
    if segs_raw is None:
        issues.append({
            "path": "segments", "severity": "error",
            "what": "缺少 segments（至少要有一段）",
            "fix": "加 segments:\\n  - id: S01\\n    src: raw/S01.wav",
        })
        segs_raw = []
    if not isinstance(segs_raw, list):
        issues.append({
            "path": "segments", "severity": "error",
            "what": f"segments 必须是列表，实际：{type(segs_raw).__name__}",
            "fix": "写成 segments:\\n  - id: S01\\n    src: ...",
        })
        segs_raw = []

    segments: list[Segment] = []
    seen_ids: set[str] = set()
    for i, s in enumerate(segs_raw):
        p = f"segments[{i}]"
        if not isinstance(s, dict):
            issues.append({
                "path": p, "severity": "error",
                "what": f"每段必须是 mapping，实际：{type(s).__name__}",
                "fix": "每段写成 - id: / src: 的形式",
            })
            continue
        unknown = set(s) - SEGMENT_KEYS
        for k in sorted(unknown):
            issues.append({
                "path": f"{p}.{k}", "severity": "warn",
                "what": f"未知字段：{k}",
                "fix": f"可用字段：{', '.join(sorted(SEGMENT_KEYS))}",
            })
        sid = s.get("id")
        if not sid:
            sid = f"S{i + 1:02d}"
            issues.append({
                "path": f"{p}.id", "severity": "warn",
                "what": "缺少 id，已自动补为 " + sid,
                "fix": f"显式写 id: {sid}，自动编号在重排后会变",
            })
        sid = str(sid)
        if sid in seen_ids:
            issues.append({
                "path": f"{p}.id", "severity": "error",
                "what": f"id 重复：{sid}",
                "fix": "每段 id 必须唯一，重名的改掉",
            })
        seen_ids.add(sid)

        src = s.get("src")
        if not src:
            issues.append({
                "path": f"{p}.src", "severity": "error",
                "what": "缺少 src",
                "fix": f"写 src: raw/{sid}.wav（相对 project.yml 所在目录）",
            })
            src = ""

        trim = s.get("trim") or {}
        if not isinstance(trim, dict):
            issues.append({
                "path": f"{p}.trim", "severity": "error",
                "what": f"trim 必须是 mapping，实际：{type(trim).__name__}",
                "fix": "写成 trim: {start: 0.0, end: null}",
            })
            trim = {}
        t_start = _num(trim.get("start"), f"{p}.trim.start", issues)
        t_end_raw = trim.get("end")
        t_end = None if t_end_raw is None else _num(t_end_raw, f"{p}.trim.end", issues)
        if t_end is not None and t_end <= t_start:
            issues.append({
                "path": f"{p}.trim", "severity": "error",
                "what": f"trim.end ({t_end}) 必须大于 trim.start ({t_start})",
                "fix": "调整 end，或直接删掉 trim 用整段",
            })
            t_end = None

        seg = Segment(
            index=i,
            id=sid,
            src=str(src),
            trim_start=t_start,
            trim_end=t_end,
            gain_db=_num(s.get("gain_db"), f"{p}.gain_db", issues, allow_negative=True),
            fade_in=_num(s.get("fade_in"), f"{p}.fade_in", issues),
            fade_out=_num(s.get("fade_out"), f"{p}.fade_out", issues),
            pad_before=_num(s.get("pad_before"), f"{p}.pad_before", issues),
            pad_after=_num(s.get("pad_after"), f"{p}.pad_after", issues),
            crossfade_next=_num(s.get("crossfade_next"), f"{p}.crossfade_next", issues),
            enabled=bool(s.get("enabled", True)),
            expect_text=s.get("expect_text"),
            note=s.get("note"),
        )
        segments.append(seg)

    if segments and segments[-1].crossfade_next:
        issues.append({
            "path": f"segments[{len(segments) - 1}].crossfade_next",
            "severity": "warn",
            "what": f"最后一段设了 crossfade_next={segments[-1].crossfade_next}，没有下一段可接",
            "fix": "把最后一段的 crossfade_next 设为 0 或删掉该字段",
        })

    out_raw = raw.get("output") or {}
    if not isinstance(out_raw, dict):
        issues.append({
            "path": "output", "severity": "error",
            "what": f"output 必须是 mapping，实际：{type(out_raw).__name__}",
            "fix": "写成 output:\\n  master: xxx.wav",
        })
        out_raw = {}
    output = {
        "master": out_raw.get("master") or f"{name}_master.wav",
        "master_gain_db": _num(out_raw.get("master_gain_db", 0.0),
                               "output.master_gain_db", issues, allow_negative=True),
        "limiter": None,
        "mp3": None,
    }
    lim_raw = out_raw.get("limiter")
    if lim_raw is not None:
        if not isinstance(lim_raw, dict):
            issues.append({
                "path": "output.limiter", "severity": "error",
                "what": f"output.limiter 必须是 mapping，实际：{type(lim_raw).__name__}",
                "fix": "写成 output:\\n  limiter:\\n    limit_db: -3.2",
            })
        elif lim_raw.get("enabled", True):
            ldb = _num(lim_raw.get("limit_db", -3.0), "output.limiter.limit_db",
                       issues, allow_negative=True, default=-3.0)
            if ldb > 0:
                issues.append({
                    "path": "output.limiter.limit_db", "severity": "error",
                    "what": f"limit_db 必须 <= 0（0 dBFS 是满量程），实际：{ldb}",
                    "fix": "改成负数，例如 -3.0",
                })
            output["limiter"] = {
                "limit_db": ldb,
                "attack": _num(lim_raw.get("attack", 5.0), "output.limiter.attack",
                               issues, default=5.0),
                "release": _num(lim_raw.get("release", 50.0), "output.limiter.release",
                                issues, default=50.0),
            }
    mp3_raw = out_raw.get("mp3")
    if mp3_raw:
        if not isinstance(mp3_raw, dict):
            issues.append({
                "path": "output.mp3", "severity": "error",
                "what": f"output.mp3 必须是 mapping，实际：{type(mp3_raw).__name__}",
                "fix": "写成 output:\\n  mp3:\\n    bitrate: 192k",
            })
        elif mp3_raw.get("enabled", True):
            output["mp3"] = {
                "name": mp3_raw.get("name") or f"{name}_master.mp3",
                "bitrate": str(mp3_raw.get("bitrate", "192k")),
                "sample_rate": int(_num(mp3_raw.get("sample_rate", audio["sample_rate"]),
                                        "output.mp3.sample_rate", issues)),
                "joint_stereo": bool(mp3_raw.get("joint_stereo", True)),
            }

    proj = Project(
        path=path,
        base_dir=base_dir,
        name=str(name),
        out_dir=os.path.normpath(os.path.join(base_dir, out_dir_rel)),
        audio=audio,
        measure=measure,
        targets=targets,
        segments=segments,
        output=output,
        segment_checks=seg_checks,
        raw=raw,
    )

    errs = [i for i in issues if i.get("severity") == "error"]
    if errs:
        raise ValidationFailed(issues, path)
    return proj


def validate_only(raw: dict, path: str = "<memory>") -> tuple[Project | None, list[dict]]:
    """校验但不抛异常。返回 (project or None, issues)。"""
    try:
        return parse(raw, path), []
    except ValidationFailed as e:
        return None, e.issues


def segment_fingerprint(seg: Segment, proj: Project, src_stat: os.stat_result | None) -> str:
    """段落指纹 —— 用于缓存。IR 没变、源没变，就不重新构建。"""
    h = hashlib.sha256()
    parts = [
        seg.id, seg.src, f"{seg.trim_start}", f"{seg.trim_end}",
        f"{seg.gain_db}", f"{seg.fade_in}", f"{seg.fade_out}",
        f"{seg.pad_before}", f"{seg.pad_after}",
        f"{proj.audio['sample_rate']}", f"{proj.audio['channels']}",
    ]
    if src_stat is not None:
        parts += [str(int(src_stat.st_mtime)), str(src_stat.st_size)]
    h.update("|".join(parts).encode("utf-8"))
    return h.hexdigest()[:16]


TEMPLATE = """\
# acomp 工程文件 —— 声明式音频后期 IR
# 原则：AI 写这份文本，工具负责编译。不点界面、不拖时间轴。
version: 1

project:
  name: {name}
  out_dir: ./out

audio:
  sample_rate: 44100
  channels: 2

measure:
  # 判定首尾静音的阈值（dBFS）。低于此电平视为静音。
  silence_threshold_db: -50.0

# 交付目标 —— render 后逐项打 PASS/FAIL
# 写法统一为 [min, max]，任一端写 null 表示不设限
targets:
  total_duration: [300.0, 420.0]   # 整章 5-7 分钟
  peak_dbfs: [null, -3.0]          # 峰值不超过 -3 dBFS
  rms_dbfs: [-23.0, -18.0]         # 响度落在 -23..-18 dBFS
  lead_silence: [null, 0.25]       # 开头静音 < 0.25s
  trail_silence: [null, 0.25]      # 结尾静音 < 0.25s

segments:
  - id: S01
    src: raw/S01.wav
    # trim: {{start: 0.0, end: null}}   # 可选裁剪
    gain_db: 0.0
    fade_in: 0.0
    fade_out: 0.0
    pad_before: 0.0
    pad_after: 1.0                   # 段尾留 1s 静音
    crossfade_next: 0.4              # 与下一段交叉淡化 0.4s
    # expect_text: "..."              # 可选：用于 ASR 对齐校验

output:
  master: {name}_master.wav
  mp3:
    bitrate: 192k
    sample_rate: 44100
    joint_stereo: true
"""
