/**
 * 与后端 FastAPI 返回结构对齐的前端类型定义（仅包含当前页面用到的字段）。
 * 后端字段扩展时，在此增量补充，避免 any 泛滥。
 */

/** 与后端 IngestionStatus 枚举字符串一致 */
export type IngestionStatus =
  | "UPLOADED"
  | "CLASSIFIED"
  | "PARSED"
  | "EXTRACTED"
  | "MAPPED"
  | "VALIDATED"
  | "NEED_USER_INPUT"
  | "DRAFT_CREATED"
  | "FAILED"
  | "CANCELED";

export interface AuditEvent {
  at: string;
  status: IngestionStatus;
  message: string;
}

export interface HealthResponse {
  status: string;
  erp_client_mode: string;
  erp_create_body_style?: string;
  queue_backend?: string;
  queue_name?: string;
  queue_available?: boolean;
  mineru_enabled?: boolean;
  mineru_api_base?: string;
  mineru_model?: string;
  llm_extract_enabled: boolean;
  llm_router_enabled: boolean;
  llm_api_key_configured: boolean;
  llm_model: string;
  llm_base_url: string;
  llm_prompt_version: string;
}

export interface CurrentUserResponse {
  userName: string;
  orgId: string;
  source: string;
}

export interface AssistantLlmProbeResponse {
  enabled: boolean;
  api_key_configured: boolean;
  model: string;
  base_url: string;
  ok: boolean;
  attempted: boolean;
  tool_name?: string | null;
  action?: string | null;
  arguments: Record<string, unknown>;
  reason: string;
  error?: string | null;
}

export interface OrderPreviewHeader {
  org: string;
  customerName: string;
  customerPoNo: string;
  salesUser: string;
  orderDate: string;
  orderStatus: string;
  deliveryAddr: string;
  rate?: number | null;
  currency: string;
  deliveryDate: string;
}

export interface OrderPreviewDetail {
  materialCode: string;
  productName: string;
  productSpec: string;
  ph: string;
  customerMaterialNo: string;
  qty?: number | null;
  price?: number | null;
  taxPrice?: number | null;
  amount?: number | null;
  allAmount?: number | null;
  tax?: number | null;
  taxAmount?: number | null;
  gift: boolean;
  remark: string;
}

export interface OrderPreviewData {
  order: OrderPreviewHeader;
  details: OrderPreviewDetail[];
}

export interface PreviewEditableField {
  path: string;
  label: string;
  current_value: string;
  required: boolean;
  reason: string;
  confidence: number;
}

export interface PreviewIssue {
  path: string;
  level: "info" | "warning" | "error" | string;
  message: string;
}

export interface IngestionResponse {
  ingestion_id: string;
  file_id: string;
  file_hash: string;
  user_id: string;
  org_id: string;
  source_file_object_key?: string | null;
  /** 后端返回的文件信息 */
  file?: { source_file_name?: string | null; source_file_object_key?: string | null };
  /** 上传时的原始文件名（无对象存储时仍用于解析扩展名与分类） */
  source_file_name?: string | null;
  extract_version: string;
  model_version: string;
  prompt_version: string;
  status: IngestionStatus;
  doc_type_hint?: "PO" | "GR" | "INV" | null;
  /** 后端生效的解析档案 id；无则走默认契约 */
  extraction_profile_id?: string | null;
  /** 请求里传的 extraction_profile_id（可能未命中文件） */
  extraction_profile_requested?: string | null;
  /** explicit | org_id | default | none */
  extraction_profile_resolution?: string | null;
  /** 当前单据类型下必填键顺序（含档案扩展），用于补全表单 */
  required_resolve_keys?: string[];
  missing_fields: string[];
  resolved_fields: Record<string, string>;
  audit_events: AuditEvent[];
  draft_no?: string | null;
  draft_url?: string | null;
  error_code?: string | null;
  /** 解析出的正文预览（仅部分格式有内容；无对象存储时通常为空） */
  extract_preview?: string | null;
  parsed_char_count?: number | null;
  /** 后端 document_extract 返回的 format_label，如 pdf_text、pdf_no_text_engine、docx_text、ocr_image(lang=eng) */
  parse_format_label?: string | null;
  /** ERP 映射阶段返回的候选（便于补全与审计） */
  vendor_candidates?: Array<Record<string, string>>;
  material_candidates?: Array<Record<string, string>>;
  warehouse_candidates?: Array<Record<string, string>>;
  tax_code_candidates?: Array<Record<string, string>>;
  /** validate_draft / create_draft 等摘要（元数据） */
  erp_call_log?: Array<Record<string, unknown>>;
  preview_data?: OrderPreviewData | null;
  editable_fields?: PreviewEditableField[];
  issues?: PreviewIssue[];
  error_details?: {
    category?: "master_data" | "permission" | "timeout" | "upstream_error" | string;
    erp_error_code?: string;
    erp_message?: string;
    erp_status_code?: number;
    upstream_request_id?: string | null;
    field_errors?: Array<Record<string, unknown>>;
    raw?: Record<string, unknown>;
    [key: string]: unknown;
  };
}

/** 与 ``GET /ingestions/{id}/document`` 对齐，供集成方拉取解析 JSON */
export interface DocumentParseExport {
  schema_version: string;
  ingestion_id: string;
  file_id: string;
  file_hash: string;
  org_id: string;
  user_id: string;
  status: string;
  doc_type_hint?: string | null;
  file?: { source_file_name?: string | null; source_file_object_key?: string | null };
  parse: {
    format_label?: string | null;
    char_count?: number | null;
    text_preview?: string | null;
    full_text?: string | null;
    full_text_truncated?: boolean;
  };
  extracted_fields: Record<string, string>;
  /** PO 表格明细行（与后端 ``line_items_json`` 展开一致） */
  line_items: Array<Record<string, string>>;
  missing_required_fields: string[];
  mapping_candidates: {
    vendor: Array<Record<string, string>>;
    material: Array<Record<string, string>>;
    warehouse: Array<Record<string, string>>;
    tax_code: Array<Record<string, string>>;
  };
  versions: Record<string, string>;
  error_code?: string | null;
  error_details?: Record<string, unknown>;
}

export interface UploadResponse {
  file_id: string;
  ingestion_id: string;
  status: IngestionStatus;
}

export interface ChatErpQaResponse {
  answer: string;
  erp_tools_used: string[];
}

export interface ToolUi {
  type: string;
  data: Record<string, unknown>;
}

export interface ToolResult {
  tool: string;
  status: string;
  message: string;
  ingestion_id?: string | null;
  ui?: ToolUi | null;
  ingestion?: IngestionResponse | null;
  draft?: CreateDraftResponse | null;
}

export interface ChatToolMessage {
  role: "assistant" | "system" | "user";
  content: string;
  ui?: ToolUi | null;
}

export interface ChatTaskState {
  type: string;
  ingestion_id?: string | null;
  status?: string | null;
}

export interface ChatMessageResponse {
  session_id?: string | null;
  messages: ChatToolMessage[];
  active_task?: ChatTaskState | null;
  tool_result?: ToolResult | null;
  ui?: ToolUi | null;
}

export interface AssistantSessionResponse {
  session_id: string;
  messages: ChatToolMessage[];
  active_task?: ChatTaskState | null;
  ui?: ToolUi | null;
}

export interface CreateDraftResponse {
  ingestion_id: string;
  status: IngestionStatus;
  draft_no: string;
  draft_url: string;
  idempotency_key: string;
}
