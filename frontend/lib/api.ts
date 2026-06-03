/**
 * 与 Orchestrator API 通信的薄封装层。
 * - 统一 baseUrl、请求头（含 x-request-id）、错误解析；
 * - 每次请求/响应通过 clientLogger 记录，满足审计与排障要求。
 */

import { clientLogger } from "./client-logger";
import type {
  AssistantSessionResponse,
  AssistantLlmProbeResponse,
  ChatErpQaResponse,
  ChatMessageResponse,
  CreateDraftResponse,
  CurrentUserResponse,
  DocumentParseExport,
  HealthResponse,
  IngestionResponse,
  OrderPreviewData,
  UploadResponse,
} from "./types";

export type AssistantStreamEvent =
  | { event: "session"; data: { session_id: string } }
  | { event: "delta"; data: { content: string } }
  | { event: "final"; data: ChatMessageResponse }
  | { event: "error"; data: { message?: string } };

/**
 * 后端 API 根路径（不要末尾 `/`）。
 * - 未设置 `NEXT_PUBLIC_API_BASE_URL` 时默认走 **同源** `/api/orchestrator`（由 `next.config.mjs` rewrite 到 FastAPI），
 *   避免 CORS、以及用手机/局域网 IP 打开页面时误连「手机本机 127.0.0.1」。
 * - 生产或独立域名部署时请显式设置 `NEXT_PUBLIC_API_BASE_URL`。
 */
export function getApiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (raw) return raw.replace(/\/$/, "");
  return "/api/orchestrator";
}

export async function getHealth(): Promise<HealthResponse> {
  return apiFetchJson<HealthResponse>("/health", { method: "GET" });
}

export async function getCurrentUser(): Promise<CurrentUserResponse> {
  return apiFetchJson<CurrentUserResponse>("/current-user", { method: "GET" });
}

export async function postAssistantLlmProbe(body: {
  message: string;
  org_id: string;
  user_id?: string;
  active_task_id?: string | null;
}): Promise<AssistantLlmProbeResponse> {
  return apiFetchJson<AssistantLlmProbeResponse>("/assistant/llm-router/probe", {
    method: "POST",
    jsonBody: body,
  });
}

export async function postConfirmPreview(
  ingestionId: string,
  previewData: OrderPreviewData,
): Promise<IngestionResponse> {
  return apiFetchJson<IngestionResponse>(`/ingestions/${encodeURIComponent(ingestionId)}/confirm-preview`, {
    method: "POST",
    jsonBody: { preview_data: previewData },
  });
}

