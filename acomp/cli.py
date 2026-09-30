"""命令行入口。

设计取向（AI 是一等公民）：
  * 每条命令默认「无副作用 / 可预演」：validate 不写盘，plan 不写盘，
    render 只在确认后才动文件系统，且带段落级缓存，重跑幂等。
  * 输出既有给人看的表格，也有给机器读的 report.json（--json 直接打到 stdout）。
  * 出错一律 JSON：code / what_failed / minimal_fix / retryable。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import (audio, diff as diff_mod, feasibility, ir as ir_mod,
               measure, report as report_mod)
from .errors import AcompError, ValidationFailed


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


# --------------------------------------------------------------------------
def cmd_init(a) -> int:
    path = os.path.join(a.dir, a.file or f"{a.name}.yml")
    if os.path.exists(path) and not a.force:
        raise AcompError(
            code="INIT_EXISTS",
            what_failed=f"文件已存在：{path}",
            minimal_fix="加 --force 覆盖，或换 --file 名字",
            retryable=False, detail={"path": path},
        )
    os.makedirs(a.dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(ir_mod.TEMPLATE.format(name=a.name))
    print(json.dumps({"ok": True, "created": os.path.abspath(path)}, ensure_ascii=False))
    return 0


def cmd_validate(a) -> int:
    try:
        proj = ir_mod.load(a.file)
        issues: list[dict] = []
    except ValidationFailed as e:
        proj, issues = None, e.issues
        raw_segments = _raw(a.file).get("segments") or []
    else:
        raw_segments = proj.segments
    errors = [i for i in issues if i["severity"] == "error"]
    warns = [i for i in issues if i["severity"] == "warn"]
    payload = {"ok": not errors, "file": os.path.abspath(a.file),
               "segments": len(raw_segments),
               "errors": errors, "warnings": warns}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def _raw(path):
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cmd_plan(a) -> int:
    proj = ir_mod.load(a.file)
    rows, problems = [], []
    total = 0.0
    seg_stats: list[dict] = []
    for s in proj.active_segments:
        src = proj.resolve(s.src)
        if not os.path.exists(src):
            problems.append({"segment": s.id, "src": s.src,
                             "what": "源文件不存在",
                             "fix": f"把文件放到 {src}，或改 segments[].src"})
            rows.append({"id": s.id, "src": s.src, "dur": None, "status": "MISSING"})
            continue
        info = audio.media_info(src)
        src_dur = info["duration"]
        end = s.trim_end if s.trim_end is not None else src_dur
        body = max(0.0, (end or 0.0) - s.trim_start)
        pad = s.pad_before + s.pad_after
        d = s.effective_duration(src_dur)
        rows.append({"id": s.id, "src": s.src, "dur": None if d is None else round(d, 3),
                     "status": "OK"})
        if d:
            total += d
        # 可达性预测要的是「trim 之后的内容窗口」的实测电平，
        # 拿整素材去测会被首尾静音污染，预测从第一步就错。
        # true_peak 是可选的（每段多跑一次 ebur128），但不开就拿不准 mp3 编码后的真实峰值。
        if a.feasibility and body > 0:
            try:
                w = measure.measure_window(src, s.trim_start or None, body,
                                           with_true_peak=True)
                seg_stats.append({"id": s.id, "body_dur": body, "pad_dur": pad,
                                  "gain_db": s.gain_db, **w})
            except AcompError:
                pass
    cf_sum = sum(s.crossfade_next for s in proj.active_segments[:-1])
    est = round(max(0.0, total - cf_sum), 3) if rows else 0.0

    warns = []
    if any(r["status"] == "OK" for r in rows):
        rng = proj.targets.get("total_duration")
        if rng and not rng.holds(est):
            warns.append(f"预估总时长 {est}s 不满足 targets.total_duration {rng.describe()}")

    feas = None
    if a.feasibility and seg_stats and not problems:
        try:
            feas = feasibility.analyze(
                seg_stats, proj.targets,
                master_gain_db=proj.output.get("master_gain_db") or 0.0,
                limiter=proj.output.get("limiter"),
                crossfade_total=cf_sum,
                has_mp3=bool(proj.output.get("mp3")),
            )
        except AcompError as e:
            warns.append(f"可达性预测跳过：{e.what_failed}")

    payload = {
        "ok": not problems,
        "project": proj.name,
        "out_dir": proj.out_dir,
        "segments": rows,
        "estimated_total_duration": est,
        "crossfade_total": round(cf_sum, 3),
        "will_write": [proj.master_path]
        + ([os.path.join(proj.out_dir, proj.output["mp3"]["name"])]
           if proj.output.get("mp3") else [])
        + [os.path.join(proj.out_dir, "report.json")],
        "problems": problems,
        "warnings": warns,
    }
    if feas:
        payload["feasibility"] = feas
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"plan: {proj.name}  （dry-run，不写任何文件）")
        print(f"{'ID':<8}{'STATUS':<10}{'DUR(s)':>10}   SRC")
        for r in rows:
            print(f"{r['id']:<8}{r['status']:<10}"
                  f"{'' if r['dur'] is None else format(r['dur'], '.2f'):>10}   {r['src']}")
        print(f"\n预估总时长 {est}s（已扣交叉淡化 {cf_sum}s）")
        if feas:
            print()
            print(feasibility.format_feasibility(feas))
        if warns:
            print()
            for w in warns:
                print(f"  警告: {w}")
        if problems:
            print(f"\n{len(problems)} 个阻塞问题：")
            for p in problems:
                print(f"  [{p['segment']}] {p['what']} —— {p['fix']}")
    return 0 if not problems else 1


def _master_post(proj) -> str | None:
    """拼接之后追加的母带链：整体增益 + 限幅。

    限幅不是为了"更好听"，是为了让 Peak 和 RMS 能同时达标 ——
    素材峰均比（crest）大于目标窗口时，纯增益在数学上无解，只能压峰值。
    """
    bits: list[str] = []
    g = proj.output.get("master_gain_db") or 0.0
    if g:
        bits.append(f"volume={g}dB")
    lim = proj.output.get("limiter")
    if lim:
        linear = 10.0 ** (lim["limit_db"] / 20.0)
        bits.append(
            f"alimiter=limit={linear:.6f}:attack={lim['attack']}:release={lim['release']}"
            f":level=disabled"
        )
    return ",".join(bits) or None


def cmd_render(a) -> int:
    proj = ir_mod.load(a.file)
    if a.out:
        proj.out_dir = os.path.abspath(a.out)
    os.makedirs(proj.out_dir, exist_ok=True)
    cache = os.path.join(proj.out_dir, ".cache")
    os.makedirs(cache, exist_ok=True)

    segs = proj.active_segments
    if not segs:
        raise AcompError(
            code="NO_SEGMENTS",
            what_failed="没有启用任何段落",
            minimal_fix="至少把一段的 enabled 设为 true",
            retryable=False,
        )

    built, reused = [], 0
    for s in segs:
        src = proj.resolve(s.src)
        if not os.path.exists(src):
            raise AcompError(
                code="SRC_MISSING",
                what_failed=f"段落 {s.id} 的源文件不存在：{src}",
                minimal_fix=f"把文件放到该路径，或改 segments[{s.index}].src",
                retryable=False, detail={"segment": s.id, "path": src},
            )
        st = os.stat(src)
        fp = ir_mod.segment_fingerprint(s, proj, st)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in s.id)
        dst = os.path.join(cache, f"{s.index:02d}_{safe_id}_{fp}.wav")
        if os.path.exists(dst) and not a.force:
            reused += 1
        else:
            audio.build_segment(s, src, dst, proj)
        built.append(dst)

    crossfades = [s.crossfade_next for s in segs[:-1]]
    master = proj.master_path
    audio.concat_segments(built, crossfades, master, proj, post=_master_post(proj))
    artifacts = {"master": os.path.abspath(master)}

    if proj.output.get("mp3"):
        cfg = proj.output["mp3"]
        mp3_path = os.path.join(proj.out_dir, cfg["name"])
        audio.encode_mp3(master, mp3_path, cfg)
        artifacts["mp3"] = os.path.abspath(mp3_path)

    thr = proj.measure["silence_threshold_db"]
    seg_metrics = []
    for s, w in zip(segs, built):
        # 段落级不测 LUFS：响度是整章指标，段落的 LUFS 没有验收意义，
        # 而且每段多跑一次 ebur128 在长章上是纯浪费。
        m = measure.measure_file(w, silence_threshold_db=thr, with_lufs=False)
        entry = {"id": s.id, "index": s.index, "src": s.src, "metrics": m}
        if s.expect_text and a.asr:
            got = measure.asr_text(w, a.asr_model)
            entry["asr"] = {
                "expect": s.expect_text,
                "got": got,
                "similarity": None if got is None
                else measure.text_similarity(s.expect_text, got),
            }
        seg_metrics.append(entry)

    m_metrics = measure.measure_file(master, silence_threshold_db=thr)
    m_metrics["path"] = os.path.basename(master)

    seg_targets = report_mod.segment_targets(proj)
    rep = report_mod.build_report(proj, seg_metrics, m_metrics, artifacts)
    # 用 IR 里指定的段落级检查项重算（默认只对段落查电平，边缘静音归 master）
    for row in rep["segments"]:
        row["checks"] = report_mod.check_metrics(row["metrics"], proj.targets, seg_targets)
        row["pass"] = all(c["pass"] for c in row["checks"])
    rep["summary"]["failures"] = [
        {"scope": "segment", "id": r["id"], **c}
        for r in rep["segments"] for c in r["checks"] if not c["pass"]
    ] + [{"scope": "master", "id": proj.name, **c}
         for c in rep["master"]["checks"] if not c["pass"]]
    rep["summary"]["segments_passed"] = sum(1 for r in rep["segments"] if r["pass"])
    rep["summary"]["segments_failed"] = len(rep["segments"]) - rep["summary"]["segments_passed"]
    rep["ok"] = rep["master"]["pass"] and rep["summary"]["segments_failed"] == 0
    rep["segment_checks_scope"] = list(seg_targets)

    rp = os.path.join(proj.out_dir, "report.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    rep["artifacts"]["report"] = os.path.abspath(rp)

    print(report_mod.format_report(rep, as_json=a.json), file=sys.stdout)
    if not a.json and reused:
        print(f"\n（{reused}/{len(segs)} 段命中缓存，未重新构建）")
    if a.exit_zero:
        return 0
    return 0 if rep["ok"] else 1


def cmd_probe(a) -> int:
    if not os.path.exists(a.path):
        raise AcompError(code="SRC_MISSING",
                         what_failed=f"文件不存在：{a.path}",
                         minimal_fix="检查路径", retryable=False)
    thr = -50.0
    targets = None
    if a.project:
        proj = ir_mod.load(a.project)
        thr = proj.measure["silence_threshold_db"]
        targets = proj.targets
    m = measure.measure_file(a.path, silence_threshold_db=thr)
    payload = {"ok": True, "metrics": m}
    if targets:
        checks = report_mod.check_metrics(m, targets, report_mod.MASTER_TARGETS)
        payload["checks"] = checks
        payload["pass"] = all(c["pass"] for c in checks)
    if a.asr:
        got = measure.asr_text(a.path, a.asr_model)
        payload["asr"] = {"got": got}
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{os.path.basename(a.path)}")
        for k in ("duration", "sample_rate", "channels", "peak_dbfs", "rms_dbfs",
                  "lead_silence", "trail_silence", "integrated_lufs",
                  "true_peak_dbfs", "lra", "crest_db", "bytes"):
            if m.get(k) is not None:
                print(f"  {k:<16}{m.get(k)}")
        for c in payload.get("checks", []):
            print(f"  {'PASS' if c['pass'] else 'FAIL'}  {c['target']:<16}"
                  f"{c['value']}   要求 {c['range']}")
    return 0


def cmd_diff(a) -> int:
    d = diff_mod.diff(a.a, a.b)
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(diff_mod.format_diff(d))
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="acomp",
        description="acomp —— 声明式音频后期编译器（AI 原生：读 IR，出产物 + 测量报告）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="生成一份 project.yml 模板")
    s.add_argument("name")
    s.add_argument("--dir", default=".")
    s.add_argument("--file", default=None)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("validate", help="只校验 IR，不写任何文件")
    s.add_argument("file")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("plan", help="dry-run：探测素材、预估时长、判定目标是否可达")
    s.add_argument("file")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-feasibility", dest="feasibility",
                   action="store_false", default=True,
                   help="跳过可达性预测（不解码素材，只做静态检查）")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("render", help="构建 master + mp3 + 测量报告")
    s.add_argument("file")
    s.add_argument("--out", default=None, help="覆盖 project.out_dir")
    s.add_argument("--force", action="store_true", help="忽略缓存全部重建")
    s.add_argument("--json", action="store_true")
    s.add_argument("--exit-zero", action="store_true",
                   help="即使有 FAIL 也返回 0")
    s.add_argument("--asr", action="store_true",
                   help="对设了 expect_text 的段落跑语音转写对齐（需要 faster_whisper）")
    s.add_argument("--asr-model", default="base")
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("probe", help="测单个音频文件，输出纯数字")
    s.add_argument("path")
    s.add_argument("--project", default=None, help="带上 project.yml 以判定 PASS/FAIL")
    s.add_argument("--json", action="store_true")
    s.add_argument("--asr", action="store_true")
    s.add_argument("--asr-model", default="base")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("diff", help="语义 diff：两份 IR 或两份 report")
    s.add_argument("a")
    s.add_argument("b")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_diff)
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AcompError as e:
        print(e.render(), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "code": "INTERRUPTED",
                          "what_failed": "用户中断",
                          "minimal_fix": "重跑即可；已写入的中间文件在 out/.cache",
                          "retryable": True}, ensure_ascii=False), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
