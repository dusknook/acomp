"""报告层 —— PASS/FAIL 是数据，不是感觉。

render 的产出不只是音频文件，还有这份报告：
调用方（人或 AI）不需要"听一下看看行不行"，直接读表。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os

from .ir import Range

# 每个 target 对应 metrics 里的哪个字段
TARGET_FIELD = {
    "total_duration": "duration",
    "duration": "duration",
    "peak_dbfs": "peak_dbfs",
    "true_peak_dbfs": "true_peak_dbfs",
    "rms_dbfs": "rms_dbfs",
    "integrated_lufs": "integrated_lufs",
    "lead_silence": "lead_silence",
    "trail_silence": "trail_silence",
}

# 段落级默认只查电平：首尾静音是整章（master）层面的要求，
# 段落之间的 pad_after 本来就会制造静音，拿它判段落没有意义。
# 想在段落上查别的，在 IR 里写 targets.segment_checks: [peak_dbfs, rms_dbfs, ...]
DEFAULT_SEGMENT_TARGETS = ("peak_dbfs", "rms_dbfs")
MASTER_TARGETS = ("total_duration", "peak_dbfs", "true_peak_dbfs", "rms_dbfs",
                  "integrated_lufs", "lead_silence", "trail_silence")


def segment_targets(proj) -> tuple[str, ...]:
    """返回该工程实际要查的段落级 targets。

    三档：
      - IR 没写 segment_checks  → 默认（peak + rms，paragraph-level 兜底）
      - IR 写了非空列表        → 严格按列表
      - IR 写了 []              → 空 tuple，关闭所有段落级检查
                                （ONE_SHOT_MIXED 模式段落是混合产物，关掉）
    """
    if proj is None:
        return DEFAULT_SEGMENT_TARGETS
    raw_targets = (proj.raw.get("targets") or {}) if getattr(proj, "raw", None) else {}
    if "segment_checks" not in raw_targets:
        return DEFAULT_SEGMENT_TARGETS
    return tuple(proj.segment_checks)


def check_one(name: str, metrics: dict, rng: Range | None) -> dict | None:
    if rng is None:
        return None
    field = TARGET_FIELD[name]
    value = metrics.get(field)
    if value is None:
        return {"target": name, "value": None, "range": rng.describe(),
                "pass": False, "reason": "missing_metric"}
    ok = rng.holds(float(value))
    return {
        "target": name,
        "value": round(float(value), 3),
        "range": rng.describe(),
        "pass": ok,
        "reason": None if ok else (
            "below_min" if rng.lo is not None and value < rng.lo else "above_max"
        ),
    }


def check_metrics(metrics: dict, targets: dict, names: tuple[str, ...]) -> list[dict]:
    out = []
    for n in names:
        c = check_one(n, metrics, targets.get(n))
        if c:
            out.append(c)
    return out


def ir_sha256(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def build_report(proj, seg_metrics: list[dict], master: dict,
                 artifacts: dict | None = None) -> dict:
    seg_rows = []
    failures: list[dict] = []
    seg_targets = segment_targets(proj)
    for m in seg_metrics:
        checks = check_metrics(m["metrics"], proj.targets, seg_targets)
        passed = all(c["pass"] for c in checks)
        row = {
            "id": m["id"],
            "index": m["index"],
            "src": m["src"],
            "metrics": m["metrics"],
            "checks": checks,
            "pass": passed,
        }
        if m.get("asr"):
            row["asr"] = m["asr"]
        seg_rows.append(row)
        for c in checks:
            if not c["pass"]:
                failures.append({"scope": "segment", "id": m["id"], **c})

    m_checks = check_metrics(master, proj.targets, MASTER_TARGETS)
    master_pass = all(c["pass"] for c in m_checks)
    for c in m_checks:
        if not c["pass"]:
            failures.append({"scope": "master", "id": proj.name, **c})

    passed_n = sum(1 for r in seg_rows if r["pass"])
    return {
        "ok": master_pass and passed_n == len(seg_rows),
        "project": proj.name,
        "ir": os.path.abspath(proj.path),
        "ir_sha256": ir_sha256(proj.path),
        "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "master": {"metrics": master, "checks": m_checks, "pass": master_pass},
        "segments": seg_rows,
        "summary": {
            "segments": len(seg_rows),
            "segments_passed": passed_n,
            "segments_failed": len(seg_rows) - passed_n,
            "master_pass": master_pass,
            "failures": failures,
        },
        "artifacts": artifacts or {},
    }


def _fmt(v, spec: str = ".2f") -> str:
    if v is None:
        return "-"
    return format(v, spec)


def _fail_str(checks: list[dict]) -> str:
    bad = [c["target"] for c in checks if not c["pass"]]
    return ",".join(bad) if bad else ""


def format_table(report: dict) -> str:
    m = report["master"]["metrics"]
    lines: list[str] = []

    def row(cols, widths):
        return "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))

    W = [8, 10, 9, 9, 8, 8, 7, 26]
    lines.append(row(["ID", "DUR(s)", "PEAK(dB)", "RMS(dB)", "LEAD", "TRAIL", "RESULT",
                      "FAILED TARGETS"], W))
    lines.append("  ".join("-" * w for w in W))
    for s in report["segments"]:
        mm = s["metrics"]
        lines.append(row([
            s["id"],
            _fmt(mm.get("duration")),
            _fmt(mm.get("peak_dbfs")),
            _fmt(mm.get("rms_dbfs")),
            _fmt(mm.get("lead_silence")),
            _fmt(mm.get("trail_silence")),
            "PASS" if s["pass"] else "FAIL",
            _fail_str(s["checks"]),
        ], W))

    lines.append("")
    lines.append(row([
        "MASTER",
        _fmt(m.get("duration")),
        _fmt(m.get("peak_dbfs")),
        _fmt(m.get("rms_dbfs")),
        _fmt(m.get("lead_silence")),
        _fmt(m.get("trail_silence")),
        "PASS" if report["master"]["pass"] else "FAIL",
        _fail_str(report["master"]["checks"]),
    ], W))

    if m.get("integrated_lufs") is not None:
        lines.append("")
        lines.append(f"LUFS: {_fmt(m.get('integrated_lufs'))} LUFS   "
                     f"true peak {_fmt(m.get('true_peak_dbfs'))} dBFS   "
                     f"LRA {_fmt(m.get('lra'))} LU   crest {_fmt(m.get('crest_db'))} dB")

    sm = report["summary"]
    lines.append("")
    lines.append(f"结果：{sm['segments_passed']}/{sm['segments']} 段通过，"
                 f"master {'PASS' if sm['master_pass'] else 'FAIL'}，"
                 f"整体 {'OK' if report['ok'] else 'NEEDS FIX'}")

    if report["summary"]["failures"]:
        lines.append("")
        lines.append("未达标项：")
        for f in report["summary"]["failures"]:
            lines.append(f"  [{f['scope']}] {f['id']}.{f['target']} "
                         f"= {f['value']}  要求 {f['range']}  ({f['reason']})")

    arts = report.get("artifacts") or {}
    if arts:
        lines.append("")
        lines.append("产出：")
        for k, v in arts.items():
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def format_report(report: dict, *, as_json: bool = False) -> str:
    import json
    if as_json:
        return json.dumps(report, ensure_ascii=False, indent=2)
    return format_table(report)