function newRequestId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random()}`;
}

async function parseJsonSafe(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return { _raw: text };
  }
}

/**
 * 通用 fetch：自动附加 JSON 头与 x-request-id，并记录日志。
 */
export async function apiFetchJson<T>(
  path: string,
  init: RequestInit & { jsonBody?: unknown } = {},
): Promise<T> {
  const base = getApiBaseUrl();
  const url = `${base}${path.startsWith("/") ? path : `/${path}`}`;
  const requestId = newRequestId();

  const headers = new Headers(init.headers);
  headers.set("x-request-id", requestId);
  if (init.jsonBody !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  clientLogger.info("发起 API 请求", {
    requestId,
    url,
    method: init.method ?? "GET",
    hasBody: init.jsonBody !== undefined,
  });

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      credentials: init.credentials ?? "include",
      headers,
      body: init.jsonBody !== undefined ? JSON.stringify(init.jsonBody) : init.body,
    });
  } catch (e) {
    const errPart =
      e instanceof Error
        ? { name: e.name, message: e.message, stack: e.stack?.slice(0, 2000) }
        : { value: String(e) };
    clientLogger.error("API 请求网络异常（未收到响应）", {
      requestId,
      url,
      hint: "若使用同源代理，请确认已 `npm run dev`（含 Next）且 ORCHESTRATOR_PROXY_TARGET 指向可达的 API",
      error: errPart,
    });
    throw e;
  }

  const payload = await parseJsonSafe(response);

  clientLogger.info("收到 API 响应", {
    requestId,
    url,
    status: response.status,
    ok: response.ok,
    responseRequestId: response.headers.get("x-request-id"),
  });

  if (!response.ok) {
    clientLogger.error("API 请求失败", { requestId, url, status: response.status, payload });
    const err = new Error(`API ${response.status}: ${JSON.stringify(payload)}`);
    (err as Error & { status?: number; payload?: unknown }).status = response.status;
    (err as Error & { status?: number; payload?: unknown }).payload = payload;
    throw err;
  }

  return payload as T;
}

export async function postUploads(body: {
  file_name: string;
  file_hash: string;
  user_id: string;
  org_id: string;
  extraction_profile_id?: string | null;
  extract_version?: string;
  model_version?: string;
  prompt_version?: string;
}): Promise<UploadResponse> {
  return apiFetchJson<UploadResponse>("/uploads", {
    method: "POST",
    jsonBody: body,
  });
}

/**
 * multipart 上传：由服务端读取文件并计算 SHA-256，再创建 ingestion。
 * 不设置 Content-Type（由浏览器为 FormData 自动带 boundary），但必须带 x-request-id。
 */
export async function postUploadBinary(
  file: File,
  userId: string,
  orgId: string,
  extractionProfileId?: string | null,
): Promise<UploadResponse> {
  const base = getApiBaseUrl();
  const url = `${base}/uploads/binary`;
  const requestId = newRequestId();

  const form = new FormData();
  form.append("file", file);
  form.append("user_id", userId);
  form.append("org_id", orgId);
  const prof = (extractionProfileId ?? "").trim();
  if (prof) {
    form.append("extraction_profile_id", prof);
  }

  clientLogger.info("发起 multipart 上传", {
    requestId,
    url,
    fileName: file.name,
    fileSize: file.size,
  });

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "x-request-id": requestId },
      body: form,
    });
  } catch (e) {
    const errPart =
      e instanceof Error
        ? { name: e.name, message: e.message, stack: e.stack?.slice(0, 2000) }
        : { value: String(e) };
    clientLogger.error("multipart 上传网络异常（未收到响应）", {
      requestId,
      url,
      hint: "多为 API 未启动、地址/NEXT_PUBLIC_API_BASE_URL 错误、HTTPS 页面请求 HTTP、或 CORS",
      error: errPart,
    });
    throw e;
  }

  const payload = await parseJsonSafe(response);

  clientLogger.info("multipart 上传响应", {
    requestId,
    status: response.status,
    ok: response.ok,
    responseRequestId: response.headers.get("x-request-id"),
  });

  if (!response.ok) {
    clientLogger.error("multipart 上传失败", { requestId, status: response.status, payload });
    const err = new Error(`API ${response.status}: ${JSON.stringify(payload)}`);
    (err as Error & { status?: number; payload?: unknown }).status = response.status;
    (err as Error & { status?: number; payload?: unknown }).payload = payload;
    throw err;
  }

  return payload as UploadResponse;
}

/** ERP 主数据问答：答案仅来自 ERP 查询接口返回的数据（当前无大模型润色）。 */
export async function postChatFile(
  file: File,
  userId: string,
  orgId: string,
  extractionProfileId?: string | null,
  sessionId?: string | null,
): Promise<ChatMessageResponse> {
  const base = getApiBaseUrl();
  const url = `${base}/chat/files`;
  const requestId = newRequestId();

  const form = new FormData();
  form.append("file", file);
  form.append("user_id", userId);
  form.append("org_id", orgId);
  const prof = (extractionProfileId ?? "").trim();
  if (prof) form.append("extraction_profile_id", prof);
  if (sessionId) form.append("session_id", sessionId);

  clientLogger.info("发起对话式文件上传", { requestId, url, fileName: file.name, fileSize: file.size });
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "x-request-id": requestId },
      body: form,
    });
  } catch (e) {
    clientLogger.error("对话式文件上传网络异常", { requestId, url, error: e });
    throw e;
  }

  const payload = await parseJsonSafe(response);
  clientLogger.info("对话式文件上传响应", {
    requestId,
    status: response.status,
    ok: response.ok,
    responseRequestId: response.headers.get("x-request-id"),
  });

  if (!response.ok) {
    clientLogger.error("对话式文件上传失败", { requestId, status: response.status, payload });
    const err = new Error(`API ${response.status}: ${JSON.stringify(payload)}`);
    (err as Error & { status?: number; payload?: unknown }).status = response.status;
    (err as Error & { status?: number; payload?: unknown }).payload = payload;
    throw err;
  }

  return payload as ChatMessageResponse;
}

export async function postChatErpQa(body: {
  message: string;
  org_id: string;
  user_id?: string;
}): Promise<ChatErpQaResponse> {
  return apiFetchJson<ChatErpQaResponse>("/chat/erp-qa", {
    method: "POST",
    jsonBody: body,
  });
}

export async function postChatMessage(body: {
  session_id?: string | null;
  message?: string;
  org_id: string;
  user_id?: string;
  active_task_id?: string | null;
  tool?: string;
  action?: "get_status" | "submit_missing_fields" | "confirm_preview" | "create_draft" | "cancel";
  fields?: Record<string, string>;
  preview_data?: OrderPreviewData | null;
}): Promise<ChatMessageResponse> {
  return apiFetchJson<ChatMessageResponse>("/chat/messages", {
    method: "POST",
    jsonBody: {
      tool: "pdf_to_erp",
      message: body.message ?? "",
      ...body,
    },
  });
}

export async function postAssistantMessage(body: {
  session_id?: string | null;
  message?: string;
  org_id: string;
  user_id?: string;
  active_task_id?: string | null;
  tool?: string;
  action?: "get_status" | "submit_missing_fields" | "confirm_preview" | "create_draft" | "cancel";
  fields?: Record<string, string>;
  preview_data?: OrderPreviewData | null;
}): Promise<ChatMessageResponse> {
  return apiFetchJson<ChatMessageResponse>("/assistant/messages", {
    method: "POST",
    jsonBody: {
      message: body.message ?? "",
      ...body,
    },
  });
}

function parseSseBlock(block: string): AssistantStreamEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) return null;
  const dataText = dataLines.join("\n");
  try {
    const data = JSON.parse(dataText);
    if (event === "session" || event === "delta" || event === "final" || event === "error") {
      return { event, data } as AssistantStreamEvent;
    }
  } catch (e) {
    clientLogger.warn("assistant stream event parse failed", { event, dataText, error: e });
  }
  return null;
}

export async function streamAssistantMessage(
  body: {
    session_id?: string | null;
    message?: string;
    org_id: string;
    user_id?: string;
    active_task_id?: string | null;
    tool?: string;
    action?: "get_status" | "submit_missing_fields" | "confirm_preview" | "create_draft" | "cancel";
    fields?: Record<string, string>;
    preview_data?: OrderPreviewData | null;
  },
  onEvent: (event: AssistantStreamEvent) => void,
): Promise<void> {
  const base = getApiBaseUrl();
  const url = `${base}/assistant/messages/stream`;
  const requestId = newRequestId();

  clientLogger.info("assistant stream request", {
    requestId,
    url,
    hasActiveTask: Boolean(body.active_task_id),
  });

  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "Accept": "text/event-stream",
      "Cache-Control": "no-cache",
      "Content-Type": "application/json",
      "x-request-id": requestId,
    },
    body: JSON.stringify({ message: body.message ?? "", ...body }),
  });

  if (!response.ok) {
    const payload = await parseJsonSafe(response);
    clientLogger.error("assistant stream failed", { requestId, status: response.status, payload });
    const err = new Error(`API ${response.status}: ${JSON.stringify(payload)}`);
    (err as Error & { status?: number; payload?: unknown }).status = response.status;
    (err as Error & { status?: number; payload?: unknown }).payload = payload;
    throw err;
  }

  if (!response.body) {
    throw new Error("assistant stream response body is empty");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      const event = parseSseBlock(block);
      if (event) onEvent(event);
    }
  }
  buffer += decoder.decode();
  const tail = buffer.trim();
  if (tail) {
    const event = parseSseBlock(tail);
    if (event) onEvent(event);
  }
}

export async function getAssistantSession(sessionId: string): Promise<AssistantSessionResponse> {
  return apiFetchJson<AssistantSessionResponse>(`/assistant/sessions/${encodeURIComponent(sessionId)}`, {
    method: "GET",
  });
}

export async function postAssistantFile(
  file: File,
  userId: string,
  orgId: string,
  extractionProfileId?: string | null,
  sessionId?: string | null,
  forceReprocess = false,
): Promise<ChatMessageResponse> {
  const base = getApiBaseUrl();
  const url = `${base}/assistant/files`;
  const requestId = newRequestId();

  const form = new FormData();
  form.append("file", file);
  form.append("user_id", userId);
  form.append("org_id", orgId);
  const prof = (extractionProfileId ?? "").trim();
  if (prof) form.append("extraction_profile_id", prof);
  if (sessionId) form.append("session_id", sessionId);
  if (forceReprocess) form.append("force_reprocess", "true");

  clientLogger.info("发起助手文件上传", { requestId, url, fileName: file.name, fileSize: file.size });
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "x-request-id": requestId },
      body: form,
    });
  } catch (e) {
    clientLogger.error("助手文件上传网络异常", { requestId, url, error: e });
    throw e;
  }

  const payload = await parseJsonSafe(response);
  clientLogger.info("助手文件上传响应", {
    requestId,
    status: response.status,
    ok: response.ok,
    responseRequestId: response.headers.get("x-request-id"),
  });

  if (!response.ok) {
    clientLogger.error("助手文件上传失败", { requestId, status: response.status, payload });
    const err = new Error(`API ${response.status}: ${JSON.stringify(payload)}`);
    (err as Error & { status?: number; payload?: unknown }).status = response.status;
    (err as Error & { status?: number; payload?: unknown }).payload = payload;
    throw err;
  }

  return payload as ChatMessageResponse;
}

export async function getIngestion(ingestionId: string): Promise<IngestionResponse> {
  return apiFetchJson<IngestionResponse>(`/ingestions/${encodeURIComponent(ingestionId)}`, {
    method: "GET",
  });
}

/** 拉取解析与抽取结果的稳定 JSON（集成/落库用）；大文件慎用 ``includeFullText``。 */
export async function getIngestionDocumentExport(
  ingestionId: string,
  opts?: { includeFullText?: boolean },
): Promise<DocumentParseExport> {
  const q = opts?.includeFullText ? "?include_full_text=true" : "";
  return apiFetchJson<DocumentParseExport>(
    `/ingestions/${encodeURIComponent(ingestionId)}/document${q}`,
    { method: "GET" },
  );
}

export async function postResolve(
  ingestionId: string,
  fields: Record<string, string>,
): Promise<IngestionResponse> {
  return apiFetchJson<IngestionResponse>(`/ingestions/${encodeURIComponent(ingestionId)}/resolve`, {
    method: "POST",
    jsonBody: { fields },
  });
}

export async function postCreateDraft(ingestionId: string): Promise<CreateDraftResponse> {
  // 后端 create-draft 当前无请求体；不传 Content-Type: application/json，避免部分代理对空 body 误判。
  return apiFetchJson<CreateDraftResponse>(`/ingestions/${encodeURIComponent(ingestionId)}/create-draft`, {
    method: "POST",
  });
}

export async function postCancelIngestion(ingestionId: string): Promise<IngestionResponse> {
  return apiFetchJson<IngestionResponse>(`/ingestions/${encodeURIComponent(ingestionId)}/cancel`, {
    method: "POST",
  });
}
