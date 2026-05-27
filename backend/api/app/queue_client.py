import json
import os
from typing import Dict

import redis


QUEUE_NAME = "ingestion_jobs"


def get_queue_name() -> str:
    return (os.getenv("QUEUE_NAME") or QUEUE_NAME).strip() or QUEUE_NAME


def get_ingestion_fallback_mode() -> str:
    mode = (os.getenv("INGESTION_QUEUE_FALLBACK_MODE") or "none").strip().lower()
    if mode in {"inline", "thread", "none"}:
        return mode
    return "none"


def enqueue_ingestion_job(ingestion_id: str) -> bool:
    # 入队失败时这里返回 False 而不是直接抛异常。
    # 这样 API 可以先保证“任务创建成功并可查询”，即使 Redis 暂时不可用，
    # 系统也能以降级方式运行（例如后续人工补偿或重试机制处理）。
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
        payload = json.dumps({"ingestion_id": ingestion_id})
        client.rpush(get_queue_name(), payload)
        return True
    except Exception:
        return False


def remove_ingestion_job(ingestion_id: str) -> int:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
        payload = json.dumps({"ingestion_id": ingestion_id})
        return int(client.lrem(get_queue_name(), 0, payload) or 0)
    except Exception:
        return 0


def queue_health_payload() -> Dict[str, object]:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    available = False
    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
        available = bool(client.ping())
    except Exception:
        available = False
    return {
        "queue_backend": "redis",
        "queue_name": get_queue_name(),
        "queue_available": available,
        "ingestion_queue_fallback_mode": get_ingestion_fallback_mode(),
    }
