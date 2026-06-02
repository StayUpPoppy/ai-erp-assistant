"use client";

import type { OrderPreviewData, PreviewEditableField, PreviewIssue } from "@/lib/types";

const HEADER_FIELDS: Array<{ key: keyof OrderPreviewData["order"]; label: string; type?: "text" | "number"; required?: boolean }> = [
  { key: "org", label: "销售组织", required: true },
  { key: "customerName", label: "客户名称", required: true },
  { key: "customerPoNo", label: "客户采购单号" },
  { key: "salesUser", label: "销售员" },
  { key: "orderDate", label: "订单日期", required: true },
  { key: "orderStatus", label: "订单状态" },
  { key: "deliveryAddr", label: "收货地址" },
  { key: "rate", label: "汇率", type: "number" },
  { key: "currency", label: "币别", required: true },
  { key: "deliveryDate", label: "交货期", required: true },
];

const DETAIL_COLUMNS: Array<{
  key: keyof OrderPreviewData["details"][number];
  label: string;
  type?: "text" | "number" | "boolean";
  required?: boolean;
}> = [
  { key: "materialCode", label: "物料编码", required: true },
  { key: "productName", label: "物料名称" },
  { key: "productSpec", label: "物料规格" },
  { key: "ph", label: "物料牌号" },
  { key: "customerMaterialNo", label: "客户物料编码" },
  { key: "qty", label: "数量", type: "number", required: true },
  { key: "price", label: "不含税单价", type: "number" },
  { key: "taxPrice", label: "含税单价", type: "number" },
  { key: "amount", label: "不含税金额", type: "number" },
  { key: "allAmount", label: "含税金额", type: "number" },
  { key: "tax", label: "税率", type: "number" },
  { key: "taxAmount", label: "税额", type: "number" },
  { key: "gift", label: "赠品", type: "boolean" },
  { key: "remark", label: "备注" },
];

function emptyDetail(): OrderPreviewData["details"][number] {
  return {
    materialCode: "",
    productName: "",
    productSpec: "",
    ph: "",
    customerMaterialNo: "",
    qty: null,
    price: null,
    taxPrice: null,
    amount: null,
    allAmount: null,
    tax: null,
    taxAmount: null,
    gift: false,
    remark: "",
  };
}

function stringify(value: unknown): string {
  if (value === null || value === undefined) return "";
  return String(value);
}

function isBlank(value: unknown): boolean {
  if (typeof value === "boolean") return false;
  return stringify(value).trim() === "";
}

function parsePath(
  path: string,
):
  | { scope: "order"; key: keyof OrderPreviewData["order"] }
  | { scope: "detail"; index: number; key: keyof OrderPreviewData["details"][number] }
  | null {
  if (path.startsWith("order.")) {
    return { scope: "order", key: path.slice(6) as keyof OrderPreviewData["order"] };
  }
  const match = /^details\[(\d+)\]\.([A-Za-z0-9_]+)$/.exec(path);
  if (!match) return null;
  return {
    scope: "detail",
    index: Number(match[1]),
    key: match[2] as keyof OrderPreviewData["details"][number],
  };
}

function readPreviewValue(preview: OrderPreviewData, path: string, fallback: string): string {
  const parsed = parsePath(path);
  if (!parsed) return fallback;
  if (parsed.scope === "order") return stringify(preview.order[parsed.key]);
  const detail = preview.details[parsed.index];
  if (!detail) return fallback;
  return stringify(detail[parsed.key]);
}

function fieldPathForOrder(key: keyof OrderPreviewData["order"]): string {
  return `order.${String(key)}`;
}

function fieldPathForDetail(index: number, key: keyof OrderPreviewData["details"][number]): string {
  return `details[${index}].${String(key)}`;
}

export interface OrderPreviewEditorProps {
  preview: OrderPreviewData;
  editableFields: PreviewEditableField[];
  issues: PreviewIssue[];
  onChange: (next: OrderPreviewData) => void;
  onConfirm: () => void | Promise<void>;
  onCreateDraft: () => void | Promise<void>;
  confirming: boolean;
  creatingDraft: boolean;
  createDraftDisabled: boolean;
  hideActions?: boolean;
  readOnly?: boolean;
  lockedSalesUser?: string;
}

