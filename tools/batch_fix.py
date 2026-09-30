#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批量体检 / 修复一个音频目录 —— 「IR 是数据」的实际用法。

这个脚本本身就说明了 acomp 的设计意图：
它先测一遍目录里的每个文件，算出「该补多少增益、该裁多少首尾静音」，
然后**程序生成** project.yml，再交给 acomp 渲染。
整个过程不需要打开任何音频编辑器，也不需要逐个试听。

源文件只读，产物写到 --out（默认 tools/_out），绝不覆盖源目录里的任何东西。

用法示例：

    # 只体检，不改动任何东西（默认）
    python tools/batch_fix.py --src ../my_book/DELIVERY_PACKAGE

    # 体检 + 实际渲染修复版 + 复检
    python tools/batch_fix.py --src ../my_book/DELIVERY_PACKAGE --render

    # 换规格：目标响度 -19 dBFS，限幅阈值 -2.0 dB
    python tools/batch_fix.py --src audio/ --target-rms -19 --limit-db -2.0

判定口径（都在命令行可调）：
    peak <= -3.0 dBFS      采样峰值
    true_peak <= -3.0 dBFS 真峰值（BS.1770-4），交付验收应该看这个
    rms 在 -23 .. -18 dBFS
    首尾静音 < 0.25s
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acomp import measure  # noqa: E402
from acomp.errors import AcompError  # noqa: E402

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma"}

TPL = """version: 1

project:
  name: {name}
  out_dir: {out_dir}

audio:
  sample_rate: {sample_rate}
  channels: 2

measure:
  silence_threshold_db: -50.0

targets:
  peak_dbfs: [null, {peak_lim}]
  # 真峰值才是交付验收口径：mp3 编码后采样峰值会低估 0.2~0.4 dB
  true_peak_dbfs: [null, {peak_lim}]
  rms_dbfs: [{rms_lo}, {rms_hi}]
  lead_silence: [null, {max_edge}]
  trail_silence: [null, {max_edge}]
  segment_checks: [peak_dbfs]

segments:
  - id: MAIN
    src: {src}
{trim}    pad_after: {pad_after}

output:
  master: {name}_fixed.wav
  master_gain_db: {gain}
  # 限幅不是为了好听，是为了让 peak 和 rms 能同时达标：
  # crest = peak - rms 由素材内容决定，调增益改不了它。
  limiter:
    enabled: true
    limit_db: {limit_db}
    attack: 5
    release: 50
  mp3:
    bitrate: 192k
    sample_rate: {sample_rate}
    joint_stereo: true
"""


