"""可达性预测 —— 在 render 之前就回答「这组目标能不能同时满足」。

这是 acomp 里最像「给 AI 用的工具」的一块。

人用工具时，"试一下不对再调"成本很低：拖一下滑块，听一秒，知道不对。
AI 没有这条回路 —— 它唯一的办法是 render 完看报告。而报告只在**做完了**
才告诉你不对。所以要把判定往前挪：素材还在磁盘上的时候就先算一遍。

核心是一条算术：

    Peak <= Pmax  且  RMS >= Rmin
    等价于   crest = peak - rms  <=  Pmax - Rmin

增益只是整体平移，改不了 crest。所以 crest 超了就是**数学上无解**，
不是"再调调音量就行"。这时候必须压峰值 —— 也就是限幅。

这类"在做之前说走不通"的判断，才是 AI 原生工具相对 GUI 的真正优势：
GUI 永远只能告诉你"刚才那次不对"，它没法告诉你"这个目标根本到不了"。
"""

from __future__ import annotations

import math

# 限幅把峰值压下去时，RMS 也会被拖下来一点（削掉的是最高的那部分能量）。
# 系数用真实语音素材（5 章有声书，约 35 分钟）实测校准过：削峰 1.74 dB，RMS 只掉 0.07 dB
# —— 语音素材的峰值很稀疏，硬限幅对整体能量影响远小于直觉。
# 仍然给区间而不是点估计：不同素材的峰值密度差别很大。
RMS_COUPLING_LO = 0.03
RMS_COUPLING_HI = 0.15

# mp3 编码会让峰值反弹（实测 0.1~0.3 dB）。有 mp3 输出时必须把这段吃掉，
# 否则"刚好卡在 -3.0"的母带编出来就是 -2.8，白跑一趟。
ENCODE_MARGIN_DB = 0.3

# 采样峰值 -> 真峰值的高估量。实测 mp3 的 true peak 比 sample peak 高 0.2~0.4 dB。
# 只在 targets 里写了 true_peak_dbfs 时才纳入计算。
TRUE_PEAK_HEADROOM = 0.4

# 限幅阈值相对峰值上限留的余量
LIMITER_MARGIN = 0.3

# crest 距离上限不足这么多时，判"OK 但没余量"——理论可达，实践中一踩线就废
TIGHT_MARGIN_DB = 1.0

# 削峰超过这个量就该警告：alimiter 是硬限幅，削太多会出可闻失真
DISTORTION_WARN_DB = 6.0
DISTORTION_HARD_DB = 10.0


def _after_limiter(peak_pre: float, rms_pre: float, limit_db: float,
                   ) -> tuple[float, float, float]:
    """限幅后的 (peak, rms_pessimistic, rms_optimistic)。"""
    cut = max(0.0, peak_pre - limit_db)
    return (min(peak_pre, limit_db),
            rms_pre - RMS_COUPLING_HI * cut,
            rms_pre - RMS_COUPLING_LO * cut)


def _min_gain(rms0: float, peak0: float, limit_db: float, r_min: float) -> float:
    """峰值由限幅兜住时，让 rms >= r_min 所需的最小 master 增益。"""
    g_a = r_min - rms0                       # 不撞限幅的情形
    if peak0 + g_a <= limit_db:
        return g_a
    c = RMS_COUPLING_HI                      # 撞限幅后 rms 只能按 (1-c) 的比例涨
    return (r_min - rms0 + c * peak0 - c * limit_db) / (1.0 - c)


