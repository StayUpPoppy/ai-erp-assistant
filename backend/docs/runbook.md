# AI ERP Assistant 本地启动手册（MVP）

本文档用于快速拉起当前版本的 `api + worker + redis/postgres/minio` 本地环境，并验证核心链路是否可用。

**产品边界与交接对照**：见同目录 [`handover-vs-repo.md`](./handover-vs-repo.md)（发版 / 大改 ingestion·ERP·问答前建议先读）。

## 1. 前置条件

- 已安装 Python 3.8+（建议 3.10+）；API 依赖本地包 `backend/packages/shared`（`erp-assistant-shared`），安装 `backend/api/requirements.txt` 时会 `-e` 安装该目录
- 已安装 Docker Desktop（或可用 docker compose）
- 工作目录：`d:\workspace\ai-erp-assistant`

## 2. 准备环境变量

1. 复制模板：
   - 从 `.env.example` 复制为 `.env`
2. 默认值即可本地运行，如端口冲突请改：
   - `API_PORT`
   - `REDIS_URL`
   - `POSTGRES_PORT`
   - `MINIO_ENDPOINT`

## 3. 启动基础设施（Redis / Postgres / MinIO）

在仓库根目录执行：

```powershell
docker compose -f backend/infra/docker-compose.yml up -d
```

查看状态：

```powershell
docker compose -f backend/infra/docker-compose.yml ps
```

若要让 `POST /uploads/binary` 实际落文件，请确认 MinIO 环境变量已配置（见根 `.env.example`）：

- `MINIO_ENDPOINT`
- `MINIO_ACCESS_KEY`
- `MINIO_SECRET_KEY`
- `MINIO_BUCKET`
- `MINIO_USE_SSL`

## 3.1 配置数据库（可选但推荐）

若你希望 ingestion 任务**重启不丢失**，请在运行 API 的终端环境中设置 `DATABASE_URL`：

```text
postgresql+psycopg://postgres:postgres@127.0.0.1:5432/ai_erp_assistant
```

- 与 `backend/infra/docker-compose.yml` 中 Postgres 默认账号一致。
- 不配 `DATABASE_URL` 时，API 会自动使用**内存存储**（仅适合本地快速联调）。

API 启动时会自动 `create_all` 创建 `ingestions` 表（轻量迁移）；后续可替换为 Alembic。

## 3.2 图片 / 扫描 PDF 页 OCR（Tesseract | HTTP | PaddleOCR | 阿里云）

上传 **PNG / JPG / JPEG / WEBP / TIF / BMP** 以及「稀疏文字层 PDF 的多页渲染」时，解析阶段使用 **`OCR_ENGINE`** 所选引擎（见根目录 `.env.example`）：

### A. Tesseract（默认）

1. **安装（Windows）**  
   - 仓库脚本（多镜像依次尝试）：`powershell -ExecutionPolicy Bypass -File backend/scripts/install-tesseract-windows.ps1`  
   - 或手动安装后将目录加入 `PATH`，或设置 **`TESSERACT_CMD`** 指向 `tesseract.exe`。

2. **语言包**  
   - 默认 **`TESSERACT_OCR_LANG=eng`**；有中文包时可设 `chi_sim+eng`（以本机 `tesseract --list-langs` 为准）。

3. **未安装时**  
   - 返回空字串 + `tesseract_not_found` 等标签，任务不中断；日志告警。

### B. HTTP OCR（国内 API / 自建网关，无需本机 Tesseract）

- 设置 **`OCR_ENGINE=http`** 与 **`OCR_HTTP_URL`**（POST JSON：默认 `image_base64` + `lang`，响应默认字段 `text`，均可环境变量覆盖）。  
- 由你们在网关内转发到阿里云、百度、腾讯等 OCR 即可；可选 **`OCR_HTTP_SSL_VERIFY`**、**`OCR_HTTP_HEADERS_JSON`** 鉴权。  
- 若配置了 `http` 却未填 URL，且 **`OCR_ENGINE_AUTO_FALLBACK=true`**（默认），会**自动回退** Tesseract。

