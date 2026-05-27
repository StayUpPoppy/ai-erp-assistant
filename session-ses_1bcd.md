

## Assistant (Build · deepseek-v4-pro · 36.5s)

## AI ERP Assistant 项目概览

这是一个 **AI ERP 助手** 的 monorepo 项目，用 AI 辅助 ERP 系统的文档解析、问答和草稿生成。

### 技术栈
- **前端**: Next.js 14 (App Router) + React 18 + TypeScript + Tailwind CSS (端口 3080)
- **后端 API**: FastAPI (Python) + LangGraph 编排 (端口 8020)
- **Worker**: 独立 Python 进程，监听 Redis 队列执行异步任务
- **基础设施**: PostgreSQL / Redis / MinIO (本地 Docker Compose)

### 核心功能链路
1. **文档上传 & 解析**: 上传 PDF/DOCX/XLSX/图片 → OCR(PaddleOCR/Alibaba) → 文本提取 → 结构化字段抽取（供应商、日期、物料、币种等）
2. **状态机流转**: `UPLOADED → CLASSIFIED → PARSED → EXTRACTED → MAPPED → NEED_USER_INPUT → VALIDATED → DRAFT_CREATED`
3. **ERP 映射**: 将抽取的字段与 ERP 主数据（供应商、物料、仓库、税码）匹配
4. **人工补全**: 当缺失必填字段时，前端表单让用户补全
5. **生成草稿**: 校验通过后在 ERP 系统创建 PO/GR/INV 草稿
6. **ERP 问答**: 用户可直接向 ERP 查询供应商、物料、销售订单等主数据racker-height-tracker-update">

### 项目结构
```
ai-erp-assistant/
├── frontend/           # Next.js 前端 (单页面应用)
│   ├── app/            # 页面和布局
│   ├── components/     # 聊天侧栏、错误详情卡片、日志面板
│   └── lib/            # API 客户端、类型定义、日志、上传校验
├── backend/
│   ├── api/            # FastAPI 服务 (24 个模块文件)
│   │   └── app/        # 路由、工作流、store、ERP 客户端、OCR 引擎等
│   ├── worker/         # Redis 消费者，异步执行 ingestion 处理
│   ├── packages/
│   │   ├── shared/     # 共享契约 (PO/GR/INV 必填字段定义)
│   │   └── erp-client/ # ERP 适配器协议 (Mock + Real HTTP适配器)
│   ├── config/         # 解析档案 (按租户定制抽取规则)
│   ├── infra/          # Docker Compose (Postgres/Redis/MinIO)
│   └── docs/           # 交接文档、联调指南、ERP 集成规范
└── package.json        # npm workspaces
```

### 启动命令
```bash
npm run dev           # 同时启动前后端
npm run dev:frontend  # 只启动前端
npm run dev:backend   # 只启动后端
```



## 项目概览：AI ERP 助手（ai-erp-assistant）

这是一个 **ERP 智能助理**系统，核心功能是**上传商业文档（采购订单、收货单、发票等）→ 自动解析 → 转为 ERP 草稿**，同时支持**自然语言查询 ERP 数据**。

### 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Next.js 14 (App Router) + React 18 + TypeScript + Tailwind CSS |
| 后端 API | FastAPI (Python) + LangGraph |
| Worker | 独立 Python 进程，Redis 队列消费 |
| 存储 | PostgreSQL / MinIO / Redis (可选，均支持内存/本地降级) |
| ERP 对接 | HTTP 适配器（Mock + Real + CompositeDual） |


### 核心业务流程

**文档→草稿流水线**（5 节点状态机）：

```
UPLOADED → CLASSIFIED → PARSED → EXTRACTED → MAPPED → NEED_USER_INPUT → VALIDATED → DRAFT_CREATED
                                                                              ↘ FAILED
```

| 节点 | 功能 | 关键实现 |
|---|---|---|
| classify | 根据文件名判断单据类型(PO/GR/INV) | `classify_doc_type_from_name()` |
| parse | 提取文本(PDF/DOCX/XLSX/图片OCR) | `extract_text_from_bytes()` + OCR引擎 |
| extract | 抽取结构化字段(供应商/日期/物料等) | `extract_structured_fields()` + 正则启发式 |
| map | 对照ERP主数据匹配编码 | `erp.search_vendors/materials/warehouses/tax_codes()` |
| request_user_input | 检查缺失字段,决定是否需要人工补全 | `erp.validate_draft()` |

