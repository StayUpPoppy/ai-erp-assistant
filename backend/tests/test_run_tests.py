from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_tests


class _RunResult:
    def __init__(self, returncode: int):
        self.returncode = returncode


def test_run_tests_quick_mode(monkeypatch):
    captured = {"cmd": None, "cwd": None, "check": None, "env": None}

    def _fake_run(cmd, cwd=None, check=False, env=None, **_kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["check"] = check
        captured["env"] = env
        return _RunResult(returncode=0)

    monkeypatch.setattr("run_tests.subprocess.run", _fake_run)
    monkeypatch.setattr("run_tests.sys.argv", ["run_tests.py", "--mode", "quick"])

    code = run_tests.main()
    assert code == 0
    assert captured["cwd"] == str(ROOT)
    assert captured["check"] is False
    assert captured["env"] is not None
    assert "PYTHONPATH" in captured["env"]
    assert str(ROOT / "api") in captured["env"]["PYTHONPATH"]
    joined = " ".join(captured["cmd"])
    assert "api/tests" in joined
    assert "worker/tests" in joined
    assert "tests/test_run_verification.py" in joined


def test_run_tests_full_mode(monkeypatch):
    captured = {"cmd": None}

    def _fake_run(cmd, cwd=None, check=False, env=None, **_kwargs):
        captured["cmd"] = cmd
        assert cwd == str(ROOT)
        assert check is False
        return _RunResult(returncode=2)

    monkeypatch.setattr("run_tests.subprocess.run", _fake_run)
    monkeypatch.setattr("run_tests.sys.argv", ["run_tests.py", "--mode", "full"])

    code = run_tests.main()
    assert code == 2
    # full 模式只应调用 pytest，不追加路径参数。
    assert captured["cmd"] == [sys.executable, "-m", "pytest"]