### C. PaddleOCR（本机中文，可选）

- 设置 **`OCR_ENGINE=paddle`**，并按 `backend/api/requirements-ocr-paddle.txt` 安装 **paddlepaddle + paddleocr**（体积较大，按需启用）。  
- 适合不便访问外网 Tesseract 安装包、又希望离线中文识别的环境。

### D. 阿里云通用文字识别（RecognizeGeneral，直连）

- 设置 **`OCR_ENGINE=aliyun`**，并配置 **`ALIYUN_OCR_ACCESS_KEY_ID`** / **`ALIYUN_OCR_ACCESS_KEY_SECRET`**（或使用 **`ALIBABA_CLOUD_ACCESS_KEY_ID`** / **`ALIBABA_CLOUD_ACCESS_KEY_SECRET`**）。  
- 默认 **`ALIYUN_OCR_ENDPOINT=ocr-api.cn-hangzhou.aliyuncs.com`**；RAM 用户需具备 **`ocr:RecognizeGeneral`**（或 **`AliyunOCRFullAccess`** 等含该 Action 的策略）。  
- 未配 AK/SK 时返回 **`aliyun_not_configured`**；若 **`OCR_ENGINE_AUTO_FALLBACK=true`**（默认），会回退 Tesseract（与 HTTP 未配 URL 行为一致）。

### 扫描 PDF（文字层极少时）

- 当 `pypdf` 抽出的全文长度 **低于** `PDF_SPARSE_TEXT_THRESHOLD`（默认 48）且 `PDF_FIRST_PAGE_OCR` 不为 `false` 时，会用 **PyMuPDF** 将 PDF **前 N 页**（`PDF_OCR_MAX_PAGES`，默认 3，最大 20）逐页渲染为位图，再走**当前 `OCR_ENGINE` 对应的 OCR**（无需 Poppler）。  
- 依赖 `pymupdf`（已在 `requirements.txt`）。引擎不可用时仍保留文字层结果（可能为空），不中断任务。

**健康检查**：`GET /health` 返回 `ocr_engine`、`ocr_http_url_configured`、`aliyun_ocr_configured`、`paddleocr_importable` 等与 Tesseract 探测字段，便于部署自检。

## 3.3 解析档案（按客户扩展必填字段，可选）

默认 PO/GR/INV 必填键来自 `backend/packages/shared`。若某 `org_id`（或上传时显式传入的 `extraction_profile_id`）需要**额外必填键**或**正文正则槽位**，在仓库 `backend/config/extraction_profiles/` 下放置同名 `{id}.json` 即可（示例见 `_example-extra-po.json`）。可选环境变量 **`EXTRACTION_PROFILES_DIR`** 指向其它目录（便于挂载 ConfigMap / 挂载卷）。

行为与交接红线见 [`handover-vs-repo.md`](./handover-vs-repo.md) §R5 与 §3「多租户解析」行。档案 JSON 另支持：`field_aliases`（客户正文键名映射到契约键，仅目标为空时填充）、`extract_rules[].capture_group`（默认 1）；非法正则规则在加载时丢弃并打日志。

## 4. 启动 API

