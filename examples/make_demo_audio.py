"""生成 3 段合成测试音。

S01 / S03 电平正常（rms ≈ -20 dBFS），S02 故意做过响（peak ≈ -0.9 dBFS），
用来演示 acomp 的完整闭环：render 报 FAIL -> 改一行 IR -> 再 render -> PASS。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from acomp import audio  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")

# 注意两个坑：
#   1. ffmpeg 的 sine 滤镜默认振幅只有 0.125（-18 dBFS），再叠 volume 会得到意外电平；
#      用 aevalsrc 才能精确指定振幅。
#   2. 单声道 + `-ac 2` 会被 ffmpeg 隐式衰减 3 dB。
#      这里直接生成立体声（两个声道表达式用 | 分隔），绕开这个问题。
#
# rms = amp / sqrt(2)：amp=0.1414 -> rms = -20.0 dBFS, peak = -17.0 dBFS
SPECS = [
    ("S01", 220, 8.0, 0.1414),
    ("S02", 330, 8.0, 0.9000),   # 故意过响：peak -0.9 dBFS, rms -3.9 dBFS
    ("S03", 440, 8.0, 0.1414),
]


def main() -> int:
    os.makedirs(RAW, exist_ok=True)
    for name, freq, dur, amp in SPECS:
        dst = os.path.join(RAW, f"{name}.wav")
        expr = "|".join([f"{amp}*sin(2*PI*{freq}*t)"] * 2)
        audio.run(
            ["-v", "error", "-y",
             "-f", "lavfi", "-i", f"aevalsrc={expr}:d={dur}:s=44100:c=stereo",
             "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", dst],
            purpose=f"合成测试音 {name}",
        )
        print("wrote", dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
