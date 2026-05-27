/**
 * 拖拽/选择上传策略：与后端 document_extract 支持的扩展名及路由体积上限对齐。
 * @see backend/api/app/document_extract.py
 * @see backend/api/app/routes.py _MAX_UPLOAD_BYTES
 */

/** 与 extract_text_from_bytes / guess_extension 一致（小写） */
export const SUPPORTED_UPLOAD_EXTENSIONS = new Set([
  "pdf",
  "txt",
  "csv",
  "md",
  "json",
  "xml",
  "log",
  "docx",
  "xlsx",
  "png",
  "jpg",
  "jpeg",
  "webp",
  "tif",
  "tiff",
  "bmp",
]);

/** 与 API upload_binary 413 阈值对齐（留少量余量给 multipart 开销） */
export const CLIENT_MAX_UPLOAD_BYTES = 29 * 1024 * 1024;

export function extensionOfFileName(fileName: string): string {
  const n = (fileName || "").trim().toLowerCase();
  if (!n.includes(".")) return "";
  return n.split(".").pop() || "";
}

export type UploadPrecheckResult = { ok: true } | { ok: false; message: string };

export function precheckUploadFile(file: File): UploadPrecheckResult {
  if (!file || !file.name) {
    return { ok: false, message: "未选择有效文件。" };
  }
  if (file.size > CLIENT_MAX_UPLOAD_BYTES) {
    return {
      ok: false,
      message: `文件过大（>${Math.floor(CLIENT_MAX_UPLOAD_BYTES / (1024 * 1024))}MB），请压缩或拆分后再传。`,
    };
  }
  const ext = extensionOfFileName(file.name);
  if (!ext || !SUPPORTED_UPLOAD_EXTENSIONS.has(ext)) {
    return {
      ok: false,
      message: `不支持的扩展名「.${ext || "无"}」。当前支持：${Array.from(SUPPORTED_UPLOAD_EXTENSIONS).sort().join(", ")}`,
    };
  }
  return { ok: true };
}
