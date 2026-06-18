"use client";

/**
 * 助手主页面（MVP）
 *
 * 职责划分：
 * 1) 聊天区：左侧意图（含「文档解析」「报表入库」）；ERP 问答走 /chat/erp-qa；上传走 /uploads/binary；
 * 2) 拖拽上传：multipart 调 POST /uploads/binary（服务端算哈希并建任务；JSON 版 /uploads 仍保留作兼容）；
 * 3) 任务区：轮询 GET /ingestions/{id} 展示状态机与审计事件；
 * 4) 补全区：当 NEED_USER_INPUT 时提交 POST /ingestions/{id}/resolve（字段已齐时 worker 也可能直接推到 VALIDATED）；
 * 5) 建草稿：VALIDATED 后 POST /ingestions/{id}/create-draft，展示草稿号与跳转链接；
 * 6) 日志：所有关键步骤写入 clientLogger（控制台 + 页面 LogPanel）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ErrorDetailsCard } from "@/components/ErrorDetailsCard";
import { LogPanel } from "@/components/LogPanel";
import { OrderPreviewEditor } from "@/components/OrderPreviewEditor";
import { clientLogger } from "@/lib/client-logger";
import {
  getAssistantSession,
  getApiBaseUrl,
  getCurrentUser,
  getHealth,
  getIngestion,
  postAssistantFile,
  postAssistantLlmProbe,
  postAssistantMessage,
  postCancelIngestion,
  streamAssistantMessage,
} from "@/lib/api";
import { precheckUploadFile, SUPPORTED_UPLOAD_EXTENSIONS } from "@/lib/upload-policy";
import type { AuditEvent, HealthResponse, IngestionResponse, IngestionStatus, OrderPreviewData, ToolUi } from "@/lib/types";

type ChatRole = "user" | "assistant" | "system";
type WorkspaceMode = "pdf_to_erp" | "assistant";

interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
  progressStatus?: IngestionStatus;
  toolUi?: ToolUi | null;
}

interface ChatSessionMeta {
  id: string;
  title: string;
  lastMessage: string;
  updatedAt: string;
  taskType?: string | null;
  taskStatus?: string | null;
  taskIngestionId?: string | null;
  titleEdited?: boolean;
}

type ClientDraftStateByIngestion = Record<string, { draft_no?: string | null; draft_url?: string | null }>;
type IngestionById = Record<string, IngestionResponse>;
type PreviewDraftByIngestion = Record<string, OrderPreviewData | null>;
type ResolveFieldsByIngestion = Record<string, Record<string, string>>;
type BooleanByIngestion = Record<string, true>;
type StatusByIngestion = Record<string, IngestionStatus | null>;
type StringByIngestion = Record<string, string | null>;

/** 已切换到新任务前的解析任务摘要（会话内保留，便于回看编号与文件名） */
interface IngestionHistoryItem {
  id: string;
  fileName: string;
  status: string;
}

interface PendingReprocessUpload {
  file: File;
  userId: string;
  orgId: string;
  extractionProfileId?: string;
  sessionId: string;
  ingestionId?: string;
}

interface WorkspaceWindowState {
  chatInput: string;
  chatMessages: ChatMessage[];
  assistantSessionId: string | null;
  ingestion: IngestionResponse | null;
  ingestionId: string | null;
  ingestionsById: IngestionById;
  pollingIngestionIds: BooleanByIngestion;
  ingestionHistory: IngestionHistoryItem[];
  resolveFields: Record<string, string>;
  resolveFieldsByIngestion: ResolveFieldsByIngestion;
  previewDraft: OrderPreviewData | null;
  previewDraftsByIngestion: PreviewDraftByIngestion;
  confirmedPreviewIds: Record<string, true>;
  previewDirtyByIngestion: BooleanByIngestion;
  lastStatus: IngestionStatus | null;
  lastStatusByIngestion: StatusByIngestion;
  workflowToolCardKey: string | null;
  workflowToolCardKeyByIngestion: StringByIngestion;
  lastIngestionFileName: string | null;
  lastIngestionFileNameByIngestion: StringByIngestion;
  previewDirty: boolean;
  previewIngestionId: string | null;
  poll404Warned: boolean;
  poll404WarnedByIngestion: BooleanByIngestion;
}

const INGESTION_HISTORY_SS_KEY = "ai_erp_assistant_ingestion_history_v1";
const ASSISTANT_SESSION_LS_KEY = "ai_erp_assistant_session_id_v1";
const ASSISTANT_SESSIONS_LS_KEY = "ai_erp_assistant_sessions_v1";

const POLL_FAST_MS = 1000;
const POLL_STEADY_MS = 2000;
const POLL_ERROR_MAX_MS = 5000;

function createAssistantSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function createSessionMeta(id: string, now = new Date().toISOString()): ChatSessionMeta {
  return {
    id,
    title: "新聊天",
    lastMessage: "还没有消息",
    updatedAt: now,
  };
}

function isChatSessionMeta(value: unknown): value is ChatSessionMeta {
  return Boolean(value) && typeof (value as ChatSessionMeta).id === "string";
}

function parseChatSessionMetas(raw: string | null): ChatSessionMeta[] {
  try {
    const parsed = JSON.parse(raw ?? "[]") as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isChatSessionMeta).slice(0, 50);
  } catch {
    return [];
  }
}

function mergeChatSessionMetas(primary: ChatSessionMeta[], secondary: ChatSessionMeta[]): ChatSessionMeta[] {
  const seen = new Set<string>();
  const merged: ChatSessionMeta[] = [];
  for (const session of [...primary, ...secondary]) {
    if (!session.id || seen.has(session.id)) continue;
    seen.add(session.id);
    merged.push(session);
  }
  return merged.slice(0, 50);
}

function compactSessionText(text: string, fallback: string): string {
  const cleaned = (text || "").replace(/\s+/g, " ").trim();
  if (!cleaned) return fallback;
  return cleaned.length > 28 ? `${cleaned.slice(0, 28)}...` : cleaned;
}

function sessionTaskLabel(taskType?: string | null): string | null {
  if (!taskType) return null;
  if (taskType === "pdf_to_erp") return "PDF 转 ERP";
  if (taskType === "erp_qa") return "ERP 查询";
  if (taskType === "assistant") return "普通对话";
  return taskType;
}

function pdfToErpProgressPercent(status: IngestionStatus | null | undefined, previewReady = false): number {
  if (previewReady) return 100;
  if (!status) return 0;
  if (status === "VALIDATED" || status === "DRAFT_CREATED") return 100;
  if (status === "CANCELED") return 100;
  if (status === "FAILED") return 100;
  const idx = statusIndex(status);
  if (idx < 0) return 0;
  return Math.round(((idx + 1) / STATUS_PIPELINE.length) * 100);
}

/** 状态机顺序：用于在 UI 上高亮当前进度（与后端枚举一致） */
const STATUS_PIPELINE: IngestionStatus[] = [
  "UPLOADED",
  "CLASSIFIED",
  "PARSED",
  "EXTRACTED",
  "MAPPED",
  "NEED_USER_INPUT",
  "VALIDATED",
  "DRAFT_CREATED",
  "CANCELED",
];

function statusIndex(status: IngestionStatus): number {
  return STATUS_PIPELINE.indexOf(status);
}

const TERMINAL_STATUSES = new Set<IngestionStatus>(["DRAFT_CREATED", "FAILED", "CANCELED"]);
const BACKGROUND_RUNNING_STATUSES = new Set<IngestionStatus>([
  "UPLOADED",
  "CLASSIFIED",
  "PARSED",
  "EXTRACTED",
  "MAPPED",
]);

function isBackgroundRunningStatus(status: IngestionStatus | null | undefined): boolean {
  return Boolean(status && BACKGROUND_RUNNING_STATUSES.has(status));
}

function pdfToErpProgressState(
  status: IngestionStatus | null | undefined,
  previewReady = false,
): "running" | "done" | "failed" | "canceled" {
  if (status === "FAILED") return "failed";
  if (status === "CANCELED") return "canceled";
  if (previewReady || status === "NEED_USER_INPUT" || status === "VALIDATED" || status === "DRAFT_CREATED") return "done";
  return "running";
}

function nextIngestionPollDelay(status: IngestionStatus | null | undefined, failureCount = 0): number | null {
  if (status && TERMINAL_STATUSES.has(status)) return null;
  if (!isBackgroundRunningStatus(status)) return null;
  if (failureCount > 0) return Math.min(POLL_ERROR_MAX_MS, POLL_STEADY_MS + failureCount * 1000);
  return status === "UPLOADED" || status === "CLASSIFIED" ? POLL_FAST_MS : POLL_STEADY_MS;
}

function shouldCancelTaskOnSessionDelete(status: string | null | undefined): boolean {
  return Boolean(status && !["DRAFT_CREATED", "FAILED", "CANCELED", "DONE"].includes(status));
}

function buildPdfToErpProgressUi(ingestion: IngestionResponse, status: IngestionStatus): ToolUi {
  return {
    type: "processing",
    data: {
      ingestion_id: ingestion.ingestion_id,
      status,
      tool_status: status,
      file_name: ingestion.file?.source_file_name ?? ingestion.source_file_name ?? "",
      preview_ready: Boolean(ingestion.preview_data),
    },
  };
}

function mergeDraftCreatedState(
  base: IngestionResponse,
  draft: { ingestion_id?: string; status: IngestionStatus; draft_no?: string | null; draft_url?: string | null },
): IngestionResponse {
  if (draft.ingestion_id && base.ingestion_id !== draft.ingestion_id) return base;
  if (draft.status !== "DRAFT_CREATED" && !draft.draft_no) return base;
  return {
    ...base,
    status: "DRAFT_CREATED",
    draft_no: draft.draft_no ?? base.draft_no,
    draft_url: draft.draft_url ?? base.draft_url,
    audit_events: base.audit_events?.some((event) => event.status === "DRAFT_CREATED")
      ? base.audit_events
      : [
          ...(base.audit_events ?? []),
          {
            at: new Date().toISOString(),
            status: "DRAFT_CREATED",
            message: `draft created${draft.draft_no ? `: ${draft.draft_no}` : ""}`,
          },
        ],
  };
}

function applyClientDraftState(
  ingestion: IngestionResponse,
  clientDrafts: ClientDraftStateByIngestion,
): IngestionResponse {
  const clientDraft = clientDrafts[ingestion.ingestion_id];
  if (!clientDraft) return ingestion;
  return mergeDraftCreatedState(ingestion, {
    ingestion_id: ingestion.ingestion_id,
    status: "DRAFT_CREATED",
    draft_no: clientDraft.draft_no,
    draft_url: clientDraft.draft_url,
  });
}

function displayIngestionStatus(
  ingestion: IngestionResponse | null | undefined,
  clientDrafts: ClientDraftStateByIngestion = {},
): IngestionStatus | null {
  if (!ingestion) return null;
  if (clientDrafts[ingestion.ingestion_id]) return "DRAFT_CREATED";
  return ingestion.status;
}

function buildPdfToErpToolUi(ingestion: IngestionResponse): ToolUi | null {
  const base = {
    ingestion_id: ingestion.ingestion_id,
    status: ingestion.status,
    tool_status: ingestion.status,
  };

  if (isBackgroundRunningStatus(ingestion.status)) {
    return buildPdfToErpProgressUi(ingestion, ingestion.status);
  }

  if (ingestion.status === "NEED_USER_INPUT") {
    const editableByPath = new Map((ingestion.editable_fields ?? []).map((field) => [field.path, field]));
    const fields = (ingestion.missing_fields ?? []).map((key) => {
      const editable = editableByPath.get(key);
      return {
        key,
        label: editable?.label ?? RESOLVE_FIELD_LABELS[key] ?? key,
        current_value: ingestion.resolved_fields?.[key] ?? editable?.current_value ?? "",
        required: editable?.required ?? true,
        reason: editable?.reason ?? "required",
        confidence: editable?.confidence ?? 0,
      };
    });
    return {
      type: "missing_fields_form",
      data: {
        ...base,
        fields,
        preview_data: ingestion.preview_data ?? null,
      },
    };
  }

  if (ingestion.status === "VALIDATED") {
    return {
      type: "upload_confirm",
      data: {
        ...base,
        preview_data: ingestion.preview_data ?? null,
        editable_fields: ingestion.editable_fields ?? [],
        issues: ingestion.issues ?? [],
      },
    };
  }

  if (ingestion.status === "DRAFT_CREATED") {
    return {
      type: "draft_result",
      data: {
        ...base,
        draft_no: ingestion.draft_no ?? "",
        draft_url: ingestion.draft_url ?? "",
      },
    };
  }

  if (ingestion.status === "FAILED") {
    return {
      type: "error",
      data: {
        ...base,
        error_code: ingestion.error_code ?? "UNKNOWN",
        error_details: ingestion.error_details ?? null,
      },
    };
  }

  return null;
}

function ProgressSpinner({ className = "" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={[
        "inline-block h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent",
        className,
      ].join(" ")}
    />
  );
}

/** 给外行看的进度说明（系统消息里用）；技术枚举仍在下方详情里可见 */
function ingestionStatusLabelZh(status: IngestionStatus | string): string {
  const map: Record<string, string> = {
    UPLOADED: "已收到文件，正在排队处理",
    CLASSIFIED: "已判断单据类型",
    PARSED: "已从文件中读出文字",
    EXTRACTED: "已尝试自动填写部分字段",
    MAPPED: "已对照系统里的编码",
    NEED_USER_INPUT: "需要您再补几项信息",
    VALIDATED: "信息已齐，可以点「生成草稿」",
    DRAFT_CREATED: "草稿已生成",
    FAILED: "处理失败，请看下方说明",
    CANCELED: "任务已取消",
  };
  return map[status] ?? String(status);
}

function ingestionStatusShortLabel(status: IngestionStatus | string): string {
  const map: Record<string, string> = {
    UPLOADED: "已上传",
    CLASSIFIED: "已分类",
    PARSED: "已读取",
    EXTRACTED: "已抽取",
    MAPPED: "已映射",
    NEED_USER_INPUT: "待补充",
    VALIDATED: "待上传",
    DRAFT_CREATED: "已生成草稿",
    FAILED: "失败",
    CANCELED: "已取消",
  };
  return map[status] ?? String(status);
}

function pdfToErpWorkflowCardText(ingestion: IngestionResponse, status: IngestionStatus): string {
  if (status === "NEED_USER_INPUT") {
    const count = ingestion.missing_fields?.length ?? 0;
    return count > 0
      ? `我已经读完这份文件，还需要你补充 ${count} 个字段。`
      : "我已经读完这份文件，还需要你补充几个字段。";
  }
  if (status === "VALIDATED") {
    return "订单信息已经校验通过。请在下面确认预览，确认后我就上传到 ERP 创建草稿。";
  }
  if (status === "DRAFT_CREATED") {
    return ingestion.draft_no ? `ERP 草稿已经创建：${ingestion.draft_no}` : "ERP 草稿已经创建。";
  }
  if (status === "FAILED" && ingestion.error_code === "UNSUPPORTED_DOCUMENT") {
    return "";
  }
  if (status === "FAILED" && ingestion.error_code === "UNSUPPORTED_DOCUMENT") {
    return "当前文件非采购订单，已停止处理。请重新上传采购订单";
  }
  if (status === "FAILED") {
    return "这次 PDF 转 ERP 处理失败了，我把错误信息放在下面。";
  }
  return ingestionStatusLabelZh(status);
}

function pdfToErpWorkflowCardKey(ingestion: IngestionResponse, status: IngestionStatus): string {
  const missing = (ingestion.missing_fields ?? []).join(",");
  const draftNo = ingestion.draft_no ?? "";
  const errorCode = ingestion.error_code ?? "";
  return [ingestion.ingestion_id, status, missing, draftNo, errorCode].join(":");
}

function hasWorkflowCardForIngestion(messages: ChatMessage[], ingestion: IngestionResponse): boolean {
  const status = ingestion.status;
  return messages.some((message) => {
    const ui = message.toolUi;
    if (!ui) return false;
    if (!["missing_fields_form", "upload_confirm", "draft_result", "error"].includes(ui.type)) return false;
    return String(ui.data.ingestion_id ?? "") === ingestion.ingestion_id && String(ui.data.status ?? "") === status;
  });
}

function chatMessageDedupeKey(message: Pick<ChatMessage, "role" | "content" | "toolUi">): string {
  const ui = message.toolUi;
  if (!ui) return `${message.role}:text:${message.content}`;
  return [
    message.role,
    ui.type,
    String(ui.data.ingestion_id ?? ""),
    String(ui.data.status ?? ""),
    String(ui.data.error_code ?? ""),
    message.content,
  ].join(":");
}

function formatPreviewAmount(value: number | null | undefined, currency: string | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未识别";
  return `${currency || ""} ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)}`.trim();
}

function sumPreviewAmount(preview: OrderPreviewData): number | null {
  let total = 0;
  let seen = false;
  for (const detail of preview.details ?? []) {
    const value = typeof detail.allAmount === "number" ? detail.allAmount : detail.amount;
    if (typeof value === "number" && Number.isFinite(value)) {
      total += value;
      seen = true;
    }
  }
  return seen ? total : null;
}

function updatePdfToErpProgressCards(messages: ChatMessage[], ingestion: IngestionResponse, status: IngestionStatus): ChatMessage[] {
  const progressUi = buildPdfToErpProgressUi(ingestion, status);
  let changed = false;
  const next = messages.map((message) => {
    const ui = message.toolUi;
    if (!ui || ui.type !== "processing") return message;
    if (String(ui.data.ingestion_id ?? "") !== ingestion.ingestion_id) return message;
    changed = true;
    return { ...message, toolUi: progressUi };
  });
  return changed ? next : messages;
}

function updateWorkflowToolCard(messages: ChatMessage[], ingestion: IngestionResponse, toolUi: ToolUi): ChatMessage[] {
  const ingestionId = String(toolUi.data.ingestion_id ?? ingestion.ingestion_id);
  let changed = false;
  let updated = false;
  const next: ChatMessage[] = [];
  for (const message of messages) {
    const ui = message.toolUi;
    const sameCard = Boolean(ui && ui.type === toolUi.type && String(ui.data.ingestion_id ?? "") === ingestionId);
    if (!sameCard) {
      next.push(message);
      continue;
    }
    if (updated) {
      changed = true;
      continue;
    }
    updated = true;
    changed = true;
    next.push({
      ...message,
      content: pdfToErpWorkflowCardText(ingestion, ingestion.status),
      toolUi,
    });
  }
  return changed ? next : messages;
}

/** FAILED 时根据审计事件推断失败前最后一档，用于进度条高亮已走过节点 */
function removePdfToErpTaskCards(messages: ChatMessage[], ingestionId: string): ChatMessage[] {
  const removableTypes = new Set(["processing", "missing_fields_form", "upload_confirm", "draft_result", "error"]);
  const next = messages.filter((message) => {
    const ui = message.toolUi;
    if (!ui || !removableTypes.has(ui.type)) return true;
    return String(ui.data.ingestion_id ?? "") !== ingestionId;
  });
  return next.length === messages.length ? messages : next;
}

