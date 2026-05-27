# AI ERP Assistant 交接进度清单（2026-05-07）

本清单用于后续开发快速判断：哪些能力已落地可跑，哪些仍是占位或待接入。

**防跑偏（必存）**：交接说明与代码是否一致，以 [`backend/docs/handover-vs-repo.md`](./handover-vs-repo.md) 为准；大 PR / 发版前请对照更新该文件。

## 进度总览（持续更新）

- **总进度**：约 **94%**（以“MVP 可跑 + 可测试 + 可交接”为口径；随文档模块双轴口径与实现小步增强略上调）
- **当前进度（本轮）**：
  - 文档解析/OCR/抽取：综合 **58%** + **双口径**（工程可跑约 72% / 业务精度约 48%，见下表）；补 `.docx` 文本层与结构化中文槽位小步增强
  - 前端错误文案映射已配置化
  - 支持环境变量覆盖分类标签与处理建议
- **当前里程碑状态**：`MVP 主链路已跑通，测试与可观测性已成体系，进入真实能力接入阶段`

### 模块拆分进度（建议口径）

- **API 编排层**：**80%**
  - 路由、状态机、错误码、审计、日志、幂等已基本完成
  - 待完成：真实 LangGraph 复杂分支与真实业务节点
- **Worker 异步层**：**75%**
  - 队列消费、回调处理、容错退避、单测已完成
  - 待完成：跨进程 e2e 与生产级重试/死信策略
- **前端交互层（web）**：**68%**
  - 主流程可跑（上传/轮询/补全/建草稿），失败详情可视化已接入
  - 待完成：异常态体验、权限态、可用性优化
- **ERP 适配层**：**82%**
  - mock 契约完成，真实模式骨架/错误映射/字段映射/鉴权签名/请求映射/token刷新/错误详情已接入
  - 待完成：真实 ERP API 接入、鉴权、错误映射
- **文档解析/OCR/抽取**：**综合 58%**（双口径：**工程可跑约 72%** / **业务精度约 48%**）
  - **工程可跑**：PDF 文字层（pypdf）、多编码纯文本、**`.docx` 正文（标准 OOXML，无额外 pip 依赖）**、常见图片 **Tesseract OCR**、稀疏 PDF 下 **PyMuPDF 渲染多页 + OCR**（阈值/页数/开关可配）；失败降级不阻断工作流；`backend/api/tests/test_document_extract.py` 等（OCR 可 mock）。
  - **业务精度**：文件名与正文启发式分类、`structured_extract` 按 PO/GR/INV 与 `backend/packages/shared` 必填键对齐；持续靠 **脱敏样本文本 + 单测** 迭代正则即可稳步抬高本指标。
  - **仍明显低于「生产级单据 AI」**：无版面/表格结构解析与行级明细、无 xls/xlsx/ppt、无云端视觉或 LLM 抽取；复杂版式与手写/低质扫描依赖环境与调参。
- **测试与质量保障**：**72%**
  - API/worker 关键路径和 DB 门控测试已具备
  - 待完成：全链路 e2e、性能压测、回归基线

## 一、已完成（可运行）

- Monorepo 基础结构已建好：`frontend`、`backend/api`、`backend/worker`、`backend/packages/erp-client`、`backend/packages/shared`（已落地可安装包 `erp-assistant-shared`，见 `backend/packages/shared/README.md`）、`infra`。
- API MVP 路由已具备：
  - `POST /uploads`
  - `POST /uploads/binary`
  - `POST /ingestions`
  - `GET /ingestions/{id}`
  - `POST /ingestions/{id}/resolve`
  - `POST /ingestions/{id}/create-draft`
  - `POST /internal/ingestions/{id}/process`（worker 内部调用）
- ingestion 统一状态机已实现并打通：`UPLOADED -> CLASSIFIED -> PARSED -> EXTRACTED -> MAPPED` 之后，若必填已齐则可直接 **`VALIDATED`**（工作流内自动 `validate_draft`），否则 **`NEED_USER_INPUT`**，再经 `/resolve` 到 **`VALIDATED`**，最后 **`DRAFT_CREATED`**。
- 幂等能力已实现：
  - 以 `file_hash` 去重，避免同文件重复建 ingestion；
  - 创建草稿使用稳定 `idempotency_key`，重复调用返回同一草稿结果。