```powershell
cd backend/api
python -m pip install -r requirements.txt
$env:DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/ai_erp_assistant"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

不配数据库时，去掉 `DATABASE_URL` 环境变量即可。

### 4.2 切换真实 ERP 适配器（可选）

默认 `ERP_CLIENT_MODE=mock`。若要联调真实 ERP，请在 API 终端设置：

```powershell
$env:ERP_CLIENT_MODE="real"
$env:ERP_BASE_URL="https://erp-sandbox.example.com"
$env:ERP_AUTH_MODE="bearer"
$env:ERP_API_TOKEN="your-token"
$env:ERP_TIMEOUT_SECONDS="10"
```

若 ERP 使用 API Key 鉴权：

```powershell
$env:ERP_AUTH_MODE="api_key"
$env:ERP_API_KEY_HEADER="X-ERP-Key"
$env:ERP_API_KEY="your-api-key"
```

若 ERP Bearer token 会过期，可开启自动刷新（401 后重试 1 次）：

```powershell
$env:ERP_AUTH_REFRESH_ENABLED="true"
$env:ERP_AUTH_REFRESH_URL="https://erp-sandbox.example.com/auth/refresh"
$env:ERP_AUTH_REFRESH_TOKEN="your-refresh-token"
$env:ERP_AUTH_REFRESH_ACCESS_TOKEN_FIELD="access_token"
```

若 ERP 需要请求签名（HMAC-SHA256）：

```powershell
$env:ERP_SIGN_ENABLED="true"
$env:ERP_SIGN_HEADER="X-ERP-Sign"
$env:ERP_SIGN_TS_HEADER="X-ERP-TS"
$env:ERP_SIGN_SECRET="your-sign-secret"
```

如 ERP 返回字段名与默认契约不同，可额外配置（示例）：

```powershell
$env:ERP_ITEMS_FIELD="data"
$env:ERP_VALIDATE_VALID_FIELD="ok"
$env:ERP_VALIDATE_MISSING_FIELDS_FIELD="missing"
$env:ERP_DRAFT_NO_FIELD="draftNumber"
$env:ERP_DRAFT_URL_FIELD="draftLink"
$env:ERP_ERROR_CODE_FIELD="code"
$env:ERP_ERROR_MESSAGE_FIELD="msg"
```

如 ERP 要求请求体字段名与默认值不同，可再配置（示例）：

```powershell
$env:ERP_REQ_TYPE_FIELD="docType"
$env:ERP_REQ_PAYLOAD_FIELD="content"
$env:ERP_REQ_IDEMPOTENCY_FIELD="idemKey"
$env:ERP_REQ_PAYLOAD_FIELD_MAP='{"vendor_code":"vendorCode","doc_date":"documentDate"}'
```

**联调第一阶段（对方暂未提供 validate、或路径与默认不一致）**：

- `ERP_SKIP_UPSTREAM_VALIDATE=true`：不调 ERP 的校验接口，仅按 ingestion 的必填键在本地判断是否可进入 `VALIDATED`（与对方「先建好数据、校验后补」一致；**上线前应改回 false** 并接通真实 validate）。
- `ERP_VALIDATE_PATH`、`ERP_CREATE_DRAFT_PATH`：覆盖默认的 `/erp/drafts/validate` 与 `/erp/drafts`（与对方 Swagger 实际路径对齐）。
- `ERP_ALLOW_EMPTY_DRAFT_URL=true`：对方响应只有单号、无跳转 URL 时不报错（`draft_url` 可为空字符串）。

### 4.3 Datynk 销售订单（save-with-details）并联调（不等 Swagger 也可先做）

对方固定写单地址为 `POST /api/sale-order/save-with-details`、请求体为 `{ "order", "details" }`、成功响应 `{ "code":200, "data":"<单号>" }` 时，在启动 API 的终端中设置（PowerShell 示例）：

```powershell
$env:ERP_CLIENT_MODE="real"
$env:ERP_WRITE_BASE_URL="https://erp.datynk.com"
$env:ERP_CREATE_BODY_STYLE="datynk_sale_order"
$env:ERP_CREATE_DRAFT_PATH="/api/sale-order/save-with-details"
$env:ERP_ALLOW_EMPTY_DRAFT_URL="true"
$env:ERP_SKIP_UPSTREAM_VALIDATE="true"
# 若补全里不填 org，可设默认厂名：
# $env:ERP_DATYNK_DEFAULT_ORG="英科1厂"
# 鉴权暂关时可不配 token；恢复鉴权时可二选一：Bearer/API Key（ERP_API_TOKEN 等），或 Cookie 会话：
# $env:ERP_AUTH_MODE="cookie_session"
# $env:ERP_COOKIE_LOGIN_PATH="/api/auth/login"
# $env:ERP_LOGIN_USERNAME="..."
# $env:ERP_LOGIN_PASSWORD="..."
```

**本机文件方式（推荐）**：将 `backend/api/datynk-real.env.example` 复制为同目录下的 `.env`（勿提交 `.env`）。使用 `npm run dev:api` 时，`backend/api/backend/scripts/dev.cjs` 若发现 `backend/api/.env` 存在，会为 uvicorn 追加 **`--env-file`** 自动加载，无需在终端逐个 `$env:`。修改后**重启 API**，用 **`GET /health`** 确认 `erp_client_mode` 为 `real`。启动前若端口已被占用，脚本会**直接退出并提示** `netstat`/`taskkill`（避免误以为已切到 real 却仍打到旧进程）。

**主数据 404 与问答**：Datynk 往往不提供默认的 `/erp/vendors/search` 等路径；未单独配置时，适配层在 **`ERP_SOFT_FAIL_MASTER_SEARCH` 未显式关闭** 且 **`ERP_CREATE_BODY_STYLE=datynk_sale_order`** 时，会对主数据 GET 的 **404/405** 返回空列表（避免 `/chat/erp-qa` 整段失败）。`GET /health` 中 **`erp_soft_fail_master_search`** 可确认是否开启。Datynk 供应商分页实际多为 **`GET /api/supplier/page`**（非 `vendor`）；默认主数据路径已按此对齐，仍可用 `ERP_VENDORS_SEARCH_PATH` 覆盖。

**解析档案（推荐）**：仓库已提供 `backend/config/extraction_profiles/datynk-dev.json`，在 `POST /ingestions` 时传 **`extraction_profile_id":"datynk-dev"`**，会在 PO 默认必填之外增加 **`org`、`customerName`**，与 Datynk `order` 头对齐；其余仍用默认契约键（`vendor_code`、`doc_date`、`currency`、`material_code`、`line_qty` 等）。可选字段（`deliveryAddr`、`customerPoNo`、`salesUser`、`jhq` 等）可在 `/resolve` 补全；多行明细可在补全里传 **`datynk_details_json`**（合法 JSON 数组字符串）。