def estimate_master(seg_stats: list[dict]) -> dict:
    """把各段的测量结果合成 master 的预测值。

    seg_stats 每项：{id, body_dur, pad_dur, peak_dbfs, rms_dbfs, gain_db,
                       true_peak_dbfs (可选)}
    peak/rms 是**应用段增益之前**、trim 之后的内容窗口实测值。

    时长按 body+pad 计，能量只算 body（pad 是静音，不贡献能量）——
    漏掉这点会让加了段间静音的工程 RMS 预测偏高。
    """
    total_dur = 0.0
    energy = 0.0
    peak = None
    true_peak = None
    for s in seg_stats:
        body = max(0.0, float(s["body_dur"] or 0.0))
        pad = max(0.0, float(s["pad_dur"] or 0.0))
        if body <= 0:
            continue
        total_dur += body + pad
        g = float(s.get("gain_db") or 0.0)
        energy += body * (10.0 ** ((float(s["rms_dbfs"]) + g) / 10.0))
        p = float(s["peak_dbfs"]) + g
        peak = p if peak is None else max(peak, p)
        # 真峰值：实测优先；缺数据时用「采样峰值 + 经验高估量」兜底
        tp_raw = s.get("true_peak_dbfs")
        if tp_raw is None:
            tp = float(s["peak_dbfs"]) + TRUE_PEAK_HEADROOM + g
        else:
            tp = float(tp_raw) + g
        true_peak = tp if true_peak is None else max(true_peak, tp)
    if total_dur <= 0 or peak is None:
        return {"peak_dbfs": None, "rms_dbfs": None,
                "true_peak_dbfs": None, "duration": 0.0}
    return {"peak_dbfs": round(peak, 2),
            "rms_dbfs": round(10.0 * math.log10(max(energy / total_dur, 1e-30)), 2),
            "true_peak_dbfs": round(true_peak, 2),
            "duration": round(total_dur, 3)}