- 存储双通路已实现：
  - 配置 `DATABASE_URL` 时落 Postgres；
  - 未配置时自动回退内存存储，便于本地无依赖联调。
- Redis + worker 异步处理链路已打通（队列消费后回调 API 内部端点推进状态机）。
- MinIO 兼容上传通路已接入（未配置 MinIO 时可降级仅建任务）。
- 审计轨迹基础能力已实现：`audit_events` 按状态推进持续追加。
- 日志基础能力已实现：
  - API 统一日志初始化与脱敏过滤（`vendor_code`、`invoice_no`、`tax_no`）；
  - `store`、`ingestion_db`、`routes`、`worker` 均已有关键链路日志。
- 工作流骨架已抽离到 `backend/api/app/workflow.py`：
  - `store.process_ingestion` 不再硬编码步骤，改为调用统一 workflow；
  - 为后续替换为 LangGraph 图式编排预留稳定入口。
- 工作流节点化执行已完成（MVP）：
  - 已按 `classify/parse/extract/map/request_user_input` 划分节点；
  - 每个节点具备统一日志（start/end、耗时、状态、指标）。
- LangGraph 最小编排已接入：
  - workflow 默认优先使用 LangGraph 执行节点链路；
  - 若运行环境未安装 LangGraph，会自动降级为线性执行，保证联调不中断。
- 失败治理（workflow）已增强：
  - 节点异常会统一收敛为 `FAILED` 状态并写入 `error_code`；
  - 当前已支持 `WORKFLOW_NODE_FAILED` 与 `WORKFLOW_UNEXPECTED_ERROR` 两类错误码。
- map 节点重试机制已加入：
  - 支持 `WORKFLOW_MAP_MAX_RETRIES` 与 `WORKFLOW_MAP_RETRY_BACKOFF_MS` 环境变量；
  - 重试行为写日志并追加审计事件，便于回放失败恢复过程。
- 通用节点重试能力已抽象：
  - 重试逻辑已封装为通用方法，减少节点内重复代码；
  - `parse/extract/map` 已接入统一重试与观测日志。
- 节点重试超时保护已加入：
  - 支持节点级 `WORKFLOW_<NODE>_MAX_ELAPSED_MS`（例如 `WORKFLOW_MAP_MAX_ELAPSED_MS`）；
  - 超时后会终止继续重试并按 workflow 失败路径收敛。
- workflow 错误码已细分：
  - 重试耗尽：`WORKFLOW_RETRY_EXHAUSTED`；
  - 重试超时：`WORKFLOW_RETRY_TIMEOUT`；
  - 便于前端与告警按失败类型分流处理。
- workflow 错误码已扩展到节点粒度：
  - parse/extract/map 的 `RETRY_TIMEOUT` 与 `RETRY_EXHAUSTED` 已可单独识别；
  - 其余节点暂走通用错误码，后续按真实场景继续细化。
- workflow 最小回归单测已加入：
  - 新增 `backend/api/tests/test_workflow.py`；
  - 已覆盖节点级错误码映射与失败收敛分支（timeout / retry exhausted / fallback）。
- `/internal/ingestions/{id}/process` 集成测试已加入：
  - 新增 `backend/api/tests/test_process_ingestion_api.py`；
  - 已覆盖成功推进、404、workflow 失败错误码透传。
- `/resolve` 与 `/create-draft` 集成测试已加入：
  - 新增 `backend/api/tests/test_resolve_and_create_draft_api.py`；
  - 已覆盖缺字段拦截、补全后建草稿、幂等重放不重复建单。
- DB 模式集成测试已加入（按环境自动开关）：
  - 新增 `backend/api/tests/test_db_mode_integration.py`；
  - `DATABASE_URL` 已配置时验证 DB 查询与错误码持久化，未配置时自动 skip。
- worker 关键链路测试已加入：
  - 新增 `backend/worker/tests/test_worker_process_job.py`；
  - 已覆盖回调 API 成功路径与 HTTP 异常容错路径。
- worker 主循环已可测试化：
  - 新增 `poll_once` 单次轮询函数，`main` 复用该函数循环执行；
  - 新增轮询测试覆盖任务处理、心跳空队列、异常退避分支。