**销售订单问答**：`POST /chat/erp-qa` 中用户问题含「销售订单」「订单列表」「查订单」「客户订单」「发货单」等词时，会调用 **`search_sale_orders`**（`GET …/api/sale-order/page`，`org_id`→`org` 参数、关键词→`customerName`）。未显式设置 `ERP_SALE_ORDER_PAGE_ENABLED` 时，若已设 `ERP_CREATE_BODY_STYLE=datynk_sale_order` 则默认开启分页查询。

**客户主数据问答**：问题含「查客户」「客户列表」「往来单位」等（且**不构成**「销售订单」意图）时调用 **`search_customers`**（默认 `GET …/api/customer/page`，与销单分页相同 `pageNum`/`pageSize`/`org`，关键字参数名默认 `customerName`，可用 `ERP_CUSTOMER_PAGE_KEYWORD_PARAM` 覆盖）。`GET /health` 的 **`erp_customer_page_enabled`** 可确认是否启用。宽泛检索打开时，若客户分页已启用，会与其它主数据并行查询。

**宽泛主数据查询**：默认开启「无类型词时并行查供应商/物料/仓库/税码」，并在 **`erp_customer_page_enabled=true`** 时一并查 **客户分页**；库存/财报等泛问词会自动跳过（与销单兜底共用排除表）。若需关闭：`ERP_QA_BROAD_MASTER_SEARCH=false`。

**未识别意图时的销单兜底（默认开）**：若问题里**没有**供应商/物料/仓库/税码/客户/销售订单等类型词，且不像库存/报销等泛问，会再按 **销售订单分页**（`customerName` 关键字）试查一次，减少「说了公司名却查不到」的情况；不需要时设 **`ERP_QA_FALLBACK_WHEN_NO_INTENT=false`**。

