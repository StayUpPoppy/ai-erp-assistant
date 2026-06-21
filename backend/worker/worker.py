"""
异步 worker：从 Redis 取 ingestion 任务并回调 API `/internal/ingestions/{id}/process`。

业务侧后续可增强（与编排层约定后再改代码）：
- 已实现：对 HTTP **502/503/504** 与 **`URLError`（连接失败、超时等）** 有限次退避重试（`WORKER_PROCESS_MAX_RETRIES` / `WORKER_PROCESS_RETRY_BACKOFF_SEC`）。
- 已实现：终态失败写入 Redis **DLQ**（`WORKER_DLQ_NAME`，默认 `ingestion_jobs_dlq`）；`WORKER_DLQ_ENABLED=0` 可关闭。
- 待办：其它状态码策略、耗时指标、从 DLQ 人工补跑工具等。
"""

from __future__ import annotations

import time
import json
import logging
import os
import urllib.request
import urllib.error
from typing import Optional

import redis


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.getenv("QUEUE_NAME", "ingestion_jobs")
API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8020")
WORKER_PROCESS_MAX_RETRIES = int(os.getenv("WORKER_PROCESS_MAX_RETRIES", "2"))
WORKER_PROCESS_RETRY_BACKOFF_SEC = float(os.getenv("WORKER_PROCESS_RETRY_BACKOFF_SEC", "1.0"))
WORKER_PROCESS_TIMEOUT_SECONDS = float(os.getenv("WORKER_PROCESS_TIMEOUT_SECONDS", "600"))
logger = logging.getLogger("ai_erp_worker")


def _dlq_queue_name() -> str:
    name = (os.getenv("WORKER_DLQ_NAME", "ingestion_jobs_dlq") or "ingestion_jobs_dlq").strip()
    return name or "ingestion_jobs_dlq"


def _dlq_push_enabled() -> bool:
    return os.getenv("WORKER_DLQ_ENABLED", "1").strip().lower() not in ("0", "false", "no")


def push_process_job_failure_dlq(
    redis_client: Optional[redis.Redis],
    ingestion_id: str,
    *,
    kind: str,
    detail: str,
    max_attempts: int,
    http_status: Optional[int] = None,
) -> None:
    """处理回调终态失败时写入 DLQ，便于运维拉取/补跑（与主队列 JSON 风格一致）。"""
    if redis_client is None or not _dlq_push_enabled():
        return
    detail = (detail or "")[:2000]
    payload: dict = {
        "ingestion_id": ingestion_id,
        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
        "detail": detail,
        "max_attempts": max_attempts,
    }
    if http_status is not None:
        payload["http_status"] = http_status
    try:
        line = json.dumps(payload, ensure_ascii=False)
        qn = _dlq_queue_name()
        redis_client.rpush(qn, line)
        logger.info("dlq_pushed queue=%s ingestion_id=%s kind=%s", qn, ingestion_id, kind)
    except Exception:
        logger.exception("dlq_push_failed ingestion_id=%s", ingestion_id)


