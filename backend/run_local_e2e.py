"""
本地跨进程 e2e 验证脚本（开发环境）。

流程：
1) 启动 API 进程（uvicorn main:app）
2) 启动 worker 进程
3) 等待 API 健康检查通过
4) 调用 verify_async_flow.py 验证异步链路
5) 结束子进程并返回退出码

注意：
- 本脚本不会自动启动 Redis，请先按 runbook 启动基础设施。
- 若配置 DATABASE_URL，可通过参数传入。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request


ROOT = Path(__file__).resolve().parent


def _wait_api_ready(api_base: str, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    health_url = f"{api_base.rstrip('/')}/health"
    while time.time() < deadline:
        try:
            with request.urlopen(health_url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description="本地跨进程 e2e 验证：api + worker + 异步链路")
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--org-id", default="org-demo")
    parser.add_argument("--user-id", default="u-demo")
    parser.add_argument("--database-url", default="", help="可选，传入后 API 走 DB 模式")
    parser.add_argument("--startup-timeout-seconds", type=int, default=30)
    args = parser.parse_args()

    api_base = f"http://{args.api_host}:{args.api_port}"

    api_env = os.environ.copy()
    worker_env = os.environ.copy()
    if args.database_url.strip():
        api_env["DATABASE_URL"] = args.database_url.strip()
    api_env["PYTHONPATH"] = str(ROOT / "api")
    worker_env["PYTHONPATH"] = str(ROOT / "worker")
    worker_env["API_BASE_URL"] = api_base
    worker_env["REDIS_URL"] = args.redis_url

    api_cmd = [sys.executable, "-m", "uvicorn", "main:app", "--host", args.api_host, "--port", str(args.api_port)]
    worker_cmd = [sys.executable, "worker.py"]
    verify_cmd = [
        sys.executable,
        str(ROOT / "verify_async_flow.py"),
        "--api-base",
        api_base,
        "--org-id",
        args.org_id,
        "--user-id",
        args.user_id,
    ]

    print("[e2e] starting api process...")
    api_proc = subprocess.Popen(api_cmd, cwd=str(ROOT / "api"), env=api_env)
    worker_proc = None
    try:
        print("[e2e] waiting for api health...")
        if not _wait_api_ready(api_base, args.startup_timeout_seconds):
            print("[e2e] api not ready within timeout")
            return 10

        print("[e2e] starting worker process...")
        worker_proc = subprocess.Popen(worker_cmd, cwd=str(ROOT / "worker"), env=worker_env)
        time.sleep(1.5)

        print("[e2e] running async flow verification...")
        verify_result = subprocess.run(verify_cmd, cwd=str(ROOT), check=False)
        return verify_result.returncode
    finally:
        print("[e2e] cleaning up processes...")
        if worker_proc is not None:
            _terminate_process(worker_proc)
        _terminate_process(api_proc)


if __name__ == "__main__":
    sys.exit(main())
