"""ERP userInfo Cookie 到当前用户响应的轻量解析测试。"""

from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import current_user


def test_current_user_reads_user_info_cookie():
    request = SimpleNamespace(
        cookies={
            "userinfo": "%7B%22userId%22%3A%2231%22%2C%22username%22%3A%22%E5%BC%A0%E5%AE%87%E6%B6%B5%22%2C%22realName%22%3A%22%E5%BC%A0%E5%AE%87%E6%B6%B5%22%2C%22currentOrgId%22%3A2%2C%22currentOrgName%22%3A%22%E8%8B%B1%E7%A7%911%E5%8E%82%22%7D"
        },
        state=SimpleNamespace(request_id="test-request"),
    )

    data = current_user(request)

    assert data.userName == "张宇涵"
    assert data.orgId == "英科1厂"
    assert data.source == "userinfo_cookie"
