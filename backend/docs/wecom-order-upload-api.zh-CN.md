# 企业微信机器人订单上传接口说明

本文档给企业微信机器人程序员使用，用于把客户群里的 PDF 订单上传到订单助手。

## 1. 接口地址

生产环境外部地址：

```text
POST http://111.170.173.2:8081/api/orchestrator/integrations/wecom/order-files
```

接口类型：

```text
multipart/form-data
```

说明：

- 机器人只负责上传 PDF 和客户公司名。
- 机器人不需要 ERP 销售员账号密码。
- 机器人不要传最终 `user_id`。
- 后端会根据 `customer_name` 查询客户销售员映射表，自动把订单分配到对应销售员的待处理订单队列。

## 2. 鉴权

请求头必须带：

```http
Authorization: Bearer wDUFD7TX_yBpLxtTV0o22ekO1DNzs0hKKbUOSamBLT8
```

`<WECOM_INGEST_TOKEN>` 由服务端环境变量 `WECOM_INGEST_TOKEN` 配置。

不要把真实 token 写进代码仓库、日志或截图里。

## 3. 主接口字段

### 必填字段

| 字段 | 类型 | 说明 | 示例 |
| --- | --- | --- | --- |
| `file` | file | PDF 文件二进制内容 | `order.pdf` |
| `file_name` | string | PDF 文件名 | `POGSVC2600205.pdf` |
| `customer_name` | string | 客户公司全称，必须能在映射表中找到 | `格鲁赛特阀门配件江苏有限公司` |
| `wecom_message_id` | string | 企业微信消息唯一防止重复上传同一pdf ID，测试时可自定义但建议每次不同 | `msg-20260707-001` |

### 可选字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sender_user_id` | string | 企业微信发送人 userId |
| `sender_name` | string | 企业微信发送人姓名 |
| `wecom_group_id` | string | 企业微信群 ID |
| `wecom_group_name` | string | 企业微信群名称 |
| `sent_at` | string | 消息发送时间，建议 ISO 8601 格式 |
| `file_hash` | string | PDF 文件 SHA-256；如果传了，必须和后端计算结果一致 |
| `customer_name_hint` | string | 客户名提示，兼容字段 |
| `factory_name_hint` | string | 工厂名提示，兼容字段 |
| `extraction_profile_id` | string | 指定解析模板，通常不传 |

## 4. curl 示例

```bash
curl -X POST "http://111.170.173.2:8081/api/orchestrator/integrations/wecom/order-files" \
  -H "Authorization: Bearer wDUFD7TX_yBpLxtTV0o22ekO1DNzs0hKKbUOSamBLT8" \
  -F "file=@/path/to/order.pdf;type=application/pdf" \
  -F "file_name=order.pdf" \
  -F "customer_name=格鲁赛特阀门配件江苏有限公司" \
  -F "wecom_message_id=msg-20260707-001" \
  -F "sender_name=客户张三" \
  -F "wecom_group_name=格鲁赛特阀门配件江苏有限公司"
```

## 5. JavaScript FormData 示例

```javascript
const form = new FormData();
form.append("file", pdfBlob, "order.pdf");
form.append("file_name", "order.pdf");
form.append("customer_name", "格鲁赛特阀门配件江苏有限公司");
form.append("wecom_message_id", "msg-20260707-001");
form.append("sender_name", "客户张三");
form.append("wecom_group_name", "格鲁赛特阀门配件江苏有限公司");

const res = await fetch(
  "http://111.170.173.2:8081/api/orchestrator/integrations/wecom/order-files",
  {
    method: "POST",
    headers: {
      Authorization: "Bearer wDUFD7TX_yBpLxtTV0o22ekO1DNzs0hKKbUOSamBLT8",
    },
    body: form,
  }
);

const data = await res.json();
```

注意：使用 `FormData` 时，不要手动设置 `Content-Type`，让运行环境自动生成 multipart boundary。

## 6. 成功响应

HTTP 状态码：

```text
200
```

响应示例：

