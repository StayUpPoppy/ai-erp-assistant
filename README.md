# AI ERP Assistant

Conversation-first AI assistant UI + FastAPI orchestration + async worker for ERP Q&A and document-to-ERP draft flows.

## Monorepo

- `frontend`: Next.js frontend
- `backend/api`: FastAPI API, assistant routes, document extraction, OCR, ERP orchestration
- `backend/worker`: Redis worker that advances uploaded file jobs through parse/extract/map
- `backend/packages/erp-client`: ERP adapter SDK
- `backend/packages/shared`: shared Python contract package
- `backend/config/extraction_profiles`: optional extraction profiles
- `backend/infra`: local Docker Compose for Redis/Postgres/MinIO

## Quick Start Docs

- `backend/docs/runbook.md`: detailed local startup and verification steps
- `backend/docs/handover-vs-repo.md`: handover goals mapped to repository implementation
- `backend/docs/project-progress.zh-CN.md`: milestone progress
- `backend/packages/erp-client/README.md`: ERP adapter contract
- `backend/docs/erp-integration-spec.zh-CN.md`: real ERP integration details
- `backend/docs/erp-colleague-handoff.zh-CN.md`: checklist for ERP-side colleagues
- `frontend/.env.example`: frontend env template

## Local Assistant Dev

Recommended local ports for the conversation-first assistant:

- API: `http://127.0.0.1:8020`
- Frontend: `http://127.0.0.1:3080`
- API env file: `backend/api/.env`
- Worker queue: must match `/health.queue_name`

Start order:

1. Infrastructure
2. API
3. Worker
4. Frontend
5. Health check

1. docker compose 启动 Redis/Postgres/MinIO
2. 启动 API 8020，读取 backend/api/.env
3. 启动 worker，连接同一个 Redis 队列 ingestion_jobs_8020
4. 启动 frontend 3080
5. 运行 scripts/check-local.ps1

### 1. Start Infrastructure

```powershell
docker compose -f backend/infra/docker-compose.yml up -d
```

This starts Redis/Postgres/MinIO for local development.

### 2. Start API

```powershell
cd backend/api
python -m uvicorn main:app --host 127.0.0.1 --port 8020 --env-file .env
```

Use `backend/api/.env` for local API configuration, including LLM and ERP settings.
The assistant tool router uses quick defaults (`ASSISTANT_ROUTER_MAX_TOKENS=512`,
`ASSISTANT_ROUTER_TIMEOUT_SECONDS=20`) so normal chat can begin streaming sooner.
Obvious ordinary chat can bypass tool routing with
`ASSISTANT_PLAIN_CHAT_FAST_PATH_ENABLED=true`; ERP/PDF/business-keyword messages
still go through the router.

### 3. Start Worker

```powershell
cd backend/worker
$env:REDIS_URL="redis://127.0.0.1:6379/0"
$env:QUEUE_NAME="ingestion_jobs_8020"
$env:API_BASE_URL="http://127.0.0.1:8020"
$env:WORKER_PROCESS_TIMEOUT_SECONDS="120"
python worker.py
```

The worker is required for uploaded files to move through:

```text
UPLOADED -> CLASSIFIED -> PARSED -> EXTRACTED -> MAPPED -> NEED_USER_INPUT / VALIDATED
```

`QUEUE_NAME` must match the API `/health` response field `queue_name`. If they differ, uploads can be enqueued successfully while the worker listens to another queue, and PDF-to-ERP tasks will appear stuck.

### 4. Start Frontend

```powershell
cd frontend
$env:ORCHESTRATOR_PROXY_TARGET="http://127.0.0.1:8020"
npm run dev
```

Open `http://127.0.0.1:3080`.

### 5. Check Local Readiness

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check-local.ps1
```

The script checks API health, frontend HTTP, LLM router, ERP mode, queue availability, and OCR engine.

## ERP Mode

If `/health` reports `erp_client_mode=mock`, clicking "confirm upload ERP" only creates a mock draft number. It does not write to the real ERP.

Switch `ERP_CLIENT_MODE=real` and the real ERP settings in `backend/api/.env` before a real integration test.


## 常用维护命令
重启全部服务：
cd /ai-erp-assistant
docker compose --env-file deploy/ecs-panel/prod.env -f deploy/ecs-panel/docker-compose.prod.yml restart

只重启 后端API：
docker compose --env-file deploy/ecs-panel/prod.env -f deploy/ecs-panel/docker-compose.prod.yml restart api

更新代码后重新构建ta：
cd /ai-erp-assistant
git pull
docker compose --env-file deploy/ecs-panel/prod.env -f deploy/ecs-panel/docker-compose.prod.yml up -d --build

映射网站
http://111.170.173.2:8081/