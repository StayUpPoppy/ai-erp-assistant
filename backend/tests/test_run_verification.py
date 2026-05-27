from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_verification


class _RunResult:
    def __init__(self, returncode: int):
        self.returncode = returncode


def test_quick_mode_builds_verify_command(monkeypatch):
    captured = {"cmd": None, "cwd": None}

    def _fake_run(cmd, cwd, check):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        assert check is False
        return _RunResult(returncode=0)

    monkeypatch.setattr("run_verification.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "run_verification.sys.argv",
        [
            "run_verification.py",
            "--mode",
            "quick",
            "--api-host",
            "127.0.0.1",
            "--api-port",
            "8001",
            "--org-id",
            "org-x",
            "--user-id",
            "u-x",
            "--timeout-seconds",
            "20",
            "--poll-interval",
            "1.0",
        ],
    )

    code = run_verification.main()
    assert code == 0
    assert "verify_async_flow.py" in " ".join(captured["cmd"])
    assert "--api-base" in captured["cmd"]
    assert "http://127.0.0.1:8001" in captured["cmd"]
    assert captured["cwd"] == str(ROOT)


def test_full_mode_builds_e2e_command_with_database_url(monkeypatch):
    captured = {"cmd": None}

    def _fake_run(cmd, cwd, check):
        captured["cmd"] = cmd
        assert cwd == str(ROOT)
        assert check is False
        return _RunResult(returncode=3)

    monkeypatch.setattr("run_verification.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "run_verification.sys.argv",
        [
            "run_verification.py",
            "--mode",
            "full",
            "--api-port",
            "9000",
            "--redis-url",
            "redis://localhost:6379/1",
            "--database-url",
            "postgresql://demo",
        ],
    )

    code = run_verification.main()
    assert code == 3
    joined = " ".join(captured["cmd"])
    assert "run_local_e2e.py" in joined
    assert "--database-url" in captured["cmd"]
    assert "postgresql://demo" in captured["cmd"]