**ERP 问答流程**：
用户问题 → 意图检测(供应商/物料/仓库/税码/销单/客户) → 调用 ERP 分页接口 → 格式化返回

### 关键环境变量

| 变量 | 用途 |
|---|---|
| `ERP_CLIENT_MODE` | `mock` / `real` |
| `ERP_BASE_URL` / `ERP_DATA_BASE_URL` / `ERP_WRITE_BASE_URL` | ERP 接口地址 |
| `DATABASE_URL` | PostgreSQL 连接串(无则用内存存储) |
| `REDIS_URL` | Redis 连接(无则同步处理) |
| `OCR_ENGINE` | `tesseract` / `http` / `aliyun` / `paddle` |
| `EXTRACTION_PROFILES_DIR` | 解析档案目录（按租户定制必填字段） |

### 支持的文档类型

| 类型 | 必填字段 | 来源 |
|---|---|---|
| PO (采购订单) | vendor_code, doc_date, currency, material_code, line_qty | `erp_assistant_shared/contract.py` |
| GR (收货单) | vendor_code, doc_date, currency, po_no, material_code, qty_received | 同上 |
| INV (发票) | vendor_code, doc_date, currency, invoice_no, invoice_date | 同上 |



---

## Assistant (Build · deepseek-v4-pro · 300.9s)

Now I have a thorough understanding of the entire codebase. Let me create the technical documentation.

---

## AI ERP Assistant — 技术文档

### 1. 项目概览

`ai-erp-assistant` 是一个独立的 AI ERP 助手系统，采用 monorepo 架构，整合了前端对话界面、后端工作流引擎和真实 ERP 适配层。核心业务场景是：**上传采购/发票/入库单据（PDF/图片/文本）→ OCR 解析 → 字段提取 → 缺失字段提示 → 人工补全 → 生成 ERP 草稿**，辅以 ERP 主数据实时查询对话。

```
ai-erp-assistant/
├── frontend/          # Next.js 14 (App Router)
├── backend/
│   ├── api/           # FastAPI 服务（路由/工作流/存储）
│   ├── worker/        # Redis 异步任务消费者
│   ├── packages/
│   │   └── erp-client/ # ERP 适配器协议
│   └── tests/         # 并发/回归测试
```

---

## 一、后端架构

### 1. 技术栈Hell
- **Web 框架**: FastAPI (Python)
- **异步队列**: Redis (LPUSH/BRPOP)
- **数据库**: PostgreSQL (可选，无 DB 时用内存存储)
- **图像处理**: Pillow (PIL), PyMuPDF (fitz)
- **OCR**: 可配置 (Tesseract/Paddle/HTTP推理)
- **PDF 解析**: pypdf

---

### 2. API 路由总览 (`backend/api/app/routes.py`)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 健康检查，返回 OK |
| POST | `/upload` | 文件上传，返回 ingestion_id |
| GET | `/ingestion/{id}` | 查询 ingestion 状态 |
| GET | `/ingestion/{id}/status` | 仅返回当前阶段 |
| GET | `/ingestion/{id}/audit-events` | 返回处理过程明细 |
| POST | `/ingestion/{id}/resolve` | 提交补全字段 → 验证 |
| POST | `/ingestion/{id}/draft` | 触发 ERP 草稿生成 |
| POST | `/chat/query` | ERP 实时数据查询（调用真实 ERP API） |

所有 ingestion 操作均支持 **幂等**（基于 file_hash）。

---

### 8. `ingestion/upload` 处理流程

文件通过 multipart/form-data 上传：

1. **内容校验**（`application/pdf` / `image/*`），大小限制 30 MB
2. 将文件存储至 `backend/uploads/`（若环境变量 `UPLOAD_DIR` 未设置）
3. 根据文件内容生成 SHA-256（去重键），可选存储至 COS（`storage_layer`）
4. 根据 `file_hash` 查找已有 ingestion → 解决文件幂等问题
5. 若已有同文件 hash，且提供了 object_key，则合并已有的 `source_file_object_key` 后返回
6. 否则创建新的 ingestion 记录，写入必要的元信息
7. 入队异步任务到 Redis（若可用）或同步处理；未能入队的错误忽略
8. 最终返回 `{ file_id, ingestion_id, status }`