**问答里的「报表 / 任意只读表」（配置化，不接大模型）**：复制仓库根 `backend/config/erp_qa_reports.example.json` 为 **`backend/config/erp_qa_reports.json`**（已加入 `.gitignore`，勿提交敏感路径），按 `match.any_substrings` / `all_substrings` 绑定用户说法，命中**唯一**一条时发 **GET**（路径须在 **`ERP_CUSTOM_READ_PATH_PREFIX`** 下，默认 `/api/`）；多条同时命中则只返回**澄清提示**、不调接口。`GET /health` 的 **`erp_qa_report_definitions_count`** 表示已加载条数。也可用环境变量 **`ERP_QA_REPORTS_PATH`** 指向自定义 JSON 文件。

**客户保存（新增客户）**：适配 `POST /api/customer/save`，请求体默认 **`{ "customer": { ...扁平字段 } }`**，成功响应按 Datynk 信封解析 **`data`**（字符串或含 `customerNumber`/`customerId` 的对象）。启用：`ERP_CUSTOMER_SAVE_ENABLED=true`；写双宿主时与 **`create_draft` 相同走写侧**。对外 HTTP：**`POST /integrations/erp/customer`**（JSON：`org_id`、`fields`；服务端会将 `org_id` 填入 `fields.org` 若未显式传入）。适配代码入口：`erp_client.save_customer(payload)`。

**最小验收**：上传/解析一条 **PO** → 补全至 `VALIDATED` → `POST /ingestions/{id}/create-draft` → `GET /ingestions/{id}` 中 `draft_no` 应为对方返回的 `data` 单号。

**健康检查**：`GET /health` 响应中含 `erp_client_mode`、`erp_sale_order_page_enabled`、`erp_customer_page_enabled`、`erp_customer_save_enabled`、`erp_create_body_style`、`erp_auth_mode`、`erp_qa_report_definitions_count`（不含密钥），便于确认当前进程加载的适配配置。

**服务索引**：`GET /` 返回 JSON，含 `links.health`、`links.docs`、ERP 客户保存与上传等相对路径，便于本机浏览器或脚本快速发现接口。

**命令行烟测**（可选）：仓库 `backend/scripts/smoke-datynk-erp.ps1`，示例  
`powershell -ExecutionPolicy Bypass -File backend/scripts/smoke-datynk-erp.ps1 -Org "英科1厂"`；需登录时加 `-User` / `-Password`。加 **`-ProbeCustomerSave`** 可在分页 GET 之后用同一 Cookie 会话 POST **`/api/customer/save`**（默认内层为 `org` + 随机 `customerName`；完整内层可传 **`-CustomerInnerJson`** JSON 字符串）。仅测客户、不调分页：**`-SkipSaleOrderPage -ProbeCustomerSave`**。  
**主数据路径批量探测**（本机一次跑完，不必在聊天里逐条贴 F12）：`backend/scripts/probe-datynk-master-paths.ps1`。  
1. 复制 `backend/scripts/datynk-probe.credentials.local.json.example` 为同目录 **`datynk-probe.credentials.local.json`**，填入 `baseUrl` / `org` / `username` / `password`（该文件名已写入根目录 `.gitignore`，**勿提交、勿在聊天发送密码**）。  
2. 执行：  
`powershell -ExecutionPolicy Bypass -File backend/scripts/probe-datynk-master-paths.ps1`  
也可仅用环境变量 `DATYNK_PROBE_USER`、`DATYNK_PROBE_PASSWORD`、`DATYNK_PROBE_ORG`（避免密码进命令行历史）。  
看各 Path 的 HTTP 与 `records_count`，把有数据的 path 写入 `ERP_*_SEARCH_PATH`。下次协作只需把**脚本输出**（可脱敏）贴出即可。`backend/api/datynk-real.env.example` 已写入与浏览器一致的默认主数据 path（供应商 `/api/supplier/page` 等），复制为 `.env` 后按需改 `ERP_DATYNK_DEFAULT_ORG` 或在前端选厂即可，**一厂/二厂共用同一套 path**。

