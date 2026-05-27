from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import worker


class _DummyResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_process_job_calls_internal_api(monkeypatch):
    called = {"url": None, "timeout": None}

    def _fake_urlopen(req, timeout):
        called["url"] = req.full_url
        called["timeout"] = timeout
        return _DummyResponse(status=200)

    monkeypatch.setattr("worker.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("worker.API_BASE", "http://api-test")

    worker.process_job("ing-001")

    assert called["url"] == "http://api-test/internal/ingestions/ing-001/process"
    assert called["timeout"] == worker.WORKER_PROCESS_TIMEOUT_SECONDS


def test_process_job_retries_503_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def _fake_urlopen(_req, timeout=None, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                url="http://api-test/internal/ingestions/ing-retry/process",
                code=503,
                msg="unavailable",
                hdrs=None,
                fp=io.BytesIO(b"temporary"),
            )
        return _DummyResponse(status=200)

    monkeypatch.setattr("worker.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("worker.time.sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr("worker.API_BASE", "http://api-test")
    monkeypatch.setattr("worker.WORKER_PROCESS_MAX_RETRIES", 2)

    worker.process_job("ing-retry")

    assert calls["n"] == 2
    assert sleeps == [1.0]


def test_process_job_retries_urlerror_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def _fake_urlopen(_req, timeout=None, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("timed out")
        return _DummyResponse(status=200)

    monkeypatch.setattr("worker.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("worker.time.sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr("worker.API_BASE", "http://api-test")

    worker.process_job("ing-url")

    assert calls["n"] == 2
    assert sleeps == [1.0]


def test_process_job_handles_http_error(monkeypatch):
    def _fake_urlopen(_req, timeout=None, **_kw):
        raise urllib.error.HTTPError(
            url="http://api-test/internal/ingestions/ing-err/process",
            code=500,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(b"internal error"),
        )

    monkeypatch.setattr("worker.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("worker.API_BASE", "http://api-test")

    # 目标是验证 worker 能吞掉异常并继续运行，不应抛出到调用方。
    worker.process_job("ing-err")


def test_process_job_dlq_on_terminal_http_500(monkeypatch):
    class _R:
        def __init__(self):
            self.rows: list[tuple[str, dict]] = []

        def rpush(self, key: str, line: str):
            self.rows.append((key, json.loads(line)))

    r = _R()

    def _fake_urlopen(_req, timeout=None, **_kw):
        raise urllib.error.HTTPError(
            url="http://api-test/x",
            code=500,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(b"body"),
        )

    monkeypatch.setattr("worker.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("worker.API_BASE", "http://api-test")

    worker.process_job("ing-dlq-500", redis_client=r)  # type: ignore[arg-type]

    assert len(r.rows) == 1
    assert r.rows[0][0] == "ingestion_jobs_dlq"
    assert r.rows[0][1]["ingestion_id"] == "ing-dlq-500"
    assert r.rows[0][1]["kind"] == "http_error"
    assert r.rows[0][1]["http_status"] == 500


def test_process_job_dlq_after_exhausted_503(monkeypatch):
    class _R:
        def __init__(self):
            self.rows: list[dict] = []

        def rpush(self, _key: str, line: str):
            self.rows.append(json.loads(line))

    r = _R()
    calls = {"n": 0}

    def _fake_urlopen(_req, timeout=None, **_kw):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            url="http://api-test/x",
            code=503,
            msg="unavailable",
            hdrs=None,
            fp=io.BytesIO(b"x"),
        )

    monkeypatch.setattr("worker.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("worker.time.sleep", lambda _s: None)
    monkeypatch.setattr("worker.API_BASE", "http://api-test")
    monkeypatch.setattr("worker.WORKER_PROCESS_MAX_RETRIES", 1)

    worker.process_job("ing-dlq-503", redis_client=r)  # type: ignore[arg-type]

    assert calls["n"] == 2
    assert len(r.rows) == 1
    assert r.rows[0]["ingestion_id"] == "ing-dlq-503"
    assert r.rows[0]["http_status"] == 503


def test_process_job_dlq_skipped_when_worker_dlq_disabled(monkeypatch):
    class _R:
        def __init__(self):
            self.rows: list[tuple[str, str]] = []

        def rpush(self, key: str, line: str):
            self.rows.append((key, line))

    r = _R()
    monkeypatch.setenv("WORKER_DLQ_ENABLED", "0")

    def _fake_urlopen(_req, timeout=None, **_kw):
        raise urllib.error.HTTPError(
            url="http://api-test/x",
            code=500,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(b"x"),
        )

    monkeypatch.setattr("worker.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("worker.API_BASE", "http://api-test")

    worker.process_job("ing-off", redis_client=r)  # type: ignore[arg-type]

    assert r.rows == []


class _FakeRedisClient:
    def __init__(self, result=None, err: Exception = None):
        self._result = result
        self._err = err
        self.dlq_pushes: list[tuple[str, dict]] = []

    def rpush(self, key: str, value: str):
        self.dlq_pushes.append((key, json.loads(value)))

    def blpop(self, _queue_name, timeout=5):
        if self._err is not None:
            raise self._err
        return self._result


def test_poll_once_processes_ingestion_job(monkeypatch):
    called: dict = {"ingestion_id": None, "redis": None}

    def _fake_process_job(ingestion_id: str, redis_client=None):
        called["ingestion_id"] = ingestion_id
        called["redis"] = redis_client

    monkeypatch.setattr("worker.process_job", _fake_process_job)
    client = _FakeRedisClient(result=("ingestion_jobs", '{"ingestion_id":"ing-123"}'))

    ok = worker.poll_once(client)

    assert ok is True
    assert called["ingestion_id"] == "ing-123"
    assert called["redis"] is client


def test_poll_once_returns_true_on_heartbeat():
    client = _FakeRedisClient(result=None)

    ok = worker.poll_once(client)

    assert ok is True


def test_poll_once_returns_false_on_error():
    client = _FakeRedisClient(err=RuntimeError("redis down"))

    ok = worker.poll_once(client)

    assert ok is False


def test_main_retries_with_backoff_on_poll_failure(monkeypatch):
    class _DummyRedis:
        pass

    monkeypatch.setattr("worker.redis.Redis.from_url", lambda _url, decode_responses=True: _DummyRedis())

    sequence = {"step": 0}
    sleeps = []

    def _fake_poll_once(_client):
        sequence["step"] += 1
        if sequence["step"] == 1:
            return True
        if sequence["step"] == 2:
            return False
        raise KeyboardInterrupt("stop test loop")

    monkeypatch.setattr("worker.poll_once", _fake_poll_once)
    monkeypatch.setattr("worker.time.sleep", lambda seconds: sleeps.append(seconds))

    try:
        worker.main()
        assert False, "expected KeyboardInterrupt to stop infinite loop in test"
    except KeyboardInterrupt:
        pass

    # 第二轮失败后应进行退避等待。
    assert sleeps == [2]