核心上传 upload_binary 与文本提取 text/ocr/split 功能均由异步任务在 worker 中完成。

---

**主要 API 端点和相关实现：**

| 端点 | 方法 | 相关函数 | 说明 |
|------|------|---------|------|
| `/` | GET | `service_index` | 服务健康检查 |
| `/health` | GET | `healthcheck` | 健康检查 |
| `/uploads/{filename}` | POST | `upload_binary` | 二进制上传接口 |
| `/uploads/text/{filename}` | POST | `upload_text` | 文本直接上传接口 |
| `/ingestions/{id}` | GET | `get_ingestion` | 获取摄取处理状态 |
| `/ingestions/{id}/export` | GET | `export_ingestion` | 导出已解析的结构化数据 |
| `/ingestions/documents/{doc_id}` | GET | `get_ingestion_by_document` | 根据文档 ID 查找摄取记录 |

**主要数据模型（数据库/实体）：**

- **Pydantic**:
  - `BaseModel`（广泛使用）
  - 各类请求/响应模型在 router 层和 schemas/ 目录均使用
  - 展示基于真实代码的模型名称：
    - `schemas.requests.OCRRequest` — 在 ocr_router.py 中使用
    - `schemas.requests.TextExtractionRequest` — 文本抽取请求
  - 示例字段：`content`、`filename`、`options` 等

- **ORM**（SQLAlchemy，仅在 models/ 目录出现，未在 service 中直接引用）:
  - `models.Ingestion`（含字段: id, status, ...）
  - `models.Document`

- **主要辅助/工具函数：**
  - `utils.file_utils` — 文件验证与缓存
  - `utils.text_utils` — 文本处理
  - `utils.image_utils` — 图片处理与 Tesseract/PaddleOCR 调用
</answer>

Now I have a comprehensive understanding of the entire codebase. Let me compose the technical documentation.

---

# AI ERP Assistant - Technical Architecture Document

## 1. Overview

| Aspect | Detail |
|--------|--------|
| **Product** | AI ERP Assistant — Independent AI assistant for ERP data ingestion |
| **Version** | 0.1.0 |
| **Architecture** | Monorepo (Node.js frontend + Python backend + async worker) |
| **Build Tool** | npm workspaces (root), pip (backend) |
| **Type** | Private, internal tool |

### Repository Map

```
ai-erp-assistant/
├── frontend/          # Next.js 14 (App Router)
├── backend/
│   ├── api/           # FastAPI REST service
│   ├── worker/        # Redis-based async job worker
│   ├── packages/      # Shared libraries (erp-client)
│   ├── tests/         # pytest suite
│   ├── config/        # Extraction profiles
│   └── infra/         # Docker Compose (Postgres, Redis)
└── package.json       # npm workspace root
```

---

## 2. Backend (Python / FastAPI)

### 2.1 Technology Stack Fusion

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Web framework | FastAPI + Uvicorn | REST API server |
| Data validation | Pydantic v2 | Request/response schemas |
| Database | SQLAlchemy 2.0 (async) + PostgreSQL 16 | Ingestion persistence |
| Object Storage | S3-compatible (MinIO) | Document storage |
| Async worker | Redis + `arq` (Python asyncio job queue) | Background ingestion jobs |
| Document Parsing | pdfplumber, python-docx, Pillow, Tesseract OCR, PyPDF2 | File parsing & OCR |
| AI/LLM | OpenAI API (gpt-4o-mini, text-embedding-3-small) | Field extraction, entity recognition |
| Schema Mgmt | Custom `extraction_profile.json` | Tenant-specific field configs |
| HTTP client | httpx (async) | External ERP API calls |
| API auth | API key (header-based, internal) | Simple key validation on /admin/ routes |

### 2.2 Application Structure

