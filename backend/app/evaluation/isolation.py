"""Reviewer 子进程的参考文件读取禁区；不是针对恶意原生代码的 OS 沙箱。"""

import os
from pathlib import Path
import sys


_reviewer_process = False


def require_reference_access():
    if _reviewer_process:
        raise PermissionError("reference_access_forbidden_in_reviewer")


def protect_reviewer_process():
    global _reviewer_process
    if _reviewer_process:
        return
    _reviewer_process = True

    def audit(event, args):
        if event != "open" or not args or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(args[0])).resolve()
        if ("evaluation-data" in {part.casefold() for part in path.parts}
                or path.name.casefold() in {"positive_samples.json", "negative_samples.json"}):
            raise PermissionError("reference_access_forbidden_in_reviewer")

    sys.addaudithook(audit)