- worker `main` 轻量语义测试已补充：
  - 已验证轮询失败时会执行 `sleep(2)` 退避；
  - 已验证主循环不会因单次失败退出（持续进入下一轮轮询）。
- 最小端到端验证脚本已提供：
  - 新增仓库根脚本 `verify_async_flow.py`；
  - 可一键验证“创建任务 -> 轮询状态推进 -> 审计事件输出”。
- 跨进程本地 e2e 脚本已提供：
  - 新增仓库根脚本 `run_local_e2e.py`；
  - 可自动拉起 API + worker，执行异步链路验证并自动清理进程。
- 统一验证入口已提供：
  - 新增仓库根脚本 `run_verification.py`；
  - 支持 `--mode quick|full`，统一“手动环境验证”和“跨进程验证”入口。
- 统一验证入口测试已加入：
  - 新增 `tests/test_run_verification.py`；
  - 已覆盖 `quick/full` 模式命令构造与参数透传。
- 测试聚合入口已提供：
  - 新增仓库根脚本 `run_tests.py`；
  - 支持 `--mode quick|full`，统一核心回归与全量回归执行。
- 测试聚合入口测试已加入：
  - 新增 `tests/test_run_tests.py`；
  - 已覆盖 `quick/full` 模式命令构造与返回码透传。
- 真实 ERP 对接说明已提供：
  - 新增 `backend/docs/erp-integration-spec.zh-CN.md`；
  - 包含接口清单、字段契约、错误码映射、幂等规则、联调验收项。
- ERP 客户端真实模式骨架已提供：
  - `backend/api/app/erp_client.py` 支持 `ERP_CLIENT_MODE=mock|real`；
  - 已支持 `ERP_BASE_URL`、`ERP_API_TOKEN`、`ERP_TIMEOUT_SECONDS`。
- **双 ERP 宿主**已支持：主数据查询与写单可分别配置 `ERP_DATA_BASE_URL` / `ERP_WRITE_BASE_URL`（及可选分 token），适配层使用 `CompositeDualErpClient` 组合；未分宿主时行为与单 `ERP_BASE_URL` 一致。
- ERP 真实模式错误映射已打通：
  - `resolve/create-draft` 已接入 `ErpClientError -> ErrorCode` 映射；
  - 新增测试：`backend/api/tests/test_erp_store_integration.py`。
- ERP 字段映射配置能力已提供：
  - `RealErpClient` 支持通过环境变量配置 `items/valid/missing/draft_no/draft_url/error_code/message` 字段名；
  - 新增测试：`backend/api/tests/test_erp_client.py`（`test_real_erp_client_supports_configurable_fields`）。
- ERP 鉴权与签名能力已提供：
  - 支持 `ERP_AUTH_MODE=bearer|api_key`；
  - 支持 HMAC-SHA256 签名头（时间戳+签名）配置；
  - 新增测试：`backend/api/tests/test_erp_client.py`（`test_real_erp_client_adds_api_key_and_signature_headers`）。
- ERP 请求体映射配置能力已提供：
  - 支持 `ERP_REQ_TYPE_FIELD`、`ERP_REQ_PAYLOAD_FIELD`、`ERP_REQ_IDEMPOTENCY_FIELD`；
  - 支持 `ERP_REQ_PAYLOAD_FIELD_MAP`（JSON）对 payload 内字段重命名；
  - 新增测试：`backend/api/tests/test_erp_client.py`（`test_real_erp_client_supports_request_payload_mapping`）。
- ERP token 刷新能力已提供：
  - 支持 `ERP_AUTH_REFRESH_ENABLED`、`ERP_AUTH_REFRESH_URL`、`ERP_AUTH_REFRESH_TOKEN`；
  - 当 Bearer 模式遇到 401，会尝试刷新 token 并自动重试一次；
  - 新增测试：`backend/api/tests/test_erp_client.py`（`test_real_erp_client_refreshes_token_on_401`）。
- ERP 结构化错误详情已打通：
  - `IngestionResponse` 新增 `error_details`，并在 `resolve/create-draft` 的 ERP 异常路径落库/返回；
  - 详情包含 `erp_status_code`、`erp_error_code`、`erp_message` 及可选 `violations/request_id/details`。