def analyze(seg_stats: list[dict], targets: dict, *,
            master_gain_db: float = 0.0,
            limiter: dict | None = None,
            crossfade_total: float = 0.0,
            has_mp3: bool = False) -> dict:
    """预测 master 能否达标，并在不能达标时给出可执行的修法。"""
    est = estimate_master(seg_stats)
    peak0, rms0 = est["peak_dbfs"], est["rms_dbfs"]
    true_peak0 = est["true_peak_dbfs"]
    if peak0 is None:
        return {"verdict": "NO_DATA", "reason": "没有任何有效段落可测",
                "reachable": False}

    duration = round(max(0.0, est["duration"] - crossfade_total), 3)

    p_rng = targets.get("peak_dbfs")
    r_rng = targets.get("rms_dbfs")
    p_raw = None if p_rng is None else p_rng.hi
    r_min = None if r_rng is None else r_rng.lo
    r_max = None if r_rng is None else r_rng.hi
    # 有 mp3 输出时把编码反弹吃进预算，否则判"可达"但实际编出来就超
    p_eff = None if p_raw is None else round(
        p_raw - (ENCODE_MARGIN_DB if has_mp3 else 0.0), 2)

    # ---- 按当前 IR 配置的预测 ----
    g = master_gain_db
    tp0 = est["true_peak_dbfs"]
    if limiter:
        pk, rl, rh = _after_limiter(peak0 + g, rms0 + g, limiter["limit_db"])
        # 限幅后真峰值 ≈ limit + 真峰高估（alimiter 实测高 0.1~0.4 dB）
        tp_post = limiter["limit_db"] + TRUE_PEAK_HEADROOM
        cur = {"peak_dbfs": round(pk, 2),
               "rms_dbfs": round((rl + rh) / 2.0, 2),
               "rms_range": [round(rl, 2), round(rh, 2)],
               "true_peak_dbfs": round(tp_post, 2),
               "duration": duration}
    else:
        cur = {"peak_dbfs": round(peak0 + g, 2), "rms_dbfs": round(rms0 + g, 2),
               "true_peak_dbfs": round(tp0 + g, 2), "duration": duration}
    # 限幅已经把编码余量吃在阈值里了（limit = Pmax - LIMITER_MARGIN），
    # 再扣一次 ENCODE_MARGIN 就是双重扣减，会把明明能过的配置判成 FAIL。
    p_judge = p_raw if limiter else p_eff
    cur["pass"] = _passes(cur, p_judge, r_min, r_max)
    # 真峰值的真值：限幅后预测已含编码余量；无限幅时还要加
    tp_rng = targets.get("true_peak_dbfs")
    tp_max = None if tp_rng is None else tp_rng.hi
    if tp_max is not None:
        if limiter:
            cur["true_peak_est"] = cur["true_peak_dbfs"]
        else:
            # 无限幅：真峰值 + 编码反弹（mp3 通常再高 0.1~0.3 dB）
            extra = ENCODE_MARGIN_DB if has_mp3 else 0.0
            cur["true_peak_est"] = round(cur["true_peak_dbfs"] + extra, 2)
        if cur["true_peak_est"] > tp_max:
            cur["pass"] = False

    # ---- 可达性：crest 判据 ----
    crest = round(peak0 - rms0, 2)
    allowed = None if (p_eff is None or r_min is None) else round(p_eff - r_min, 2)
    reason = None
    if allowed is not None and crest > allowed:
        reachable = False
        reason = (f"crest {crest} dB 超出允许上限 {allowed} dB（差 "
                  f"{round(crest - allowed, 2)} dB）：Peak<={p_raw} 与 RMS>={r_min} "
                  f"无法只靠增益同时满足，必须压峰值")
    else:
        reachable = True

    tight = False
    if reachable and allowed is not None:
        head = allowed - crest
        if head < TIGHT_MARGIN_DB:
            tight = True
            if limiter:
                pass
            else:
                reason = (f"理论可达但只剩 {round(head, 2)} dB 余量 —— "
                          f"任何素材变动或编码抖动都会踩线，建议直接上限幅")

    if reachable and not limiter and p_eff is not None and r_min is not None:
        g_lo = r_min - rms0
        g_hi = p_eff - peak0
        if g_lo > g_hi + 1e-9:
            reachable = False
            reason = (f"增益窗口为空：满足 RMS>={r_min} 至少 +{round(g_lo, 2)} dB，"
                      f"但 Peak<={p_eff} 最多只能 +{round(g_hi, 2)} dB")

    # 当前配置能不能过是硬事实；reachable_by_gain_only 只是补充信息 ——
    # 靠限幅达成的达标也是达标，不该因为"gain-only 无解"就判失败。
    if cur["pass"]:
        verdict = "OK" if reachable else "OK_WITH_LIMITER"
    elif not reachable or (tight and not limiter and p_eff is not None and crest > allowed - 0.001):
        verdict = "NEEDS_LIMITER"
    else:
        verdict = "NEEDS_ADJUST"

    # ---- 建议 ----
    rec: dict = {}
    notes: list[str] = []
    if cur["pass"]:
        if reachable:
            notes.append("当前配置预测可达标")
        else:
            notes.append("当前配置预测可达标 —— 但这是靠限幅达成的："
                         "素材 crest 本身就超过纯增益能处理的范围，别把限幅去掉")
    else:
        base = p_raw
        if tp_max is not None:
            base = (base if base is not None else tp_max)
            base = min(base, tp_max) - TRUE_PEAK_HEADROOM
        limit_db = limiter["limit_db"] if limiter else round(
            (base if base is not None else -3.0) - LIMITER_MARGIN, 2)
        if limiter and tp_max is not None \
                and limiter["limit_db"] > tp_max - TRUE_PEAK_HEADROOM - LIMITER_MARGIN + 1e-9:
            notes.append(f"当前限幅阈值 {limiter['limit_db']} 偏高：真峰值会到 "
                         f"{round(limiter['limit_db'] + TRUE_PEAK_HEADROOM, 2)} 左右，"
                         f"建议降到 {round(tp_max - TRUE_PEAK_HEADROOM - LIMITER_MARGIN, 2)}")
        target_rms = (r_min if r_min is not None else -23.0) + 0.5   # 留 0.5 防卡线
        g_need = round(_min_gain(rms0, peak0, limit_db, target_rms), 2)
        rec = {"master_gain_db": g_need,
               "limiter": {"enabled": True, "limit_db": limit_db,
                           "attack": 5, "release": 50}}
        if not reachable:
            notes.append(
                f"只调增益无解（crest {crest} > {allowed}）。"
                f"需增益 {g_need:+} dB 并用限幅把峰值压到 {limit_db}")
        elif tight and not limiter:
            notes.append(
                f"理论可达但余量只有 {round(allowed - crest, 2)} dB，"
                f"建议直接上限幅（阈值 {limit_db}）+ 增益 {g_need:+} dB，别赌那点余量")
        else:
            notes.append(f"按当前配置预测不达标，建议增益 {g_need:+} dB"
                         + (f" 并用限幅把峰值压到 {limit_db}" if not limiter else ""))
        cut = max(0.0, peak0 + g_need - limit_db)
        notes.append(f"预计削峰 {round(cut, 2)} dB；"
                     f"限幅后 RMS 实际值以 render 报告为准（本工具只给区间）")
        if cut > DISTORTION_HARD_DB:
            notes.append(f"削峰 {round(cut, 1)} dB 超过 {DISTORTION_HARD_DB} dB —— "
                         f"硬限幅会出可闻失真，应先用 acompressor 压动态，"
                         f"或者去提升偏轻段落的 gain_db")
        elif cut > DISTORTION_WARN_DB:
            notes.append(f"削峰 {round(cut, 1)} dB 偏大，注意听感；"
                         f"更好的做法是提升偏轻段落的 gain_db，而不是全靠母带限幅")

    patch = None
    if rec:
        lines = ["output:"]
        lines.append(f"  master_gain_db: {rec['master_gain_db']}")
        lm = rec["limiter"]
        lines += ["  limiter:", "    enabled: true",
                  f"    limit_db: {lm['limit_db']}",
                  f"    attack: {lm['attack']}",
                  f"    release: {lm['release']}"]
        patch = "\n".join(lines)

    return {
        "verdict": verdict,
        "reachable_by_gain_only": reachable,
        "tight": tight,
        "reason": reason,
        "crest": {"estimated": crest, "allowed": allowed,
                  "excess": None if allowed is None else round(crest - allowed, 2)},
        "baseline": {"peak_dbfs": peak0, "rms_dbfs": rms0,
                     "true_peak_dbfs": true_peak0, "duration": duration},
        "predicted_with_current_ir": cur,
        "recommendation": rec or None,
        "patch": patch,
        "notes": notes,
        "confidence": {
            "peak": "high（取各段峰值最大值；拼接只会持平或更低）",
            "rms": "medium（未建模 crossfade 重叠的能量叠加，实测通常略高于预测）",
            "duration": "high",
        },
        "segments_measured": len(seg_stats),
    }