function pipelineHighlightStepIndex(
  status: IngestionStatus | null | undefined,
  auditEvents: AuditEvent[] | undefined,
): number {
  if (!status) return -1;
  if (status !== "FAILED") return statusIndex(status);
  const evs = auditEvents ?? [];
  if (evs.length < 2) return -1;
  const last = evs[evs.length - 1];
  const prev = evs[evs.length - 2];
  if (last?.status === "FAILED" && prev?.status) {
    const p = statusIndex(prev.status as IngestionStatus);
    return p >= 0 ? p : -1;
  }
  return -1;
}

/** 与后端 `required_field_keys` 一致，用于补全表单字段列表 */
const RESOLVE_KEYS_BY_DOC: Record<string, string[]> = {
  PO: ["vendor_code", "doc_date", "currency", "material_code", "line_qty"],
  GR: ["vendor_code", "doc_date", "currency", "po_no", "material_code", "qty_received"],
  INV: ["vendor_code", "doc_date", "currency", "invoice_no", "invoice_date"],
};

/** 非必填但与 ERP 草稿常用；resolve 时若有填写会一并提交 */
const OPTIONAL_RESOLVE_KEYS = ["warehouse_code", "tax_code"] as const;

/** 补全表单字段中文说明（与键名并列展示） */
const RESOLVE_FIELD_LABELS: Record<string, string> = {
  org: "销售组织",
  customerName: "客户名称",
  vendor_code: "供应商编码",
  doc_date: "单据日期",
  currency: "币别",
  material_code: "物料编码",
  line_qty: "数量",
  delivery_date: "交货日期",
  po_no: "采购订单号",
  qty_received: "收货数量",
  invoice_no: "发票号码",
  invoice_date: "发票日期",
  warehouse_code: "仓库编码",
  tax_code: "税码",
};

function resolveFieldKeys(
  docHint: string | null | undefined,
  ing: Pick<IngestionResponse, "required_resolve_keys"> | null,
): string[] {
  const fromApi = ing?.required_resolve_keys?.filter(Boolean);
  if (fromApi?.length) return [...fromApi];
  const k = (docHint ?? "PO").toString().toUpperCase();
  return RESOLVE_KEYS_BY_DOC[k] ?? RESOLVE_KEYS_BY_DOC.PO;
}

function bindPreviewSalesUser(preview: OrderPreviewData, salesUser: string): OrderPreviewData {
  if (preview.order.salesUser === salesUser) return preview;
  return {
    ...preview,
    order: {
      ...preview.order,
      salesUser,
    },
  };
}

function syncPreviewOrg(preview: OrderPreviewData, org: string): OrderPreviewData {
  if (!org || preview.order.org === org) return preview;
  return {
    ...preview,
    order: {
      ...preview.order,
      org,
    },
  };
}

function syncPreviewDefaults(preview: OrderPreviewData, salesUser: string, org: string): OrderPreviewData {
  return syncPreviewOrg(bindPreviewSalesUser(preview, salesUser), org);
}

function resolvedFieldsFromIngestion(ingestion: IngestionResponse): Record<string, string> {
  if (!ingestion.resolved_fields) return {};
  const rf = ingestion.resolved_fields as Record<string, string | undefined>;
  const keys = resolveFieldKeys(ingestion.doc_type_hint, ingestion);
  const next: Record<string, string> = {};
  for (const k of keys) {
    const v = rf[k];
    if (v != null && String(v).trim() !== "") next[k] = String(v);
  }
  for (const k of OPTIONAL_RESOLVE_KEYS) {
    const v = rf[k];
    if (v != null && String(v).trim() !== "") next[k] = String(v);
  }
  return next;
}

function withoutRecordKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}

