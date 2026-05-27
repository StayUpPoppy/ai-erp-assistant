import os
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.logging_config import setup_logging
from app.routes import router

# 先初始化日志：后续 lifespan 与中间件都需要 logger。
logger = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期：启动时初始化数据库表（若已配置 DATABASE_URL）。
    若未配置数据库连接串，则跳过建表，store 自动回退到内存模式。
    """
    from app.database import init_db, is_database_enabled

    try:
        init_db()
        if is_database_enabled():
            logger.info("数据库初始化完成：已检查/创建 ingestions 表")
        else:
            logger.info("未配置 DATABASE_URL：ingestion 使用内存存储（重启后数据丢失）")
    except Exception:
        logger.exception("数据库初始化失败，进程将退出以便及早发现问题")
        raise

    try:
        import pypdf  # noqa: F401

        _pypdf_ok = True
    except ImportError:
        _pypdf_ok = False
    try:
        import fitz  # noqa: F401

        _pymupdf_ok = True
    except ImportError:
        _pymupdf_ok = False
    logger.info(
        "runtime_python exe=%s pypdf_installed=%s pymupdf_installed=%s",
        sys.executable,
        _pypdf_ok,
        _pymupdf_ok,
    )
    if not _pypdf_ok and not _pymupdf_ok:
        logger.warning(
            "PDF 文字层不可用：当前解释器未安装 pypdf 与 pymupdf。请使用 backend/api/.venv "
            "或在当前环境执行 pip install -r backend/api/requirements.txt；本地推荐 npm run dev:api。"
        )
    yield


# 这是 API 进程入口：挂载路由、CORS、请求追踪中间件。
app = FastAPI(title="AI ERP Assistant API", version="0.1.0", lifespan=lifespan)

# 允许独立前端（Next.js 本地开发）跨域调用 API，否则浏览器会拦截 fetch。
# 默认覆盖常见本机场景（含非 3000 端口、IPv6）；局域网 IP 或生产域名请设 CORS_ALLOW_ORIGINS。
_cors_allow_origins = [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://127.0.0.1:3080",
    "http://localhost:3080",
]
_extra_origins = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
if _extra_origins:
    for _o in _extra_origins.split(","):
        _o = _o.strip()
        if _o and _o not in _cors_allow_origins:
            _cors_allow_origins.append(_o)
_cors_origin_regex = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip() or None
if _cors_origin_regex is None:
    # 本机任意端口；含局域网 IP（手机/同事机访问你电脑上的 Next 时常见），否则浏览器会报 Failed to fetch
    _cors_origin_regex = (
        r"^https?://("
        r"127\.0\.0\.1|localhost|\[::1\]"
        r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
        r")(:\d+)?$"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_origin_regex=_cors_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id"],
)

app.include_router(router)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    # 这里统一处理请求链路追踪：
    # 1) 如果客户端传了 x-request-id 就沿用，便于跨系统串联日志；
    # 2) 如果没传就自动生成，保证每次请求都可定位；
    # 3) 把 request_id 放进 request.state，路由层可以直接读取并写业务日志；
    # 4) 最后把 request_id 回写到响应头，方便前端与运维排查问题。
    request_id = request.headers.get("x-request-id") or str(uuid4())
    request.state.request_id = request_id
    logger.info("request_start request_id=%s method=%s path=%s", request_id, request.method, request.url.path)
    response = await call_next(request)
    logger.info(
        "request_end request_id=%s method=%s path=%s status_code=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
    )
    response.headers["x-request-id"] = request_id
    return response