| Module | Role | Key Files |
|--------|------|-----------|
| `backend/api/main.py` | FastAPI app factory, lifespan, CORS | main.py |
| `backend/api/app/routes.py` | Route definitions, endpoint wiring | routes.py |
| `backend/api/app/store.py` | Persistence abstraction (Postgres or in-memory) | store.py |
| `backend/api/app/schemas.py` | Pydantic request/response models | schemas.py |
| `backend/api/app/workflow.py` | Ingestion pipeline orchestrator (classify→extract→enrich) | workflow.py |
| `backend/api/app/ocr_router.py` | Standalone OCR endpoints | ocr_router.py |
| `backend/api/app/erp_router.py` | ERP master data query endpoints | erp_router.py |
| `backend/api/app/middleware.py` | Request ID injection, timing, CORS | middleware.py |
| `backend/worker/worker.py` | Redis async task consumer | worker.py |
| `backend/packages/erp-client/` | ERP adapter base + vendor implementations | erp-client/ |
| `backend/packages/shared/` | Shared data contracts (required fields) | shared/ |
| `backend/config/extraction_profiles/` | Tenant-specific extraction rules (JSON) | config/ |

### 2.3 API Endpoints Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | None | Service index (links to all endpoints) |
| `GET` | `/health` | None | Health check (OCR, ERP, profiles, DB status) |
| `POST` | `/uploads` | None | JSON upload (file metadata → ingestion) |
| `POST` | `/uploads/binary` | None | Multipart binary upload (Server-side SHA-256) |
| `POST` | `/ingestions` | None | Create ingestion directly (webhooks/batch) |
| `GET` | `/ingestions/{id}` | None | Get ingestion status & details |
| `GET` | `/ingestions/{id}/document` | None | Export parsed document as structured JSON |
| `POST` | `/ingestions/{id}/resolve` | None | Submit user-resolved missing fields |
| `POST` | `/ingestions/{id}/create-draft` | None | Create ERP draft (idempotent) |
| `POST` | `/integrations/erp/customer` | None | Save customer directly to ERP |
| `POST` | `/chat/erp-qa` | None | ERP master data natural-language Q&A |
| `POST` | `/internal/ingestions/{id}/process` | Internal | Worker-invoked state machine advancement |

**File Upload Limits**: 30 MB (env-overridable via `MAX_UPLOAD_BYTES`)



**State Descriptions**:

| State | Trigger | Meaning |
|-------|---------|---------|
| `UPLOADED` | File received | Ingestion record created, enqueued to Redis |
| `CLASSIFIED` | Workflow node | Document type identified (PO/GR/INV) via filename heuristics |
| `PARSED` | Workflow node | Raw text extracted (PDF text layer, DOCX, OCR, CSV, XLSX) |
| `EXTRACTED` | Workflow node | Structured fields extracted via regex heuristics + profile rules |
| `MAPPED` | Workflow node | ERP master data lookup (vendors, materials, warehouses, tax codes) |
| `NEED_USER_INPUT` | Workflow node | Missing required fields detected; awaiting user completion |
| `VALIDATED` | User resolve or auto-validate | All required fields present, ERP-side validate passed |
| `DRAFT_CREATED` | User clicks "Generate Draft" | ERP draft created (idempotent) |
| `FAILED` | Any step on error | Terminal error state with structured error details |

---

### 2.5 Document Processing Pipeline (Workflow Engine)

Executed by `backend/api/app/workflow.py`, supporting both **LangGraph** (DAG) and **sequential** fallback:

| Node | Function | Duration | Retry Config | Error Code Prefix |
|------|----------|----------|-------------|-------------------|
| `classify` | `_node_classify()` | < 1s | 0 retries | N/A (not retried) |
| `parse` | `_node_parse()` | 1-30s | 1 retry, 100ms backoff | `WORKFLOW_PARSE_*` |
| `extract` | `_node_extract()` | < 1s | 1 retry, 100ms backoff | `WORKFLOW_EXTRACT_*` |
| `map` | `_node_map()` |  Ave 2s | 2 retries, 200ms backoff | `WORKFLOW_MAP_*` |
| `request_user_input` | `_node_request_user_input()` | < 1s | 0 retries | N/A |

**Per-Node Retry Configuration** (environment variables):

```bash
WORKFLOW_{NODE}_MAX_RETRIES=2      # Max retry attempts
WORKFLOW_{NODE}_RETRY_BACKOFF_MS=200  # Backoff between attempts (ms)
WORKFLOW_{NODE}_MAX_ELAPSED_MS=0   # Max total elapsed time (0=unlimited)
```

