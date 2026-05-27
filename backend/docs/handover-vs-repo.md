# 交接目标 ↔ 仓库实现对照（防跑偏执行清单）

本文档是 **「产品/交接约束」与「本仓库代码」** 的单一对照面：发版、合入大功能、换 Agent 接手时，**必须先过一遍本节勾选**，避免 silently drift。

---

## 1. 何时必须更新 / 对照本文档

| 时机 | 动作 |
|------|------|
| 合并影响 **ingestion / ERP / 问答 / 审计 / 解析档案** 的 PR | Author 在 PR 描述中写明：对照 §2 哪几行状态有变；Reviewer 抽查 §2 相关行 |
| 发版或对外演示前 | 维护者快速扫 §2「红线」与 §3「刻意简化」是否仍诚实 |
| 新 Agent / 外包入场 | 先读本文件 + `backend/docs/runbook.md`，再改代码 |
| 真实 ERP 契约变更 | 同步改 `backend/docs/erp-integration-spec.zh-CN.md`、`.env.example`，并更新下表「实现位置」列 |

---

## 2. 红线（不可违背；违背即视为跑偏）

以下与产品交接说明一致；实现可以简陋，**原则不能换实现口径**。

| # | 约束 | 仓库中应持续成立 | 自检方式 |
|---|------|------------------|----------|
| R1 | **ERP 事实源**：问答与业务数字不得靠「模型记忆」冒充 ERP 实时态 | `backend/api/app/erp_qa.py` 仅通过 `ErpClientProtocol` 调 `search_*`；不得把 ERP 结果写入外部知识库当真相源 | Code review：问答路径无「无 tool 直接答业务数值」的捷径 |
| R2 | **安全入账**：先草稿 + 校验，不直驱正式过账 | `create-draft` / `validate` 经 `backend/api/app/store.py` + `erp_client`；无「跳过 validate 直写正式单」的 API | 搜路由与 store：无正式过账端点 |
| R3 | **全链路可审计** | `audit_events`、`erp_call_log`（及 DB 中 ingestion 持久化字段）随状态推进写入 | 跑一单 ingestion，GET 详情含事件与 `erp_call_log` |
| R4 | **幂等防重** | 同 `file_hash` 不重复建 ingestion；`create_draft` 使用稳定 `idempotency_key` | 读 `backend/api/app/store.py` + 相关测试 |
| R5 | **三单据类型** PO/GR/INV + **租户可扩展必填** | **默认**（无解析档案时）必填键仍以 `backend/packages/shared` → `erp_assistant_shared.contract.required_field_keys` 为唯一事实源；API 经 `structured_extract` 与 `workflow`/`store` 使用同一套默认逻辑。**租户/客户差异**通过 JSON **解析档案**解决：`backend/config/extraction_profiles/{id}.json`（或 `EXTRACTION_PROFILES_DIR`），加载与合并见 `backend/api/app/extraction_profile.py`；不得在各处复制一份互不一致的默认列表。**校验一致**：`MockErpClient`/`store.resolve`/`workflow` 在带档案时须以 `validate_draft(..., required_keys=ingestion.required_resolve_keys)` 与缺项计算对齐。**前端补全**：以 `GET /ingestions/{id}` 返回的 **`required_resolve_keys` 顺序为准**；`frontend/app/page.tsx` 中 `RESOLVE_KEYS_BY_DOC` **仅作兜底**（无档案、或旧数据未回填时），不得在有 `required_resolve_keys` 时仍以硬编码列表覆盖 API。 | **改「系统默认」PO/GR/INV 键名/顺序**：先改 `backend/packages/shared/.../contract.py`，再视需要调整兜底常量 `RESOLVE_KEYS_BY_DOC`，跑 `test_structured_extract.py`、`test_shared_contract.py`。**改「某客户」字段**：只维护对应 `{org_id}.json` 或显式 `extraction_profile_id` 档案（`required_fields_by_doc_type` / `extra_required_fields_by_doc_type` / `extract_rules`），原则上**无需改**前端 TS 常量；跑一单带该档案的 ingestion，核对 `missing_fields`、`required_resolve_keys` 与补全 UI 一致。 |
| R6 | **双 ERP 时读写分离** | 主数据 `search_*` 与写单 `validate/create` 可拆宿主：`CompositeDualErpClient`（`backend/api/app/erp_client.py`） | 配 `ERP_DATA_BASE_URL` ≠ `ERP_WRITE_BASE_URL` 烟测 |

---

## 3. 交接说明 ↔ 当前实现（诚实状态）

**约定**：✅ 已对齐 MVP 口径｜⚠️ 有实现但与交接「深度/范围」不一致｜⬜ 未做或仅占位

