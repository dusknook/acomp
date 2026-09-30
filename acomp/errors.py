"""机器可读错误。

给人用的工具出错可以说「出错了，请重试」，因为人会自己想办法。
给 AI 用的工具不行 —— 它只能根据你返回的东西决策。
所以每条错误必须回答三件事：

    1. what_failed   哪个前提挂了
    2. minimal_fix   最小修法（能直接执行，不是「请检查配置」这种废话）
    3. retryable     重试有没有意义

stdout/stderr 一律输出 JSON，方便调用方直接 parse。
"""

from __future__ import annotations

import json


class AcompError(Exception):
    def __init__(self, code: str, what_failed: str, minimal_fix: str,
                 retryable: bool = False, detail: dict | None = None):
        super().__init__(what_failed)
        self.code = code
        self.what_failed = what_failed
        self.minimal_fix = minimal_fix
        self.retryable = retryable
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "code": self.code,
            "what_failed": self.what_failed,
            "minimal_fix": self.minimal_fix,
            "retryable": self.retryable,
            "detail": self.detail,
        }

    def render(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class ValidationFailed(AcompError):
    """IR 不合 schema。retryable=False —— 改文件才有意义，重跑没意义。"""

    def __init__(self, issues: list[dict], path: str = "<unknown>"):
        errors = [i for i in issues if i.get("severity") == "error"]
        super().__init__(
            code="IR_INVALID",
            what_failed=f"{len(errors)} 个 IR 校验错误（文件：{path}）",
            minimal_fix="按 issues[].fix 逐条修改 project.yml 后重跑；重跑前不必重试",
            retryable=False,
            detail={"issues": issues},
        )
        self.issues = issues