def _passes(cur: dict, p_max: float | None, r_min: float | None,
            r_max: float | None) -> bool:
    if p_max is not None and cur["peak_dbfs"] > p_max:
        return False
    if r_min is not None and cur["rms_dbfs"] < r_min:
        return False
    if r_max is not None and cur["rms_dbfs"] > r_max:
        return False
    return True


def format_feasibility(f: dict) -> str:
    lines: list[str] = []
    lines.append(f"可达性：{f['verdict']}"
                 + ("（仅靠增益可达）" if f.get("reachable_by_gain_only") else ""))
    if f["reason"]:
        lines.append(f"  原因：{f['reason']}")
    c = f["crest"]
    if c["allowed"] is not None:
        tail = (f"超出 {c['excess']} dB" if c["excess"] > 0
                else f"余量 {round(-c['excess'], 2)} dB")
        lines.append(f"  crest：素材 {c['estimated']} dB / 允许 {c['allowed']} dB / {tail}")
    b = f["baseline"]
    tp0_str = f" / 真峰值 {b['true_peak_dbfs']}" if b.get("true_peak_dbfs") is not None else ""
    lines.append(f"  素材基线（含段增益、未含母带增益）："
                 f"peak {b['peak_dbfs']} / rms {b['rms_dbfs']}{tp0_str} / {b['duration']}s")
    cur = f["predicted_with_current_ir"]
    extra = f"（RMS 区间 {cur['rms_range'][0]} ~ {cur['rms_range'][1]}）" \
        if "rms_range" in cur else ""
    lines.append(f"  按当前 IR 预测：peak {cur['peak_dbfs']} / rms {cur['rms_dbfs']}"
                 f"{extra}  ->  {'PASS' if cur['pass'] else 'FAIL'}")
    if cur.get("true_peak_est") is not None:
        lines.append(f"  真峰值预测：{cur['true_peak_est']} dBFS"
                     f"（采样峰值 +{TRUE_PEAK_HEADROOM} 经验高估量）")
    if f["recommendation"]:
        lines.append("")
        lines.append("  建议改动（可直接粘进 IR 的 output: 段）：")
        for ln in (f["patch"] or "").splitlines():
            lines.append("    " + ln)
    for n in f["notes"]:
        lines.append(f"  注：{n}")
    return "\n".join(lines)
