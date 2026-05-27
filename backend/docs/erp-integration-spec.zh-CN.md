# ERP 接口对接说明（AI ERP Assistant）

本文档用于指导 ERP 接口提供方与 AI ERP Assistant 进行联调。目标是以最小接口集先打通 `PO/GR/INV` 草稿链路，再逐步扩展。

## 1. 对接目标与范围

- 对接目标：替换当前 `MockErpClient`，接入真实 ERP API。
- 当前最小范围（MVP）：
  - 主数据查询：
    - `searchVendors`
    - `searchMaterials`
    - `searchWarehouses`（可选，用于映射候选与问答）
    - `searchTaxCodes`（可选）
  - 单据处理：
    - `validateDraft(type, payload)`
    - `createDraft(type, payload, idempotency_key)`
- 业务约束：
  - 问答/建单以 ERP 实时查询结果为准，不将 ERP 事实作为外部知识库真相源。
  - 写入 ERP 必须支持幂等，避免重复建草稿。

---

## 2. 双方系统角色

- AI ERP Assistant（调用方）：
  - 提供文档解析、字段抽取、用户补全、流程编排。
  - 在 `validateDraft/createDraft` 前后保留审计轨迹与错误码。
- ERP API（被调用方）：
  - 提供主数据查询、草稿校验、草稿创建。
  - 返回稳定错误码与可解析错误信息。

### 2.1 主数据与写单分属两套 ERP（双宿主）

常见形态：**主数据**（供应商/物料查询）来自 MDG、数据中台或只读网关；**写单**（校验/建草稿）走 S/4、业务中台或独立单据服务。

- 适配层实现：`backend/api/app/erp_client.py` 中的 `CompositeDualErpClient`（`search_vendors` / `search_materials` / `search_warehouses` / `search_tax_codes` → 主数据宿主，`validate_draft` / `create_draft` → 写单宿主）。
- 环境变量（`real` 模式）：
  - `ERP_DATA_BASE_URL`：主数据 HTTP 根地址；未配时回退 `ERP_BASE_URL`。
  - `ERP_WRITE_BASE_URL`（或别名 `ERP_TRANS_BASE_URL`）：写单根地址；未配时回退 `ERP_BASE_URL`。
  - `ERP_DATA_API_TOKEN` / `ERP_WRITE_API_TOKEN`：可选；未配时均用 `ERP_API_TOKEN`。
- 若主数据与写单 **URL 与 token 完全相同**，仍只建一个 `RealErpClient`（无组合包装），行为与单宿主一致。
- 两套系统若 **鉴权方式、签名、字段映射** 不同：当前实现共用同一组 `ERP_AUTH_*` / `ERP_SIGN_*` / `ERP_REQ_*` 环境变量；差异极大时建议拆部署实例或后续扩展 `ERP_WRITE_*` 专用鉴权前缀（需另开需求）。

---

## 3. 接口清单（必须）

## 3.1 供应商查询：`searchVendors`

- **Method**：`GET`（或 `POST`，双方一致即可）
- **建议路径**：`/erp/vendors/search`
- **请求参数**：
  - `org_id` `string` 必填
  - `keyword` `string` 必填（可支持模糊）
- **成功响应示例**：

```json
{
  "items": [
    { "vendor_code": "V001", "vendor_name": "Vendor One" },
    { "vendor_code": "V002", "vendor_name": "Vendor Two" }
  ]
}
```

---

## 3.2 物料查询：`searchMaterials`

- **Method**：`GET`（或 `POST`）
- **建议路径**：`/erp/materials/search`
- **请求参数**：
  - `org_id` `string` 必填
  - `keyword` `string` 必填
- **成功响应示例**：

```json
{
  "items": [
    { "material_code": "M001", "material_name": "Material A" },
    { "material_code": "M002", "material_name": "Material B" }
  ]
}
```

---

## 3.3 仓库查询：`searchWarehouses`（可选）

- **Method**：`GET`（或 `POST`）
- **建议路径**：`/erp/warehouses/search`（可用环境变量 `ERP_WAREHOUSES_SEARCH_PATH` 覆盖）
- **请求参数**：`org_id`、`keyword`（与供应商/物料查询一致）
- **成功响应示例**（列表字段名可用 `ERP_ITEMS_FIELD` 配置，默认 `items`）：

```json
{
  "items": [
    { "warehouse_code": "WH01", "warehouse_name": "Main DC" },
    { "warehouse_code": "WH02", "warehouse_name": "Transit" }
  ]
}
```

---

## 3.4 税码查询：`searchTaxCodes`（可选）

- **Method**：`GET`（或 `POST`）
- **建议路径**：`/erp/tax-codes/search`（可用环境变量 `ERP_TAX_CODES_SEARCH_PATH` 覆盖）
- **请求参数**：`org_id`、`keyword`
- **成功响应示例**：

```json
{
  "items": [
    { "tax_code": "J1", "tax_name": "Output VAT 13%" },
    { "tax_code": "J0", "tax_name": "Zero-rated" }
  ]
}
```

---

## 3.5 草稿校验：`validateDraft(type, payload)`

