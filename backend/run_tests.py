"""
仓库级测试聚合入口。

模式：
- quick: 跑核心回归（API+worker+验证入口）
- full: 跑当前仓库可发现的全部 pytest 用例
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _run_pytest(args: list[str]) -> int:
    cmd = [sys.executable, "-m", "pytest", *args]
    env = os.environ.copy()
    api_pkg = str(ROOT / "api")
    prev = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = f"{api_pkg}{os.pathsep}{prev}" if prev else api_pkg
    result = subprocess.run(cmd, cwd=str(ROOT), env=env, check=False)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="AI ERP Assistant 测试聚合入口")
    parser.add_argument("--mode", choices=["quick", "full"], default="quick")
    ns = parser.parse_args()

    if ns.mode == "quick":
        return _run_pytest(
            [
                "api/tests",
                "worker/tests",
                "tests/test_run_verification.py",
            ]
        )
    return _run_pytest([])


if __name__ == "__main__":
    raise SystemExit(main())