**主数据与写单分属两套 ERP 时**（查询走系统 A，校验/建草稿走系统 B）：除 `ERP_BASE_URL` 外可配置 `ERP_DATA_BASE_URL`（`search_vendors` / `search_materials`）与 `ERP_WRITE_BASE_URL`（或 `ERP_TRANS_BASE_URL`，用于 `validate_draft` / `create_draft`）；未配的一侧回退到 `ERP_BASE_URL`。两套 token 不同时可用 `ERP_DATA_API_TOKEN`、`ERP_WRITE_API_TOKEN`（未配则用 `ERP_API_TOKEN`）。详见 `backend/docs/erp-integration-spec.zh-CN.md` §2.1。

真实 ERP 模式下的错误处理（当前实现）：

- `MASTER_DATA_NOT_FOUND` -> `ERP_MASTER_DATA_NOT_FOUND`
- `PERMISSION_DENIED/FORBIDDEN` -> `ERP_PERMISSION_DENIED`
- `UPSTREAM_TIMEOUT/TIMEOUT` -> `ERP_UPSTREAM_TIMEOUT`
- 其他异常 -> `ERP_UPSTREAM_ERROR`

在 `/resolve` 或 `/create-draft` 调用 ERP 异常时，ingestion 会写入 `FAILED` + `error_code`，并附带标准化 `error_details`，可通过 `GET /ingestions/{id}` 查询。主要字段：

- `category`: `master_data | permission | timeout | upstream_error`
- `erp_error_code`: ERP 原始错误码
- `erp_message`: ERP 原始错误信息
- `erp_status_code`: 上游 HTTP 状态码
- `upstream_request_id`: ERP 请求 ID（若有）
- `field_errors`: 字段级错误数组（来自 `violations`）
- `raw`: ERP 返回的原始结构化细节

前端消费建议（示例）：

```json
{
  "ingestion_id": "ing-001",
  "status": "FAILED",
  "error_code": "ERP_MASTER_DATA_NOT_FOUND",
  "error_details": {
    "category": "master_data",
    "erp_error_code": "MASTER_DATA_NOT_FOUND",
    "erp_message": "vendor not found",
    "erp_status_code": 404,
    "upstream_request_id": "erp-req-001",
    "field_errors": [
      { "code": "MASTER_DATA_NOT_FOUND", "field": "vendor_code", "message": "vendor not found" }
    ],
    "raw": {
      "violations": [
        { "code": "MASTER_DATA_NOT_FOUND", "field": "vendor_code", "message": "vendor not found" }
      ],
      "request_id": "erp-req-001"
    }
  }
}
```

前端渲染建议：

- 标题优先显示 `error_code` 对应文案。
- 副标题显示 `error_details.erp_message`（若有）。
- 字段级错误列表优先用 `error_details.field_errors`。
- 排障信息可折叠展示：`erp_status_code`、`upstream_request_id`、`raw`。
- 若需按业务口径调整文案，可在前端环境变量中覆盖：
  - `NEXT_PUBLIC_ERROR_CATEGORY_LABELS_JSON`
  - `NEXT_PUBLIC_ERROR_CATEGORY_HINTS_JSON`

健康检查：

```text
GET http://127.0.0.1:8000/health
```

## 4.1 启动前端（Next.js）

1. 复制前端环境变量模板：

```powershell
copy frontend\.env.example frontend\.env.local
```

2. **推荐**：在 `frontend/.env.local` 中**不要**设置 `NEXT_PUBLIC_API_BASE_URL`（或留空）。前端会走同源路径 `/api/orchestrator`，由 Next（`frontend/next.config.mjs`）转发到本机 API（默认 `http://127.0.0.1:8020`，可用环境变量 `ORCHESTRATOR_PROXY_TARGET` 覆盖）。这样用手机/局域网 IP 打开本机 Next（开发默认 **`http://x.x.x.x:3080`**）时也不会因直连 `127.0.0.1:8020` 而出现 **Failed to fetch**。若 API 部署在独立域名，再设置 `NEXT_PUBLIC_API_BASE_URL` 为完整 URL。

