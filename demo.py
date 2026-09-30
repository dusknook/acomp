#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""acomp 一键演示。

跑这一条命令就够了：

    python demo.py

它会自动完成下面这件事，中间不需要你手动装任何东西：

    造 3 段合成测试音
      -> validate（只校验 IR，不写盘）
      -> plan（dry-run，预判目标能不能达到）
      -> render 过响版（S02 报 FAIL，退出码 1）
      -> render 修好版（全部 PASS，退出码 0）
      -> diff 两份报告（看语义级差异）

依赖（pyyaml / numpy / imageio-ffmpeg）缺失时会自动建 `.venv` 并安装，
装好后自动切到该环境重跑自己。ffmpeg 由 imageio-ffmpeg 内置提供，不需要单独安装。

参数：
    --fresh     先清掉 examples/raw 与 examples/out，从零重来
    --no-setup  不自动建 venv / 装依赖，缺依赖就直接报错退出
    --keep      保留自动生成的 _demo_hot.yml（调试用）
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACOMP_PY = ROOT / "acomp.py"
EXAMPLES = ROOT / "examples"
OUT_DIR = EXAMPLES / "out"
RAW_DIR = EXAMPLES / "raw"
HOT_YML = EXAMPLES / "_demo_hot.yml"
VENV = ROOT / ".venv"
REQUIRED = ("yaml", "numpy", "imageio_ffmpeg")
BOOTSTRAP_FLAG = "ACOMP_DEMO_BOOTSTRAPPED"

TOTAL_STEPS = 6


# ---------------------------------------------------------------- 终端输出
def _utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
        except Exception:
            pass


def banner(text: str) -> None:
    print()
    print("=" * 76)
    print(text)
    print("=" * 76)


def step(idx: int, title: str) -> None:
    print()
    print(f"[{idx}/{TOTAL_STEPS}] {title}")
    print("-" * 76)


def note(text: str) -> None:
    print(f"    {text}")


# ---------------------------------------------------------------- 环境自举
def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def missing_deps() -> list[str]:
    import importlib.util

    return [m for m in REQUIRED if importlib.util.find_spec(m) is None]


