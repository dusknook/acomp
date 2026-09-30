"""acomp — 声明式音频后期编译器。

给 AI 用的音频工具：状态是一份 YAML（可写可读可 diff），
render 是纯函数（IR -> 产物 + 测量报告），反馈是数字不是波形。
"""

__version__ = "0.1.0"

from .errors import AcompError, ValidationFailed  # noqa: F401