export function OrderPreviewEditor({
  preview,
  editableFields,
  issues,
  onChange,
  onConfirm,
  onCreateDraft,
  confirming,
  creatingDraft,
  createDraftDisabled,
  hideActions = false,
  readOnly = false,
  lockedSalesUser,
}: OrderPreviewEditorProps) {
  const editableByPath = new Map(editableFields.map((field) => [field.path, field]));

  const updateOrderField = (key: keyof OrderPreviewData["order"], raw: string) => {
    if (key === "salesUser" && lockedSalesUser !== undefined) return;
    onChange({
      ...preview,
      order: {
        ...preview.order,
        [key]: key === "rate" ? (raw.trim() ? Number(raw) : null) : raw,
      },
    });
  };

  const updateDetailField = (index: number, key: keyof OrderPreviewData["details"][number], raw: string | boolean) => {
    const nextDetails = preview.details.map((detail, i) => {
      if (i !== index) return detail;
      if (typeof raw === "boolean") return { ...detail, [key]: raw };
      const isNumeric = DETAIL_COLUMNS.find((column) => column.key === key)?.type === "number";
      return {
        ...detail,
        [key]: isNumeric ? (raw.trim() ? Number(raw) : null) : raw,
      };
    });
    onChange({ ...preview, details: nextDetails });
  };

  const addDetailRow = () => onChange({ ...preview, details: [...preview.details, emptyDetail()] });
  const removeDetailRow = (index: number) =>
    onChange({
      ...preview,
      details: preview.details.length > 1 ? preview.details.filter((_, i) => i !== index) : [emptyDetail()],
    });

  const missingPaths = new Set<string>();
  for (const field of HEADER_FIELDS) {
    const path = fieldPathForOrder(field.key);
    const meta = editableByPath.get(path);
    if ((field.required || meta?.required) && isBlank(preview.order[field.key])) missingPaths.add(path);
  }
  preview.details.forEach((detail, index) => {
    for (const column of DETAIL_COLUMNS) {
      const path = fieldPathForDetail(index, column.key);
      const meta = editableByPath.get(path);
      if ((column.required || meta?.required) && isBlank(detail[column.key])) missingPaths.add(path);
    }
  });

  const issueRows = [
    ...Array.from(missingPaths).map((path) => ({
      path,
      level: "error",
      message: editableByPath.get(path)?.reason || "LLM 未识别到该必填字段，请在表格中补充。",
    })),
    ...issues,
  ];
  const warningCount = issueRows.filter((issue) => issue.level !== "error").length;

  const inputClass = (path: string) =>
    [
      "w-full rounded-lg border bg-white px-3 py-2 outline-none transition",
      missingPaths.has(path)
        ? "border-red-400 bg-red-50/60 text-red-950 focus:border-red-500 focus:ring-2 focus:ring-red-200"
        : "border-slate-200 focus:border-sky-300 focus:ring-2 focus:ring-sky-200/80",
    ].join(" ");

  return (
    <div className="w-full rounded-2xl border border-slate-200/90 bg-white p-4 shadow-sm shadow-slate-200/30 ring-1 ring-slate-900/[0.03] sm:p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-base font-semibold text-slate-900">订单预览与确认</div>
          <div className="mt-1 text-sm text-slate-600">LLM 已识别的内容会自动填入表格，红色字段需要人工补齐后再确认。</div>
        </div>
        <div className={hideActions ? "hidden" : "flex flex-wrap gap-2"}>
          <button
            type="button"
            disabled={confirming || readOnly}
            onClick={() => void onConfirm()}
            className="rounded-xl bg-slate-900 px-5 py-3 text-base font-semibold text-white shadow-sm transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {confirming ? "确认中..." : "确认预览并校验"}
          </button>
          <button
            type="button"
            disabled={createDraftDisabled || creatingDraft || readOnly}
            onClick={() => void onCreateDraft()}
            className="rounded-xl bg-emerald-700 px-5 py-3 text-base font-semibold text-white shadow-sm transition hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {creatingDraft ? "生成中..." : "生成草稿"}
          </button>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2 text-sm">
        <span className={missingPaths.size ? "rounded-lg bg-red-50 px-3 py-1.5 font-medium text-red-800 ring-1 ring-red-200" : "rounded-lg bg-emerald-50 px-3 py-1.5 font-medium text-emerald-800 ring-1 ring-emerald-200"}>
          {missingPaths.size ? `还有 ${missingPaths.size} 个必填字段未补齐` : "必填字段已补齐"}
        </span>
        <span className="rounded-lg bg-slate-50 px-3 py-1.5 font-medium text-slate-700 ring-1 ring-slate-200">
          {warningCount ? `${warningCount} 个字段建议核对` : "暂无额外风险提示"}
        </span>
      </div>

      <div className="mt-4 overflow-hidden rounded-xl border border-slate-200">
        <table className="min-w-full border-collapse text-sm">
          <thead className="bg-slate-50 text-left text-slate-700">
            <tr>
              <th className="border-b border-slate-200 px-3 py-2 font-semibold">订单头字段</th>
              <th className="border-b border-slate-200 px-3 py-2 font-semibold">值</th>
            </tr>
          </thead>
          <tbody>
            {HEADER_FIELDS.map((field) => {
              const path = fieldPathForOrder(field.key);
              const locked = field.key === "salesUser" && lockedSalesUser !== undefined;
              const value = locked ? lockedSalesUser : stringify(preview.order[field.key]);
              return (
                <tr key={field.key} className={missingPaths.has(path) ? "bg-red-50/40" : "odd:bg-white even:bg-slate-50/40"}>
                  <td className={["border-b border-slate-100 px-3 py-2", missingPaths.has(path) ? "font-semibold text-red-800" : "text-slate-700"].join(" ")}>
                    {field.label}
                    {field.required ? <span className="ml-1 text-red-600">*</span> : null}
                    {missingPaths.has(path) ? <div className="mt-0.5 text-xs font-normal text-red-600">未识别，需补充</div> : null}
                  </td>
                  <td className="border-b border-slate-100 px-3 py-2">
                    <input
                      value={value}
                      disabled={readOnly || locked}
                      onChange={(event) => updateOrderField(field.key, event.target.value)}
                      className={locked ? `${inputClass(path)} cursor-not-allowed bg-slate-100 text-slate-600` : inputClass(path)}
                      placeholder={field.type === "number" ? "请输入数字" : ""}
                    />
                    {locked ? <div className="mt-1 text-xs text-slate-400">已绑定当前登录用户</div> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="mt-5">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm font-semibold text-slate-900">订单明细</div>
          <button
            type="button"
            disabled={readOnly}
            onClick={addDetailRow}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
          >
            新增一行
          </button>
        </div>
        <div className="overflow-x-auto rounded-xl border border-slate-200">
          <table className="min-w-[1100px] border-collapse text-sm">
            <thead className="bg-slate-50 text-left text-slate-700">
              <tr>
                <th className="border-b border-slate-200 px-3 py-2 font-semibold">行</th>
                {DETAIL_COLUMNS.map((column) => (
                  <th key={column.key} className="border-b border-slate-200 px-3 py-2 font-semibold">
                    {column.label}
                    {column.required ? <span className="ml-1 text-red-600">*</span> : null}
                  </th>
                ))}
                <th className="border-b border-slate-200 px-3 py-2 font-semibold">操作</th>
              </tr>
            </thead>
            <tbody>
              {preview.details.map((detail, index) => (
                <tr key={index} className="odd:bg-white even:bg-slate-50/40 align-top">
                  <td className="border-b border-slate-100 px-3 py-2 font-mono text-slate-500">{index + 1}</td>
                  {DETAIL_COLUMNS.map((column) => {
                    const path = fieldPathForDetail(index, column.key);
                    return (
                      <td key={String(column.key)} className={["border-b border-slate-100 px-2 py-2", missingPaths.has(path) ? "bg-red-50/50" : ""].join(" ")}>
                        {column.type === "boolean" ? (
                          <label className="flex items-center justify-center pt-2">
                            <input
                              type="checkbox"
                              checked={Boolean(detail[column.key] as boolean)}
                              disabled={readOnly}
                              onChange={(event) => updateDetailField(index, column.key, event.target.checked)}
                              className="h-4 w-4 rounded border-slate-300 text-sky-600 focus:ring-sky-300"
                            />
                          </label>
                        ) : (
                          <>
                            <input
                              value={stringify(detail[column.key])}
                              disabled={readOnly}
                              onChange={(event) => updateDetailField(index, column.key, event.target.value)}
                              className={[inputClass(path), "min-w-[7rem] px-2.5"].join(" ")}
                              placeholder={column.type === "number" ? "数字" : ""}
                            />
                            {missingPaths.has(path) ? <div className="mt-1 text-xs text-red-600">未识别，需补充</div> : null}
                          </>
                        )}
                      </td>
                    );
                  })}
                  <td className="border-b border-slate-100 px-3 py-2">
                    <button
                      type="button"
                      disabled={readOnly}
                      onClick={() => removeDetailRow(index)}
                      className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-5 rounded-xl border border-slate-200 bg-slate-50 p-4">
        <div className="text-sm font-semibold text-slate-900">问题摘要</div>
        <div className="mt-2 space-y-2">
          {issueRows.length === 0 ? (
            <div className="text-sm text-slate-600">当前没有需要提示的问题。</div>
          ) : (
            issueRows.map((issue, index) => (
              <div
                key={`${issue.path}-${index}`}
                className={[
                  "rounded-lg bg-white px-3 py-2 text-sm ring-1",
                  issue.level === "error" ? "text-red-800 ring-red-200" : "text-slate-700 ring-slate-200",
                ].join(" ")}
              >
                <div className="font-medium">
                  {issue.level === "error" ? "必填缺失" : issue.level.toUpperCase()}
                  {issue.path ? <span className="ml-2 font-mono text-xs opacity-70">{issue.path}</span> : null}
                </div>
                <div className="mt-1">{issue.message}</div>
                {issue.path ? <div className="mt-1 text-xs opacity-70">请在上方表格对应字段中补充或核对。</div> : null}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