- ERP 错误详情标准结构已落地：
  - 固定字段：`category`、`upstream_request_id`、`field_errors`、`raw`；
  - `category` 支持：`master_data | permission | timeout | upstream_error`。
- 前端消费规范已补充：
  - `runbook` 新增 `GET /ingestions/{id}` 失败样例 JSON；
  - 给出错误标题/副标题/字段错误/排障信息的渲染建议。
- 前端失败态错误卡片已落地：
  - 新增组件：`frontend/components/ErrorDetailsCard.tsx`；
  - 页面已接入 `error_code + error_details` 可视化展示（分类/字段错误/原始详情）。
- 前端失败态操作建议已落地：
  - 按 `category` 显示处理建议（主数据/权限/超时/上游错误）；
  - 降低一线用户在失败场景下的试错成本。
- 前端错误文案映射已可配置：
  - 新增 `frontend/lib/error-hints.ts`；
  - 支持通过 `NEXT_PUBLIC_ERROR_CATEGORY_LABELS_JSON` 与 `NEXT_PUBLIC_ERROR_CATEGORY_HINTS_JSON` 覆盖默认文案。
- 文档正文与 OCR 提取已落地（`backend/api/app/document_extract.py`）：PDF 文本层、纯文本多编码、**`.docx` OOXML 正文**、常见图片 Tesseract、稀疏 PDF 下 PyMuPDF 多页渲染补 OCR；`structured_extract.py` 提供 PO/GR/INV 规则化字段槽位；`backend/api/tests/test_document_extract.py` 覆盖解析与分类/启发式关键路径。
- 启动与联调文档已提供：`backend/docs/runbook.md`。

## 二、部分完成（MVP可演示，但需增强）

- 文档解析/OCR/抽取：**正文提取与 Tesseract OCR 已接入代码路径**（见 `backend/api/app/document_extract.py` 与 `backend/docs/runbook.md` 可选依赖说明），结构化字段为 **规则/启发式**（`structured_extract.py`），非大模型抽取；复杂版式、表格行项目与 Office 等仍属增强项。
- LangGraph 编排框架尚未完整落地为图式工作流，当前以 API + worker 的状态推进逻辑为主。
- ERP Adapter 已有真实模式调用骨架，但真实 ERP 接口字段映射与鉴权细节仍待联调。
- 前端可跑通上传/轮询/补全/建草稿主流程，但体验与异常态提示仍有优化空间。

## 三、未完成（后续优先项）

- 接入真实 ERP 查询与写入接口（供应商/物料/仓库/税码、validateDraft、createDraft）。
- 问答能力接 LangChain/LangGraph Tool 调用，严格执行“先查 ERP 再回答”。
- 完整权限与认证体系（当前为简化认证，后续需接 SSO）。
- 审计治理增强：
  - ERP 请求/响应摘要脱敏落库；
  - 组织/角色级权限隔离；
  - 可检索的审计查询接口。
- 错误码体系扩展与统一映射（字段级错误、权限、业务校验、上游超时等）。
- 自动化测试补齐（API、状态机、幂等、worker 重试、端到端流程）。
- 数据库迁移工具（Alembic）与生产级部署配置完善。

## 四、建议下一步（执行顺序）

1. 固化 LangGraph 工作流与节点边界（分类、解析、抽取、映射、校验、补问、建草稿）。
2. 先接入真实 `searchVendors/searchMaterials/validateDraft/createDraft`，保留 mock 可切换。
3. 补齐可观测性：trace_id 串联 API 与 worker、失败重试计数、告警阈值。
4. 增加最小回归测试集，覆盖“重复上传不重复建单”和“草稿创建幂等重放”。

**若今日主线是文档「工程 + 精度」双提升**：优先 **脱敏样本文本 → 单测（失败样例先行）→ 改 `structured_extract` / `document_extract`** 的小闭环；每合并一批样例，把「业务精度」百分比在团队内复盘一次（不必每次改总表数字）。工程侧同日可并行：**xlsx/csv 表格最小读入**、parse 节点 **format_label / 字符数** 指标透出到审计或日志采样。