- **Method**：`POST`
- **建议路径**：`/erp/drafts/validate`
- **请求体**：
  - `type` `string` 必填：`PO | GR | INV`
  - `payload` `object` 必填：待校验单据字段
- **成功响应示例**：

```json
{
  "valid": false,
  "missing_fields": ["vendor_code", "currency"],
  "violations": [
    { "code": "MASTER_DATA_NOT_FOUND", "field": "vendor_code", "message": "vendor not found" }
  ]
}
```

- **返回约定**：
  - `valid=true` 时，`missing_fields` 为空数组。
  - `valid=false` 时，`missing_fields` 与 `violations` 至少有一项可用。

---

## 3.6 创建草稿：`createDraft(type, payload, idempotency_key)`

- **Method**：`POST`
- **建议路径**：`/erp/drafts`
- **请求体**：
  - `type` `string` 必填：`PO | GR | INV`
  - `payload` `object` 必填
  - `idempotency_key` `string` 必填
- **成功响应示例**：

```json
{
  "draft_no": "PO-DRAFT-000123",
  "draft_url": "https://erp.example.com/drafts/PO-DRAFT-000123",
  "idempotent_replay": false
}
```

- **幂等约定**：
  - 相同 `idempotency_key` 重复请求返回同一 `draft_no`；
  - 返回 `idempotent_replay=true`（建议）表示命中幂等重放。

---

## 4. 单据字段最小模型（MVP）

## 4.1 公共头字段

- `org_id/company_id`（组织）
- `vendor_code`（ERP 编码）
- `doc_date`
- `currency`
- `tax_included`
- `source_file_id/source_file_hash`（审计追踪）

## 4.2 行项目字段（按类型）

- **PO**
  - `material_code`, `qty`, `uom`, `price`, `tax_code`, `delivery_date`
- **GR**
  - `po_no`, `po_line_no`, `material_code`, `qty_received`, `warehouse_code`
- **INV**
  - `invoice_no`, `invoice_date`, `po_no/gr_no`, `material_or_expense_code`, `qty`, `unit_price`, `tax_code`

---

## 5. 鉴权与安全要求（需 ERP 提供）

- token 获取方式：
  - 授权地址、grant 类型、client 配置
- token 有效期：
  - access token 过期时间、刷新策略
- 签名规则（如有）：
  - 签名算法、参与签名字段、时钟偏差容忍范围
- 权限隔离：
  - 如何按 `org_id` 或角色进行访问控制

---

## 6. 错误码对齐（ERP -> Assistant）

ERP 需提供稳定错误码，Assistant 侧会映射并落审计。建议最小错误码：

- `MASTER_DATA_NOT_FOUND`
- `MISSING_REQUIRED_FIELDS`
- `PERMISSION_DENIED`
- `AMOUNT_NOT_BALANCED`
- `DUPLICATE_REQUEST`
- `UPSTREAM_TIMEOUT`
- `SYSTEM_ERROR`

建议 ERP 错误响应格式：

```json
{
  "error_code": "MASTER_DATA_NOT_FOUND",
  "message": "vendor_code not found",
  "details": { "field": "vendor_code" },
  "request_id": "erp-req-xxx"
}
```

---

## 7. 联调环境与验收清单

ERP 侧需提供：

- 沙箱地址（base URL）
- 测试账号（至少 1 套）
- 组织维度测试数据（vendor/material）
- 每个接口至少 1 组成功与 1 组失败样例

Assistant 侧验收用例（最小）：

1. `searchVendors/searchMaterials`（及已启用的 `searchWarehouses/searchTaxCodes`）能返回候选
2. `validateDraft` 能准确返回缺失字段
3. `createDraft` 首次成功返回草稿号
4. 同一 `idempotency_key` 再次调用返回同草稿号
5. 主数据不存在时返回可映射错误码

---

## 8. 当前代码契约对齐说明（实现参考）

- Assistant 当前 ERP 调用协议位于：`backend/api/app/erp_client.py`
  - `search_vendors(org_id, keyword)`
  - `search_materials(org_id, keyword)`
  - `search_warehouses(org_id, keyword)`
  - `search_tax_codes(org_id, keyword)`
  - `validate_draft(doc_type, payload)`
  - `create_draft(doc_type, payload, idempotency_key)`
- 建议 ERP 适配实现：
  - 保持上述方法签名不变；
  - 在方法内部做“ERP 实际接口 -> 统一返回模型”的转换；
  - 避免上层 `store/workflow` 直接感知 ERP 私有字段。
- **联调第一阶段**（对方暂未提供 `validate`、或路径不同）：可在调用方配置 `ERP_SKIP_UPSTREAM_VALIDATE=true`（仅本地必填键）、`ERP_VALIDATE_PATH` / `ERP_CREATE_DRAFT_PATH`、`ERP_ALLOW_EMPTY_DRAFT_URL`；详见 `backend/docs/runbook.md` §4.2 段落「联调第一阶段」。

---

## 9. 版本与变更管理建议

- 接口版本建议放在路径或 header 中（如 `/v1/...`）。
- 任何字段破坏性变更须提前通知并保留兼容窗口。
- 每次联调变更记录：
  - 日期、变更人、变更内容、影响范围、回滚方案。
