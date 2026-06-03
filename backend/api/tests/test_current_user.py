"""ERP userInfo Cookie 到当前用户响应的轻量解析测试。"""

from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import current_user
from app.routes import _assert_ingestion_owner
from app.schemas import IngestionResponse, IngestionStatus


def test_current_user_reads_user_info_cookie():
    request = SimpleNamespace(
        cookies={
            "userinfo": "%7B%22userId%22%3A%2231%22%2C%22username%22%3A%22%E5%BC%A0%E5%AE%87%E6%B6%B5%22%2C%22realName%22%3A%22%E5%BC%A0%E5%AE%87%E6%B6%B5%22%2C%22currentOrgId%22%3A2%2C%22currentOrgName%22%3A%22%E8%8B%B1%E7%A7%911%E5%8E%82%22%7D"
        },
        state=SimpleNamespace(request_id="test-request"),
    )

    data = current_user(request)

    assert data.userId == "31"
    assert data.userName == "张宇涵"
    assert data.orgId == "英科1厂"
    assert data.source == "userinfo_cookie"


def test_assert_ingestion_owner_rejects_other_user():
    request = SimpleNamespace(
        cookies={
            "userinfo": "%7B%22userId%22%3A%2231%22%2C%22username%22%3A%22%E5%BC%A0%E5%AE%87%E6%B6%B5%22%2C%22realName%22%3A%22%E5%BC%A0%E5%AE%87%E6%B6%B5%22%2C%22currentOrgName%22%3A%22%E8%8B%B1%E7%A7%911%E5%8E%82%22%7D"
        },
        state=SimpleNamespace(request_id="test-request"),
    )
    ingestion = IngestionResponse(
        ingestion_id="ing-1",
        file_id="file-1",
        file_hash="hash",
        user_id="99",
        org_id="英科1厂",
        extract_version="v0",
        model_version="mock",
        prompt_version="prompt",
        status=IngestionStatus.UPLOADED,
    )

    try:
        _assert_ingestion_owner(ingestion, request)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("expected forbidden owner check")
