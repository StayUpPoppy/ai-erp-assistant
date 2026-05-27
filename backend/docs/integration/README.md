# ERP 订单接口对接材料

## 本目录

- `order-api-params.xlsx`：对方提供的订单接口参数说明（含 JSON 示例）。原始文件来自微信缓存，已复制入仓库并改用英文文件名，避免路径/编码问题。

## 与当前 `RealErpClient` 的差异（接真实 ERP 前必读）

对方示例为 **嵌套结构**：顶层含订单头（如 `org`、`customerName`、`orderDate`、`currency` 等）与 **`details` 行数组**（`materialCode`、`qty`、`price` / `taxPrice` 等）。

本仓库 `backend/api/app/erp_client.py` 中 `create_draft` 默认发送的是 **`{ type, payload, idempotency_key }` 且 `payload` 为扁平键值**，无法仅靠 `ERP_REQ_PAYLOAD_FIELD_MAP` 完整表达上述嵌套体。

**接法二选一**（与 `backend/docs/runbook.md` §4.2、`backend/docs/erp-integration-spec.zh-CN.md` 一并看）：

1. **对方或中间 BFF** 提供一层接口：接收扁平/简化字段，服务端组装为当前 Excel 中的 JSON；本仓库仍用现有适配器 + 映射环境变量。  
2. **在本仓库扩展适配器**：在 `RealErpClient.create_draft`（或专用 `OrderPayloadBuilder`）中根据 `doc_type` + `resolved_fields` 组装对方要求的 JSON，再 `POST` 到 `ERP_CREATE_DRAFT_PATH`。

拿到 **Base URL、鉴权、真实路径、成功响应字段名** 后，再定选用 1 还是 2 并实现。
