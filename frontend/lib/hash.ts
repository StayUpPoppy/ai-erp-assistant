/**
 * 文件哈希工具：用于可选的 JSON 版 ``POST /uploads``（前端自行提供 file_hash）。
 * 主页面默认已改为 multipart ``POST /uploads/binary``，本模块保留给特殊场景或自动化脚本复用。
 */

/**
 * 计算文件的 SHA-256（十六进制小写字符串）。
 * 依赖 Web Crypto API（HTTPS 或 localhost 环境通常可用）。
 */
export async function computeFileSha256Hex(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  const bytes = new Uint8Array(digest);
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
