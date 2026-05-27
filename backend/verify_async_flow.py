"""
最小端到端异步链路验证脚本（本地联调用）。

功能：
1) 调用 POST /uploads 创建 ingestion；
2) 轮询 GET /ingestions/{id} 等待 worker 推进状态（含 NEED_USER_INPUT 与自动到达的 VALIDATED）；
3) 可选：若停在 NEED_USER_INPUT，用 --resolve-json 提交补全后再轮询；
4) 可选：在 VALIDATED 时用 --create-draft 调用 POST .../create-draft，打印草稿号；
5) 输出关键状态与审计事件摘要。

仅测「本仓库 API → ERP 客户保存」适配（不跑 ingestion）：

  python verify_async_flow.py --api-base http://127.0.0.1:8000 --integrations-save-customer @customer-body.json

其中 JSON 须符合 SaveCustomerRequest：`{"org_id":"...","user_id":"可选","fields":{...}}`（也可用内联 JSON 字符串）。以 `@` 引用文件时：若当前工作目录下找不到，会再按**本脚本所在仓库根**解析（可在 `backend/api` 下执行仍使用 `@backend/scripts/...`）。

使用示例：
  python verify_async_flow.py --api-base http://127.0.0.1:8000 --create-draft
  python verify_async_flow.py --api-base http://127.0.0.1:8000 --resolve-json @fields.json --create-draft
（--resolve-json 可为 JSON 字符串，或以 @ 开头的 UTF-8 文件路径；内容为字段对象或含 \"fields\" 的整段 resolve 请求体。）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error, request

# 本脚本位于仓库根；@ 文件路径在 cwd 找不到时回退到仓库根（便于在 backend/api 下执行 python ../verify_async_flow.py …）
_REPO_ROOT = Path(__file__).resolve().parent


def _read_json_file_after_at(raw_arg: str) -> str:
    """解析以 @ 开头的路径：先试 cwd 相对/绝对路径，再试相对仓库根。"""
    inner = raw_arg[1:].strip()
    if not inner:
        raise OSError("empty @ path")
    p = Path(inner).expanduser()
    if p.is_file():
        return p.read_text(encoding="utf-8")
    alt = _REPO_ROOT / inner
    if alt.is_file():
        return alt.read_text(encoding="utf-8")
    raise FileNotFoundError(f"找不到 JSON 文件: {inner}（已尝试 cwd 与仓库根 {_REPO_ROOT}）")


def _json_request(method: str, url: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = request.Request(url=url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def _poll_until_terminal(
    detail_url: str,
    started: float,
    timeout_seconds: float,
    poll_interval: float,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """轮询直到 NEED_USER_INPUT / VALIDATED / FAILED / DRAFT_CREATED 或超时。返回 (last_detail, status_or_none)。"""
    while True:
        if time.time() - started > timeout_seconds:
            return {}, None
        try:
            detail = _json_request("GET", detail_url)
        except Exception as exc:
            print(f"[警告] 轮询失败: {exc}")
            time.sleep(poll_interval)
            continue
        now_status = detail.get("status")
        error_code = detail.get("error_code")
        print(f"[轮询] status={now_status} error_code={error_code}")
        if now_status in {"NEED_USER_INPUT", "VALIDATED", "FAILED", "DRAFT_CREATED"}:
            return detail, str(now_status)
        time.sleep(poll_interval)


def _print_terminal_summary(detail: Dict[str, Any], now_status: str) -> None:
    events = detail.get("audit_events", [])
    print(f"[完成] 最终状态={now_status} 审计事件数={len(events)}")
    if events:
        last = events[-1]
        print(f"[最后事件] at={last.get('at')} status={last.get('status')} message={last.get('message')}")
    if now_status == "VALIDATED":
        print("[提示] 已进入 VALIDATED：可直接调 create-draft（或使用本脚本 --create-draft）。")


def _build_upload_payload(user_id: str, org_id: str) -> Dict[str, Any]:
    seed = f"{org_id}:{user_id}:{datetime.utcnow().isoformat()}".encode("utf-8")
    file_hash = hashlib.sha256(seed).hexdigest()
    return {
        "file_name": "verify-async-flow.txt",
        "file_hash": file_hash,
        "user_id": user_id,
        "org_id": org_id,
    }


def _load_json_arg(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if raw.startswith("@") and len(raw) > 1:
        text = _read_json_file_after_at(raw)
    else:
        text = raw
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层须为对象")
    return data


def _run_integrations_save_customer(api_base: str, raw_arg: str) -> int:
    try:
        body = _load_json_arg(raw_arg)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"[失败] 无法解析 --integrations-save-customer: {exc}")
        return 7
    if "org_id" not in body or "fields" not in body:
        print("[失败] 请求体须包含 org_id 与 fields（与 POST /integrations/erp/customer 一致）")
        return 7
    url = f"{api_base.rstrip('/')}/integrations/erp/customer"
    try:
        out = _json_request("POST", url, body)
    except error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        print(f"[失败] save-customer HTTP {exc.code} body={err_body}")
        return 8
    except Exception as exc:
        print(f"[失败] save-customer 请求异常: {exc}")
        return 8
    print(f"[客户保存] customer_no={out.get('customer_no')} customer_url={out.get('customer_url')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 AI ERP Assistant 异步处理链路")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000", help="API 基础地址")
    parser.add_argument("--org-id", default="org-demo", help="组织 ID")
    parser.add_argument("--user-id", default="u-demo", help="用户 ID")
    parser.add_argument("--timeout-seconds", type=int, default=45, help="轮询总超时时间")
    parser.add_argument("--poll-interval", type=float, default=1.5, help="轮询间隔秒数")
    parser.add_argument(
        "--create-draft",
        action="store_true",
        help="在最终为 VALIDATED 时自动 POST /ingestions/{id}/create-draft",
    )
    parser.add_argument(
        "--resolve-json",
        default=None,
        metavar="JSON",
        help="若首次停在 NEED_USER_INPUT：POST /resolve 的 fields JSON（需与 doc_type 必填一致）",
    )
    parser.add_argument(
        "--integrations-save-customer",
        default=None,
        metavar="JSON",
        help="若设置：仅 POST {api}/integrations/erp/customer，JSON 或 @path，须含 org_id 与 fields",
    )
    args = parser.parse_args()

    if args.integrations_save_customer:
        return _run_integrations_save_customer(args.api_base, args.integrations_save_customer)

    uploads_url = f"{args.api_base.rstrip('/')}/uploads"
    try:
        created = _json_request("POST", uploads_url, _build_upload_payload(args.user_id, args.org_id))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        print(f"[失败] 创建上传任务失败: status={exc.code} body={body}")
        return 1
    except Exception as exc:
        print(f"[失败] 创建上传任务异常: {exc}")
        return 1

    ingestion_id = created.get("ingestion_id")
    status = created.get("status")
    if not ingestion_id:
        print(f"[失败] 上传返回缺少 ingestion_id: {created}")
        return 1

    print(f"[创建成功] ingestion_id={ingestion_id} status={status}")
    base = args.api_base.rstrip("/")
    detail_url = f"{base}/ingestions/{ingestion_id}"
    started = time.time()

    detail, now_status = _poll_until_terminal(detail_url, started, float(args.timeout_seconds), args.poll_interval)
    if now_status is None:
        print(f"[超时] {args.timeout_seconds}s 内未到目标状态，请检查 worker/redis/api 日志")
        return 2

    _print_terminal_summary(detail, now_status)
    exit_code = 0 if now_status != "FAILED" else 3

    if now_status == "NEED_USER_INPUT" and args.resolve_json:
        raw_arg = (args.resolve_json or "").strip()
        try:
            if raw_arg.startswith("@") and len(raw_arg) > 1:
                raw_text = _read_json_file_after_at(raw_arg)
            else:
                raw_text = raw_arg
            parsed = json.loads(raw_text)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[失败] 无法解析 --resolve-json: {exc}")
            return 4
        if not isinstance(parsed, dict):
            print("[失败] --resolve-json 解析后必须是 JSON 对象")
            return 4
        if "fields" in parsed and isinstance(parsed["fields"], dict):
            resolve_body: Dict[str, Any] = parsed
        else:
            resolve_body = {"fields": parsed}
        resolve_url = f"{base}/ingestions/{ingestion_id}/resolve"
        try:
            detail = _json_request("POST", resolve_url, resolve_body)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            print(f"[失败] resolve HTTP {exc.code} body={body}")
            return 5
        now_status = str(detail.get("status") or "")
        print(f"[resolve 已提交] status={now_status} missing_fields={detail.get('missing_fields')}")
        started2 = time.time()
        detail, now_status = _poll_until_terminal(detail_url, started2, float(args.timeout_seconds), args.poll_interval)
        if now_status is None:
            print(f"[超时] resolve 后 {args.timeout_seconds}s 内未到终态")
            return 2
        _print_terminal_summary(detail, now_status)
        exit_code = 0 if now_status != "FAILED" else 3

    if args.create_draft:
        if now_status != "VALIDATED":
            print(f"[跳过] --create-draft 要求当前为 VALIDATED，实际为 {now_status!r}")
            return exit_code
        draft_url = f"{base}/ingestions/{ingestion_id}/create-draft"
        try:
            draft = _json_request("POST", draft_url, {})
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            print(f"[失败] create-draft HTTP {exc.code} body={body}")
            return 6
        print(
            f"[草稿] status={draft.get('status')} draft_no={draft.get('draft_no')} "
            f"draft_url={draft.get('draft_url')} idempotency_key={draft.get('idempotency_key')}"
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
