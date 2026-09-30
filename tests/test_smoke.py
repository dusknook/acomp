"""不需要 ffmpeg 的单元测试：IR 解析、校验、缓存指纹、错误格式。

跑：
    pytest -q

（涉及真实音频的端到端验证交给 `python demo.py` —— 它是可执行的冒烟测试。）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acomp import ir, report  # noqa: E402
from acomp.errors import AcompError, ValidationFailed  # noqa: E402

BASE = """version: 1

project:
  name: t
  out_dir: ./out

audio:
  sample_rate: 44100
  channels: 2

targets:
  peak_dbfs: [null, -3.0]
  rms_dbfs: [-23.0, -18.0]

segments:
  - id: S01
    src: raw/S01.wav
    gain_db: {gain}
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "p.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_demo_project_parses_readonly():
    """仓库里的示例工程必须始终是可解析的。"""
    proj = ir.load(str(ROOT / "examples" / "demo.yml"))
    assert [s.id for s in proj.segments] == ["S01", "S02", "S03"]
    assert proj.name == "demo"
    assert proj.out_dir.replace("\\", "/").endswith("/examples/out")
    assert proj.targets["peak_dbfs"].holds(-6.0)
    assert not proj.targets["peak_dbfs"].holds(-1.0)


def test_src_resolves_relative_to_ir(tmp_path):
    proj = ir.load(str(write(tmp_path, BASE.replace("{gain}", "0.0"))))
    assert proj.resolve("raw/S01.wav") == str(tmp_path / "raw" / "S01.wav")


def test_missing_src_is_a_hard_error(tmp_path):
    text = BASE.replace("{gain}", "0.0").replace("    src: raw/S01.wav\n", "")
    with pytest.raises(ValidationFailed) as ei:
        ir.load(str(write(tmp_path, text)))
    issues = ei.value.issues
    assert any(i["severity"] == "error" and "src" in i["path"] for i in issues)
    # 报错必须带可直接执行的修法
    assert all(i.get("fix") for i in issues)


def test_duplicate_segment_id_rejected(tmp_path):
    text = BASE.replace("{gain}", "0.0") + "  - id: S01\n    src: raw/S02.wav\n"
    with pytest.raises(ValidationFailed) as ei:
        ir.load(str(write(tmp_path, text)))
    assert any("id 重复" in i["what"] for i in ei.value.issues)


def test_segment_checks_three_modes(tmp_path):
    """不写 = 默认；写列表 = 严格按列表；写 [] = 全部关掉。"""
    def load(extra: str):
        text = BASE.replace("{gain}", "0.0").replace(
            "targets:\n", "targets:\n" + extra) if extra else BASE.replace("{gain}", "0.0")
        return ir.load(str(write(tmp_path, text)))

    assert report.segment_targets(load("")) == ("peak_dbfs", "rms_dbfs")
    assert report.segment_targets(load("  segment_checks: [peak_dbfs]\n")) == ("peak_dbfs",)
    assert report.segment_targets(load("  segment_checks: []\n")) == ()


def test_unknown_segment_check_is_rejected(tmp_path):
    text = BASE.replace("{gain}", "0.0").replace(
        "targets:\n", "targets:\n  segment_checks: [loudness]\n")
    with pytest.raises(ValidationFailed) as ei:
        ir.load(str(write(tmp_path, text)))
    assert any("segment_checks" in i["path"] for i in ei.value.issues)


def test_fingerprint_tracks_ir_changes(tmp_path):
    """缓存指纹只认「IR 参数 + 源文件状态」—— 改了增益就必须重建。"""
    a = ir.load(str(write(tmp_path, BASE.replace("{gain}", "0.0"))))
    b = ir.load(str(write(tmp_path, BASE.replace("{gain}", "-6.0"))))
    assert ir.segment_fingerprint(a.segments[0], a, None) != \
           ir.segment_fingerprint(b.segments[0], b, None)

    c = ir.load(str(write(tmp_path, BASE.replace("{gain}", "0.0"))))
    assert ir.segment_fingerprint(a.segments[0], a, None) == \
           ir.segment_fingerprint(c.segments[0], c, None)


def test_range_semantics():
    r = ir.Range(None, -3.0)
    assert r.holds(-3.0) and r.holds(-40.0) and not r.holds(-2.99)
    with pytest.raises(ValueError):
        ir.Range(1.0, 0.0)


def test_error_payload_is_machine_readable():
    """错误必须是 JSON，且回答三件事：哪里挂了 / 怎么修 / 能不能重试。"""
    e = AcompError(code="SRC_MISSING", what_failed="文件不存在", minimal_fix="检查路径",
                   retryable=False)
    payload = json.loads(e.render())
    assert payload["ok"] is False
    assert payload["code"] == "SRC_MISSING"
    assert set(payload) >= {"code", "what_failed", "minimal_fix", "retryable"}
