"""
统一验证入口脚本。

模式：
- quick: 仅调用 verify_async_flow.py（要求 API/worker 已手动启动）
- full: 调用 run_local_e2e.py 自动拉起 API+worker 再验证
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="AI ERP Assistant 统一验证入口")
    parser.add_argument("--mode", choices=["quick", "full"], default="quick")
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--org-id", default="org-demo")
    parser.add_argument("--user-id", default="u-demo")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--startup-timeout-seconds", type=int, default=30)
    args = parser.parse_args()

    if args.mode == "quick":
        cmd = [
            sys.executable,
            str(ROOT / "verify_async_flow.py"),
            "--api-base",
            f"http://{args.api_host}:{args.api_port}",
            "--org-id",
            args.org_id,
            "--user-id",
            args.user_id,
            "--timeout-seconds",
            str(args.timeout_seconds),
            "--poll-interval",
            str(args.poll_interval),
        ]
    else:
        cmd = [
            sys.executable,
            str(ROOT / "run_local_e2e.py"),
            "--api-host",
            args.api_host,
            "--api-port",
            str(args.api_port),
            "--redis-url",
            args.redis_url,
            "--org-id",
            args.org_id,
            "--user-id",
            args.user_id,
            "--startup-timeout-seconds",
            str(args.startup_timeout_seconds),
        ]
        if args.database_url.strip():
            cmd.extend(["--database-url", args.database_url.strip()])

    result = subprocess.run(cmd, cwd=str(ROOT), check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
