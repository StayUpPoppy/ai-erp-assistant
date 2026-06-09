type CategoryKey = "master_data" | "permission" | "timeout" | "upstream_error" | "unsupported_document" | "default";

const DEFAULT_LABELS: Record<CategoryKey, string> = {
  master_data: "主数据问题",
  permission: "权限问题",
  timeout: "上游超时",
  upstream_error: "上游系统错误",
  unsupported_document: "单据类型不支持",
  default: "未知错误分类",
};

const DEFAULT_HINTS: Record<CategoryKey, string> = {
  master_data: "请检查供应商/物料等主数据编码是否存在，并重新提交补全。",
  permission: "当前账号可能无对应组织或单据权限，请联系管理员开通后重试。",
  timeout: "ERP 请求超时，建议稍后重试；若频繁出现，请联系运维排查上游性能。",
  upstream_error: "ERP 返回系统错误，请记录上游请求ID并联系 ERP 团队排查。",
  unsupported_document: "当前文件非采购订单，已停止处理。请重新上传采购订单",
  default: "请查看原始错误详情并联系技术支持定位问题。",
};

function parseMapFromEnv(raw?: string): Partial<Record<CategoryKey, string>> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    const output: Partial<Record<CategoryKey, string>> = {};
    for (const [k, v] of Object.entries(parsed)) {
      if (typeof v !== "string") continue;
      if (
        k === "master_data" ||
        k === "permission" ||
        k === "timeout" ||
        k === "upstream_error" ||
        k === "unsupported_document" ||
        k === "default"
      ) {
        output[k] = v;
      }
    }
    return output;
  } catch {
    return {};
  }
}

const envLabels = parseMapFromEnv(process.env.NEXT_PUBLIC_ERROR_CATEGORY_LABELS_JSON);
const envHints = parseMapFromEnv(process.env.NEXT_PUBLIC_ERROR_CATEGORY_HINTS_JSON);

const LABELS: Record<CategoryKey, string> = { ...DEFAULT_LABELS, ...envLabels };
const HINTS: Record<CategoryKey, string> = { ...DEFAULT_HINTS, ...envHints };

function toCategoryKey(category?: string): CategoryKey {
  if (
    category === "master_data" ||
    category === "permission" ||
    category === "timeout" ||
    category === "upstream_error" ||
    category === "unsupported_document"
  ) {
    return category;
  }
  return "default";
}

export function getCategoryLabel(category?: string): string {
  return LABELS[toCategoryKey(category)];
}

export function getCategoryHint(category?: string): string {
  return HINTS[toCategoryKey(category)];
}