3. 若本机未启动 Redis/worker，可将 `NEXT_PUBLIC_ENABLE_DEV_INTERNAL=1` 打开，用于在页面点击「手动触发 process」模拟异步推进（仅开发）。

4. 安装依赖并启动：

```powershell
cd frontend
npm install
npm run dev
```

5. 浏览器打开 `http://127.0.0.1:3080`（本仓库将 Next 开发端口固定为 **3080**，避免与占用 **3000** 的其它本机进程冲突，如部分 IDE 内置服务）：拖拽文件 → 轮询状态 → 补全字段 → 创建草稿。

## 5. 启动 Worker（新终端）

```powershell
cd backend/worker
python -m pip install -r requirements.txt
$env:API_BASE_URL="http://127.0.0.1:8000"
$env:REDIS_URL="redis://127.0.0.1:6379/0"
python worker.py
```

预期看到 `worker started`，队列空闲时会周期输出 `worker heartbeat`。

## 6. 验证异步链路（上传 -> 入队 -> worker推进）

### 6.1 创建上传任务

推荐（multipart，服务端计算 SHA-256）：

```http
POST /uploads/binary
Content-Type: multipart/form-data

file: <binary>
user_id: u-demo
org_id: org-demo
```

兼容（JSON，需自行提供 file_hash）：

```http
POST /uploads
Content-Type: application/json

{
  "file_name": "demo-po.pdf",
  "file_hash": "hash-demo-001",
  "user_id": "u-demo",
  "org_id": "org-demo"
}
```

预期：返回 `status=UPLOADED`（说明只创建任务，等待 worker 异步处理）。

### 6.2 查询 ingestion

```text
GET /ingestions/{ingestion_id}
```

在 worker 消费 `process` 后，`audit_events` 中通常依次出现：

- `CLASSIFIED`
- `PARSED`
- `EXTRACTED`
- `MAPPED`

最终状态取决于**是否已满足当前 `doc_type_hint` 下的必填字段**（并由 Mock/真实 ERP 的 `validate_draft` 校验）：

- `doc_type_hint` 由工作流 **文件名**（`classify_doc_type_from_name`）与 **正文**（`classify_doc_type_from_text`）启发式推断，规则见 `backend/api/app/document_extract.py`（如 `vat-inv`、`goods-receipt`、`purchase order number` 等）。
- 若正文启发式已补全全部必填：可能直接进入 **`VALIDATED`**（工作流末尾会自动调用一次 `validate_draft`，并写入 `erp_call_log`）。
- 若仍有缺失：进入 **`NEED_USER_INPUT`**，需再调 `/resolve` 补全。

### 6.2.1 一键脚本验证（推荐）

仓库根目录提供了最小验证脚本 `verify_async_flow.py`，可自动完成：

- 创建上传任务（`POST /uploads`）
- 轮询 ingestion 状态（`GET /ingestions/{id}`）
- 输出最终状态和最后一条审计事件
- 可选：`--resolve-json` 在停在 `NEED_USER_INPUT` 时提交 `/resolve`（支持 `@路径.json` 读文件；JSON 可为字段对象或含 `fields` 的整段请求体）
- 可选：`--create-draft` 在已为 `VALIDATED` 时调用 `POST /ingestions/{id}/create-draft` 并打印草稿号
- 可选：**`--integrations-save-customer`**：不跑 ingestion，仅 `POST {api}/integrations/erp/customer`（JSON 或 `@文件`，须含 `org_id` 与 `fields`），用于自接验证客户写 ERP；样例请求体见仓库 **`backend/scripts/integrations-save-customer.sample.json`**。`@` 路径若在当前工作目录不存在，会再按 **`verify_async_flow.py` 所在仓库根**解析（便于在 `backend/api` 下执行仍写 `@backend/scripts/...`）。

执行示例：