def bootstrap(no_setup: bool) -> None:
    """依赖不全时建 venv 装依赖，然后把自己重新拉起一次。"""
    if not missing_deps():
        return

    missing = ", ".join(missing_deps())
    if no_setup:
        sys.exit(
            f"缺少依赖：{missing}\n"
            f"手动装：{Path(sys.executable).name} -m pip install -r requirements.txt\n"
            f"（或去掉 --no-setup 让本脚本自动建 .venv）"
        )

    print(f"缺少依赖：{missing}")
    py = venv_python()
    if not py.exists():
        print(f"创建虚拟环境：{VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    print("安装依赖（pyyaml / numpy / imageio-ffmpeg）……")
    subprocess.run(
        [str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check",
         "-r", str(ROOT / "requirements.txt")],
        check=True,
    )
    print("依赖就绪，切换到该环境继续。")
    env = dict(os.environ, **{BOOTSTRAP_FLAG: "1"})
    raise SystemExit(subprocess.run([str(py), str(Path(__file__).resolve()),
                                     *sys.argv[1:]], env=env).returncode)


# ---------------------------------------------------------------- 步骤
def run(cmd: list[str], expect_codes: tuple[int, ...] = (0,)) -> int:
    note("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run([str(c) for c in cmd], cwd=str(ROOT))
    code = proc.returncode
    if code not in expect_codes:
        print()
        print(f"!! 上一步退出码 {code}，不在预期 {expect_codes} 内 —— 演示中断。")
        raise SystemExit(code)
    return code


def write_hot_yml() -> None:
    """把 demo.yml 复制一份，只改 S02 的 gain_db —— 故意让它过响。"""
    text = (EXAMPLES / "demo.yml").read_text(encoding="utf-8")
    hot, n = re.subn(r"^(\s*)gain_db:\s*-?16\.0.*$", r"\1gain_db: 0.0", text,
                     flags=re.M)
    if n != 1:
        raise SystemExit("demo.yml 里找不到 S02 的 gain_db: -16.0，无法生成过响版本")
    HOT_YML.write_text(hot, encoding="utf-8")


def main() -> int:
    _utf8()

    ap = argparse.ArgumentParser(description="acomp 一键演示")
    ap.add_argument("--fresh", action="store_true", help="清掉 raw/out 从零重来")
    ap.add_argument("--no-setup", action="store_true", help="不自动建 venv 装依赖")
    ap.add_argument("--keep", action="store_true", help="保留临时生成的 _demo_hot.yml")
    a = ap.parse_args()

    if os.environ.get(BOOTSTRAP_FLAG) != "1":
        bootstrap(a.no_setup)

    if a.fresh:
        for d in (RAW_DIR, OUT_DIR):
            if d.exists():
                shutil.rmtree(d)

    banner("acomp 一键演示：从「听起来还行」到 PASS/FAIL")
    note(f"仓库目录  {ROOT}")
    note(f"Python    {sys.version.split()[0]}  ({sys.executable})")

    # --- 1/6 环境 ---------------------------------------------------------
    step(1, "检查运行环境（依赖 + ffmpeg）")
    sys.path.insert(0, str(ROOT))
    from acomp import audio  # noqa: E402

    try:
        ff = audio.ffmpeg_exe()
    except Exception as e:  # pragma: no cover
        print(e)
        return 2
    note("依赖      pyyaml / numpy / imageio-ffmpeg  OK")
    note(f"ffmpeg    {ff}")
    note("（ffmpeg 由 imageio-ffmpeg 内置提供，不需要单独安装，也不需要 ffprobe）")

    # --- 2/6 测试音 -------------------------------------------------------
    step(2, "生成 3 段合成测试音（S02 故意过响）")
    note("S01/S03 电平正常（rms ≈ -20 dBFS），S02 峰值 ≈ -0.9 dBFS，用来制造一次真实失败。")
    run([sys.executable, EXAMPLES / "make_demo_audio.py"])

    # --- 3/6 validate -----------------------------------------------------
    step(3, "validate：只校验 IR 是否合规，不写任何文件")
    run([sys.executable, ACOMP_PY, "validate", EXAMPLES / "demo.yml"])

    # --- 4/6 plan ---------------------------------------------------------
    step(4, "plan：dry-run，探测素材、预估时长、判定目标是否可达")
    note("注意 plan 不写盘，它会在动手之前告诉你「这个目标能不能达到」。")
    run([sys.executable, ACOMP_PY, "plan", EXAMPLES / "demo.yml"])

    # --- 5/6 render 过响版 -------------------------------------------------
    step(5, "render 过响版：S02 电平超标，应当报 FAIL（退出码 1）")
    write_hot_yml()
    try:
        code = run([sys.executable, ACOMP_PY, "render", HOT_YML], expect_codes=(1,))
        note(f"退出码 {code} = 有 FAIL（构建成功，但不达标）—— 这就是给 AI 读的反馈。")
        shutil.copyfile(OUT_DIR / "report.json", OUT_DIR / "report_hot.json")
        note("已把这份报告存为 examples/out/report_hot.json，等会儿用来对比。")
    finally:
        if not a.keep and HOT_YML.exists():
            HOT_YML.unlink()

    # --- 6/6 render 修好版 + diff ------------------------------------------
    step(6, "render 修好版：改一行 gain_db，应当全 PASS（退出码 0）")
    note("改动只有一处：examples/demo.yml 里 S02 的 gain_db 从 0.0 变成 -16.0。")
    note("S01/S03 会命中段落级缓存直接复用，只重建变了的那一段。")
    run([sys.executable, ACOMP_PY, "render", EXAMPLES / "demo.yml"], expect_codes=(0,))
    note("退出码 0 = 全部达标。")
    print()
    note("diff：两份报告的语义级差异（只看变化，不看时间戳之类噪声）")
    run([sys.executable, ACOMP_PY, "diff",
         OUT_DIR / "report_hot.json", OUT_DIR / "report.json"])

    banner("演示完成 —— 你刚刚走完了 acomp 的完整闭环")
    note("产物：")
    for p in (OUT_DIR / "demo_master.wav", OUT_DIR / "demo_master.mp3",
              OUT_DIR / "report.json", OUT_DIR / "report_hot.json"):
        note(f"  {p.relative_to(ROOT)}" + ("" if p.exists() else "   (缺失!)"))
    note("")
    note("接着可以试：")
    note(f"  {Path(sys.executable).name} acomp.py probe examples/out/demo_master.mp3")
    note(f"  {Path(sys.executable).name} acomp.py init my_project    # 生成一份 IR 模板")
    note(f"  {Path(sys.executable).name} acomp.py render my_project.yml")
    note("")
    note("原理：写 IR（文本）-> render（纯函数）-> 读数字报告 -> 改一行 -> 再来一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