def process_job(ingestion_id: str, redis_client: Optional[redis.Redis] = None) -> None:
    # worker 消费到任务后，不直接改数据库，而是回调 API 内部端点推进状态机。
    # 这样“状态变更入口”仍统一在 API 服务，便于做鉴权、审计、日志与一致性控制。
    url = f"{API_BASE}/internal/ingestions/{ingestion_id}/process"
    max_attempts = max(1, WORKER_PROCESS_MAX_RETRIES + 1)
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=WORKER_PROCESS_TIMEOUT_SECONDS) as resp:
                logger.info(
                    "processed ingestion_id=%s status_code=%s attempt=%s/%s",
                    ingestion_id,
                    resp.status,
                    attempt + 1,
                    max_attempts,
                )
                return
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode(errors="ignore")
            except Exception:
                body = ""
            if exc.code in (502, 503, 504) and attempt + 1 < max_attempts:
                sleep_s = WORKER_PROCESS_RETRY_BACKOFF_SEC * (attempt + 1)
                logger.warning(
                    "process_retry ingestion_id=%s status=%s attempt=%s/%s sleep_s=%s body_prefix=%s",
                    ingestion_id,
                    exc.code,
                    attempt + 1,
                    max_attempts,
                    sleep_s,
                    body[:200],
                )
                time.sleep(sleep_s)
                continue
            logger.error("process_failed ingestion_id=%s status_code=%s body=%s", ingestion_id, exc.code, body)
            push_process_job_failure_dlq(
                redis_client,
                ingestion_id,
                kind="http_error",
                detail=f"status={exc.code} body_prefix={body[:800]!r}",
                max_attempts=max_attempts,
                http_status=exc.code,
            )
            return
        except urllib.error.URLError as exc:
            if attempt + 1 < max_attempts:
                sleep_s = WORKER_PROCESS_RETRY_BACKOFF_SEC * (attempt + 1)
                logger.warning(
                    "process_retry ingestion_id=%s url_error=%r attempt=%s/%s sleep_s=%s",
                    ingestion_id,
                    exc.reason,
                    attempt + 1,
                    max_attempts,
                    sleep_s,
                )
                time.sleep(sleep_s)
                continue
            logger.error("process_failed ingestion_id=%s url_error=%r", ingestion_id, exc.reason)
            push_process_job_failure_dlq(
                redis_client,
                ingestion_id,
                kind="url_error",
                detail=repr(exc.reason),
                max_attempts=max_attempts,
            )
            return
        except TimeoutError as exc:
            if attempt + 1 < max_attempts:
                sleep_s = WORKER_PROCESS_RETRY_BACKOFF_SEC * (attempt + 1)
                logger.warning(
                    "process_retry ingestion_id=%s timeout=%r attempt=%s/%s sleep_s=%s",
                    ingestion_id,
                    exc,
                    attempt + 1,
                    max_attempts,
                    sleep_s,
                )
                time.sleep(sleep_s)
                continue
            logger.error("process_failed ingestion_id=%s timeout=%r", ingestion_id, exc)
            push_process_job_failure_dlq(
                redis_client,
                ingestion_id,
                kind="timeout",
                detail=repr(exc),
                max_attempts=max_attempts,
            )
            return
        except Exception as exc:
            logger.exception("process_failed ingestion_id=%s err=%s", ingestion_id, str(exc))
            push_process_job_failure_dlq(
                redis_client,
                ingestion_id,
                kind="unexpected_error",
                detail=repr(exc),
                max_attempts=max_attempts,
            )
            return


def poll_once(client: redis.Redis) -> bool:
    """
    执行一次队列轮询并处理一条任务。

    返回值：
    - True: 本次轮询处理了任务或正常心跳；
    - False: 本次轮询出现异常，调用方可决定是否退避重试。
    """
    try:
        item = client.blpop(QUEUE_NAME, timeout=5)
        if not item:
            logger.info("worker_heartbeat queue=%s", QUEUE_NAME)
            return True
        _, payload = item
        job = json.loads(payload)
        ingestion_id = job.get("ingestion_id")
        if ingestion_id:
            process_job(ingestion_id, redis_client=client)
        return True
    except Exception as exc:
        logger.exception("worker_error err=%s", str(exc))
        return False


def main() -> None:
    # worker 日志统一到标准 logging，便于后续接入文件输出与集中采集。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logger.info(
        "worker_started redis_url=%s queue=%s dlq=%s api_base=%s",
        REDIS_URL,
        QUEUE_NAME,
        _dlq_queue_name(),
        API_BASE,
    )
    # 使用 Redis 阻塞读取（BLPOP）持续消费队列。
    # 这种模式能在空闲时降低 CPU 占用，同时保证有任务时立即处理。
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    while True:
        ok = poll_once(client)
        if not ok:
            time.sleep(2)


if __name__ == "__main__":
    main()
