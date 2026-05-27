import type { IngestionResponse } from "@/lib/types";
import { getCategoryHint, getCategoryLabel } from "@/lib/error-hints";

interface Props {
  ingestion: IngestionResponse;
}

export function ErrorDetailsCard({ ingestion }: Props) {
  if (ingestion.status !== "FAILED") return null;
  const details = ingestion.error_details ?? {};
  const fieldErrors = Array.isArray(details.field_errors) ? details.field_errors : [];

  return (
    <div className="rounded-md bg-rose-50 p-3 ring-1 ring-rose-200">
      <div className="font-semibold text-rose-900">
        出错了（代码 {ingestion.error_code || "UNKNOWN_ERROR"}）
      </div>
      <div className="mt-1 text-xs text-rose-800">
        问题类型：{getCategoryLabel(typeof details.category === "string" ? details.category : undefined)}
      </div>
      <div className="mt-1 rounded bg-white p-2 text-xs text-rose-900 ring-1 ring-rose-100">
        您可以怎么做：{getCategoryHint(typeof details.category === "string" ? details.category : undefined)}
      </div>
      {"erp_message" in details && details.erp_message ? (
        <div className="mt-1 text-xs text-rose-800">说明：{String(details.erp_message)}</div>
      ) : null}
      {"erp_status_code" in details && typeof details.erp_status_code === "number" ? (
        <div className="mt-1 text-xs text-rose-800">网络返回码：{details.erp_status_code}</div>
      ) : null}
      {"upstream_request_id" in details && details.upstream_request_id ? (
        <div className="mt-1 text-xs text-rose-800">请求编号（给技术支持用）：{String(details.upstream_request_id)}</div>
      ) : null}

      {fieldErrors.length > 0 ? (
        <div className="mt-2 rounded bg-white p-2 ring-1 ring-rose-100">
          <div className="text-xs font-semibold text-rose-900">具体哪几项不对</div>
          <pre className="mt-1 whitespace-pre-wrap break-words text-[11px] text-rose-900">
            {JSON.stringify(fieldErrors, null, 2)}
          </pre>
        </div>
      ) : null}

      {"raw" in details && details.raw ? (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs font-semibold text-rose-900">更多技术细节（一般不用点开）</summary>
          <pre className="mt-1 whitespace-pre-wrap break-words text-[11px] text-rose-900">
            {JSON.stringify(details.raw, null, 2)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}
