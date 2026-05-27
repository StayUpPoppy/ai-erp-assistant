# ERP 联调前准备（内部填写）

等对方发来 **建单成功示例 JSON** 后，把下表「对方字段名」列补全；再据此配置 `.env` 里的 `ERP_REQ_PAYLOAD_FIELD_MAP`（JSON 映射：我方键 → 对方键）。

**参考**：`backend/docs/runbook.md` §4.2（含 `ERP_SKIP_UPSTREAM_VALIDATE`、`ERP_CREATE_DRAFT_PATH` 等第一阶段变量）。

---

## PO（当前仓库扁平必填）

| 我方 `resolved_fields` 键 | 对方请求体字段名（待填） | 备注 |
|---------------------------|--------------------------|------|
| vendor_code | | |
| doc_date | | |
| currency | | |
| material_code | | |
| line_qty | | |

## GR

| 我方键 | 对方字段名（待填） | 备注 |
|--------|-------------------|------|
| vendor_code | | |
| doc_date | | |
| currency | | |
| po_no | | |
| material_code | | |
| qty_received | | |

## INV

| 我方键 | 对方字段名（待填） | 备注 |
|--------|-------------------|------|
| vendor_code | | |
| doc_date | | |
| currency | | |
| invoice_no | | |
| invoice_date | | |

---

## 样例到达后核对清单

- [ ] API Base URL（非 `/login`）
- [ ] 鉴权：Header / Token 获取方式
- [ ] 建单路径 → `ERP_CREATE_DRAFT_PATH`
- [ ] 外层字段名若不为 `type` / `payload` / `idempotency_key` → `ERP_REQ_TYPE_FIELD` 等
- [ ] 响应里单号、链接字段名 → `ERP_DRAFT_NO_FIELD` / `ERP_DRAFT_URL_FIELD`（无 URL 则 `ERP_ALLOW_EMPTY_DRAFT_URL=true`）
- [ ] 暂无 validate 接口 → `ERP_SKIP_UPSTREAM_VALIDATE=true`（接通后改回 `false`）