| 交接项 | 当前实现位置（入口） | 状态 | 若要做到「交接全文」的后续动作 |
|--------|----------------------|------|----------------------------------|
| 独立 Next 前端 + 拖拽 + 状态 + 补全 + 草稿链接 | `frontend` | ✅ | 权限态、异常引导见 `project-progress` |
| FastAPI 编排 + ingestion API | `backend/api/app/routes.py`、`store.py` | ✅ | — |
| Redis + worker 异步 | `backend/worker`、`process` 内部路由 | ✅ | 生产级死信/重试策略 |
| LangGraph 编排 | `backend/api/app/workflow.py` | ⚠️ | 已接 LangGraph，**缺省仍为线性 MVP**；复杂分支需单独设计 |
| 文档解析 + OCR | `backend/api/app/document_extract.py` | ⚠️ | PDF/文本/图片为主；**Office 全格式**未承诺等同 |
| 结构化抽取 Pydantic + **行项目** | `structured_extract.py` + 扁平 `resolved_fields` | ⚠️ | 行级 schema、多行明细需新模块与 API 契约 |
| **多租户解析**（少改代码：扩展必填 + 正则槽位 + 键别名） | `backend/config/extraction_profiles/*.json`、`EXTRACTION_PROFILES_DIR`、`app/extraction_profile.py`（`field_aliases`、`capture_group`、非法正则剔除）；`workflow`/`store` 合并必填与 `ingestion.required_resolve_keys`；ingestion 暴露 `extraction_profile_requested` / `extraction_profile_resolution`；`routes` multipart 可选 `extraction_profile_id`；`/health` 含档案目录统计；前端优先读 `required_resolve_keys` | ✅ | 极复杂版式仍可能需要专用插件或 LLM（见 `project-progress`）；档案仅 JSON 时勿承诺版面级精度 |
| 映射：供应商/物料/仓库/税码 | `workflow` map 节点 + `search_*` | ⚠️ | 仓库/税码未做独立 search 与必填 |
| validate / NEED_USER_INPUT / VALIDATED | `workflow.py`、`store.py`、前端补全 | ✅ | 与交接状态机一致（含自动 VALIDATED） |
| ERP mock → 真实可切换 | `erp_client.py`、`ERP_CLIENT_MODE` | ✅ | 用户提供真实契约后改 adapter + 联调 |
| 双 ERP 宿主 | `ERP_DATA_BASE_URL` / `ERP_WRITE_BASE_URL` | ✅ | 两套鉴权策略完全不同时需扩展 env 或拆部署 |
| 审计：ERP 请求/响应**脱敏摘要** | `erp_call_log` 元数据 | ⚠️ | 若合规要求全文脱敏存档，需产品细则 + 实现 |
| SSO / 角色隔离 | 前端 `org_id`/`user_id` 简化 | ⬜ | 交接已标「后续」 |
| Dify / 知识库 | 未绑主线 | ✅（与「ERP 不入知识库」一致） | 若接入不可违背 R1 |

---

## 4. 每次发版前最小命令（可自动化进 CI）

在仓库根目录：

```powershell
python run_tests.py --mode quick
```

或：

```powershell
cd backend/api
python -m pytest tests/ -q
```

前端有变更时额外：

```powershell
cd frontend
npm run lint
```

---

## 5. 相关文档索引

| 文档 | 用途 |
|------|------|
| `backend/config/extraction_profiles/` | 解析档案 JSON 示例与按 `org_id`/显式 id 加载说明（见目录内 `_example-*.json`） |
| `backend/docs/runbook.md` | 本地启动、环境变量、联调步骤 |
| `backend/docs/erp-integration-spec.zh-CN.md` | 真实 ERP 路径、字段映射、错误码 |
| `backend/docs/project-progress.zh-CN.md` | 粗粒度完成度与模块百分比 |
| `.env.example` | 全部可调环境变量（含双 ERP） |

---

## 6. 修订记录

| 日期 | 变更 |
|------|------|
| 2026-05-06 | 初版：红线 + 对照表 + 发版自检命令；R5 必填字段迁入 `backend/packages/shared`（`erp_assistant_shared`） |
| 2026-05-11 | 解析档案与 R5：`required_resolve_keys` / `validate_draft(required_keys)` / 前端以 API 为准；`field_aliases`、`extract_rules.capture_group`、加载时剔除非法正则；`extraction_profile_requested` & `extraction_profile_resolution`；`/health` 挂载档案目录与 JSON 数；§3「多租户解析」与索引 `backend/config/extraction_profiles/` |

维护者更新实现时，**请同步改本文件对应行**，避免文档与代码再次分叉。