```json
{
  "ok": true,
  "assigned": true,
  "ingestion_id": "455383bd-278f-4547-a7ec-961c6d435b86",
  "file_id": "abc123",
  "status": "UPLOADED",
  "file_hash": "sha256...",
  "user_id": "31",
  "sales_user_name": "张宇涵",
  "org_id": "英科1厂",
  "customer_name": "格鲁赛特阀门配件江苏有限公司",
  "factory_name": "",
  "message": "订单已接收，等待解析"
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `ingestion_id` | 订单助手内部任务 ID，可用于排查日志 |
| `file_id` | 文件 ID |
| `status` | 初始通常为 `UPLOADED` |
| `file_hash` | 后端计算的 PDF SHA-256 |
| `user_id` | 后端根据客户映射出的 ERP 销售员 userId |
| `sales_user_name` | 后端根据客户映射出的销售员姓名 |
| `org_id` | 后端根据客户映射出的销售组织/工厂 |
| `customer_name` | 匹配到的客户公司名 |

## 7. 常见错误响应

### 7.1 客户公司未绑定销售员

HTTP 状态码：

```text
409
```

响应示例：

```json
{
  "detail": {
    "ok": false,
    "code": "UNMAPPED_WECOM_CUSTOMER",
    "assigned": false,
    "customer_name": "未知客户公司",
    "wecom_group_id": "",
    "wecom_group_name": "",
    "customer_name_hint": "",
    "factory_name_hint": "",
    "message": "订单接收失败：客户公司未绑定销售员，请维护客户销售员映射后重传。"
  }
}
```

处理建议：

- 机器人可以直接把 `detail.message` 展示给操作人员。
- 此时后端不会创建订单任务，避免订单挂到错误销售员名下。
- 维护客户销售员映射后，需要重新上传该 PDF。

### 7.2 Token 缺失或错误

HTTP 状态码：

```text
401
```

说明：

- 请求头缺少 `Authorization: Bearer <token>`。
- 或 token 和服务器环境变量 `WECOM_INGEST_TOKEN` 不一致。

### 7.3 服务器未启用机器人上传

HTTP 状态码：

```text
503
```

说明：

- 服务器没有配置 `WECOM_INGEST_TOKEN`。
- 需要运维或后端人员在服务端环境变量中配置。

### 7.4 文件 hash 不一致

HTTP 状态码：

```text
400
```

说明：

- 如果传了 `file_hash`，后端会重新计算 PDF 的 SHA-256。
- 两者不一致时拒绝接收。
- 不确定时可以不传 `file_hash`。

### 7.5 文件过大

HTTP 状态码：

```text
413
```

说明：

- PDF 超过后端允许的上传大小。
- 当前前端建议单个文件不超过约 29MB。

## 8. 客户销售员映射要求

接口能否分配成功，取决于数据库映射表 `wecom_order_routes`。

至少需要维护：

| 字段 | 说明 |
| --- | --- |
| `customer_name` | 客户公司全称，必须和接口传入的 `customer_name` 一致 |
| `erp_user_id` | ERP 销售员 userId |
| `sales_user_name` | 销售员姓名 |
| `org_id` | 销售组织/工厂 |
| `enabled` | 必须为 `true` |

示例：

```text
customer_name: 格鲁赛特阀门配件江苏有限公司
erp_user_id: 31
sales_user_name: 张宇涵
org_id: 英科1厂
enabled: true
```

## 9. 兜底 base64 接口

优先使用 multipart 主接口。只有在机器人侧确实不方便传文件流时，再使用 base64 兜底接口。

接口地址：

```text
POST http://111.170.173.2:8081/api/orchestrator/integrations/wecom/order-files/base64
```

请求类型：

```text
application/json
```

必填字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `fileName` | string | PDF 文件名 |
| `contentType` | string | 固定传 `application/pdf` |
| `base64Content` | string | PDF 文件 base64 内容 |
| `customerName` | string | 客户公司全称 |
| `wecomMessageId` | string | 企业微信消息唯一 ID |

请求示例：

```json
{
  "fileName": "order.pdf",
  "contentType": "application/pdf",
  "base64Content": "JVBERi0xLj...",
  "customerName": "格鲁赛特阀门配件江苏有限公司",
  "wecomMessageId": "msg-20260707-001",
  "senderName": "客户张三",
  "wecomGroupName": "格鲁赛特阀门配件江苏有限公司"
}
```

## 10. 对接检查清单

- 已拿到正确的 `WECOM_INGEST_TOKEN`。
- 上传时使用 `multipart/form-data`。
- `file` 传 PDF 二进制文件，不是文件名字符串。
- `customer_name` 使用客户公司全称。
- `wecom_message_id` 每条企业微信消息保持唯一。
- 未绑定客户时，机器人展示后端返回的 `detail.message`。
- 成功上传后，销售员登录 ERP 订单助手，在“我的待处理订单”中查看并确认。
