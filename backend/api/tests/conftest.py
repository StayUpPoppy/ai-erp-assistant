"""
保证未执行 pip install -e ../../backend/packages/shared 时，pytest 仍能从源码找到 erp_assistant_shared。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_shared_src_on_path() -> None:
    here = Path(__file__).resolve()
    # tests -> backend/api -> apps -> repo root
    repo_root = here.parents[3]
    shared_src = repo_root / "packages" / "shared" / "src"
    if shared_src.is_dir():
        p = str(shared_src)
        if p not in sys.path:
            sys.path.insert(0, p)


_ensure_shared_src_on_path()