export default function HomePage() {
  /** 简化认证：先从 ERP userInfo Cookie 同步用户和组织，后续再升级为服务端可信身份 */
  const [orgId, setOrgId] = useState("英科一厂");
  const [userId, setUserId] = useState("演示用户");
  const [userName, setUserName] = useState("演示用户");
  const [assistantSessionId, setAssistantSessionId] = useState<string | null>(null);
  const [healthInfo, setHealthInfo] = useState<HealthResponse | null>(null);
  /** 可选：对应 API 侧 backend/config/extraction_profiles/{id}.json；留空则按 org_id / default 自动选 */
  const [extractionProfileId, setExtractionProfileId] = useState("datynk-dev");

  const [chatInput, setChatInput] = useState("");
  const [isChatSending, setIsChatSending] = useState(false);
  const [isLlmProbeRunning, setIsLlmProbeRunning] = useState(false);
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>("pdf_to_erp");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatSessions, setChatSessions] = useState<ChatSessionMeta[]>([]);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  /** 当前 ingestion 详情：由轮询刷新 */
  const [ingestion, setIngestion] = useState<IngestionResponse | null>(null);
  const [ingestionId, setIngestionId] = useState<string | null>(null);
  const [ingestionsById, setIngestionsById] = useState<IngestionById>({});
  const [pollingIngestionIds, setPollingIngestionIds] = useState<BooleanByIngestion>({});
  const [ingestionPollNonce, setIngestionPollNonce] = useState(0);
  const [ingestionHistory, setIngestionHistory] = useState<IngestionHistoryItem[]>([]);

  /** 拖拽上传中的 UX 状态 */
  const [isDragging, setIsDragging] = useState(false);
  /** 上传请求进行中（含网络传输与服务端读文件算哈希时间） */
  const [isUploading, setIsUploading] = useState(false);

  /** 补全表单：键与后端 `required_field_keys` 对齐 */
  const [resolveFields, setResolveFields] = useState<Record<string, string>>({});
  const [resolveFieldsByIngestion, setResolveFieldsByIngestion] = useState<ResolveFieldsByIngestion>({});
  const [previewDraft, setPreviewDraft] = useState<OrderPreviewData | null>(null);
  const [previewDraftsByIngestion, setPreviewDraftsByIngestion] = useState<PreviewDraftByIngestion>({});
  const [confirmedPreviewIds, setConfirmedPreviewIds] = useState<Record<string, true>>({});
  const [previewDirtyByIngestion, setPreviewDirtyByIngestion] = useState<BooleanByIngestion>({});

  const [isResolving, setIsResolving] = useState(false);
  const [isConfirmingPreview, setIsConfirmingPreview] = useState(false);
  const [isCreatingDraft, setIsCreatingDraft] = useState(false);
  const [resolvingIngestionIds, setResolvingIngestionIds] = useState<BooleanByIngestion>({});
  const [confirmingPreviewIngestionIds, setConfirmingPreviewIngestionIds] = useState<BooleanByIngestion>({});
  const [creatingDraftIngestionIds, setCreatingDraftIngestionIds] = useState<BooleanByIngestion>({});

  const lastStatusRef = useRef<IngestionStatus | null>(null);
  const lastStatusByIngestionRef = useRef<StatusByIngestion>({});
  const workflowToolCardKeyRef = useRef<string | null>(null);
  const workflowToolCardKeyByIngestionRef = useRef<StringByIngestion>({});
  const resolveFieldsByIngestionRef = useRef<ResolveFieldsByIngestion>({});
  const historyHydratedRef = useRef(false);
  const chatSessionsHydratedRef = useRef(false);
  const previewDirtyRef = useRef(false);
  const previewDirtyByIngestionRef = useRef<BooleanByIngestion>({});
  const previewIngestionIdRef = useRef<string | null>(null);
  const chatSessionMetaTimerRef = useRef<number | null>(null);
  /** 当前 ingestion 对应的上传文件名，用于归档进历史/聊天时展示（跨多次上传仍准确） */
  const lastIngestionFileNameRef = useRef<string | null>(null);
  const lastIngestionFileNameByIngestionRef = useRef<StringByIngestion>({});
  const pollTimerRef = useRef<number | null>(null);
  const multiPollTimerRef = useRef<number | null>(null);
  const pollingInFlightRef = useRef<BooleanByIngestion>({});
  const assistantSessionIdRef = useRef<string | null>(null);
  const ingestionIdRef = useRef<string | null>(null);
  const ingestionsByIdRef = useRef<IngestionById>({});
  const clientDraftStateRef = useRef<ClientDraftStateByIngestion>({});
  const poll404WarnedRef = useRef(false);
  const poll404WarnedByIngestionRef = useRef<BooleanByIngestion>({});
  /** 避免子元素触发 dragleave 导致「拖拽高亮」闪烁 */
  const dragDepthRef = useRef(0);
  const chatPanelRef = useRef<HTMLDivElement | null>(null);
  const chatEndRef = useRef<HTMLDivElement | null>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const pendingReprocessUploadsRef = useRef<Record<string, PendingReprocessUpload>>({});
  const workspaceWindowsRef = useRef<Record<WorkspaceMode, WorkspaceWindowState | null>>({
    pdf_to_erp: null,
    assistant: null,
  });

  useEffect(() => {
    let cancelled = false;
    void getCurrentUser()
      .then((erpUser) => {
        if (cancelled) return;
        if (erpUser.userId) setUserId(erpUser.userId);
        if (erpUser.userName) setUserName(erpUser.userName);
        if (erpUser.orgId) setOrgId(erpUser.orgId);
        clientLogger.info("已从后端同步 ERP 用户信息", erpUser);
      })
      .catch((error) => {
        if (cancelled) return;
        clientLogger.warn("后端 ERP 用户信息同步失败，使用默认用户信息", { error });
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const [logOpen, setLogOpen] = useState(false);

  const uploadAcceptAttr = useMemo(() => {
    return Array.from(SUPPORTED_UPLOAD_EXTENSIONS)
      .sort()
      .map((e) => `.${e}`)
      .join(",");
  }, []);

  const devInternalEnabled = useMemo(() => {
    return process.env.NEXT_PUBLIC_ENABLE_DEV_INTERNAL === "1";
  }, []);

  useEffect(() => {
    resolveFieldsByIngestionRef.current = resolveFieldsByIngestion;
  }, [resolveFieldsByIngestion]);

  const upsertIngestionState = useCallback(
    (raw: IngestionResponse, opts?: { activate?: boolean; fileName?: string | null; poll?: boolean }) => {
      const data = applyClientDraftState(raw, clientDraftStateRef.current);
      const id = data.ingestion_id;
      const activate = opts?.activate ?? true;
      ingestionsByIdRef.current = { ...ingestionsByIdRef.current, [id]: data };
      setIngestionsById((prev) => ({ ...prev, [id]: data }));

      const displayStatus = displayIngestionStatus(data, clientDraftStateRef.current) ?? data.status;
      lastStatusByIngestionRef.current = { ...lastStatusByIngestionRef.current, [id]: displayStatus };
      const fileName = opts?.fileName ?? data.file?.source_file_name ?? data.source_file_name ?? null;
      if (fileName) {
        lastIngestionFileNameByIngestionRef.current = {
          ...lastIngestionFileNameByIngestionRef.current,
          [id]: fileName,
        };
      }

      const resolved = resolvedFieldsFromIngestion(data);
      if (Object.keys(resolved).length > 0) {
        setResolveFieldsByIngestion((prev) => ({
          ...prev,
          [id]: {
            ...(prev[id] ?? {}),
            ...resolved,
          },
        }));
      }

      if (!previewDirtyByIngestionRef.current[id]) {
        const nextPreview = data.preview_data ? syncPreviewDefaults(data.preview_data, userName, orgId) : null;
        setPreviewDraftsByIngestion((prev) => {
          if (prev[id] === nextPreview) return prev;
          return { ...prev, [id]: nextPreview };
        });
        if (activate) setPreviewDraft(nextPreview);
      }

      if (opts?.poll ?? isBackgroundRunningStatus(displayStatus)) {
        setPollingIngestionIds((prev) => (prev[id] ? prev : { ...prev, [id]: true }));
      } else if (!isBackgroundRunningStatus(displayStatus)) {
        setPollingIngestionIds((prev) => withoutRecordKey(prev, id) as BooleanByIngestion);
      }

      if (activate) {
        setIngestion(data);
        setIngestionId(id);
        ingestionIdRef.current = id;
        lastStatusRef.current = displayStatus;
        lastIngestionFileNameRef.current = fileName;
        const activeResolved = Object.keys(resolved).length > 0 ? resolved : resolveFieldsByIngestionRef.current[id] ?? {};
        setResolveFields(activeResolved);
      }

      return data;
    },
    [orgId, userName],
  );

  const resetCurrentTaskState = useCallback(() => {
    setIngestion(null);
    setIngestionId(null);
    setIngestionsById({});
    ingestionsByIdRef.current = {};
    setPollingIngestionIds({});
    pollingInFlightRef.current = {};
    ingestionIdRef.current = null;
    lastStatusRef.current = null;
    lastStatusByIngestionRef.current = {};
    workflowToolCardKeyRef.current = null;
    workflowToolCardKeyByIngestionRef.current = {};
    lastIngestionFileNameRef.current = null;
    lastIngestionFileNameByIngestionRef.current = {};
    setResolveFields({});
    setResolveFieldsByIngestion({});
    setPreviewDraft(null);
    setPreviewDraftsByIngestion({});
    setConfirmedPreviewIds({});
    setPreviewDirtyByIngestion({});
    setResolvingIngestionIds({});
    setConfirmingPreviewIngestionIds({});
    setCreatingDraftIngestionIds({});
    previewDirtyRef.current = false;
    previewDirtyByIngestionRef.current = {};
    previewIngestionIdRef.current = null;
    poll404WarnedRef.current = false;
    poll404WarnedByIngestionRef.current = {};
    if (pollTimerRef.current) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (multiPollTimerRef.current) {
      window.clearTimeout(multiPollTimerRef.current);
      multiPollTimerRef.current = null;
    }
  }, []);

  const restoreActiveIngestion = useCallback(
    async (
      sid: string,
      activeIngestionId: string | null | undefined,
      restoredMessages: ChatMessage[],
      isCancelled: () => boolean = () => false,
    ) => {
      if (isCancelled()) return;
      if (!activeIngestionId) {
        resetCurrentTaskState();
        return;
      }
      setIngestionId(activeIngestionId);
      ingestionIdRef.current = activeIngestionId;
      poll404WarnedRef.current = false;
      previewDirtyRef.current = false;
      previewDirtyByIngestionRef.current = withoutRecordKey(previewDirtyByIngestionRef.current, activeIngestionId) as BooleanByIngestion;
      previewIngestionIdRef.current = null;
      setPreviewDraft(null);
      try {
        const data = await getIngestion(activeIngestionId);
        if (isCancelled() || assistantSessionIdRef.current !== sid || data.ingestion_id !== ingestionIdRef.current) return;
        const displayData = upsertIngestionState(data, { activate: true });
        const displayStatus = displayIngestionStatus(displayData, clientDraftStateRef.current) ?? data.status;
        lastIngestionFileNameRef.current =
          displayData.file?.source_file_name ?? displayData.source_file_name ?? lastIngestionFileNameRef.current;
        previewDirtyRef.current = false;
        previewIngestionIdRef.current = displayData.ingestion_id;
        workflowToolCardKeyByIngestionRef.current = {
          ...workflowToolCardKeyByIngestionRef.current,
          [displayData.ingestion_id]: hasWorkflowCardForIngestion(restoredMessages, displayData)
            ? pdfToErpWorkflowCardKey(displayData, displayStatus)
            : null,
        };
        workflowToolCardKeyRef.current = hasWorkflowCardForIngestion(restoredMessages, displayData)
          ? pdfToErpWorkflowCardKey(displayData, displayStatus)
          : null;
      } catch (e) {
        if (isCancelled() || assistantSessionIdRef.current !== sid) return;
        clientLogger.warn("恢复助手会话任务失败", e);
        resetCurrentTaskState();
      }
    },
    [resetCurrentTaskState, upsertIngestionState],
  );

  const activateAssistantSession = useCallback(
    (sid: string) => {
      assistantSessionIdRef.current = sid;
      setAssistantSessionId(sid);
      try {
        localStorage.setItem(ASSISTANT_SESSION_LS_KEY, sid);
      } catch {
        /* quota / private mode */
      }
      resetCurrentTaskState();
      setChatMessages([]);

      let cancelled = false;
      void getAssistantSession(sid)
        .then((session) => {
          if (cancelled || assistantSessionIdRef.current !== sid) return;
          const restoredMessages = session.messages.map((m, index) => ({
            id: `restored-${index}-${session.session_id}`,
            role: m.role,
            content: m.content,
            createdAt: "",
            toolUi: m.ui ?? null,
          }));
          setChatMessages(restoredMessages);
          void restoreActiveIngestion(sid, session.active_task?.ingestion_id ?? null, restoredMessages, () => cancelled);
        })
        .catch(() => {
          if (!cancelled && assistantSessionIdRef.current === sid) setChatMessages([]);
        });
      return () => {
        cancelled = true;
      };
    },
    [resetCurrentTaskState, restoreActiveIngestion],
  );

  useEffect(() => {
    let cancelled = false;
    void getHealth()
      .then((data) => {
        if (!cancelled) setHealthInfo(data);
      })
      .catch((e) => clientLogger.warn("读取健康状态失败", e));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    let sid: string | null = null;
    let metas: ChatSessionMeta[] = [];
    try {
      metas = parseChatSessionMetas(localStorage.getItem(ASSISTANT_SESSIONS_LS_KEY));
      sid = localStorage.getItem(ASSISTANT_SESSION_LS_KEY) || metas[0]?.id || null;
      if (!sid) {
        sid = createAssistantSessionId();
      }
    } catch {
      sid = createAssistantSessionId();
    }
    if (!metas.some((session) => session.id === sid)) {
      metas = [createSessionMeta(sid), ...metas].slice(0, 50);
    }
    chatSessionsHydratedRef.current = true;
    setChatSessions(metas);
    try {
      localStorage.setItem(ASSISTANT_SESSION_LS_KEY, sid);
      localStorage.setItem(ASSISTANT_SESSIONS_LS_KEY, JSON.stringify(metas));
    } catch {
      /* quota / private mode */
    }
    assistantSessionIdRef.current = sid;
    setAssistantSessionId(sid);

    void getAssistantSession(sid)
      .then((session) => {
        if (cancelled) return;
        const restoredMessages = session.messages.map((m, index) => ({
          id: `restored-${index}-${session.session_id}`,
          role: m.role,
          content: m.content,
          createdAt: "",
          toolUi: m.ui ?? null,
        }));
        setChatMessages(restoredMessages);
        void restoreActiveIngestion(sid, session.active_task?.ingestion_id ?? null, restoredMessages, () => cancelled);
      })
      .catch(() => {
        /* A brand-new local session has no server history yet. */
      });

    return () => {
      cancelled = true;
    };
  }, [restoreActiveIngestion]);

  /** 与 ``GET /ingestions/{id}/document`` 一致，便于复制到 Postman / 集成脚本 */
  const documentJsonUrls = useMemo(() => {
    if (!ingestionId) return null;
    const base = getApiBaseUrl();
    const path = `/ingestions/${encodeURIComponent(ingestionId)}/document`;
    const standard = `${base}${path}`;
    return {
      standard,
      fullText: `${standard}?include_full_text=true`,
      erpPayload: `${base}/ingestions/${encodeURIComponent(ingestionId)}/erp-payload`,
      curlStandard: `curl -sS "${standard}"`,
    };
  }, [ingestionId]);

  useEffect(() => {
    ingestionIdRef.current = ingestionId;
    poll404WarnedRef.current = false;
    workflowToolCardKeyRef.current = null;
  }, [ingestionId]);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(INGESTION_HISTORY_SS_KEY);
      const arr = JSON.parse(raw ?? "[]") as unknown;
      if (Array.isArray(arr)) {
        setIngestionHistory(
          arr
            .filter(
              (x): x is IngestionHistoryItem =>
                Boolean(x) && typeof (x as IngestionHistoryItem).id === "string",
            )
            .slice(0, 30),
        );
      }
    } catch {
      /* ignore invalid session cache */
    } finally {
      historyHydratedRef.current = true;
    }
  }, []);

  useEffect(() => {
    if (!historyHydratedRef.current) return;
    try {
      sessionStorage.setItem(INGESTION_HISTORY_SS_KEY, JSON.stringify(ingestionHistory));
    } catch {
      /* quota / private mode */
    }
  }, [ingestionHistory]);

  useEffect(() => {
    if (!chatSessionsHydratedRef.current) return;
    try {
      const stored = parseChatSessionMetas(localStorage.getItem(ASSISTANT_SESSIONS_LS_KEY));
      const merged = mergeChatSessionMetas(chatSessions, stored);
      localStorage.setItem(ASSISTANT_SESSIONS_LS_KEY, JSON.stringify(merged));
      if (merged.length !== chatSessions.length) {
        setChatSessions(merged);
      }
    } catch {
      /* quota / private mode */
    }
  }, [chatSessions]);

  useEffect(() => {
    if (!assistantSessionId) return;
    if (chatSessionMetaTimerRef.current) window.clearTimeout(chatSessionMetaTimerRef.current);
    chatSessionMetaTimerRef.current = window.setTimeout(() => {
      const visibleMessages = chatMessages;
      const firstUser = visibleMessages.find((m) => m.role === "user");
      const last = [...visibleMessages].reverse().find((m) => m.content.trim());
      const taskStatus = displayIngestionStatus(ingestion, clientDraftStateRef.current) ?? ingestion?.status ?? null;
      setChatSessions((prev) => {
        const now = new Date().toISOString();
        const existing = prev.find((session) => session.id === assistantSessionId);
        const nextMeta: ChatSessionMeta = {
          id: assistantSessionId,
          title: existing?.titleEdited
            ? existing.title
            : compactSessionText(firstUser?.content ?? existing?.title ?? "", "新聊天"),
          lastMessage: compactSessionText(last?.content ?? existing?.lastMessage ?? "", "还没有消息"),
          updatedAt: last?.createdAt || existing?.updatedAt || now,
          taskType: ingestion ? "pdf_to_erp" : existing?.taskType ?? null,
          taskStatus,
          taskIngestionId: ingestion?.ingestion_id ?? existing?.taskIngestionId ?? null,
          titleEdited: existing?.titleEdited ?? false,
        };
        if (
          existing &&
          existing.title === nextMeta.title &&
          existing.lastMessage === nextMeta.lastMessage &&
          existing.updatedAt === nextMeta.updatedAt &&
          existing.taskType === nextMeta.taskType &&
          existing.taskStatus === nextMeta.taskStatus &&
          existing.taskIngestionId === nextMeta.taskIngestionId &&
          existing.titleEdited === nextMeta.titleEdited
        ) {
          return prev;
        }
        if (!existing) return [nextMeta, ...prev].slice(0, 50);
        return prev.map((session) => (session.id === assistantSessionId ? nextMeta : session));
      });
      chatSessionMetaTimerRef.current = null;
    }, 300);
    return () => {
      if (chatSessionMetaTimerRef.current) {
        window.clearTimeout(chatSessionMetaTimerRef.current);
        chatSessionMetaTimerRef.current = null;
      }
    };
  }, [assistantSessionId, chatMessages, ingestion]);

  /** 工作流写回的已解析字段同步到补全表单 */
  useEffect(() => {
    if (!ingestion?.resolved_fields) return;
    const rf = ingestion.resolved_fields as Record<string, string | undefined>;
    const keys = resolveFieldKeys(ingestion.doc_type_hint, ingestion);
    setResolveFields((prev) => {
      const next = { ...prev };
      for (const k of keys) {
        const v = rf[k];
        if (v != null && String(v).trim() !== "") next[k] = String(v);
      }
      for (const k of OPTIONAL_RESOLVE_KEYS) {
        const v = rf[k];
        if (v != null && String(v).trim() !== "") next[k] = String(v);
      }
      return next;
    });
    // 不把整个 ingestion 放入依赖，避免轮询引用抖动导致无意义 setState。
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 仅随 ingestion_id / doc_type_hint / resolved_fields 同步表单
  }, [ingestion?.ingestion_id, ingestion?.doc_type_hint, ingestion?.resolved_fields]);

  useEffect(() => {
    const nextIngestionId = ingestion?.ingestion_id ?? null;
    if (previewIngestionIdRef.current !== nextIngestionId) {
      previewIngestionIdRef.current = nextIngestionId;
      previewDirtyRef.current = false;
      setPreviewDraft(ingestion?.preview_data ? syncPreviewDefaults(ingestion.preview_data, userName, orgId) : null);
      return;
    }
    if (!previewDirtyRef.current) {
      setPreviewDraft(ingestion?.preview_data ? syncPreviewDefaults(ingestion.preview_data, userName, orgId) : null);
    }
  }, [ingestion?.ingestion_id, ingestion?.preview_data, orgId, userName]);

  const appendChat = useCallback((role: ChatRole, content: string, extra?: Partial<ChatMessage>) => {
    setChatMessages((prev) => {
      const nextMessage: ChatMessage = {
        id: `${Date.now()}-${Math.random()}`,
        role,
        content,
        createdAt: new Date().toISOString(),
        ...extra,
      };
      const nextKey = chatMessageDedupeKey(nextMessage);
      if (nextMessage.toolUi) {
        if (prev.some((message) => chatMessageDedupeKey(message) === nextKey)) {
          return prev;
        }
      } else {
        const last = prev[prev.length - 1];
        if (last && chatMessageDedupeKey(last) === nextKey) {
          return prev;
        }
      }
      return [...prev, nextMessage];
    });
  }, []);

  const getAssistantSessionId = useCallback(() => {
    let sid = assistantSessionIdRef.current ?? assistantSessionId;
    if (!sid) {
      sid = createAssistantSessionId();
      assistantSessionIdRef.current = sid;
      setAssistantSessionId(sid);
      try {
        localStorage.setItem(ASSISTANT_SESSION_LS_KEY, sid);
      } catch {
        /* quota / private mode */
      }
    }
    return sid;
  }, [assistantSessionId]);

  const onNewChatSession = useCallback(() => {
    const sid = createAssistantSessionId();
    const meta = createSessionMeta(sid);
    setChatSessions((prev) => [meta, ...prev.filter((session) => session.id !== sid)].slice(0, 50));
    activateAssistantSession(sid);
  }, [activateAssistantSession]);

  const captureWorkspaceWindow = useCallback((): WorkspaceWindowState => {
    return {
      chatInput,
      chatMessages,
      assistantSessionId: assistantSessionIdRef.current ?? assistantSessionId,
      ingestion,
      ingestionId,
      ingestionsById,
      pollingIngestionIds,
      ingestionHistory,
      resolveFields,
      resolveFieldsByIngestion,
      previewDraft,
      previewDraftsByIngestion,
      confirmedPreviewIds,
      previewDirtyByIngestion,
      lastStatus: lastStatusRef.current,
      lastStatusByIngestion: lastStatusByIngestionRef.current,
      workflowToolCardKey: workflowToolCardKeyRef.current,
      workflowToolCardKeyByIngestion: workflowToolCardKeyByIngestionRef.current,
      lastIngestionFileName: lastIngestionFileNameRef.current,
      lastIngestionFileNameByIngestion: lastIngestionFileNameByIngestionRef.current,
      previewDirty: previewDirtyRef.current,
      previewIngestionId: previewIngestionIdRef.current,
      poll404Warned: poll404WarnedRef.current,
      poll404WarnedByIngestion: poll404WarnedByIngestionRef.current,
    };
  }, [
    assistantSessionId,
    chatInput,
    chatMessages,
    confirmedPreviewIds,
    ingestion,
    ingestionHistory,
    ingestionId,
    ingestionsById,
    pollingIngestionIds,
    previewDirtyByIngestion,
    previewDraft,
    previewDraftsByIngestion,
    resolveFields,
    resolveFieldsByIngestion,
  ]);

  const applyWorkspaceWindow = useCallback((state: WorkspaceWindowState | null, mode: WorkspaceMode) => {
    const nextState =
      state ??
      ({
        chatInput: "",
        chatMessages: [],
        assistantSessionId: createAssistantSessionId(),
        ingestion: null,
        ingestionId: null,
        ingestionsById: {},
        pollingIngestionIds: {},
        ingestionHistory: [],
        resolveFields: {},
        resolveFieldsByIngestion: {},
        previewDraft: null,
        previewDraftsByIngestion: {},
        confirmedPreviewIds: {},
        previewDirtyByIngestion: {},
        lastStatus: null,
        lastStatusByIngestion: {},
        workflowToolCardKey: null,
        workflowToolCardKeyByIngestion: {},
        lastIngestionFileName: null,
        lastIngestionFileNameByIngestion: {},
        previewDirty: false,
        previewIngestionId: null,
        poll404Warned: false,
        poll404WarnedByIngestion: {},
      } satisfies WorkspaceWindowState);

    setWorkspaceMode(mode);
    setChatInput(nextState.chatInput);
    setChatMessages(nextState.chatMessages);
    assistantSessionIdRef.current = nextState.assistantSessionId;
    setAssistantSessionId(nextState.assistantSessionId);
    setIngestion(nextState.ingestion);
    setIngestionId(nextState.ingestionId);
    setIngestionsById(nextState.ingestionsById);
    ingestionsByIdRef.current = nextState.ingestionsById;
    setPollingIngestionIds(nextState.pollingIngestionIds);
    ingestionIdRef.current = nextState.ingestionId;
    setIngestionHistory(nextState.ingestionHistory);
    setResolveFields(nextState.resolveFields);
    setResolveFieldsByIngestion(nextState.resolveFieldsByIngestion);
    setPreviewDraft(nextState.previewDraft);
    setPreviewDraftsByIngestion(nextState.previewDraftsByIngestion);
    setConfirmedPreviewIds(nextState.confirmedPreviewIds);
    setPreviewDirtyByIngestion(nextState.previewDirtyByIngestion);
    lastStatusRef.current = nextState.lastStatus;
    lastStatusByIngestionRef.current = nextState.lastStatusByIngestion;
    workflowToolCardKeyRef.current = nextState.workflowToolCardKey;
    workflowToolCardKeyByIngestionRef.current = nextState.workflowToolCardKeyByIngestion;
    lastIngestionFileNameRef.current = nextState.lastIngestionFileName;
    lastIngestionFileNameByIngestionRef.current = nextState.lastIngestionFileNameByIngestion;
    previewDirtyRef.current = nextState.previewDirty;
    previewDirtyByIngestionRef.current = nextState.previewDirtyByIngestion;
    previewIngestionIdRef.current = nextState.previewIngestionId;
    poll404WarnedRef.current = nextState.poll404Warned;
    poll404WarnedByIngestionRef.current = nextState.poll404WarnedByIngestion;
    shouldAutoScrollRef.current = true;
    if (pollTimerRef.current) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    if (multiPollTimerRef.current) {
      window.clearTimeout(multiPollTimerRef.current);
      multiPollTimerRef.current = null;
    }
    if (nextState.ingestionId) setIngestionPollNonce((n) => n + 1);
  }, []);

  const onSwitchWorkspaceMode = useCallback(
    (nextMode: WorkspaceMode) => {
      if (nextMode === workspaceMode) return;
      workspaceWindowsRef.current[workspaceMode] = captureWorkspaceWindow();
      applyWorkspaceWindow(workspaceWindowsRef.current[nextMode], nextMode);
    },
    [applyWorkspaceWindow, captureWorkspaceWindow, workspaceMode],
  );

  const onClearCurrentPage = useCallback(async () => {
    const currentIngestionId = ingestionIdRef.current;
    const currentStatus =
      displayIngestionStatus(ingestion, clientDraftStateRef.current) ?? ingestion?.status ?? lastStatusRef.current;
    if (workspaceMode === "pdf_to_erp") {
      for (const item of Object.values(ingestionsByIdRef.current)) {
        if (item.ingestion_id === currentIngestionId) continue;
        const status = displayIngestionStatus(item, clientDraftStateRef.current) ?? item.status;
        if (!shouldCancelTaskOnSessionDelete(status)) continue;
        try {
          await postCancelIngestion(item.ingestion_id);
        } catch (e) {
          clientLogger.warn("清除页面时取消 PDF 任务失败", { ingestionId: item.ingestion_id, error: e });
        }
      }
    }
    if (workspaceMode === "pdf_to_erp" && currentIngestionId && shouldCancelTaskOnSessionDelete(currentStatus)) {
      try {
        await postCancelIngestion(currentIngestionId);
      } catch (e) {
        clientLogger.warn("清除页面时取消当前 PDF 任务失败", e);
      }
    }
    pendingReprocessUploadsRef.current = {};
    setChatInput("");
    setChatMessages([]);
    resetCurrentTaskState();
    const sid = createAssistantSessionId();
    const meta = createSessionMeta(sid);
    assistantSessionIdRef.current = sid;
    setAssistantSessionId(sid);
    setChatSessions([meta]);
    try {
      localStorage.setItem(ASSISTANT_SESSION_LS_KEY, sid);
      localStorage.setItem(ASSISTANT_SESSIONS_LS_KEY, JSON.stringify([meta]));
    } catch {
      /* quota / private mode */
    }
    workspaceWindowsRef.current[workspaceMode] = {
      chatInput: "",
      chatMessages: [],
      assistantSessionId: sid,
      ingestion: null,
      ingestionId: null,
      ingestionsById: {},
      pollingIngestionIds: {},
      ingestionHistory: [],
      resolveFields: {},
      resolveFieldsByIngestion: {},
      previewDraft: null,
      previewDraftsByIngestion: {},
      confirmedPreviewIds: {},
      previewDirtyByIngestion: {},
      lastStatus: null,
      lastStatusByIngestion: {},
      workflowToolCardKey: null,
      workflowToolCardKeyByIngestion: {},
      lastIngestionFileName: null,
      lastIngestionFileNameByIngestion: {},
      previewDirty: false,
      previewIngestionId: null,
      poll404Warned: false,
      poll404WarnedByIngestion: {},
    };
  }, [ingestion, resetCurrentTaskState, workspaceMode]);

  const onSelectChatSession = useCallback(
    (sid: string) => {
      if (!sid || sid === assistantSessionIdRef.current) return;
      activateAssistantSession(sid);
    },
    [activateAssistantSession],
  );

  const onDeleteChatSession = useCallback(
    async (sid: string) => {
      const session = chatSessions.find((item) => item.id === sid);
      const relatedIngestionId =
        session?.taskIngestionId ?? (sid === assistantSessionIdRef.current ? ingestionIdRef.current : null);
      const relatedStatus =
        session?.taskStatus ??
        (sid === assistantSessionIdRef.current
          ? displayIngestionStatus(ingestion, clientDraftStateRef.current) ?? ingestion?.status ?? null
          : null);
      if (relatedIngestionId && shouldCancelTaskOnSessionDelete(relatedStatus)) {
        const ok = window.confirm("这个对话还有未完成或未上传 ERP 的 PDF 任务。删除对话时是否同时取消该任务？");
        if (ok) {
          try {
            await postCancelIngestion(relatedIngestionId);
            if (relatedIngestionId === ingestionIdRef.current) {
              lastStatusRef.current = "CANCELED";
              if (pollTimerRef.current) {
                window.clearInterval(pollTimerRef.current);
                pollTimerRef.current = null;
              }
            }
          } catch (e) {
            clientLogger.warn("取消关联 PDF 任务失败，仍会删除本地历史对话", e);
          }
        }
      }
      const remaining = chatSessions.filter((session) => session.id !== sid);
      setChatSessions(() => {
        try {
          localStorage.setItem(ASSISTANT_SESSIONS_LS_KEY, JSON.stringify(remaining));
        } catch {
          /* quota / private mode */
        }
        return remaining;
      });
      if (sid !== assistantSessionIdRef.current) return;
      const target = remaining[0]?.id || createAssistantSessionId();
      if (!remaining[0]) {
        const meta = createSessionMeta(target);
        setChatSessions([meta]);
      }
      activateAssistantSession(target);
    },
    [activateAssistantSession, chatSessions, ingestion],
  );

  const onStartRenameSession = useCallback((session: ChatSessionMeta) => {
    setRenamingSessionId(session.id);
    setRenameDraft(session.title);
  }, []);

  const onPinChatSession = useCallback((sid: string) => {
    setChatSessions((prev) => {
      const target = prev.find((session) => session.id === sid);
      if (!target || prev[0]?.id === sid) return prev;
      return [target, ...prev.filter((session) => session.id !== sid)].slice(0, 50);
    });
  }, []);

  const onCommitRenameSession = useCallback(() => {
    const sid = renamingSessionId;
    if (!sid) return;
    const title = renameDraft.trim();
    if (!title) {
      setRenamingSessionId(null);
      setRenameDraft("");
      return;
    }
    setChatSessions((prev) =>
      prev.map((session) =>
        session.id === sid
          ? {
              ...session,
              title: compactSessionText(title, "新聊天"),
              titleEdited: true,
              updatedAt: new Date().toISOString(),
            }
          : session,
      ),
    );
    setRenamingSessionId(null);
    setRenameDraft("");
  }, [renameDraft, renamingSessionId]);

  const onCancelRenameSession = useCallback(() => {
    setRenamingSessionId(null);
    setRenameDraft("");
  }, []);

  const appendToolResponse = useCallback(
    (res: Awaited<ReturnType<typeof postAssistantMessage>>, opts?: { skipMessages?: boolean }) => {
      if (res.session_id) {
        assistantSessionIdRef.current = res.session_id;
        setAssistantSessionId(res.session_id);
        try {
          localStorage.setItem(ASSISTANT_SESSION_LS_KEY, res.session_id);
        } catch {
          /* quota / private mode */
        }
      }
      const ui = res.ui ?? res.tool_result?.ui ?? null;
      const nextIngestion = res.tool_result?.ingestion;
      let handledByExistingCard = false;
      if (!opts?.skipMessages && ui && nextIngestion && ui.type !== "processing") {
        setChatMessages((prev) => {
          const next = updateWorkflowToolCard(prev, nextIngestion, ui);
          handledByExistingCard = next !== prev;
          return next;
        });
      }
      if (!opts?.skipMessages) {
        for (const msg of res.messages ?? []) {
          if (handledByExistingCard && (msg.ui ?? ui)?.type === ui?.type) continue;
          appendChat(msg.role, msg.content, { toolUi: msg.ui ?? ui });
        }
      }
      if (nextIngestion) {
        const displayData = upsertIngestionState(nextIngestion, { activate: true });
        if (!opts?.skipMessages && ui && ui.type !== "processing") {
          const displayStatus = displayIngestionStatus(displayData, clientDraftStateRef.current) ?? displayData.status;
          const cardKey = pdfToErpWorkflowCardKey(displayData, displayStatus);
          workflowToolCardKeyRef.current = cardKey;
          workflowToolCardKeyByIngestionRef.current = {
            ...workflowToolCardKeyByIngestionRef.current,
            [displayData.ingestion_id]: cardKey,
          };
        }
      }
      const draft = res.tool_result?.draft;
      if (draft) {
        clientDraftStateRef.current[draft.ingestion_id] = {
          draft_no: draft.draft_no,
          draft_url: draft.draft_url,
        };
        const base = ingestionsByIdRef.current[draft.ingestion_id];
        if (base) {
          const merged = mergeDraftCreatedState(base, draft);
          upsertIngestionState(merged, { activate: ingestionIdRef.current === draft.ingestion_id, poll: false });
        }
        setPollingIngestionIds((prev) => withoutRecordKey(prev, draft.ingestion_id) as BooleanByIngestion);
        lastStatusByIngestionRef.current = {
          ...lastStatusByIngestionRef.current,
          [draft.ingestion_id]: "DRAFT_CREATED",
        };
        if (ingestionIdRef.current === draft.ingestion_id) lastStatusRef.current = "DRAFT_CREATED";
      }
    },
    [appendChat, upsertIngestionState],
  );

  const [copiedTag, setCopiedTag] = useState<string | null>(null);
  const copyHintTimerRef = useRef<number | null>(null);

  const copyToClipboard = useCallback(
    (tag: string, text: string) => {
      void navigator.clipboard.writeText(text).then(
        () => {
          if (copyHintTimerRef.current) window.clearTimeout(copyHintTimerRef.current);
          setCopiedTag(tag);
          copyHintTimerRef.current = window.setTimeout(() => {
            setCopiedTag(null);
            copyHintTimerRef.current = null;
          }, 2000);
          clientLogger.info("已复制到剪贴板", { tag, len: text.length });
        },
        () => {
          clientLogger.error("复制失败", { tag });
          appendChat("system", "复制失败：请确认页面为 HTTPS 或 localhost，并已授权剪贴板。");
        },
      );
    },
    [appendChat],
  );

  useEffect(() => {
    return () => {
      if (copyHintTimerRef.current) window.clearTimeout(copyHintTimerRef.current);
    };
  }, []);

  useEffect(() => {
    const el = chatScrollRef.current;
    if (!el) return;
    const pauseAutoFollow = () => {
      shouldAutoScrollRef.current = false;
    };
    el.addEventListener("wheel", pauseAutoFollow, { passive: true });
    el.addEventListener("touchstart", pauseAutoFollow, { passive: true });
    return () => {
      el.removeEventListener("wheel", pauseAutoFollow);
      el.removeEventListener("touchstart", pauseAutoFollow);
    };
  }, [chatScrollRef.current]);

  useEffect(() => {
    if (!shouldAutoScrollRef.current) return;
    chatEndRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [chatMessages.length]);

  /** 轮询 ingestion：在终态前持续拉取，便于展示 worker 异步推进效果 */
  useEffect(() => {
    if (!ingestionId || pollingIngestionIds[ingestionId]) return;
    let cancelled = false;
    let failureCount = 0;

    const clearPollTimer = () => {
      if (pollTimerRef.current) {
        window.clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };

    const scheduleNextPoll = (status: IngestionStatus | null | undefined, delayFailureCount = 0) => {
      clearPollTimer();
      if (cancelled) return;
      const delay = nextIngestionPollDelay(status, delayFailureCount);
      if (delay == null) return;
      pollTimerRef.current = window.setTimeout(() => void tick(), delay);
    };

    const tick = async () => {
      try {
        const data = await getIngestion(ingestionId);
        if (data.ingestion_id !== ingestionIdRef.current) {
          clientLogger.warn("ignore stale ingestion poll response", {
            responseIngestionId: data.ingestion_id,
            currentIngestionId: ingestionIdRef.current,
          });
          return;
        }
        failureCount = 0;
        const displayData = applyClientDraftState(data, clientDraftStateRef.current);
        setIngestion(displayData);
        const polledName = data.file?.source_file_name;
        if (polledName) lastIngestionFileNameRef.current = polledName;

        const displayStatus = displayIngestionStatus(displayData, clientDraftStateRef.current) ?? data.status;
        setChatMessages((prev) => updatePdfToErpProgressCards(prev, displayData, displayStatus));
        if (lastStatusRef.current !== displayStatus) {
          clientLogger.info("ingestion status changed", {
            from: lastStatusRef.current,
            to: displayStatus,
            ingestionId,
          });
          lastStatusRef.current = displayStatus;
        }

        const workflowToolUi = isBackgroundRunningStatus(displayStatus) ? null : buildPdfToErpToolUi(displayData);
        if (workflowToolUi) {
          const cardKey = pdfToErpWorkflowCardKey(displayData, displayStatus);
          if (workflowToolCardKeyRef.current !== cardKey) {
            workflowToolCardKeyRef.current = cardKey;
            let updatedExistingCard = false;
            setChatMessages((prev) => {
              const next = updateWorkflowToolCard(prev, displayData, workflowToolUi);
              updatedExistingCard = next !== prev;
              return next;
            });
            if (!updatedExistingCard) {
              appendChat("assistant", pdfToErpWorkflowCardText(displayData, displayStatus), {
                toolUi: workflowToolUi,
              });
            }
          }
        }

        if (nextIngestionPollDelay(displayStatus) == null) {
          clearPollTimer();
          clientLogger.info("stop ingestion polling", { status: displayStatus, ingestionId });
          return;
        }
        scheduleNextPoll(displayStatus);
      } catch (e) {
        failureCount += 1;
        clientLogger.error("poll ingestion failed", e);
        const st =
          typeof e === "object" && e !== null && "status" in e
            ? (e as { status?: number }).status
            : undefined;
        if (st === 404 && !poll404WarnedRef.current) {
          poll404WarnedRef.current = true;
          appendChat(
            "system",
            "找不到这条解析任务（服务可能刚重启过）。请重新上传文件；若经常发生，请联系管理员配置任务持久化。",
          );
        }
        scheduleNextPoll(lastStatusRef.current, failureCount);
      }
    };

    void tick();

    return () => {
      cancelled = true;
      clearPollTimer();
    };
  }, [ingestionId, ingestionPollNonce, appendChat, pollingIngestionIds]);

  /** Poll every running PDF task so each order card can remain editable and actionable. */
  useEffect(() => {
    const ids = Object.keys(pollingIngestionIds);
    if (ids.length === 0) return;
    let cancelled = false;

    const clearTimer = () => {
      if (multiPollTimerRef.current) {
        window.clearTimeout(multiPollTimerRef.current);
        multiPollTimerRef.current = null;
      }
    };

    const tickOne = async (id: string) => {
      if (pollingInFlightRef.current[id]) return;
      pollingInFlightRef.current = { ...pollingInFlightRef.current, [id]: true };
      try {
        const data = await getIngestion(id);
        if (cancelled) return;
        const activate = ingestionIdRef.current === id;
        const displayData = upsertIngestionState(data, { activate });
        const polledName = data.file?.source_file_name ?? data.source_file_name ?? null;
        if (polledName) {
          lastIngestionFileNameByIngestionRef.current = {
            ...lastIngestionFileNameByIngestionRef.current,
            [id]: polledName,
          };
          if (activate) lastIngestionFileNameRef.current = polledName;
        }

        const displayStatus = displayIngestionStatus(displayData, clientDraftStateRef.current) ?? data.status;
        setChatMessages((prev) => updatePdfToErpProgressCards(prev, displayData, displayStatus));
        const previousStatus = lastStatusByIngestionRef.current[id] ?? null;
        if (previousStatus !== displayStatus) {
          clientLogger.info("ingestion status changed", {
            from: previousStatus,
            to: displayStatus,
            ingestionId: id,
          });
          lastStatusByIngestionRef.current = {
            ...lastStatusByIngestionRef.current,
            [id]: displayStatus,
          };
          if (activate) lastStatusRef.current = displayStatus;
        }

        const workflowToolUi = isBackgroundRunningStatus(displayStatus) ? null : buildPdfToErpToolUi(displayData);
        if (workflowToolUi) {
          const cardKey = pdfToErpWorkflowCardKey(displayData, displayStatus);
          if (workflowToolCardKeyByIngestionRef.current[id] !== cardKey) {
            workflowToolCardKeyByIngestionRef.current = {
              ...workflowToolCardKeyByIngestionRef.current,
              [id]: cardKey,
            };
            if (activate) workflowToolCardKeyRef.current = cardKey;
            let updatedExistingCard = false;
            setChatMessages((prev) => {
              const next = updateWorkflowToolCard(prev, displayData, workflowToolUi);
              updatedExistingCard = next !== prev;
              return next;
            });
            if (!updatedExistingCard) {
              appendChat("assistant", pdfToErpWorkflowCardText(displayData, displayStatus), {
                toolUi: workflowToolUi,
              });
            }
          }
        }

        if (nextIngestionPollDelay(displayStatus) == null) {
          setPollingIngestionIds((prev) => withoutRecordKey(prev, id) as BooleanByIngestion);
          clientLogger.info("stop ingestion polling", { status: displayStatus, ingestionId: id });
        }
      } catch (e) {
        clientLogger.error("poll ingestion failed", e);
        const st =
          typeof e === "object" && e !== null && "status" in e
            ? (e as { status?: number }).status
            : undefined;
        if (st === 404 && !poll404WarnedByIngestionRef.current[id]) {
          poll404WarnedByIngestionRef.current = {
            ...poll404WarnedByIngestionRef.current,
            [id]: true,
          };
          if (ingestionIdRef.current === id) poll404WarnedRef.current = true;
          appendChat("system", "找不到这条解析任务。请重新上传文件；若经常发生，请联系管理员检查任务持久化配置。");
        }
      } finally {
        pollingInFlightRef.current = withoutRecordKey(pollingInFlightRef.current, id) as BooleanByIngestion;
      }
    };

    const tick = async () => {
      clearTimer();
      await Promise.all(Object.keys(pollingIngestionIds).map((id) => tickOne(id)));
      if (!cancelled && Object.keys(pollingIngestionIds).length > 0) {
        multiPollTimerRef.current = window.setTimeout(() => void tick(), POLL_FAST_MS);
      }
    };

    void tick();

    return () => {
      cancelled = true;
      clearTimer();
    };
  }, [appendChat, ingestionPollNonce, pollingIngestionIds, upsertIngestionState]);

  const chatInputPlaceholder = useMemo(() => {
    if (workspaceMode === "pdf_to_erp") return "PDF 转 ERP 模式只支持上传 PDF，不支持文字对话。";
    return "可普通对话，也可查询 ERP 库存、供应商、物料等信息。Enter 发送，Shift+Enter 换行";
  }, [workspaceMode]);

  const onSendChat = useCallback(async () => {
    const text = chatInput.trim();
    if (!text || isChatSending) return;
    if (workspaceMode === "pdf_to_erp") {
      appendChat("system", "PDF 转 ERP 模式只支持上传 PDF。请切换到「普通对话 / ERP 查询」后再发送文字。");
      return;
    }
    appendChat("user", text);
    setChatInput("");
    setIsChatSending(true);
    const streamMessageId = `stream-${Date.now()}-${Math.random()}`;
    const pendingAssistantContent = "正在思考...";
    setChatMessages((prev) => [
      ...prev,
      {
        id: streamMessageId,
        role: "assistant",
        content: pendingAssistantContent,
        createdAt: new Date().toISOString(),
      },
    ]);
    try {
      let hasStreamedText = false;
      await streamAssistantMessage({
        session_id: getAssistantSessionId(),
        message: text,
        org_id: orgId,
        user_id: userId,
        active_task_id: workspaceMode === "assistant" ? null : ingestionId,
      }, (event) => {
        if (event.event === "session") {
          const sid = event.data.session_id;
          if (sid) {
            assistantSessionIdRef.current = sid;
            setAssistantSessionId(sid);
            try {
              localStorage.setItem(ASSISTANT_SESSION_LS_KEY, sid);
            } catch {
              /* quota / private mode */
            }
          }
          return;
        }
        if (event.event === "delta") {
          const delta = event.data.content ?? "";
          if (!delta) return;
          hasStreamedText = true;
          setChatMessages((prev) => {
            const existing = prev.find((m) => m.id === streamMessageId);
            if (!existing) {
              return [
                ...prev,
                {
                  id: streamMessageId,
                  role: "assistant",
                  content: delta,
                  createdAt: new Date().toISOString(),
                },
              ];
            }
            return prev.map((m) =>
              m.id === streamMessageId
                ? { ...m, content: m.content === pendingAssistantContent ? delta : `${m.content}${delta}` }
                : m,
            );
          });
          return;
        }
        if (event.event === "error") {
          clientLogger.error("assistant stream event error", event.data);
          return;
        }
        if (event.event === "final") {
          const res = event.data;
          if (!hasStreamedText) {
            setChatMessages((prev) => prev.filter((m) => m.id !== streamMessageId));
            appendToolResponse(res);
            return;
          }
          const ui = res.ui ?? res.tool_result?.ui ?? null;
          const assistantMsg = (res.messages ?? []).find((msg) => msg.role === "assistant");
          if (assistantMsg) {
            setChatMessages((prev) =>
              prev.map((m) =>
                m.id === streamMessageId
                  ? { ...m, content: assistantMsg.content || m.content, toolUi: assistantMsg.ui ?? ui }
                  : m,
              ),
            );
          }
          appendToolResponse(res, { skipMessages: true });
        }
      });
    } catch (e) {
      clientLogger.error("assistant/messages stream failed", e);
      setChatMessages((prev) =>
        prev.map((m) => (m.id === streamMessageId ? { ...m, role: "system", content: "助手暂时没有响应，请稍后再试。" } : m)),
      );
    } finally {
      setIsChatSending(false);
    }
  }, [appendChat, appendToolResponse, chatInput, getAssistantSessionId, ingestionId, isChatSending, orgId, userId, workspaceMode]);

  const onProbeLlmRouter = useCallback(async () => {
    if (isLlmProbeRunning) return;
    setIsLlmProbeRunning(true);
    try {
      const res = await postAssistantLlmProbe({
        message: "查物料 M001",
        org_id: orgId,
        user_id: userId,
        active_task_id: ingestionId,
      });
      setHealthInfo((prev) =>
        prev
          ? {
              ...prev,
              llm_router_enabled: res.enabled,
              llm_api_key_configured: res.api_key_configured,
              llm_model: res.model,
              llm_base_url: res.base_url,
            }
          : prev,
      );
      clientLogger.info("LLM 路由探针结果", res);
      appendChat(
        "system",
        res.ok
          ? `LLM 路由探针成功：工具 ${res.tool_name ?? "assistant"}${res.action ? ` / ${res.action}` : ""}${
              res.reason ? `\n原因：${res.reason}` : ""
            }`
          : `LLM 路由探针未通过：${res.error ?? "未知原因"}。当前对话仍会使用规则路由兜底。`,
      );
    } catch (e) {
      clientLogger.error("LLM 路由探针请求失败", e);
      appendChat("system", "LLM 路由探针请求失败，请检查后端是否运行。当前对话仍会使用规则路由兜底。");
    } finally {
      setIsLlmProbeRunning(false);
    }
  }, [appendChat, ingestionId, isLlmProbeRunning, orgId, userId]);

  const onCancelReprocessUpload = useCallback((token: string) => {
    delete pendingReprocessUploadsRef.current[token];
    appendChat("system", "已保留现有 ERP 草稿记录，没有重新处理该文件。");
  }, [appendChat]);

  const onConfirmReprocessUpload = useCallback(
    async (token: string) => {
      const pending = pendingReprocessUploadsRef.current[token];
      if (!pending) {
        appendChat("system", "这条重新处理请求已失效，请重新上传文件。");
        return;
      }
      delete pendingReprocessUploadsRef.current[token];
      setIsUploading(true);
      try {
        const uploadRes = await postAssistantFile(
          pending.file,
          pending.userId,
          pending.orgId,
          pending.extractionProfileId,
          pending.sessionId,
          true,
        );
        const resp = uploadRes.tool_result?.ingestion;
        if (!resp) {
          appendChat("system", `${pending.file.name} 重新处理请求没有返回任务信息，请稍后重试。`);
          return;
        }
        const resetIngestionId = resp.ingestion_id || pending.ingestionId || "";
        if (resetIngestionId) {
          delete clientDraftStateRef.current[resetIngestionId];
          setChatMessages((prev) => removePdfToErpTaskCards(prev, resetIngestionId));
        }
        appendToolResponse(uploadRes);
        previewDirtyRef.current = false;
        previewDirtyByIngestionRef.current = withoutRecordKey(
          previewDirtyByIngestionRef.current,
          resp.ingestion_id,
        ) as BooleanByIngestion;
        setPreviewDirtyByIngestion((prev) => withoutRecordKey(prev, resp.ingestion_id) as BooleanByIngestion);
        upsertIngestionState(resp, { activate: true, fileName: pending.file.name, poll: isBackgroundRunningStatus(resp.status) });
        setPreviewDraftsByIngestion((prev) => ({ ...prev, [resp.ingestion_id]: null }));
        setConfirmedPreviewIds((prev) => {
          if (!prev[resp.ingestion_id]) return prev;
          const next = { ...prev };
          delete next[resp.ingestion_id];
          return next;
        });
        delete clientDraftStateRef.current[resp.ingestion_id];
        workflowToolCardKeyRef.current = null;
        workflowToolCardKeyByIngestionRef.current = withoutRecordKey(
          workflowToolCardKeyByIngestionRef.current,
          resp.ingestion_id,
        ) as StringByIngestion;
        setIngestionPollNonce((n) => n + 1);
        clientLogger.info("重新处理任务已创建", resp);
      } catch (e) {
        clientLogger.error("重新处理上传失败", e);
        appendChat("system", "重新处理没有成功：请检查后端服务和队列是否正常后再试。");
      } finally {
        setIsUploading(false);
      }
    },
    [appendChat, appendToolResponse, upsertIngestionState],
  );

  const handleFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      if (workspaceMode !== "pdf_to_erp") {
        appendChat("system", "当前是普通对话 / ERP 查询模式。请先切换到「PDF 转 ERP」再上传 PDF。");
        return;
      }
      const list = Array.from(files).slice(0, 8);

      shouldAutoScrollRef.current = true;
      setIsUploading(true);
      const rawProfileId = extractionProfileId.trim();
      const prof = rawProfileId.toLowerCase() === "string" ? "" : rawProfileId;
      let lastUploadedName: string | null = null;
      let uploadSuccessCount = 0;
      try {
        for (let i = 0; i < list.length; i++) {
          const file = list[i];
          const pre = precheckUploadFile(file);
          if (!pre.ok) {
            appendChat("system", `${file.name}：${pre.message}`);
            continue;
          }
          clientLogger.info("用户选择文件", { name: file.name, size: file.size, type: file.type, index: i });

          const prevId = ingestionIdRef.current;
          if (prevId) {
            const prevFileName =
              lastIngestionFileNameRef.current ?? lastUploadedName ?? "（上一任务）";
            const prevStatus = String(lastStatusByIngestionRef.current[prevId] ?? lastStatusRef.current ?? "");
            setIngestionHistory((h) => {
              if (h.some((x) => x.id === prevId)) return h;
              return [
                {
                  id: prevId,
                  fileName: prevFileName,
                  status: prevStatus,
                },
                ...h,
              ].slice(0, 30);
            });
          }

          let uploadRes = await postAssistantFile(file, userId, orgId, prof || undefined, getAssistantSessionId());
          let resp = uploadRes.tool_result?.ingestion;
          if (!resp) {
            appendChat("system", `${file.name} 上传后没有返回任务信息，请稍后重试。`);
            continue;
          }
          if (resp.status === "DRAFT_CREATED") {
            appendToolResponse(uploadRes);
            const token = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
            pendingReprocessUploadsRef.current[token] = {
              file,
              userId,
              orgId,
              extractionProfileId: prof || undefined,
              sessionId: getAssistantSessionId(),
              ingestionId: resp.ingestion_id,
            };
            appendChat("assistant", " ", {
              toolUi: {
                type: "reprocess_confirm",
                data: {
                  token,
                  file_name: file.name,
                  ingestion_id: resp.ingestion_id,
                  draft_no: resp.draft_no ?? "",
                  draft_url: resp.draft_url ?? "",
                },
              },
            });
            uploadSuccessCount += 1;
            continue;
          }
          appendToolResponse(uploadRes);
          previewDirtyRef.current = false;
          previewDirtyByIngestionRef.current = withoutRecordKey(
            previewDirtyByIngestionRef.current,
            resp.ingestion_id,
          ) as BooleanByIngestion;
          setPreviewDirtyByIngestion((prev) => withoutRecordKey(prev, resp.ingestion_id) as BooleanByIngestion);
          upsertIngestionState(resp, { activate: true, fileName: file.name, poll: isBackgroundRunningStatus(resp.status) });
          setPreviewDraftsByIngestion((prev) => ({ ...prev, [resp.ingestion_id]: null }));
          setConfirmedPreviewIds((prev) => {
            if (!prev[resp.ingestion_id]) return prev;
            const next = { ...prev };
            delete next[resp.ingestion_id];
            return next;
          });
          delete clientDraftStateRef.current[resp.ingestion_id];
          lastUploadedName = file.name;
          uploadSuccessCount += 1;
          clientLogger.info("上传任务创建成功（服务端已计算文件哈希）", resp);
        }
        if (uploadSuccessCount === 0) {
          appendChat("system", "没有成功创建解析任务：请检查文件类型与大小后重试。");
        }
      } catch (e) {
        clientLogger.error("上传或创建任务失败", e);
        const detail =
          e && typeof e === "object" && "message" in e && typeof (e as Error).message === "string"
            ? (e as Error).message
            : "";
        appendChat(
          "system",
          `上传没成功：请确认浏览器里配置的 API 地址能访问、后台已启动，且文件格式与大小符合要求。${detail ? `\n（${detail.slice(0, 200)}）` : ""}`,
        );
      } finally {
        setIsUploading(false);
      }
    },
    [
      appendChat,
      appendToolResponse,
      extractionProfileId,
      getAssistantSessionId,
      ingestionHistory,
      orgId,
      upsertIngestionState,
      userId,
      workspaceMode,
    ],
  );

  const onDrop = useCallback(
    async (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      dragDepthRef.current = 0;
      setIsDragging(false);
      const files = e.dataTransfer.files;
      if (!files || files.length === 0) {
        appendChat(
          "system",
          "未检测到可上传的文件。请从文件夹把文件拖进灰色区域（不要拖浏览器里的链接或图片地址）。",
        );
        return;
      }
      await handleFiles(files);
    },
    [appendChat, handleFiles],
  );

  const onResolve = useCallback(async (overrideFields?: Record<string, string>) => {
    if (!ingestionId || !ingestion) return;
    setIsResolving(true);
    try {
      const keys = resolveFieldKeys(ingestion.doc_type_hint, ingestion);
      const sourceFields = overrideFields ?? resolveFields;
      const fields: Record<string, string> = {};
      for (const k of keys) {
        fields[k] = (sourceFields[k] ?? "").trim();
      }
      for (const k of OPTIONAL_RESOLVE_KEYS) {
        const v = (sourceFields[k] ?? "").trim();
        if (v) fields[k] = v;
      }
      clientLogger.info("提交字段补全 resolve", { ingestionId, fields });
      const chatRes = await postAssistantMessage({
        session_id: getAssistantSessionId(),
        action: "submit_missing_fields",
        message: "submit missing fields",
        org_id: orgId,
        user_id: userId,
        active_task_id: ingestionId,
        fields,
      });
      appendToolResponse(chatRes, { skipMessages: true });
      const data = chatRes.tool_result?.ingestion;
      if (!data) {
        return;
      }
      if (data.ingestion_id !== ingestionIdRef.current) {
        clientLogger.warn("忽略旧任务字段补全回包", {
          responseIngestionId: data.ingestion_id,
          currentIngestionId: ingestionIdRef.current,
        });
        return;
      }
      setIngestion(data);
      clientLogger.info("resolve 成功", { status: data.status, missing: data.missing_fields });
      appendChat("system", `已保存您填的内容。${ingestionStatusLabelZh(data.status)}`);
    } catch (e) {
      clientLogger.error("resolve 失败", e);
      appendChat("system", "保存失败：请把标红的空项填好再试。");
    } finally {
      setIsResolving(false);
    }
  }, [appendChat, appendToolResponse, getAssistantSessionId, ingestion, ingestionId, orgId, resolveFields, userId]);

  const onConfirmPreview = useCallback(async () => {
    if (!ingestionId || !previewDraft) return;
    const previewToConfirm = bindPreviewSalesUser(previewDraft, userName);
    setIsConfirmingPreview(true);
    try {
      clientLogger.info("提交订单预览确认", { ingestionId, details: previewToConfirm.details.length });
      const chatRes = await postAssistantMessage({
        session_id: getAssistantSessionId(),
        action: "confirm_preview",
        message: "确认订单预览",
        org_id: orgId,
        user_id: userId,
        active_task_id: ingestionId,
        preview_data: previewToConfirm,
      });
      appendToolResponse(chatRes);
      const data = chatRes.tool_result?.ingestion;
      if (!data) {
        return;
      }
      if (data.ingestion_id !== ingestionIdRef.current) {
        clientLogger.warn("忽略旧任务预览确认回包", {
          responseIngestionId: data.ingestion_id,
          currentIngestionId: ingestionIdRef.current,
        });
        return;
      }
      previewDirtyRef.current = false;
      setConfirmedPreviewIds((prev) => ({ ...prev, [data.ingestion_id]: true }));
      setPreviewDraft(bindPreviewSalesUser(data.preview_data ?? previewToConfirm, userName));
      setIngestion(data);
      const workflowToolUi = buildPdfToErpToolUi(data);
      if (workflowToolUi) {
        setChatMessages((prev) => updateWorkflowToolCard(prev, data, workflowToolUi));
        workflowToolCardKeyRef.current = pdfToErpWorkflowCardKey(data, data.status);
      }
    } catch (e) {
      clientLogger.error("confirm-preview 失败", e);
      appendChat("system", "订单预览确认失败：请检查表格中的必填项后再试。");
    } finally {
      setIsConfirmingPreview(false);
    }
  }, [appendChat, appendToolResponse, getAssistantSessionId, ingestionId, orgId, previewDraft, userId, userName]);

  const onPreviewDraftChange = useCallback((next: OrderPreviewData) => {
    previewDirtyRef.current = true;
    if (ingestionIdRef.current) {
      const id = ingestionIdRef.current;
      setConfirmedPreviewIds((prev) => {
        if (!prev[id]) return prev;
        const nextState = { ...prev };
        delete nextState[id];
        return nextState;
      });
    }
    setPreviewDraft(bindPreviewSalesUser(next, userName));
  }, [userName]);

  const onCreateDraft = useCallback(async () => {
    if (!ingestionId) return;
    if (isCreatingDraft) return;
    const hasDirtyPreview = Boolean(previewDraft && previewDirtyRef.current);
    if (previewDraft && hasDirtyPreview) {
      appendChat("system", "订单预览有未确认的修改，请先确认预览后再上传 ERP。");
      return;
    }
    const hasConfirmedPreview = Boolean(ingestionId && confirmedPreviewIds[ingestionId] && !previewDirtyRef.current);
    if (previewDraft && !hasConfirmedPreview) {
      appendChat("system", "请先点击「确认预览」，确认订单信息无误后再上传 ERP。");
      return;
    }
    if (displayIngestionStatus(ingestion, clientDraftStateRef.current) !== "VALIDATED") {
      appendChat("system", "当前订单还没有进入可上传状态，请先补全必填信息并等待校验通过。");
      return;
    }
    setIsCreatingDraft(true);
    try {
      if (previewDraft && ingestion?.preview_data?.order.salesUser !== userName) {
        const previewToConfirm = bindPreviewSalesUser(previewDraft, userName);
        clientLogger.info("创建草稿前同步销售员", { ingestionId, salesUser: userName });
        const confirmRes = await postAssistantMessage({
          session_id: getAssistantSessionId(),
          action: "confirm_preview",
          message: "同步销售员并确认订单预览",
          org_id: orgId,
          user_id: userId,
          active_task_id: ingestionId,
          preview_data: previewToConfirm,
        });
        const confirmed = confirmRes.tool_result?.ingestion;
        if (confirmed?.ingestion_id === ingestionIdRef.current) {
          previewDirtyRef.current = false;
          setPreviewDraft(bindPreviewSalesUser(confirmed.preview_data ?? previewToConfirm, userName));
          setIngestion(confirmed);
        }
      }
      clientLogger.info("请求创建草稿 create-draft", { ingestionId });
      const chatRes = await postAssistantMessage({
        session_id: getAssistantSessionId(),
        action: "create_draft",
        message: "确认上传 ERP，创建草稿",
        org_id: orgId,
        user_id: userId,
        active_task_id: ingestionId,
      });
      appendToolResponse(chatRes);
      const data = chatRes.tool_result?.draft;
      if (!data) {
        return;
      }
      if (data.ingestion_id !== ingestionIdRef.current) {
        clientLogger.warn("忽略旧任务创建草稿回包", {
          responseIngestionId: data.ingestion_id,
          currentIngestionId: ingestionIdRef.current,
        });
        return;
      }
      clientLogger.info("草稿创建成功", data);
      clientDraftStateRef.current[data.ingestion_id] = {
        draft_no: data.draft_no,
        draft_url: data.draft_url,
      };
      setIngestion((prev) => (prev ? mergeDraftCreatedState(prev, data) : prev));
      lastStatusRef.current = "DRAFT_CREATED";
      if (pollTimerRef.current) {
        window.clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    } catch (e) {
      clientLogger.error("create-draft 失败", e);
      appendChat("system", "草稿没生成出来：请先按下方提示补全必填项，等进度显示可以生成草稿后再点按钮。");
    } finally {
      setIsCreatingDraft(false);
    }
  }, [
    appendChat,
    appendToolResponse,
    confirmedPreviewIds,
    getAssistantSessionId,
    ingestion,
    ingestionId,
    isCreatingDraft,
    orgId,
    previewDraft,
    userId,
    userName,
  ]);

  /** 仅开发：当 worker/Redis 未启动时，可手动触发内部 process 推进状态机 */
  const onResolveTask = useCallback(
    async (targetIngestionId: string, overrideFields?: Record<string, string>) => {
      if (!targetIngestionId) return;
      const targetIngestion = ingestionsById[targetIngestionId] ?? (targetIngestionId === ingestionId ? ingestion : null);
      if (!targetIngestion) return;
      setIsResolving(true);
      setResolvingIngestionIds((prev) => ({ ...prev, [targetIngestionId]: true }));
      try {
        const keys = resolveFieldKeys(targetIngestion.doc_type_hint, targetIngestion);
        const sourceFields =
          overrideFields ??
          resolveFieldsByIngestion[targetIngestionId] ??
          (targetIngestionId === ingestionId ? resolveFields : {});
        const fields: Record<string, string> = {};
        for (const k of keys) fields[k] = (sourceFields[k] ?? "").trim();
        for (const k of OPTIONAL_RESOLVE_KEYS) {
          const v = (sourceFields[k] ?? "").trim();
          if (v) fields[k] = v;
        }
        clientLogger.info("submit resolve fields", { ingestionId: targetIngestionId, fields });
        const chatRes = await postAssistantMessage({
          session_id: getAssistantSessionId(),
          action: "submit_missing_fields",
          message: "submit missing fields",
          org_id: orgId,
          user_id: userId,
          active_task_id: targetIngestionId,
          fields,
        });
        appendToolResponse(chatRes, { skipMessages: true });
        const data = chatRes.tool_result?.ingestion;
        if (!data || data.ingestion_id !== targetIngestionId) return;
        upsertIngestionState(data, { activate: true });
        appendChat("system", `已保存您填写的内容。${ingestionStatusLabelZh(data.status)}`);
      } catch (e) {
        clientLogger.error("resolve failed", e);
        appendChat("system", "保存失败：请把标红的空项填好后再试。");
      } finally {
        setIsResolving(false);
        setResolvingIngestionIds((prev) => withoutRecordKey(prev, targetIngestionId) as BooleanByIngestion);
      }
    },
    [
      appendChat,
      appendToolResponse,
      getAssistantSessionId,
      ingestion,
      ingestionId,
      ingestionsById,
      orgId,
      resolveFields,
      resolveFieldsByIngestion,
      upsertIngestionState,
      userId,
    ],
  );

  const onPreviewDraftChangeTask = useCallback(
    (targetIngestionId: string, next: OrderPreviewData) => {
      if (!targetIngestionId) return;
      const bound = bindPreviewSalesUser(next, userName);
      previewDirtyByIngestionRef.current = {
        ...previewDirtyByIngestionRef.current,
        [targetIngestionId]: true,
      };
      setPreviewDirtyByIngestion((prev) => ({ ...prev, [targetIngestionId]: true }));
      setConfirmedPreviewIds((prev) => withoutRecordKey(prev, targetIngestionId) as Record<string, true>);
      setPreviewDraftsByIngestion((prev) => ({ ...prev, [targetIngestionId]: bound }));
      if (ingestionIdRef.current === targetIngestionId) {
        previewDirtyRef.current = true;
        setPreviewDraft(bound);
      }
    },
    [userName],
  );

  const onConfirmPreviewTask = useCallback(
    async (targetIngestionId: string) => {
      if (!targetIngestionId) return;
      const targetIngestion = ingestionsById[targetIngestionId] ?? (targetIngestionId === ingestionId ? ingestion : null);
      const draft = previewDraftsByIngestion[targetIngestionId] ?? targetIngestion?.preview_data ?? null;
      if (!draft) return;
      const previewToConfirm = bindPreviewSalesUser(draft, userName);
      setIsConfirmingPreview(true);
      setConfirmingPreviewIngestionIds((prev) => ({ ...prev, [targetIngestionId]: true }));
      try {
        clientLogger.info("confirm order preview", { ingestionId: targetIngestionId, details: previewToConfirm.details.length });
        const chatRes = await postAssistantMessage({
          session_id: getAssistantSessionId(),
          action: "confirm_preview",
          message: "确认订单预览",
          org_id: orgId,
          user_id: userId,
          active_task_id: targetIngestionId,
          preview_data: previewToConfirm,
        });
        appendToolResponse(chatRes);
        const data = chatRes.tool_result?.ingestion;
        if (!data || data.ingestion_id !== targetIngestionId) return;
        previewDirtyByIngestionRef.current = withoutRecordKey(
          previewDirtyByIngestionRef.current,
          targetIngestionId,
        ) as BooleanByIngestion;
        setPreviewDirtyByIngestion((prev) => withoutRecordKey(prev, targetIngestionId) as BooleanByIngestion);
        setConfirmedPreviewIds((prev) => ({ ...prev, [targetIngestionId]: true }));
        const nextPreview = syncPreviewDefaults(data.preview_data ?? previewToConfirm, userName, orgId);
        setPreviewDraftsByIngestion((prev) => ({ ...prev, [targetIngestionId]: nextPreview }));
        const displayData = upsertIngestionState(data, { activate: true });
        const workflowToolUi = buildPdfToErpToolUi(displayData);
        if (workflowToolUi) {
          setChatMessages((prev) => updateWorkflowToolCard(prev, displayData, workflowToolUi));
          const displayStatus = displayIngestionStatus(displayData, clientDraftStateRef.current) ?? displayData.status;
          const cardKey = pdfToErpWorkflowCardKey(displayData, displayStatus);
          workflowToolCardKeyByIngestionRef.current = {
            ...workflowToolCardKeyByIngestionRef.current,
            [targetIngestionId]: cardKey,
          };
          workflowToolCardKeyRef.current = cardKey;
        }
      } catch (e) {
        clientLogger.error("confirm preview failed", e);
        appendChat("system", "订单预览确认失败：请检查表格中的必填项后再试。");
      } finally {
        setIsConfirmingPreview(false);
        setConfirmingPreviewIngestionIds((prev) => withoutRecordKey(prev, targetIngestionId) as BooleanByIngestion);
      }
    },
    [
      appendChat,
      appendToolResponse,
      getAssistantSessionId,
      ingestion,
      ingestionId,
      ingestionsById,
      orgId,
      previewDraftsByIngestion,
      upsertIngestionState,
      userId,
      userName,
    ],
  );

  const onCreateDraftTask = useCallback(
    async (targetIngestionId: string) => {
      if (!targetIngestionId || creatingDraftIngestionIds[targetIngestionId]) return;
      const targetIngestion = ingestionsById[targetIngestionId] ?? (targetIngestionId === ingestionId ? ingestion : null);
      if (!targetIngestion) return;
      const targetPreview = previewDraftsByIngestion[targetIngestionId] ?? targetIngestion.preview_data ?? null;
      const isDirty = Boolean(targetPreview && previewDirtyByIngestion[targetIngestionId]);
      if (targetPreview && isDirty) {
        appendChat("system", "订单预览有未确认的修改，请先确认预览后再上传 ERP。");
        return;
      }
      const isConfirmed = Boolean(confirmedPreviewIds[targetIngestionId] && !isDirty);
      if (targetPreview && !isConfirmed) {
        appendChat("system", "请先点击“确认预览”，确认订单信息无误后再上传 ERP。");
        return;
      }
      const currentStatus = displayIngestionStatus(targetIngestion, clientDraftStateRef.current);
      if (currentStatus !== "VALIDATED") {
        appendChat("system", "当前订单还没有进入可上传状态，请先补全必填信息并等待校验通过。");
        return;
      }
      setIsCreatingDraft(true);
      setCreatingDraftIngestionIds((prev) => ({ ...prev, [targetIngestionId]: true }));
      try {
        clientLogger.info("create ERP draft", { ingestionId: targetIngestionId });
        const chatRes = await postAssistantMessage({
          session_id: getAssistantSessionId(),
          action: "create_draft",
          message: "确认上传 ERP，创建草稿",
          org_id: orgId,
          user_id: userId,
          active_task_id: targetIngestionId,
        });
        appendToolResponse(chatRes);
        const data = chatRes.tool_result?.draft;
        if (!data || data.ingestion_id !== targetIngestionId) return;
        clientDraftStateRef.current[data.ingestion_id] = {
          draft_no: data.draft_no,
          draft_url: data.draft_url,
        };
        const base = ingestionsByIdRef.current[data.ingestion_id] ?? targetIngestion;
        const merged = mergeDraftCreatedState(base, data);
        upsertIngestionState(merged, { activate: true, poll: false });
        setPollingIngestionIds((prev) => withoutRecordKey(prev, targetIngestionId) as BooleanByIngestion);
      } catch (e) {
        clientLogger.error("create draft failed", e);
        appendChat("system", "草稿没生成出来：请先补全必填项，等进度显示可以生成草稿后再点按钮。");
      } finally {
        setIsCreatingDraft(false);
        setCreatingDraftIngestionIds((prev) => withoutRecordKey(prev, targetIngestionId) as BooleanByIngestion);
      }
    },
    [
      appendChat,
      appendToolResponse,
      confirmedPreviewIds,
      creatingDraftIngestionIds,
      getAssistantSessionId,
      ingestion,
      ingestionId,
      ingestionsById,
      orgId,
      previewDirtyByIngestion,
      previewDraftsByIngestion,
      upsertIngestionState,
      userId,
    ],
  );

  const onDevProcess = useCallback(async () => {
    if (!ingestionId) return;
    const base = getApiBaseUrl();
    const url = `${base}/internal/ingestions/${encodeURIComponent(ingestionId)}/process`;
    clientLogger.warn("开发模式：调用内部 process 接口（勿在生产依赖）", { url });
    try {
      const rid = crypto.randomUUID();
      const res = await fetch(url, { method: "POST", headers: { "x-request-id": rid } });
      const payload = await res.json().catch(() => null);
      if (!res.ok) {
        clientLogger.error("内部 process 调用失败", { status: res.status, payload });
        return;
      }
      clientLogger.info("内部 process 调用成功", payload);
      const refreshed = await getIngestion(ingestionId);
      setIngestion(refreshed);
    } catch (e) {
      clientLogger.error("内部 process 调用异常", e);
    }
  }, [ingestionId]);

  const pipelineUi = useMemo(() => {
    const current = displayIngestionStatus(ingestion, clientDraftStateRef.current);
    const failed = current === "FAILED";
    const running = isBackgroundRunningStatus(current) || isCreatingDraft;
    const runningText = isCreatingDraft ? "正在生成草稿，请稍候..." : "系统正在处理，请稍候...";
    const stepIdx = pipelineHighlightStepIndex(current, ingestion?.audit_events);
    return (
      <>
        <div className="flex flex-wrap items-center gap-2">
          {STATUS_PIPELINE.map((s, i) => {
            const passed = stepIdx >= 0 && i <= stepIdx;
            const isCurrent = Boolean(current && !failed && current === s);
            let circle =
              "flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[11px] font-bold tabular-nums ";
            if (isCurrent) circle += "bg-sky-600 text-white shadow-sm shadow-sky-600/25";
            else if (passed) circle += "bg-emerald-600 text-white";
            else circle += "bg-slate-200 text-slate-500";
            return (
              <div key={s} className="flex items-center gap-1.5">
                <span className={circle} aria-hidden>
                  {i + 1}
                </span>
                <div
                  className={[
                    "rounded-lg border px-2.5 py-1.5 text-sm font-medium",
                    isCurrent ? "border-sky-500 bg-sky-50 text-sky-900" : "",
                    passed && !isCurrent ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "",
                    !passed && !isCurrent ? "border-slate-200 bg-white text-slate-500" : "",
                  ].join(" ")}
                >
                  {s}
                </div>
              </div>
            );
          })}
        </div>
        {running ? (
          <div className="mt-2 inline-flex items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-sm font-medium text-sky-900">
            <ProgressSpinner />
            <span>{runningText}</span>
          </div>
        ) : null}
        {failed ? (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <span className="rounded-md border border-red-400 bg-red-50 px-2 py-1 text-sm font-semibold text-red-900">
              FAILED
              {ingestion?.error_code ? (
                <>
                  {" "}
                  · <span className="font-mono font-normal">{ingestion.error_code}</span>
                </>
              ) : null}
            </span>
            <span className="text-sm text-slate-600">出错时请往下看「错误说明」和事件记录。</span>
          </div>
        ) : null}
      </>
    );
  }, [ingestion, isCreatingDraft]);

  const hasOrderPreview = Boolean(previewDraft);
  const isPreviewDirty = useMemo(() => {
    return Boolean(previewDraft && previewDirtyRef.current);
  }, [previewDraft]);
  const isPreviewConfirmed = useMemo(() => {
    return Boolean(ingestionId && confirmedPreviewIds[ingestionId] && !isPreviewDirty);
  }, [confirmedPreviewIds, ingestionId, isPreviewDirty]);

  const renderToolUi = useCallback(
    (ui: ToolUi | null | undefined) => {
      if (!ui) return null;
      if (ui.type === "reprocess_confirm") {
        const token = String(ui.data.token ?? "");
        const fileName = String(ui.data.file_name ?? "");
        const ingestionIdText = String(ui.data.ingestion_id ?? "");
        const draftNo = String(ui.data.draft_no ?? "");
        const draftUrl = String(ui.data.draft_url ?? "");
        return (
          <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50/80 p-3 text-sm text-amber-950">
            <div className="font-semibold">该文件已经上传过 ERP</div>
            <div className="mt-1 text-amber-900">请确认是否要重复上传同一订单?</div>
            {fileName ? (
              <div className="mt-2 truncate text-xs text-amber-800" title={fileName}>
                {fileName}
              </div>
            ) : null}
            {ingestionIdText ? <div className="mt-2 font-mono text-xs text-amber-800">任务编号：{ingestionIdText}</div> : null}
            {draftNo ? <div className="mt-1 font-mono text-xs text-amber-800">草稿号：{draftNo}</div> : null}
            {draftUrl ? (
              <a className="mt-2 inline-block font-medium text-amber-800 underline" href={draftUrl} target="_blank">
                打开已有草稿
              </a>
            ) : null}
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => void onConfirmReprocessUpload(token)}
                className="rounded-lg bg-amber-700 px-3 py-2 text-sm font-semibold text-white hover:bg-amber-600 disabled:cursor-not-allowed disabled:opacity-50"
                disabled={!token || isUploading}
              >
                {isUploading ? "正在提交..." : "重新处理"}
              </button>
              <button
                type="button"
                onClick={() => onCancelReprocessUpload(token)}
                className="rounded-lg border border-amber-200 bg-white px-3 py-2 text-sm font-semibold text-amber-900 hover:bg-amber-50"
              >
                否，保留已有结果
              </button>
            </div>
          </div>
        );
      }
      if (ui.type === "missing_fields_form") {
        const cardIngestionId = String(ui.data.ingestion_id ?? "");
        const taskIngestion = cardIngestionId
          ? ingestionsById[cardIngestionId] ?? (cardIngestionId === ingestionId ? ingestion : null)
          : null;
        const isCurrentTaskCard = Boolean(cardIngestionId && taskIngestion);
        const cardPreviewDirty = Boolean(cardIngestionId && previewDirtyByIngestion[cardIngestionId]);
        const cardPreviewConfirmed = Boolean(cardIngestionId && confirmedPreviewIds[cardIngestionId] && !cardPreviewDirty);
        const isResolvingCard = Boolean(cardIngestionId && resolvingIngestionIds[cardIngestionId]);
        const isConfirmingPreviewCard = Boolean(cardIngestionId && confirmingPreviewIngestionIds[cardIngestionId]);
        const isCreatingDraftCard = Boolean(cardIngestionId && creatingDraftIngestionIds[cardIngestionId]);
        const cardPreview =
          ui.data.preview_data && typeof ui.data.preview_data === "object"
            ? (ui.data.preview_data as OrderPreviewData)
            : null;
        const previewForCard = isCurrentTaskCard
          ? previewDraftsByIngestion[cardIngestionId] ?? taskIngestion?.preview_data ?? cardPreview
          : cardPreview;
        if (previewForCard) {
          const currentStatus = displayIngestionStatus(taskIngestion, clientDraftStateRef.current);
          const canCreateDraft =
            isCurrentTaskCard && currentStatus === "VALIDATED" && cardPreviewConfirmed && !cardPreviewDirty;
          const editableFields = isCurrentTaskCard ? taskIngestion?.editable_fields ?? [] : [];
          const issues = isCurrentTaskCard ? taskIngestion?.issues ?? [] : [];
          return (
            <div className="mt-3 rounded-xl border border-red-200 bg-red-50/80 p-3 text-sm text-red-950">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="font-semibold">订单预览需要确认</div>
                <span className="rounded-full bg-white/80 px-2.5 py-1 text-[11px] font-semibold text-red-800 ring-1 ring-red-100">
                  {isCurrentTaskCard ? "当前任务，可继续操作" : "历史任务，仅供查看"}
                </span>
              </div>
              <div className="mt-1 text-xs text-red-800">红色字段补齐后，点击「确认预览并校验」。</div>
              <div className="mt-3 max-h-[40rem] overflow-auto">
                <OrderPreviewEditor
                  preview={previewForCard}
                  editableFields={editableFields}
                  issues={issues}
                  onChange={(next) => onPreviewDraftChangeTask(cardIngestionId, next)}
                  onConfirm={() => onConfirmPreviewTask(cardIngestionId)}
                  onCreateDraft={() => onCreateDraftTask(cardIngestionId)}
                  confirming={isConfirmingPreviewCard}
                  creatingDraft={isCreatingDraftCard}
                  createDraftDisabled={!canCreateDraft}
                  lockedSalesUser={userName}
                  hideCreateDraftAction
                  readOnly={!isCurrentTaskCard}
                />
              </div>
            </div>
          );
        }
        const rows = Array.isArray(ui.data.fields) ? (ui.data.fields as Array<Record<string, unknown>>) : [];
        const disabled = !isCurrentTaskCard;
        const cardFields = rows.reduce<Record<string, string>>((acc, field) => {
          const key = String(field.key ?? "");
          if (!key) return acc;
          acc[key] = String(
            (isCurrentTaskCard ? resolveFieldsByIngestion[cardIngestionId]?.[key] : undefined) ??
              field.current_value ??
              "",
          );
          return acc;
        }, {});
        return (
          <div
            className={[
              "mt-3 rounded-xl border border-red-200 bg-red-50/80 p-3 text-sm text-red-950",
              String(ui.data.error_code ?? "UNKNOWN") === "UNSUPPORTED_DOCUMENT" ? "[&>div:first-child]:hidden" : "",
            ].join(" ")}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-semibold">需要补充的信息</div>
              <span className="rounded-full bg-white/80 px-2.5 py-1 text-[11px] font-semibold text-red-800 ring-1 ring-red-100">
                {isCurrentTaskCard ? "当前任务，可继续操作" : "历史任务，仅供查看"}
              </span>
            </div>
            {disabled ? <div className="mt-1 text-xs text-red-700">这是一条历史任务消息，不能在这里继续提交字段。</div> : null}
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              {rows.slice(0, 8).map((field) => (
                <label key={String(field.key)} className="rounded-lg bg-white/90 px-2.5 py-2 ring-1 ring-red-100">
                  <div className="font-medium">{String(field.label ?? field.key ?? "")}</div>
                  <input
                    value={
                      (isCurrentTaskCard ? resolveFieldsByIngestion[cardIngestionId]?.[String(field.key)] : undefined) ??
                      String(field.current_value ?? "")
                    }
                    disabled={disabled || isResolvingCard}
                    onChange={(e) =>
                      setResolveFieldsByIngestion((prev) => ({
                        ...prev,
                        [cardIngestionId]: {
                          ...(prev[cardIngestionId] ?? {}),
                          [String(field.key)]: e.target.value,
                        },
                      }))
                    }
                    className="mt-1 w-full rounded-lg border border-red-200 bg-white px-2.5 py-2 text-sm text-slate-900 outline-none focus:border-red-400 focus:ring-2 focus:ring-red-100 disabled:cursor-not-allowed disabled:bg-slate-100"
                    placeholder={String(field.key ?? "")}
                  />
                  <div className="mt-0.5 font-mono text-[11px] text-red-700">{String(field.key ?? "")}</div>
                </label>
              ))}
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                disabled={disabled || isResolvingCard || !cardIngestionId}
                onClick={() => void onResolveTask(cardIngestionId, cardFields)}
                className="rounded-lg bg-red-700 px-3 py-2 text-sm font-semibold text-white hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isResolvingCard ? "正在保存..." : "保存补充信息"}
              </button>
              <button
                type="button"
                disabled={disabled || isResolvingCard}
                onClick={() => {
                  const text = rows
                    .map(
                      (field) =>
                        `${String(field.label ?? field.key ?? "")}是${
                          resolveFieldsByIngestion[cardIngestionId]?.[String(field.key)] ?? ""
                        }`,
                    )
                    .join("，");
                  setChatInput(text);
                }}
                className="rounded-lg border border-red-200 bg-white px-3 py-2 text-sm font-semibold text-red-800 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                填到输入框
              </button>
            </div>
          </div>
        );
      }
      if (ui.type === "upload_confirm") {
        const cardIngestionId = String(ui.data.ingestion_id ?? "");
        const taskIngestion = cardIngestionId
          ? ingestionsById[cardIngestionId] ?? (cardIngestionId === ingestionId ? ingestion : null)
          : null;
        const isCurrentTaskCard = Boolean(cardIngestionId && taskIngestion);
        const disabled = !isCurrentTaskCard;
        const cardPreviewDirty = Boolean(cardIngestionId && previewDirtyByIngestion[cardIngestionId]);
        const cardPreviewConfirmed = Boolean(cardIngestionId && confirmedPreviewIds[cardIngestionId] && !cardPreviewDirty);
        const isConfirmingPreviewCard = Boolean(cardIngestionId && confirmingPreviewIngestionIds[cardIngestionId]);
        const isCreatingDraftCard = Boolean(cardIngestionId && creatingDraftIngestionIds[cardIngestionId]);
        const cardPreview =
          ui.data.preview_data && typeof ui.data.preview_data === "object"
            ? (ui.data.preview_data as OrderPreviewData)
            : null;
        const previewForCard = isCurrentTaskCard
          ? previewDraftsByIngestion[cardIngestionId] ?? taskIngestion?.preview_data ?? cardPreview
          : cardPreview;
        const currentStatus = displayIngestionStatus(taskIngestion, clientDraftStateRef.current);
        const canCreateDraft = isCurrentTaskCard && currentStatus === "VALIDATED" && cardPreviewConfirmed && !cardPreviewDirty;
        const editableFields = isCurrentTaskCard
          ? taskIngestion?.editable_fields ?? []
          : Array.isArray(ui.data.editable_fields)
            ? (ui.data.editable_fields as NonNullable<IngestionResponse["editable_fields"]>)
            : [];
        const issues = isCurrentTaskCard
          ? taskIngestion?.issues ?? []
          : Array.isArray(ui.data.issues)
            ? (ui.data.issues as NonNullable<IngestionResponse["issues"]>)
            : [];
        const detailCount = previewForCard?.details?.length ?? 0;
        const totalAmount = previewForCard ? sumPreviewAmount(previewForCard) : null;
        const summaryRows = previewForCard
          ? [
              ["客户", previewForCard.order.customerName || "未识别"],
              ["客户 PO", previewForCard.order.customerPoNo || "未识别"],
              ["明细行", `${detailCount} 行`],
              ["总金额", formatPreviewAmount(totalAmount, previewForCard.order.currency)],
            ]
          : [];
        return (
          <div className="mt-3 rounded-xl border border-emerald-200 bg-emerald-50/80 p-3 text-sm text-emerald-950">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="font-semibold">订单已校验通过</div>
              <span className="rounded-full bg-white/80 px-2.5 py-1 text-[11px] font-semibold text-emerald-800 ring-1 ring-emerald-100">
                {isCurrentTaskCard ? "当前任务，可上传" : "历史任务，仅供查看"}
              </span>
            </div>
            <div className="mt-1">确认后将调用 ERP 创建草稿单。</div>
            {disabled ? <div className="mt-2 text-xs text-emerald-800">这是历史任务的确认卡，不能在这里上传 ERP。</div> : null}
            {!disabled && cardPreviewDirty ? (
              <div className="mt-2 text-xs font-medium text-amber-800">预览有未确认修改，请先确认预览。</div>
            ) : null}
            {!disabled && !cardPreviewDirty && !cardPreviewConfirmed ? (
              <div className="mt-2 text-xs font-medium text-amber-800">请先点击「确认预览」，确认后才能上传 ERP。</div>
            ) : null}
            {summaryRows.length > 0 ? (
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {summaryRows.map(([label, value]) => (
                  <div key={label} className="rounded-lg bg-white/80 px-3 py-2 ring-1 ring-emerald-100">
                    <div className="text-[11px] font-medium text-emerald-700">{label}</div>
                    <div className="mt-0.5 truncate font-semibold text-emerald-950" title={value}>
                      {value}
                    </div>
                  </div>
                ))}
              </div>
            ) : null}
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                disabled={disabled || isConfirmingPreviewCard || !previewForCard}
                onClick={() => void onConfirmPreviewTask(cardIngestionId)}
                className="rounded-lg border border-emerald-200 bg-white px-3 py-2 text-sm font-semibold text-emerald-800 hover:bg-emerald-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isConfirmingPreviewCard ? "正在确认..." : "确认预览"}
              </button>
              <button
                type="button"
                disabled={!canCreateDraft || isCreatingDraftCard}
                onClick={() => void onCreateDraftTask(cardIngestionId)}
                className="rounded-lg bg-emerald-700 px-3 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {isCreatingDraftCard ? "正在上传..." : "上传 ERP"}
              </button>
            </div>
            {previewForCard ? (
              <details className="mt-3 rounded-xl bg-white/70 p-2 ring-1 ring-emerald-100">
                <summary className="cursor-pointer select-none px-1 py-1 text-sm font-semibold text-emerald-900">
                  查看和编辑订单预览
                </summary>
                <div className="mt-2 max-h-[32rem] overflow-auto">
                  <OrderPreviewEditor
                    preview={previewForCard}
                    editableFields={editableFields}
                    issues={issues}
                    onChange={(next) => onPreviewDraftChangeTask(cardIngestionId, next)}
                    onConfirm={() => onConfirmPreviewTask(cardIngestionId)}
                    onCreateDraft={() => onCreateDraftTask(cardIngestionId)}
                    confirming={isConfirmingPreviewCard}
                    creatingDraft={isCreatingDraftCard}
                    createDraftDisabled={!canCreateDraft}
                    lockedSalesUser={userName}
                    hideActions
                    readOnly={!isCurrentTaskCard}
                  />
                </div>
              </details>
            ) : (
              <div className="mt-3 rounded-lg bg-white/80 px-3 py-2 text-xs text-emerald-800 ring-1 ring-emerald-100">
                暂无可展示的订单预览，但任务状态已经允许上传 ERP。
              </div>
            )}
          </div>
        );
      }
      if (ui.type === "draft_result") {
        const draftNo = String(ui.data.draft_no ?? "");
        const draftUrl = String(ui.data.draft_url ?? "");
        return (
          <div className="mt-3 rounded-xl border border-sky-200 bg-sky-50/80 p-3 text-sm text-sky-950">
            <div className="font-semibold">ERP 草稿已创建</div>
            {draftNo ? <div className="mt-1 font-mono">{draftNo}</div> : null}
            {draftUrl ? (
              <a className="mt-2 inline-block font-medium text-sky-700 underline" href={draftUrl} target="_blank">
                打开草稿
              </a>
            ) : null}
          </div>
        );
      }
      if (ui.type === "erp_query_result") {
        const toolsUsed = Array.isArray(ui.data.tools_used) ? ui.data.tools_used.map((x) => String(x)) : [];
        return (
          <div className="mt-3 rounded-xl border border-indigo-200 bg-indigo-50/80 p-3 text-sm text-indigo-950">
            <div className="font-semibold">ERP 查询已完成</div>
            {toolsUsed.length > 0 ? (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {toolsUsed.map((tool) => (
                  <span
                    key={tool}
                    className="rounded-md border border-indigo-200 bg-white/80 px-2 py-1 font-mono text-[11px] text-indigo-800"
                  >
                    {tool}
                  </span>
                ))}
              </div>
            ) : (
              <div className="mt-1 text-indigo-800">已通过 ERP 查询工具返回结果。</div>
            )}
          </div>
        );
      }
      if (ui.type === "processing") {
        const status = String(ui.data.status ?? "UPLOADED") as IngestionStatus;
        const previewReady = Boolean(ui.data.preview_ready);
        const progressState = pdfToErpProgressState(status, previewReady);
        const percent = pdfToErpProgressPercent(status, previewReady);
        const fileName = String(ui.data.file_name ?? "");
        const progressLabel =
          progressState === "done"
            ? "订单预览已生成"
            : progressState === "failed"
              ? "处理失败"
              : progressState === "canceled"
                ? "任务已取消"
                : ingestionStatusLabelZh(status);
        return (
          <div className="mt-3 w-full max-w-md rounded-lg border border-sky-100 bg-sky-50/80 px-3 py-2 text-sm text-sky-950">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 font-semibold">
                  {progressState === "running" ? (
                    <ProgressSpinner className="text-sky-700" />
                  ) : (
                    <span
                      className={[
                        "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[11px] font-bold",
                        progressState === "done"
                          ? "bg-emerald-600 text-white"
                          : progressState === "failed"
                            ? "bg-red-600 text-white"
                            : "bg-slate-400 text-white",
                      ].join(" ")}
                    >
                      {progressState === "done" ? "√" : "!"}
                    </span>
                  )}
                  <span>PDF 转 ERP</span>
                </div>
                <div className="mt-0.5 truncate text-xs text-sky-700">
                  {fileName || ingestionStatusLabelZh(status)}
                </div>
              </div>
              <span className="shrink-0 rounded-full bg-white/80 px-2 py-0.5 text-xs font-semibold text-sky-700 ring-1 ring-sky-100">
                {ingestionStatusShortLabel(status)}
              </span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white">
              <div className="h-full rounded-full bg-sky-500 transition-[width] duration-500" style={{ width: `${percent}%` }} />
            </div>
            <div className="mt-1 flex items-center justify-between gap-3 text-[11px] text-sky-700">
              <span className="truncate">{progressLabel}</span>
              <span className="font-mono">{percent}%</span>
            </div>
          </div>
        );
      }
      if (ui.type === "error") {
        return (
          <div
            className={[
              "mt-3 rounded-xl border border-red-200 bg-red-50/80 p-3 text-sm text-red-950",
              String(ui.data.error_code ?? "UNKNOWN") === "UNSUPPORTED_DOCUMENT" ? "[&>div:first-child]:hidden" : "",
            ].join(" ")}
          >
            <div className="font-semibold">工具执行失败</div>
            <div className={String(ui.data.error_code ?? "UNKNOWN") === "UNSUPPORTED_DOCUMENT" ? "text-sm" : "mt-1 font-mono text-xs"}>
              {String(ui.data.error_code ?? "UNKNOWN") === "UNSUPPORTED_DOCUMENT"
                ? "当前文件非采购订单，已停止处理。请重新上传采购订单"
                : String(ui.data.error_code ?? "UNKNOWN")}
            </div>
          </div>
        );
      }
      return null;
    },
    [
      ingestion,
      ingestionId,
      confirmingPreviewIngestionIds,
      creatingDraftIngestionIds,
      confirmedPreviewIds,
      ingestionsById,
      isConfirmingPreview,
      isCreatingDraft,
      isUploading,
      isPreviewConfirmed,
      isPreviewDirty,
      isResolving,
      onCancelReprocessUpload,
      onConfirmPreview,
      onConfirmPreviewTask,
      onConfirmReprocessUpload,
      onCreateDraft,
      onCreateDraftTask,
      onPreviewDraftChangeTask,
      onPreviewDraftChange,
      onResolveTask,
      onResolve,
      previewDirtyByIngestion,
      previewDraft,
      previewDraftsByIngestion,
      resolvingIngestionIds,
      resolveFields,
      resolveFieldsByIngestion,
    ],
  );

  return (
    <div className="flex h-svh max-h-svh flex-col overflow-hidden bg-[#f5f6f8] text-slate-900">
      <div ref={chatPanelRef} id="chat-intent-panel" className="flex min-h-0 flex-1 flex-row overflow-hidden pl-4 md:pl-6">
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-white">
          <div className="shrink-0 bg-white px-5 py-3 lg:px-7">
            <div className="flex items-center justify-end">
              <button
                type="button"
                onClick={() => void onClearCurrentPage()}
                className="inline-flex h-9 items-center gap-2 rounded-lg bg-[#2248b8] px-4 text-sm font-semibold text-white shadow-sm transition hover:bg-[#1b3fa8] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-300 focus-visible:ring-offset-2"
              >
                <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
                  <rect x="4" y="4" width="16" height="16" rx="1.5" />
                  <path d="M9 4v16M4 9h5M4 15h5M13 12h4M15 10v4" strokeLinecap="round" />
                </svg>
                刷新页面
              </button>
            </div>
          </div>

          <div
            className="relative flex min-h-0 flex-1 flex-col"
            onDragEnter={(e) => {
              if (workspaceMode !== "pdf_to_erp") return;
              e.preventDefault();
              e.stopPropagation();
              dragDepthRef.current += 1;
              if (dragDepthRef.current === 1) setIsDragging(true);
            }}
            onDragOver={(e) => {
              if (workspaceMode !== "pdf_to_erp") return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "copy";
            }}
            onDragLeave={(e) => {
              if (workspaceMode !== "pdf_to_erp") return;
              e.preventDefault();
              e.stopPropagation();
              dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
              if (dragDepthRef.current === 0) setIsDragging(false);
            }}
            onDrop={(e) => void onDrop(e)}
          >
            {isDragging ? (
              <div className="pointer-events-none absolute inset-2 z-20 flex items-center justify-center rounded-2xl border-2 border-dashed border-sky-500 bg-sky-50/95 text-center shadow-lg backdrop-blur-sm">
                <p className="px-4 text-base font-medium text-sky-900">松开即可上传并解析文档</p>
              </div>
            ) : null}

            <div
              ref={chatScrollRef}
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain bg-white"
            >
              <div
                className={[
                  "flex min-h-full w-full flex-col gap-4 px-5 pt-4 lg:px-7",
                  workspaceMode === "pdf_to_erp" ? "pb-28 sm:pb-32" : "pb-4",
                ].join(" ")}
              >
                <div className="flex min-h-full w-full flex-col gap-3">
                  {workspaceMode !== "pdf_to_erp" ? (
                    <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                      <div className="text-base font-semibold text-slate-950">对话记录</div>
                      <div className="text-xs text-slate-400">{chatMessages.length ? `${chatMessages.length} 条消息` : ""}</div>
                    </div>
                  ) : null}
                  {workspaceMode === "assistant" && chatMessages.length === 0 ? (
                    <div className="mx-auto flex min-h-[18rem] w-full max-w-2xl flex-col items-center justify-center text-center">
                      <div className="text-lg font-semibold tracking-tight text-slate-900">
                        普通对话 / ERP库存查询窗口
                      </div>
                      <p className="mt-3 max-w-xl text-sm leading-6 text-slate-500">
                        {workspaceMode !== "assistant"
                          ? "请上传 PDF 文件开始识别和生成 ERP 草稿；此模式不接收文字对话。"
                          : "可直接输入普通问题，也可以查询 ERP 库存、供应商、物料等业务数据。"}
                      </p>
                    </div>
                  ) : null}
                  {chatMessages.map((m) => {
                    const isUser = m.role === "user";
                    const showProgressSpinner = m.role === "system" && isBackgroundRunningStatus(m.progressStatus);
                    const hasMessageContent = m.content.trim().length > 0;
                    const isWideToolCard = Boolean(
                      m.toolUi &&
                        [
                          "missing_fields_form",
                          "upload_confirm",
                          "erp_query_result",
                          "draft_result",
                          "error",
                        ].includes(m.toolUi.type),
                    );
                    return (
                      <div
                        key={m.id}
                        className={[
                          "flex w-full [contain:layout_paint_style] [content-visibility:auto] [contain-intrinsic-size:96px]",
                          isUser ? "justify-end" : "justify-start",
                        ].join(" ")}
                      >
                        <div
                          className={[
                            isWideToolCard
                              ? "max-w-[min(96%,58rem)] rounded-lg px-4 py-3 text-left text-sm leading-relaxed ring-1"
                              : "max-w-[min(92%,32rem)] rounded-lg px-4 py-3 text-left text-sm leading-relaxed ring-1",
                            isUser
                              ? "bg-blue-600 text-white ring-blue-700/25"
                              : m.role === "system"
                                ? "bg-amber-50 text-amber-950 ring-amber-200"
                                : "bg-white text-slate-900 ring-slate-200/90",
                          ].join(" ")}
                        >
                          <div
                            className={[
                              "text-[11px] font-semibold uppercase tracking-wide",
                              isUser
                                ? "text-sky-100"
                                : m.role === "system"
                                  ? "text-amber-700/90"
                                  : "text-slate-400",
                            ].join(" ")}
                          >
                            {isUser ? "客户" : m.role === "assistant" ? "助手" : "系统"}
                            {m.createdAt ? (
                              <span
                                className={
                                  isUser
                                    ? "ml-1 font-normal normal-case text-sky-100/90"
                                    : "ml-1 font-normal normal-case text-slate-400"
                                }
                              >
                                {isUser ? "· " : " · "}
                                {new Date(m.createdAt).toLocaleTimeString("zh-CN", {
                                  hour: "2-digit",
                                  minute: "2-digit",
                                })}
                              </span>
                            ) : (
                              <span
                                className={
                                  isUser
                                    ? "ml-1 font-normal normal-case text-sky-100/90"
                                    : "ml-1 font-normal normal-case text-slate-400"
                                }
                              >
                                · 引导
                              </span>
                            )}
                          </div>
                          {hasMessageContent ? (
                            <div
                              className={[
                                "mt-1.5 whitespace-pre-wrap leading-relaxed",
                                showProgressSpinner ? "flex items-start gap-2" : "",
                                isUser ? "text-white" : "",
                              ].join(" ")}
                            >
                              {showProgressSpinner ? <ProgressSpinner className="mt-1 text-amber-700" /> : null}
                              <span>{m.content}</span>
                            </div>
                          ) : null}
                          {renderToolUi(m.toolUi)}
                        </div>
                      </div>
                    );
                  })}
                  <div ref={chatEndRef} className="h-0 shrink-0" aria-hidden />
                </div>
                {workspaceMode === "pdf_to_erp" ? (
                  <section className="hidden">
                    <div className="border-b border-slate-100 px-5 py-4 text-base font-semibold text-slate-950">
                      上传 PDF
                    </div>
                    <button
                      type="button"
                      disabled={isUploading}
                      onClick={() => fileInputRef.current?.click()}
                      className="m-5 flex min-h-[10.5rem] w-[calc(100%-2.5rem)] flex-col items-center justify-center rounded-lg border border-dashed border-slate-300 bg-slate-50 text-center transition hover:border-blue-400 hover:bg-blue-50/40 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <svg className="h-10 w-10 text-blue-700" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
                        <path d="M4 16.5V18a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1.5" strokeLinecap="round" />
                        <path d="M12 4v12m0-12 4 4m-4-4-4 4" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                      <div className="mt-4 text-sm font-semibold text-slate-950">
                        {isUploading ? "正在上传并创建任务..." : "点击或拖拽 PDF 文件到此处上传"}
                      </div>
                      <div className="mt-2 text-sm text-slate-400">支持 PDF，单个文件建议不超过 29MB</div>
                    </button>
                  </section>
                ) : null}
                {/*
                <details
                  className="w-full rounded-xl border border-slate-200/90 bg-white/70 px-3 py-2 shadow-sm [contain:layout_paint_style] [content-visibility:auto] [contain-intrinsic-size:520px]"
                >
                  <summary className="cursor-pointer list-none text-sm font-semibold text-slate-800 [&::-webkit-details-marker]:hidden">
                    <span>当前任务详情</span>
                    <span className="ml-2 font-normal text-slate-500">
                      {ingestionId
                        ? `${displayIngestionStatus(ingestion, clientDraftStateRef.current) ?? ingestion?.status ?? "处理中"} · ${ingestionId}`
                        : "上传文件后可查看解析细节"}
                    </span>
                  </summary>
                  <div className="mt-3 space-y-4">
            <div className="w-full rounded-2xl border border-slate-200/90 bg-white p-4 shadow-sm shadow-slate-200/30 ring-1 ring-slate-900/[0.03] sm:p-5">
            <div className="text-base font-semibold text-slate-900">任务状态</div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-600">
              <span>
                任务编号：{" "}
                <span className="font-mono text-slate-900">{ingestionId ?? "（尚未上传）"}</span>
              </span>
              {ingestionId ? (
                <>
                  <button
                    type="button"
                    className="rounded border border-slate-200 bg-white px-2 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                    onClick={() => copyToClipboard("ingestion_id", ingestionId)}
                  >
                    {copiedTag === "ingestion_id" ? "已复制" : "复制"}
                  </button>
                </>
              ) : null}
            </div>
            {ingestionHistory.length > 0 ? (
              <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50/90 p-3 text-sm text-slate-700">
                <div className="font-semibold text-slate-800">最近解析任务</div>
                <p className="mt-1 text-xs text-slate-500">
                  本会话内曾上传的任务编号（刷新页面后仍尝试从浏览器 session 恢复）。当前「任务状态」只跟踪最后一次上传。
                </p>
                <ul className="mt-2 max-h-52 space-y-2 overflow-y-auto text-xs">
                  {ingestionHistory.map((h) => (
                    <li key={h.id} className="rounded-md bg-white/90 px-2 py-1.5 ring-1 ring-slate-200/80">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <span className="break-all font-mono text-slate-900">{h.id}</span>
                        <button
                          type="button"
                          className="shrink-0 rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-50"
                          onClick={() => copyToClipboard(`hist_${h.id}`, h.id)}
                        >
                          {copiedTag === `hist_${h.id}` ? "已复制" : "复制"}
                        </button>
                      </div>
                      <div className="mt-0.5 text-slate-600">{h.fileName}</div>
                      <div className="font-mono text-[11px] text-slate-400">{h.status}</div>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {documentJsonUrls ? (
              <div className="mt-4 rounded-xl border border-sky-200/90 bg-sky-50/50 p-3 ring-1 ring-sky-100 sm:p-4">
                <div className="text-sm font-semibold text-sky-950">解析结果 JSON 接口（集成用）</div>
                <p className="mt-1.5 text-xs leading-relaxed text-sky-900/90">
                  稳定结构见响应里的 <span className="font-mono">schema_version</span>、
                  <span className="font-mono">extracted_fields</span>、<span className="font-mono">line_items</span>
                  等。不同公司版式差异大时，请用顶部「解析规则编号」挂档案，或接受部分字段需人工补全。
                  当前接口为 GET、无鉴权（生产环境请自行加网关或 Token）。
                </p>
                <div className="mt-2 space-y-2">
                  <div>
                    <div className="text-[11px] font-medium uppercase tracking-wide text-sky-800/80">标准（预览 + 抽取字段）</div>
                    <div className="mt-1 flex min-w-0 flex-col gap-1.5 sm:flex-row sm:items-center">
                      <code className="min-w-0 flex-1 break-all rounded-lg bg-white px-2 py-1.5 text-[11px] leading-snug text-slate-800 ring-1 ring-slate-200">
                        {documentJsonUrls.standard}
                      </code>
                      <div className="flex shrink-0 flex-wrap gap-1.5">
                        <button
                          type="button"
                          className="rounded-md border border-sky-300 bg-white px-2 py-1 text-xs font-medium text-sky-900 hover:bg-sky-50"
                          onClick={() => copyToClipboard("document_json", documentJsonUrls.standard)}
                        >
                          {copiedTag === "document_json" ? "已复制" : "复制 URL"}
                        </button>
                        <a
                          href={documentJsonUrls.standard}
                          target="_blank"
                          rel="noreferrer"
                          className="rounded-md border border-sky-300 bg-sky-600 px-2 py-1 text-xs font-medium text-white hover:bg-sky-700"
                        >
                          新标签打开
                        </a>
                      </div>
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] font-medium uppercase tracking-wide text-sky-800/80">含全文（大文件可能截断）</div>
                    <div className="mt-1 flex min-w-0 flex-col gap-1.5 sm:flex-row sm:items-center">
                      <code className="min-w-0 flex-1 break-all rounded-lg bg-white px-2 py-1.5 text-[11px] leading-snug text-slate-800 ring-1 ring-slate-200">
                        {documentJsonUrls.fullText}
                      </code>
                      <div className="flex shrink-0 flex-wrap gap-1.5">
                        <button
                          type="button"
                          className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-800 hover:bg-slate-50"
                          onClick={() => copyToClipboard("document_json_full", documentJsonUrls.fullText)}
                        >
                          {copiedTag === "document_json_full" ? "已复制" : "复制 URL"}
                        </button>
                        <a
                          href={documentJsonUrls.fullText}
                          target="_blank"
                          rel="noreferrer"
                          className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-800 hover:bg-slate-50"
                        >
                          新标签打开
                        </a>
                      </div>
                    </div>
                  </div>
                  <div>
                    <div className="text-[11px] font-medium uppercase tracking-wide text-sky-800/80">最终 ERP JSON（order + details）</div>
                    <div className="mt-1 flex min-w-0 flex-col gap-1.5 sm:flex-row sm:items-center">
                      <code className="min-w-0 flex-1 break-all rounded-lg bg-white px-2 py-1.5 text-[11px] leading-snug text-slate-800 ring-1 ring-slate-200">
                        {documentJsonUrls.erpPayload}
                      </code>
                      <div className="flex shrink-0 flex-wrap gap-1.5">
                        <button
                          type="button"
                          className="rounded-md border border-emerald-300 bg-white px-2 py-1 text-xs font-medium text-emerald-900 hover:bg-emerald-50"
                          onClick={() => copyToClipboard("erp_payload_json", documentJsonUrls.erpPayload)}
                        >
                          {copiedTag === "erp_payload_json" ? "已复制" : "复制 URL"}
                        </button>
                        <a
                          href={documentJsonUrls.erpPayload}
                          target="_blank"
                          rel="noreferrer"
                          className="rounded-md border border-emerald-300 bg-emerald-700 px-2 py-1 text-xs font-medium text-white hover:bg-emerald-800"
                        >
                          新标签打开
                        </a>
                      </div>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                    <button
                      type="button"
                      className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50"
                      onClick={() => copyToClipboard("document_json_curl", documentJsonUrls.curlStandard)}
                    >
                      {copiedTag === "document_json_curl" ? "已复制" : "复制 curl 示例"}
                    </button>
                    <code className="hidden text-[10px] text-slate-500 sm:inline">{documentJsonUrls.curlStandard}</code>
                  </div>
                </div>
              </div>
            ) : null}
            {ingestion?.file_hash ? (
              <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-600">
                <span className="min-w-0">
                  file_hash：
                  <span className="break-all font-mono text-slate-900">{ingestion.file_hash}</span>
                </span>
                <button
                  type="button"
                  className="shrink-0 rounded border border-slate-200 bg-white px-2 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  onClick={() => copyToClipboard("file_hash", ingestion.file_hash)}
                >
                  {copiedTag === "file_hash" ? "已复制" : "复制"}
                </button>
              </div>
            ) : null}
            {ingestion?.source_file_name ? (
              <div className="mt-1 text-sm text-slate-600">
                源文件：<span className="font-mono text-slate-900">{ingestion.source_file_name}</span>
              </div>
            ) : null}

            <div className="mt-3 flex flex-wrap gap-2">{pipelineUi}</div>

            {ingestion ? (
              <div className="mt-4 space-y-2 text-sm text-slate-700">
                <div>
                  当前进度：
                  <span className="font-semibold">
                    {ingestionStatusLabelZh(displayIngestionStatus(ingestion, clientDraftStateRef.current) ?? ingestion.status)}
                  </span>
                  <span className="ml-1.5 font-mono text-xs font-normal text-slate-400">
                    {displayIngestionStatus(ingestion, clientDraftStateRef.current) ?? ingestion.status}
                  </span>
                </div>
                {ingestion.doc_type_hint ? (
                  <div>
                    系统判定的单据类型：<span className="font-mono font-semibold">{ingestion.doc_type_hint}</span>
                  </div>
                ) : null}
                {ingestion.extraction_profile_id ||
                ingestion.extraction_profile_resolution ||
                ingestion.extraction_profile_requested ? (
                  <div className="space-y-0.5">
                    {ingestion.extraction_profile_id ? (
                      <div>
                        使用的专用规则：{" "}
                        <span className="font-mono font-semibold">{ingestion.extraction_profile_id}</span>
                      </div>
                    ) : null}
                    {ingestion.extraction_profile_resolution ? (
                      <div>
                        规则怎么选的：<span className="font-mono text-xs">{ingestion.extraction_profile_resolution}</span>
                        <span className="text-slate-500">（一般不用管，交给系统自动即可）</span>
                      </div>
                    ) : null}
                    {ingestion.extraction_profile_requested &&
                    ingestion.extraction_profile_requested !== ingestion.extraction_profile_id ? (
                      <div className="text-amber-800">
                        您填的规则编号{" "}
                        <span className="font-mono">{ingestion.extraction_profile_requested}</span>{" "}
                        未找到，已改用系统自动选择。
                      </div>
                    ) : null}
                  </div>
                ) : null}
                {(ingestion.parsed_char_count ?? 0) > 0 ||
                ingestion.extract_preview ||
                ingestion.parse_format_label ? (
                  <div className="rounded-md bg-slate-50 p-3 ring-1 ring-slate-200">
                    <div className="font-semibold text-slate-800">从文件里读出的内容</div>
                    <div className="mt-1 text-slate-600">
                      大约读出 <span className="font-mono">{ingestion.parsed_char_count ?? 0}</span> 个字符
                      {ingestion.parse_format_label ? (
                        <>
                          {" "}
                          · 识别方式：<span className="font-mono text-xs">{ingestion.parse_format_label}</span>
                        </>
                      ) : null}
                    </div>
                    {ingestion.extract_preview ? (
                      <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-slate-700">
                        {ingestion.extract_preview}
                      </pre>
                    ) : null}
                  </div>
                ) : null}
                <div>
                  还缺这些信息：
                  <span className="font-mono">{ingestion.missing_fields.join(", ") || "（无）"}</span>
                </div>
                <div>
                  版本信息（技术）：extract={ingestion.extract_version} / model={ingestion.model_version} / prompt=
                  {ingestion.prompt_version}
                </div>
                <div>
                  文件在服务器上的位置：
                  <span className="font-mono">
                    {ingestion.source_file_object_key ? ingestion.source_file_object_key : "（未上传对象存储，部分环境仍可处理）"}
                  </span>
                </div>
                {ingestion.draft_no ? (
                  <div className="rounded-md bg-emerald-50 p-3 ring-1 ring-emerald-200">
                    <div className="font-semibold text-emerald-900">草稿号：{ingestion.draft_no}</div>
                    {ingestion.draft_url ? (
                      <a className="mt-1 block break-all text-sky-700 underline" href={ingestion.draft_url}>
                        {ingestion.draft_url}
                      </a>
                    ) : null}
                  </div>
                ) : null}
                <ErrorDetailsCard ingestion={ingestion} />

                {ingestion.erp_call_log && ingestion.erp_call_log.length > 0 ? (
                  <details className="rounded-md border border-violet-200 bg-violet-50 p-3">
                    <summary className="cursor-pointer font-semibold text-violet-950">与业务系统往来（供排错）</summary>
                    <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words text-xs text-violet-900">
                      {JSON.stringify(ingestion.erp_call_log, null, 2)}
                    </pre>
                  </details>
                ) : null}

                <details className="rounded-md bg-slate-50 p-3 ring-1 ring-slate-200">
                  <summary className="cursor-pointer font-semibold text-slate-800">处理过程明细（可展开）</summary>
                  <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-slate-700">
                    {JSON.stringify(ingestion.audit_events, null, 2)}
                  </pre>
                </details>
              </div>
            ) : (
              <div className="mt-3 text-base text-slate-500">上传文件后，这里会显示处理进度。</div>
            )}

            {devInternalEnabled && ingestionId ? (
              <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
                <div className="font-semibold">仅开发人员使用</div>
                <div className="mt-1">
                  若本机没有开自动处理，任务可能一直停在「已上传」。可点下面按钮模拟后台往前推一步（平时不用管）。
                </div>
                <button
                  type="button"
                  className="mt-2 rounded-md bg-amber-900 px-3 py-2 text-sm font-semibold text-white hover:bg-amber-800"
                  onClick={() => void onDevProcess()}
                >
                  手动往前推进一步
                </button>
              </div>
            ) : null}
          </div>

          {hasOrderPreview && previewDraft ? (
            <OrderPreviewEditor
              preview={previewDraft}
              editableFields={ingestion?.editable_fields ?? []}
              issues={ingestion?.issues ?? []}
              onChange={onPreviewDraftChange}
              onConfirm={onConfirmPreview}
              onCreateDraft={onCreateDraft}
              confirming={isConfirmingPreview}
              creatingDraft={isCreatingDraft}
              createDraftDisabled={
                !ingestionId ||
                displayIngestionStatus(ingestion, clientDraftStateRef.current) !== "VALIDATED" ||
                !isPreviewConfirmed ||
                isPreviewDirty
              }
              lockedSalesUser={userName}
              hideCreateDraftAction
            />
          ) : (
          <div className="w-full rounded-2xl border border-slate-200/90 bg-white p-4 shadow-sm shadow-slate-200/30 ring-1 ring-slate-900/[0.03] sm:p-5">
            <div className="text-base font-semibold text-slate-900">补全空白项</div>
            <div className="mt-2 text-sm text-slate-600">
              当下方提示「需要您补充」时，把空项填好再点「保存补全」。若已显示可以生成草稿，可跳过这步。
            </div>
            {ingestion?.vendor_candidates?.length ||
            ingestion?.material_candidates?.length ||
            ingestion?.warehouse_candidates?.length ||
            ingestion?.tax_code_candidates?.length ? (
              <div className="mt-3 rounded-md bg-sky-50 p-3 text-sm ring-1 ring-sky-200">
                <div className="font-semibold text-sky-950">系统里搜到的候选（供参考）</div>
                {ingestion.vendor_candidates?.length ? (
                  <ul className="mt-2 list-inside list-disc text-sky-900">
                    {ingestion.vendor_candidates.map((v) => (
                      <li key={v.vendor_code}>
                        <span className="font-mono">{v.vendor_code}</span> {v.vendor_name}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {ingestion.material_candidates?.length ? (
                  <ul className="mt-2 list-inside list-disc text-sky-900">
                    {ingestion.material_candidates.map((m) => (
                      <li key={m.material_code}>
                        <span className="font-mono">{m.material_code}</span> {m.material_name}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {ingestion.warehouse_candidates?.length ? (
                  <ul className="mt-2 list-inside list-disc text-sky-900">
                    {ingestion.warehouse_candidates.map((w) => (
                      <li key={w.warehouse_code}>
                        <span className="font-mono">{w.warehouse_code}</span> {w.warehouse_name}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {ingestion.tax_code_candidates?.length ? (
                  <ul className="mt-2 list-inside list-disc text-sky-900">
                    {ingestion.tax_code_candidates.map((t) => (
                      <li key={t.tax_code}>
                        <span className="font-mono">{t.tax_code}</span> {t.tax_name}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
            {ingestion?.vendor_candidates?.length ? (
              <datalist id="erp-vendor-candidates">
                {ingestion.vendor_candidates.map((v) => (
                  <option key={v.vendor_code} value={v.vendor_code} label={v.vendor_name} />
                ))}
              </datalist>
            ) : null}
            {ingestion?.material_candidates?.length ? (
              <datalist id="erp-material-candidates">
                {ingestion.material_candidates.map((m) => (
                  <option key={m.material_code} value={m.material_code} label={m.material_name} />
                ))}
              </datalist>
            ) : null}
            {ingestion?.warehouse_candidates?.length ? (
              <datalist id="erp-warehouse-candidates">
                {ingestion.warehouse_candidates.map((w) => (
                  <option key={w.warehouse_code} value={w.warehouse_code} label={w.warehouse_name} />
                ))}
              </datalist>
            ) : null}
            {ingestion?.tax_code_candidates?.length ? (
              <datalist id="erp-tax-code-candidates">
                {ingestion.tax_code_candidates.map((t) => (
                  <option key={t.tax_code} value={t.tax_code} label={t.tax_name} />
                ))}
              </datalist>
            ) : null}
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {(ingestion ? resolveFieldKeys(ingestion.doc_type_hint, ingestion) : resolveFieldKeys("PO", null)).map(
                (key) => (
                <label key={key} className="text-sm text-slate-600">
                  <span className="flex flex-col gap-0.5">
                    <span className="font-medium text-slate-800">{RESOLVE_FIELD_LABELS[key] ?? key}</span>
                    <span className="font-mono text-xs font-normal text-slate-400">{key}</span>
                  </span>
                  <input
                    value={resolveFields[key] ?? ""}
                    onChange={(e) =>
                      setResolveFields((prev) => ({
                        ...prev,
                        [key]: e.target.value,
                      }))
                    }
                    className="mt-1.5 w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-base outline-none transition placeholder:text-slate-400 focus:border-sky-300 focus:ring-2 focus:ring-sky-200/80"
                    placeholder={key === "currency" ? "例如 CNY" : key.includes("date") ? "例如 2026-04-28" : ""}
                    list={
                      key === "vendor_code" && ingestion?.vendor_candidates?.length
                        ? "erp-vendor-candidates"
                        : key === "material_code" && ingestion?.material_candidates?.length
                          ? "erp-material-candidates"
                          : key === "warehouse_code" && ingestion?.warehouse_candidates?.length
                            ? "erp-warehouse-candidates"
                            : key === "tax_code" && ingestion?.tax_code_candidates?.length
                              ? "erp-tax-code-candidates"
                              : undefined
                    }
                  />
                </label>
              ))}
            </div>
            <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
              <button
                type="button"
                disabled={!ingestionId || !ingestion || isResolving}
                onClick={() => void onResolve()}
                className="rounded-xl bg-slate-900 px-5 py-3 text-base font-semibold text-white shadow-sm transition hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              >
                保存补全
              </button>
              <button
                type="button"
                disabled={!ingestionId || isCreatingDraft}
                onClick={() => void onCreateDraft()}
                className="rounded-xl bg-emerald-700 px-5 py-3 text-base font-semibold text-white shadow-sm transition hover:bg-emerald-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              >
                生成草稿
              </button>
            </div>
          </div>
          )}
                  </div>
                </details>
                */}
              </div>
            </div>

            {workspaceMode === "pdf_to_erp" ? (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  className="sr-only"
                  accept={uploadAcceptAttr}
                  onChange={(e) => {
                    void handleFiles(e.target.files);
                    e.target.value = "";
                  }}
                />
                <div className="pointer-events-none absolute inset-x-0 bottom-0 z-10 px-4 pb-[calc(1rem+env(safe-area-inset-bottom))] sm:px-6 lg:px-8">
                  <div className="pointer-events-auto mx-auto w-full max-w-[58rem]">
                    <button
                      type="button"
                      disabled={isUploading}
                      onClick={() => fileInputRef.current?.click()}
                      className="flex min-h-[4.5rem] w-full items-center gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-left text-sm text-slate-700 shadow-[0_10px_30px_rgba(15,23,42,0.12)] transition hover:border-blue-300 hover:bg-blue-50/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-100 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-700 ring-1 ring-blue-100">
                        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
                          <path d="M4 16.5V18a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-1.5" strokeLinecap="round" />
                          <path d="M12 4v12m0-12 4 4m-4-4-4 4" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-medium text-slate-900">
                          {isUploading ? "正在上传并创建任务..." : "选择 PDF 文件或拖拽到窗口上传"}
                        </span>
                        <span className="mt-0.5 block truncate text-xs text-slate-400">支持 PDF，单个文件建议不超过 29MB</span>
                      </span>
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <div className="shrink-0 border-t border-slate-200/90 bg-white px-5 pb-[env(safe-area-inset-bottom)] shadow-[0_-6px_24px_rgba(15,23,42,0.06)] lg:px-8">
                <div className="flex w-full max-w-none flex-col gap-2 py-3">
                  <div className="flex items-end gap-3">
                    <textarea
                      value={chatInput}
                      onChange={(e) => setChatInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          void onSendChat();
                        }
                      }}
                      rows={2}
                      className="min-h-[3.25rem] max-h-40 w-full flex-1 resize-y rounded-md border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm leading-6 text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-blue-400 focus:bg-white focus:ring-2 focus:ring-blue-100 disabled:bg-slate-100 disabled:text-slate-500"
                      placeholder={chatInputPlaceholder}
                      disabled={isChatSending}
                    />
                    <button
                      type="button"
                      disabled={isChatSending || !chatInput.trim()}
                      onClick={() => void onSendChat()}
                      className="h-[3.25rem] shrink-0 rounded-md bg-blue-700 px-5 text-sm font-medium text-white transition hover:bg-blue-600 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {isChatSending ? "发送中..." : "发送"}
                    </button>
                  </div>
                  <div className="px-1 text-xs text-slate-400">Enter 发送，Shift + Enter 换行</div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {logOpen ? (
        <>
          <button
            type="button"
            className="fixed inset-0 z-40 cursor-default bg-slate-900/25"
            aria-label="关闭日志遮罩"
            onClick={() => setLogOpen(false)}
          />
          <div className="fixed inset-x-0 bottom-0 z-50 flex max-h-[min(52vh,520px)] flex-col rounded-t-2xl border border-slate-200 bg-white shadow-[0_-12px_48px_rgba(15,23,42,0.15)]">
            <div className="flex shrink-0 items-center justify-between gap-2 border-b border-slate-100 px-4 py-2.5">
              <span className="text-base font-semibold text-slate-900">运行日志</span>
              <button
                type="button"
                onClick={() => setLogOpen(false)}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
              >
                关闭
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-2">
              <LogPanel className="shadow-none ring-0" />
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}

