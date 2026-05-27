# `erp-assistant-shared`

Monorepo 内 **Python 共享契约**：当前承载 **PO / GR / INV 默认扁平必填字段列表**（无「解析档案」时的 `validate_draft` / extract 缺项 / 与兜底 UI 对齐）。

**租户专属必填或正则槽位**不在此包硬编码：用仓库 `backend/config/extraction_profiles/{org_id或显式id}.json`（见 `backend/api/app/extraction_profile.py`）；前端以接口返回的 `required_resolve_keys` 为准，详见 `backend/docs/handover-vs-repo.md` §R5。

## 使用方

- `backend/api`：`structured_extract` 从本包再导出 `required_field_keys`（勿在 API 内复制默认列表）；带档案时在 `extraction_profile` 中与档案合并后再写入 `ingestion.required_resolve_keys`。
- `frontend`：补全表单 **优先** 使用 `GET /ingestions/{id}` 的 `required_resolve_keys`；`RESOLVE_KEYS_BY_DOC` 仅兜底。**仅当修改「系统默认」三单契约**时：先改本包 `contract.py`，再视需要同步兜底常量，并发 PR 时对照 `backend/docs/handover-vs-repo.md` §R5。

## 本地安装（API / pytest）

在仓库根或 `backend/api` 下安装可编辑包（已写入 `backend/api/requirements.txt` 的 `-e` 行）：

```powershell
cd d:\workspace\ai-erp-assistant\backend\api
python -m pip install -r requirements.txt
```

## 扩展

后续可把 Pydantic 行模型、共享错误码等逐步迁入 `src/erp_assistant_shared/`，仍保持 **单一事实源** 原则。