def collect(src: Path, pattern: str) -> list[Path]:
    files = sorted(p for p in src.glob(pattern)
                   if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    return files


def make_yml(work_dir: Path, src: Path, out_dir: Path, m: dict, a) -> dict:
    """按实测结果生成一份 IR 文本。"""
    name = src.stem
    gain = round(a.target_rms - m["rms_dbfs"], 1)

    # 首尾静音超标就裁掉，裁尾之后补一点点自然的尾巴
    trim = ""
    pad_after = 0.0
    if m["lead_silence"] > a.max_edge:
        trim += f"    trim:\n      start: {max(0.0, m['lead_silence'] - 0.05):.3f}\n"
    if m["trail_silence"] > a.max_edge:
        end = m["duration"] - m["trail_silence"]
        if trim:
            trim += f"      end: {end:.3f}\n"
        else:
            trim = f"    trim:\n      end: {end:.3f}\n"
        pad_after = 0.15

    yml = work_dir / f"_gen_{name}.yml"
    body = TPL.format(
        name=name,
        out_dir=str(out_dir / name).replace("\\", "/"),
        src=str(src).replace("\\", "/"),
        sample_rate=m.get("sample_rate") or 44100,
        peak_lim=a.peak_lim, rms_lo=a.rms_lo, rms_hi=a.rms_hi,
        max_edge=a.max_edge, limit_db=a.limit_db,
        gain=gain, trim=trim, pad_after=pad_after,
    )
    yml.write_text(body, encoding="utf-8")
    return {"yml": yml, "out_dir": out_dir / name, "gain": gain,
            "trim": bool(trim), "pad_after": pad_after}


def verdict(m: dict, a) -> list[str]:
    bad = []
    if m["peak_dbfs"] is not None and m["peak_dbfs"] > a.peak_lim:
        bad.append("peak")
    tp = m.get("true_peak_dbfs")
    if tp is not None and tp > a.peak_lim:
        bad.append("true_peak")
    if not (a.rms_lo <= m["rms_dbfs"] <= a.rms_hi):
        bad.append("rms")
    if m["lead_silence"] > a.max_edge:
        bad.append("lead")
    if m["trail_silence"] > a.max_edge:
        bad.append("trail")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(
        description="批量体检 / 修复一个音频目录（源文件只读）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--src", required=True, help="源目录")
    ap.add_argument("--out", default=str(ROOT / "tools" / "_out"), help="产物根目录")
    ap.add_argument("--work-dir", default=str(ROOT / "tools"), help="生成的 IR 放哪")
    ap.add_argument("--glob", default="*", help="文件通配（如 '*.mp3'）")
    ap.add_argument("--target-rms", type=float, default=-21.5,
                    help="目标响度（规格窗口内取中间偏保守的值）")
    ap.add_argument("--rms-lo", type=float, default=-23.0)
    ap.add_argument("--rms-hi", type=float, default=-18.0)
    ap.add_argument("--peak-lim", type=float, default=-3.0)
    ap.add_argument("--limit-db", type=float, default=-3.7,
                    help="限幅阈值。要按真峰值倒推，别只给采样峰值留余量")
    ap.add_argument("--max-edge", type=float, default=0.25, help="首尾静音上限（秒）")
    ap.add_argument("--render", action="store_true", help="实际渲染修复版并复检")
    ap.add_argument("--force", action="store_true", help="忽略缓存全部重建")
    a = ap.parse_args()

    src_dir = Path(a.src).resolve()
    if not src_dir.is_dir():
        print(f"源目录不存在：{src_dir}", file=sys.stderr)
        return 2
    out_root = Path(a.out).resolve()
    work = Path(a.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)

    files = collect(src_dir, a.glob)
    if not files:
        print(f"{src_dir} 下没有找到音频文件（--glob {a.glob}）", file=sys.stderr)
        return 2

    print(f"源目录（只读）  {src_dir}")
    print(f"目标口径        peak / true_peak <= {a.peak_lim} dBFS，"
          f"rms {a.rms_lo}..{a.rms_hi} dBFS，首尾静音 < {a.max_edge}s")
    print()

    rows = []
    for f in files:
        try:
            m = measure.measure_file(str(f), with_lufs=False)
        except AcompError as e:
            print(f"  跳过 {f.name}：{e.what_failed}")
            continue
        rows.append({"file": f, "m": m,
                     **make_yml(work, f, out_root, m, a)})

    print(f"{'FILE':<28}{'PEAK':>8}{'RMS':>8}{'CREST':>8}{'LEAD':>7}{'TRAIL':>7}"
          f"{'GAIN':>7}  TRIM")
    for r in rows:
        m = r["m"]
        print(f"{r['file'].name:<28}{m['peak_dbfs']:>8}{m['rms_dbfs']:>8}"
              f"{round(m['peak_dbfs'] - m['rms_dbfs'], 2):>8}"
              f"{m['lead_silence']:>7}{m['trail_silence']:>7}"
              f"{r['gain']:>+7}  {'yes' if r['trim'] else '-'}")
    print()
    print("crest = peak - rms。要同时满足 peak <= %.1f 且 rms >= %.1f，"
          "crest 必须 <= %.1f dB。" % (a.peak_lim, a.rms_lo, a.peak_lim - a.rms_lo))
    print("素材 crest 由内容决定，调增益只能整体平移 —— crest 超限时只能限幅。")

    if not a.render:
        print()
        print(f"（体检模式，没有渲染任何东西。生成的 IR 在 {work}）")
        print("加 --render 就会真的构建修复版并复检。")
        return 0

    print()
    for r in rows:
        print(f"=== render {r['file'].name} (gain {r['gain']:+.1f} dB) ===")
        proc = subprocess.run([sys.executable, str(ROOT / "acomp.py"),
                               "render", str(r["yml"])] + (["--force"] if a.force else []),
                              cwd=str(ROOT))
        if proc.returncode not in (0, 1):
            print(f"  !! render 失败，退出码 {proc.returncode}")
        print()

    print("=" * 78)
    print(f"复检（交付口径：真峰值 <= {a.peak_lim} / rms {a.rms_lo}..{a.rms_hi} "
          f"/ 首尾静音 < {a.max_edge}s）")
    print("=" * 78)
    print(f"{'FILE':<28}{'VERDICT':<18}{'PEAK':>8}{'TRUE':>8}{'RMS':>8}"
          f"{'LUFS':>8}{'LEAD':>7}{'TRAIL':>7}")
    all_ok = True
    for r in rows:
        mp3 = r["out_dir"] / f"{r['file'].stem}_master.mp3"
        if not mp3.exists():
            print(f"{r['file'].name:<28}{'NO_OUTPUT':<18}")
            all_ok = False
            continue
        m = measure.measure_file(str(mp3), with_lufs=True)
        bad = verdict(m, a)
        if bad:
            all_ok = False
        label = "PASS" if not bad else "FAIL(" + ",".join(bad) + ")"
        print(f"{r['file'].name:<28}{label:<18}{m['peak_dbfs']:>8}"
              f"{str(m.get('true_peak_dbfs')):>8}{m['rms_dbfs']:>8}"
              f"{str(m.get('integrated_lufs')):>8}"
              f"{m['lead_silence']:>7}{m['trail_silence']:>7}")
    print()
    print("全部 PASS" if all_ok else "仍有未达标项")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