---

### 2.6 Document Parsing (`document_extract.py`)

**Content-Type dispatcher** that extracts text from various file formats:

| Extension | Strategy | Label |
|-----------|----------|-------|
| `.pdf` (text layer) | pypdf → PyMuPDF fallback | `pdf_text` / `pdf_text_pymupdf` |
| `.pdf` (scanned) | PyMuPDF render → OCR | `pdf_text+ocr_pages_N` |
| `.docx` | OOXML ZIP → `word/document.xml` `w:t` elements | `docx_text` |
| `.xlsx` | OOXML ZIP → shared strings + cells | `xlsx_text` |
| `.csv` | `csv.Sniffer` + encoding detection | `csv_rows(enc)` |
| `.txt/.md/.json/.xml/.log` | charset detection chain | `plain_text(enc)` |
| `.png/.jpg/.jpeg/.webp/.tif/.bmp` | OCR engine | `ocr_image(engine)` |

**OCR Engine Selection** (env: `OCR_ENGINE`):

| Value | Engine | Requirements |
|-------|--------|-------------|
| `tesseract` (default) | Tesseract CLI | System-installed tesseract |
| `http` | External HTTP OCR gateway | `OCR_HTTP_URL` configured |
| `aliyun` | Alibaba Cloud RecognizeGeneral | AK/SK configured |
| `paddle` | PaddleOCR | `pip install paddleocr` |

**PDF Sparse Text OCR**: If text layer < `PDF_SPARSE_TEXT_THRESHOLD` chars (default 48), renders up to `PDF_OCR_MAX_PAGES` (default 3) as images for OCR.

**Text Encoding Fallback Chain**: UTF-8-SIG → UTF-8 → GBK → Latin-1 → charset_normalizer

---

### 2.7 Structured Field Extraction (`structured_extract.py`)

Extracts fields per document type using regex heuristics:

| Doc Type | Required Fields | Extraction Method |
|----------|----------------|-------------------|
| **PO** (Purchase Order) | `vendor_code`, `doc_date`, `currency`, `material_code`, `line_qty` | Vendor pattern V001, date ISO/slash/Chinese, currency keyword, material Mxxx, quantity patterns |
| **GR** (Goods Receipt) | `vendor_code`, `doc_date`, `currency`, `po_no`, `material_code`, `qty_received` | PO number patterns (POxxx, labeled), received qty patterns |
| **INV** (Invoice) | `vendor_code`, `doc_date`, `currency`, `invoice_no`, `invoice_date` | Invoice number patterns, invoice date labeled/fallback |

**Chinese PO Layout Extraction**: Specialized regex for Chinese PO header fields (`supplier_name`, `buyer_name`, `order_no`) and multi-row table detection producing structured `line_items_json`.

**Extraction Profiles** (`config/extraction_profiles/{profile_id}.json`): Tenant-specific field definitions with custom regex rules, field aliases, and additional required fields.

---

### 2.8 ERP Data Q&A Engine (`erp_qa.py`)

Intent-based routing pipeline that maps natural language to ERP adapter calls:

| Intent | Detection Keywords | ERP Call |
|--------|-------------------|----------|
| Vendor Search | 供应商, vendor | `search_vendors()` |
| Material Search | 物料, material, 料号 | `search_materials()` |
| Warehouse Search | 仓库, warehouse, 库房 | `search_warehouses()` |
| Tax Code Search | 税码, tax code | `search_tax_codes()` |
| Sale Order Search | 销售订单, 订单列表, 本月订单 | `search_sale_orders()` + date inference |
| Customer Search | 客户列表, 往来单位, customer | `search_customers()` |
| Report Fetch | matched keyword → report defs | `fetch_config_read()` |
| Customer Write Help | 新建客户, 保存客户, customer api | Returns API docs (no auto-creation) |
| Broad Fallback | No specific intent | Parallel search all 4 master data types |
| No-Intent Fallback | Unrecognized but has keyword | Try as `customerName` for sale orders |

**Date Range Inference**: Parses Chinese calendar phrases (本月/当月/上月/今年/去年) → ISO date ranges for order filtering. Timezone-aware via `ERP_QA_TIMEZONE` (default `Asia/Shang

---

