# ERP Client Adapter Contract

本目录用于沉淀 ERP 适配层契约，目标是让 `mock -> 真实 ERP` 的替换不影响上层编排逻辑。

## 当前状态

- API 侧已经通过统一适配接口调用 ERP 能力（mock 实现）
- 后续接真实 ERP 时，仅需替换适配器实现，不改业务路由与状态机
- **双宿主**：主数据与写单可指向不同 `base_url`（环境变量 `ERP_DATA_BASE_URL` / `ERP_WRITE_BASE_URL`），由 `CompositeDualErpClient` 组合两个 `RealErpClient`；详见仓库 `backend/docs/erp-integration-spec.zh-CN.md` §2.1 与 `backend/api/app/erp_client.py`

## 必需方法（第一版）

1. `search_vendors(org_id, keyword)`
2. `search_materials(org_id, keyword)`
3. `validate_draft(doc_type, payload)`
4. `create_draft(doc_type, payload, idempotency_key)`

可选（销售订单分页等，由部署环境决定是否启用）：

5. `search_sale_orders(org_id, keyword="", page_num=1, page_size=20)`
6. `save_customer(payload)` → `(customer_key, detail_url)`（可选，见 `ERP_CUSTOMER_SAVE_ENABLED`）

## 返回约定（建议）

### search_vendors

```json
[
  { "vendor_code": "V001", "vendor_name": "Vendor Name" }
]
```

### search_materials

```json
[
  { "material_code": "M001", "material_name": "Material Name" }
]
```

### validate_draft

```json
{
  "valid": false,
  "missing_fields": ["vendor_code", "currency"]
}
```

### create_draft

```json
{
  "draft_no": "PO-DRAFT-0001",
  "draft_url": "https://erp.example.com/drafts/PO-DRAFT-0001"
}
```

## 对接时需你提供的信息

- ERP 鉴权方式（token 获取、过期策略、签名要求）
- 查询接口字段与分页规则（供应商、物料，及可选仓库/税码/单位）
- 单据校验与建草稿接口定义
- 业务错误码字典（字段缺失、权限不足、主数据不存在、金额不平等）

## 设计原则

- ERP 事实数据只走实时查询，不落外部知识库作为真相源
- 所有 ERP 写操作必须传 `idempotency_key`
- 请求/响应日志必须可脱敏落审计