```powershell
cd d:\workspace\ai-erp-assistant
python verify_async_flow.py --api-base http://127.0.0.1:8000 --org-id org-demo --user-id u-demo
python verify_async_flow.py --api-base http://127.0.0.1:8000 --create-draft
python verify_async_flow.py --api-base http://127.0.0.1:8000 --integrations-save-customer '{"org_id":"org-demo","fields":{"customerName":"脚本探测客户"}}'
```

若 worker 正常消费，预期最终状态为 `NEED_USER_INPUT` 或 `VALIDATED`（见上文）。若停在 `NEED_USER_INPUT` 且需一键闭环，可准备 `fields.json`（与当前 `doc_type_hint` 必填一致）后执行：

```powershell
python verify_async_flow.py --api-base http://127.0.0.1:8000 --resolve-json @fields.json --create-draft
```

### 6.3 补全并创建草稿

1) 补全字段：

```http
POST /ingestions/{ingestion_id}/resolve
Content-Type: application/json

{
  "fields": {
    "vendor_code": "V001",
    "doc_date": "2026-04-28",
    "currency": "CNY",
    "material_code": "M001",
    "line_qty": "1"
  }
}
```

（`PO` 在头字段外还需要 `material_code`、`line_qty`；`GR` / `INV` 的键名以后端 `required_field_keys` 为准。）

2) 创建草稿：

```http
POST /ingestions/{ingestion_id}/create-draft
```

预期：返回 `DRAFT_CREATED`、`draft_no`、`draft_url`、`idempotency_key`。

## 7. 常见问题

- **上传后一直是 `UPLOADED`**
  - 检查 worker 是否启动
  - 检查 Redis 是否可连
  - 检查 API 日志里是否有 `enqueue_failed`

- **create-draft 返回缺字段**
  - 先调用 `/resolve` 补齐必填字段，再重试 create-draft

- **端口占用**
  - 修改 `.env` / 启动命令端口，并保持 API_BASE_URL 与 worker 一致

## 8. 关闭环境

停止 API / worker 进程后执行：

```powershell
docker compose -f backend/infra/docker-compose.yml down
```

如需清理数据卷：

```powershell
docker compose -f backend/infra/docker-compose.yml down -v
```

## 9. 跨进程 e2e 一键验证（可选）

当你希望一次性验证 `api + worker + 异步链路`，可使用根目录脚本：

```powershell
cd d:\workspace\ai-erp-assistant
python run_local_e2e.py --api-host 127.0.0.1 --api-port 8000 --redis-url redis://127.0.0.1:6379/0 --org-id org-demo --user-id u-demo
```

说明：

- 该脚本会自动启动 API 和 worker 子进程；
- 内部调用 `verify_async_flow.py` 做链路验证；
- 完成后自动清理子进程；
- **不会自动启动 Redis**，请先执行 `docker compose -f backend/infra/docker-compose.yml up -d`。

## 10. 统一验证入口（quick/full）

为降低命令记忆成本，根目录提供统一脚本 `run_verification.py`：

- `quick`：仅验证链路（要求 API/worker 已手动启动）
- `full`：自动拉起 API+worker 再验证

示例：

```powershell
cd d:\workspace\ai-erp-assistant

# quick 模式（你已手动启动 API + worker）
python run_verification.py --mode quick --api-host 127.0.0.1 --api-port 8000 --org-id org-demo --user-id u-demo

# full 模式（自动拉起 API + worker）
python run_verification.py --mode full --api-host 127.0.0.1 --api-port 8000 --redis-url redis://127.0.0.1:6379/0 --org-id org-demo --user-id u-demo
```

## 11. 测试回归入口（quick/full）

根目录提供测试聚合脚本 `run_tests.py`：

- `quick`：跑核心回归（API/worker/验证入口脚本）
- `full`：跑仓库当前可发现的全部 pytest 用例

```powershell
cd d:\workspace\ai-erp-assistant

# 核心回归
python run_tests.py --mode quick

# 全量用例
python run_tests.py --mode full
```
