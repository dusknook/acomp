"""Diff —— 不是行 diff，是语义 diff。

git diff 告诉你哪一行变了；acomp diff 告诉你「第 3 段增益从 -1.5 改到 -3.0，
master 响度从 -16.9 掉到 -21.4，peak 从 FAIL 变 PASS」。
这才是能直接支撑下一步决策的信息。
"""

from __future__ import annotations

import os

import yaml

from .errors import AcompError
from . import ir as ir_mod


def load_any(path: str) -> tuple[str, object]:
    """猜输入是 IR 还是 report，返回 (kind, data)。"""
    if not os.path.exists(path):
        raise AcompError(
            code="DIFF_INPUT_MISSING",
            what_failed=f"文件不存在：{path}",
            minimal_fix="检查路径；diff 接受 project.yml 或 render 产出的 report.json",
            retryable=False,
            detail={"path": path},
        )
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise AcompError(
            code="DIFF_INPUT_UNKNOWN",
            what_failed=f"既不像 IR 也不像 report：{path}",
            minimal_fix="传入 project.yml（含 segments）或 report.json（含 master）",
            retryable=False,
        )
    if "segments" in raw and "master" not in raw:
        return "ir", ir_mod.parse(raw, path)
    if "master" in raw:
        return "report", raw
    raise AcompError(
        code="DIFF_INPUT_UNKNOWN",
        what_failed=f"分不清是 IR 还是 report：{path}（顶层键：{sorted(raw)[:8]}）",
        minimal_fix="IR 需要 segments 键，report 需要 master 键",
        retryable=False,
    )


_SEG_FIELDS = ("src", "trim_start", "trim_end", "gain_db", "fade_in", "fade_out",
               "pad_before", "pad_after", "crossfade_next", "enabled", "note")


def _seg_map(proj):
    return {s.id: s for s in proj.segments}


def diff_ir(a, b) -> dict:
    ma, mb = _seg_map(a), _seg_map(b)
    added = [i for i in mb if i not in ma]
    removed = [i for i in ma if i not in mb]
    modified = []
    for i in sorted(set(ma) & set(mb)):
        changes = []
        for f in _SEG_FIELDS:
            va, vb = getattr(ma[i], f), getattr(mb[i], f)
            if va != vb:
                changes.append({"field": f, "from": va, "to": vb})
        if ma[i].index != mb[i].index:
            changes.append({"field": "order", "from": ma[i].index, "to": mb[i].index})
        if changes:
            modified.append({"id": i, "changes": changes})

    target_changes = []
    for k in sorted(set(a.targets) | set(b.targets)):
        ra, rb = a.targets.get(k), b.targets.get(k)
        da = ra.describe() if ra else None
        db = rb.describe() if rb else None
        if da != db:
            target_changes.append({"target": k, "from": da, "to": db})

    return {
        "type": "ir",
        "a": a.path,
        "b": b.path,
        "segments": {"added": added, "removed": removed, "modified": modified},
        "targets": target_changes,
        "identical": not (added or removed or modified or target_changes),
    }


_METRIC_KEYS = ("duration", "peak_dbfs", "rms_dbfs", "lead_silence", "trail_silence")


