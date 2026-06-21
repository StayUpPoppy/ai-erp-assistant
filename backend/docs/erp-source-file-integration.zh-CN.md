# ERP 订单源文件对接

## 存储与关联

- PDF 原件存放在 MinIO，PostgreSQL 的 ingestion 记录保存文件名、SHA-256、大小、类型、上传时间和 object key。
- ERP 订单只需保存 AI ERP 返回的 `ingestion_id`，不要保存 MinIO object key。
- 同一用户重复处理相同文件时 `ingestion_id` 和 `file_id` 保持稳定。

## 服务端配置

生产 `deploy/ecs-panel/prod.env`：

```env
OBJECT_STORAGE_REQUIRED=true
SOURCE_FILE_API_TOKEN=<独立高强度随机值>
```

可在服务器执行 `openssl rand -hex 32` 生成 Token。Token 只配置在 AI ERP API 和 ERP 后端，禁止放入 ERP 前端代码。

配置后重建 API，并确认 `/health` 返回：

```json
{
  "minio_configured": true,
  "object_storage_required": true,
  "source_file_api_enabled": true
}
```

## ERP 后端读取接口

```http
GET /integrations/erp/ingestions/{ingestion_id}/source-file
HEAD /integrations/erp/ingestions/{ingestion_id}/source-file
Authorization: Bearer <SOURCE_FILE_API_TOKEN>
```

服务端验证示例：

```bash
curl -I \
  -H "Authorization: Bearer $SOURCE_FILE_API_TOKEN" \
  "http://127.0.0.1:8020/integrations/erp/ingestions/<ingestion_id>/source-file"
```

正常响应包含 `Content-Type: application/pdf`、`Content-Disposition: inline`、`Content-Length`、`ETag` 和 `Accept-Ranges: bytes`。接口支持单段 `Range` 请求。

错误语义：

- `401`：Token 缺失或错误。
- `404`：ingestion、object key 或 MinIO 对象不存在。
- `416`：Range 不合法或超出文件范围。
- `503`：接口未配置 Token，或者对象存储不可用。

## ERP 按钮实现建议

ERP 后端应先按当前登录用户校验订单权限，再携带服务 Token 请求上述接口，并把响应状态、正文及文件相关响应头转发给浏览器。ERP 前端的“查看源文件”按钮只访问 ERP 自己的后端代理接口，不直接持有服务 Token，也不直接访问 MinIO。

当前 AI ERP 的 `save-with-details` 请求体尚不发送 `ingestion_id`。ERP 字段名确定后，再在建单请求的 `order` 中增加对应字段。