def diff_report(ra: dict, rb: dict) -> dict:
    sa = {s["id"]: s for s in ra.get("segments", [])}
    sb = {s["id"]: s for s in rb.get("segments", [])}
    rows = []
    for sid in sorted(set(sa) | set(sb)):
        A, B = sa.get(sid), sb.get(sid)
        entry: dict = {"id": sid}
        if A is None:
            entry["status"] = "added"
        elif B is None:
            entry["status"] = "removed"
        else:
            ma_, mb_ = A["metrics"], B["metrics"]
            deltas = {}
            for k in _METRIC_KEYS:
                va, vb = ma_.get(k), mb_.get(k)
                if va is None and vb is None:
                    continue
                try:
                    deltas[k] = {"from": va, "to": vb,
                                 "delta": None if (va is None or vb is None)
                                 else round(float(vb) - float(va), 3)}
                except (TypeError, ValueError):
                    deltas[k] = {"from": va, "to": vb, "delta": None}
            # 只有真的变了才算 modified；delta 为 0（或 None）的项直接丢掉，
            # 否则每段都会被标成 modified，diff 就没有信噪比了
            deltas = {k: v for k, v in deltas.items()
                      if v.get("delta") not in (None, 0, 0.0)}
            pa, pb = A.get("pass"), B.get("pass")
            entry["status"] = "modified" if (deltas or pa != pb) else "unchanged"
            entry["metrics"] = deltas
            entry["pass_from"] = pa
            entry["pass_to"] = pb
            if pa is not None and pb is not None and pa != pb:
                entry["transition"] = f"{'PASS' if pa else 'FAIL'} -> {'PASS' if pb else 'FAIL'}"
        rows.append(entry)

    def mm(r, key):
        return (r.get("master") or {}).get("metrics", {}).get(key)

    master_deltas = {}
    for k in _METRIC_KEYS:
        va, vb = mm(ra, k), mm(rb, k)
        if va is None and vb is None:
            continue
        try:
            master_deltas[k] = {"from": va, "to": vb,
                                "delta": None if (va is None or vb is None)
                                else round(float(vb) - float(va), 3)}
        except (TypeError, ValueError):
            master_deltas[k] = {"from": va, "to": vb, "delta": None}

    master_deltas = {k: v for k, v in master_deltas.items()
                     if v.get("delta") not in (None, 0, 0.0)}

    pa = (ra.get("master") or {}).get("pass")
    pb = (rb.get("master") or {}).get("pass")
    return {
        "type": "report",
        "a": ra.get("ir") or ra.get("generated_at"),
        "b": rb.get("ir") or rb.get("generated_at"),
        "master": {"metrics": master_deltas,
                   "pass_from": pa, "pass_to": pb,
                   "transition": (None if (pa is None or pb is None or pa == pb)
                                  else f"{'PASS' if pa else 'FAIL'} -> {'PASS' if pb else 'FAIL'}")},
        "segments": rows,
        "identical": all(r["status"] == "unchanged" for r in rows) and not master_deltas,
    }


def diff(a_path: str, b_path: str) -> dict:
    ka, da = load_any(a_path)
    kb, db = load_any(b_path)
    if ka != kb:
        raise AcompError(
            code="DIFF_TYPE_MISMATCH",
            what_failed=f"两边类型不同：{a_path} 是 {ka}，{b_path} 是 {kb}",
            minimal_fix="diff 两边同为 project.yml，或同为 report.json",
            retryable=False,
        )
    return diff_ir(da, db) if ka == "ir" else diff_report(da, db)


def format_diff(d: dict) -> str:
    out: list[str] = []
    if d["type"] == "ir":
        out.append(f"IR diff: {d['a']}  ->  {d['b']}")
        s = d["segments"]
        if s["added"]:
            out.append(f"  新增段: {', '.join(s['added'])}")
        if s["removed"]:
            out.append(f"  删除段: {', '.join(s['removed'])}")
        for m in s["modified"]:
            out.append(f"  改段 {m['id']}:")
            for c in m["changes"]:
                out.append(f"    {c['field']}: {c['from']} -> {c['to']}")
        for t in d["targets"]:
            out.append(f"  target {t['target']}: {t['from']} -> {t['to']}")
        if d["identical"]:
            out.append("  无差异")
    else:
        out.append("report diff")
        m = d["master"]
        if m["transition"]:
            out.append(f"  MASTER {m['transition']}")
        for k, v in (m["metrics"] or {}).items():
            if v.get("delta") not in (None, 0, 0.0):
                out.append(f"  MASTER {k}: {v['from']} -> {v['to']} ({v['delta']:+.3f})")
        for r in d["segments"]:
            if r["status"] == "unchanged":
                continue
            tag = r.get("transition") or r["status"]
            out.append(f"  {r['id']}: {tag}")
            for k, v in (r.get("metrics") or {}).items():
                if v.get("delta") not in (None, 0, 0.0):
                    out.append(f"    {k}: {v['from']} -> {v['to']} ({v['delta']:+.3f})")
        if d["identical"]:
            out.append("  无差异")
    return "\n".join(out)
