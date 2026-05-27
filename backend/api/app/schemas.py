from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


class IngestionStatus(str, Enum):
    # ingestion 统一状态机。
    # API、worker、前端都依赖这一组枚举来描述任务当前阶段，
    # 保证跨模块状态语义一致，避免“同一状态不同命名”的对齐成本。
    UPLOADED = "UPLOADED"
    CLASSIFIED = "CLASSIFIED"
    PARSED = "PARSED"
    EXTRACTED = "EXTRACTED"
    MAPPED = "MAPPED"
    VALIDATED = "VALIDATED"
    NEED_USER_INPUT = "NEED_USER_INPUT"
    DRAFT_CREATED = "DRAFT_CREATED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class ErrorCode(str, Enum):
    # 稳定错误码定义。
    # 前端展示、审计检索、报警统计都应基于错误码而非错误文案，
    # 这样即使文案变化也不影响系统联动逻辑。
    INGESTION_NOT_FOUND = "INGESTION_NOT_FOUND"
    MISSING_REQUIRED_FIELDS = "MISSING_REQUIRED_FIELDS"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
    WORKFLOW_NODE_FAILED = "WORKFLOW_NODE_FAILED"
    WORKFLOW_RETRY_EXHAUSTED = "WORKFLOW_RETRY_EXHAUSTED"
    WORKFLOW_RETRY_TIMEOUT = "WORKFLOW_RETRY_TIMEOUT"
    WORKFLOW_PARSE_RETRY_EXHAUSTED = "WORKFLOW_PARSE_RETRY_EXHAUSTED"
    WORKFLOW_EXTRACT_RETRY_EXHAUSTED = "WORKFLOW_EXTRACT_RETRY_EXHAUSTED"
    WORKFLOW_MAP_RETRY_EXHAUSTED = "WORKFLOW_MAP_RETRY_EXHAUSTED"
    WORKFLOW_PARSE_RETRY_TIMEOUT = "WORKFLOW_PARSE_RETRY_TIMEOUT"
    WORKFLOW_EXTRACT_RETRY_TIMEOUT = "WORKFLOW_EXTRACT_RETRY_TIMEOUT"
    WORKFLOW_MAP_RETRY_TIMEOUT = "WORKFLOW_MAP_RETRY_TIMEOUT"
    WORKFLOW_UNEXPECTED_ERROR = "WORKFLOW_UNEXPECTED_ERROR"
    ERP_MASTER_DATA_NOT_FOUND = "ERP_MASTER_DATA_NOT_FOUND"
    ERP_PERMISSION_DENIED = "ERP_PERMISSION_DENIED"
    ERP_UPSTREAM_TIMEOUT = "ERP_UPSTREAM_TIMEOUT"
    ERP_UPSTREAM_ERROR = "ERP_UPSTREAM_ERROR"
    ERP_CUSTOMER_SAVE_DISABLED = "ERP_CUSTOMER_SAVE_DISABLED"
    INGESTION_CANCELED = "INGESTION_CANCELED"


class HealthResponse(BaseModel):
    status: str
    tesseract_available: bool = False
    tesseract_resolution: Optional[str] = None
    tesseract_cmd: Optional[str] = None
    extraction_profiles_dir: Optional[str] = None
    extraction_profile_json_count: int = 0
    ocr_engine: Optional[str] = None
    ocr_http_url_configured: bool = False
    ocr_engine_auto_fallback: bool = True
    paddleocr_importable: bool = False
    aliyun_ocr_configured: bool = False
    erp_client_mode: str = "mock"
    erp_sale_order_page_enabled: bool = False
    erp_customer_page_enabled: bool = False
    erp_create_body_style: str = "mock"
    erp_auth_mode: str = "bearer"
    erp_customer_save_enabled: bool = False
    erp_soft_fail_master_search: bool = False
    erp_master_search_query_style: str = "legacy"
    erp_master_search_datynk_envelope: bool = False
    erp_data_base_netloc: str = ""
    erp_vendors_search_path: str = ""
    erp_materials_search_path: str = ""
    erp_warehouses_search_path: str = ""
    erp_tax_codes_search_path: str = ""
    erp_customer_page_path: str = ""
    erp_vendors_search_query_key: str = ""
    erp_materials_search_query_key: str = ""
    erp_warehouses_search_query_key: str = ""
    erp_tax_codes_search_query_key: str = ""
    erp_customer_page_keyword_param: str = ""
    erp_qa_report_definitions_count: int = 0
    llm_extract_enabled: bool = False
    llm_router_enabled: bool = False
    llm_api_key_configured: bool = False
    llm_model: str = ""
    llm_base_url: str = ""
    llm_prompt_version: str = ""
    queue_backend: str = "redis"
    queue_name: str = "ingestion_jobs"
    queue_available: bool = False
    ingestion_queue_fallback_mode: str = "none"


class DocType(str, Enum):
    PO = "PO"
    GR = "GR"
    INV = "INV"


class UploadRequest(BaseModel):
    file_name: str
    file_hash: str
    user_id: str
    org_id: str
    # 原始文件在对象存储中的 key（可为空：例如未配置 MinIO 时降级运行）。
    source_file_object_key: Optional[str] = None
    # 解析档案 id（backend/config/extraction_profiles/{id}.json）；为空则按 org_id / default.json 自动解析。
    extraction_profile_id: Optional[str] = None
    extract_version: str = "v0"
    model_version: str = "mock-llm-v1"
    prompt_version: str = "prompt-v1"


class UploadResponse(BaseModel):
    file_id: str
    ingestion_id: str
    status: IngestionStatus


class CreateIngestionRequest(BaseModel):
    file_id: str
    file_hash: str
    user_id: str
    org_id: str
    source_file_object_key: Optional[str] = None
    # 原始上传文件名（multipart 等）；无对象存储时仍用于扩展名识别与单据类型启发式分类。
    source_file_name: Optional[str] = None
    doc_type_hint: Optional[DocType] = None
    extraction_profile_id: Optional[str] = None
    extract_version: str = "v0"
    model_version: str = "mock-llm-v1"
    prompt_version: str = "prompt-v1"


class AuditEvent(BaseModel):
    at: str
    status: IngestionStatus
    message: str


class IngestionResponse(BaseModel):
    # ingestion 查询接口的标准返回模型。
    # 前端轮询状态、展示缺失字段、显示草稿链接、回放审计轨迹都依赖这里的数据结构。
    ingestion_id: str
    file_id: str
    file_hash: str
    user_id: str
    org_id: str
    source_file_object_key: Optional[str] = None
    source_file_name: Optional[str] = None
    extract_version: str
    model_version: str
    prompt_version: str
    status: IngestionStatus
    doc_type_hint: Optional[DocType] = None
    # 生效的解析档案（文件名不含 .json）；无档案时为 None，走 shared 默认必填。
    extraction_profile_id: Optional[str] = None
    # 创建任务时请求体中的 extraction_profile_id（可能因文件不存在而未生效）。
    extraction_profile_requested: Optional[str] = None
    # 生效档案的解析方式：explicit | org_id | default | none
    extraction_profile_resolution: Optional[str] = None
    # 当前单据类型下用于补全/缺项判断的必填键顺序（含档案扩展字段）。
    required_resolve_keys: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    resolved_fields: Dict[str, str] = Field(default_factory=dict)
    audit_events: List["AuditEvent"] = Field(default_factory=list)
    draft_no: Optional[str] = None
    draft_url: Optional[str] = None
    error_code: Optional[str] = None
    # 结构化错误详情（用于前端展示与审计检索），例如 violations/details/request_id。
    error_details: Dict[str, object] = Field(default_factory=dict)
    # 文档解析摘要（有对象存储且为可解析格式时由 worker 流程写入）
    extract_preview: Optional[str] = None
    parsed_char_count: Optional[int] = None
    # 正文解析路径标签（如 pdf_text、docx_text、ocr_image），便于前端与排障
    parse_format_label: Optional[str] = None
    # ERP 映射阶段返回的主数据候选（审计与前端补全提示共用）
    vendor_candidates: List[Dict[str, str]] = Field(default_factory=list)
    material_candidates: List[Dict[str, str]] = Field(default_factory=list)
    warehouse_candidates: List[Dict[str, str]] = Field(default_factory=list)
    tax_code_candidates: List[Dict[str, str]] = Field(default_factory=list)
    # validate_draft / create_draft 等 ERP 写操作的摘要链（脱敏元数据）
    erp_call_log: List[Dict[str, Any]] = Field(default_factory=list)
    preview_data: Optional["OrderPreviewData"] = None
    editable_fields: List["PreviewEditableField"] = Field(default_factory=list)
    issues: List["PreviewIssue"] = Field(default_factory=list)


def _parse_float_value(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except (ValueError, TypeError):
        return 0.0


class MaterialItem(BaseModel):
    """LLM 采购订单语义抽取：物料明细。"""

    material_code: str = Field(default="", description="物料编码")
    material_name: str = Field(default="", description="物料名称")
    specification: str = Field(default="", description="规格型号")
    material_texture: str = Field(default="", description="物料材质")
    quantity: float = Field(default=0.0, description="采购数量")
    unit: str = Field(default="", description="计量单位")
    unit_price_without_tax: float = Field(default=0.0, description="不含税单价")
    unit_price_with_tax: float = Field(default=0.0, description="含税单价")
    total_amount: float = Field(default=0.0, description="兼容字段：单行不含税金额")
    total_amount_without_tax: float = Field(default=0.0, description="单行不含税金额")
    total_amount_with_tax: float = Field(default=0.0, description="单行含税金额")
    delivery_date: str = Field(default="", description="约定交货日期")
    drawing_number: str = Field(default="", description="图号/生产单号")
    evidence: Dict[str, Any] = Field(default_factory=dict, description="字段原文证据，按字段名记录 source_text/page/confidence")
    uncertain_fields: List[str] = Field(default_factory=list, description="本明细行中模型不确定的字段名")

    @field_validator(
        "quantity",
        "unit_price_without_tax",
        "unit_price_with_tax",
        "total_amount",
        "total_amount_without_tax",
        "total_amount_with_tax",
        mode="before",
    )
    @classmethod
    def parse_numeric(cls, value: object) -> float:
        return _parse_float_value(value)


class PurchaseOrder(BaseModel):
    """LLM 采购订单语义抽取结果。"""

    order_number: str = Field(default="", description="订单编号")
    purchaser_name: str = Field(default="", description="采购方名称")
    supplier_name: str = Field(default="", description="供应商名称")
    order_date: str = Field(default="", description="订单签订日期")
    payment_terms: str = Field(default="", description="付款结算条件")
    tax_rate: float = Field(default=0.0, description="税率")
    delivery_address: str = Field(default="", description="送货收货地址")
    total_order_amount: float = Field(default=0.0, description="订单整体总金额")
    items: List[MaterialItem] = Field(default_factory=list, description="物料明细")
    evidence: Dict[str, Any] = Field(default_factory=dict, description="订单头字段原文证据，按字段名记录 source_text/page/confidence")
    uncertain_fields: List[str] = Field(default_factory=list, description="订单头中模型不确定的字段名")
    extraction_notes: List[str] = Field(default_factory=list, description="抽取过程中的非业务说明或风险备注")

    @field_validator("tax_rate", "total_order_amount", mode="before")
    @classmethod
    def parse_numeric(cls, value: object) -> float:
        return _parse_float_value(value)


class OrderPreviewHeader(BaseModel):
    org: str = ""
    customerName: str = ""
    customerPoNo: str = ""
    salesUser: str = ""
    orderDate: str = ""
    orderStatus: str = "pending"
    deliveryAddr: str = ""
    rate: Optional[float] = None
    currency: str = ""
    deliveryDate: str = ""


class OrderPreviewDetail(BaseModel):
    materialCode: str = ""
    productName: str = ""
    productSpec: str = ""
    ph: str = ""
    customerMaterialNo: str = ""
    qty: Optional[float] = None
    price: Optional[float] = None
    taxPrice: Optional[float] = None
    amount: Optional[float] = None
    allAmount: Optional[float] = None
    tax: Optional[float] = None
    taxAmount: Optional[float] = None
    gift: bool = False
    remark: str = ""


class OrderPreviewData(BaseModel):
    order: OrderPreviewHeader = Field(default_factory=OrderPreviewHeader)
    details: List[OrderPreviewDetail] = Field(default_factory=list)


class PreviewEditableField(BaseModel):
    path: str
    label: str
    current_value: str = ""
    required: bool = False
    reason: str = ""
    confidence: float = 0.0


class PreviewIssue(BaseModel):
    path: str = ""
    level: str = "info"
    message: str


class ConfirmPreviewRequest(BaseModel):
    preview_data: OrderPreviewData


class DocumentParseExport(BaseModel):
    """
    对外集成用：单次 ingestion 的解析与结构化抽取快照（字段集保持稳定，便于下游 JSON 对接）。
    全文仅在查询参数 ``include_full_text=true`` 时填入 ``parse.full_text``（有大小与字符上限）。
    """

    schema_version: str = "1.0"
    ingestion_id: str
    file_id: str
    file_hash: str
    org_id: str
    user_id: str
    status: str
    doc_type_hint: Optional[str] = None
    file: Dict[str, Optional[str]] = Field(default_factory=dict)
    parse: Dict[str, Any] = Field(default_factory=dict)
    extracted_fields: Dict[str, str] = Field(default_factory=dict)
    line_items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="PO 明细行（由 line_items_json 解析）；非 PO 或未识别表格时为空",
    )
    missing_required_fields: List[str] = Field(default_factory=list)
    mapping_candidates: Dict[str, List[Dict[str, str]]] = Field(default_factory=dict)
    versions: Dict[str, str] = Field(default_factory=dict)
    error_code: Optional[str] = None
    error_details: Dict[str, Any] = Field(default_factory=dict)


class ErpPayloadPreviewResponse(BaseModel):
    schema_version: str = "1.0"
    ingestion_id: str
    doc_type: str
    body_style: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class ResolveIngestionRequest(BaseModel):
    fields: Dict[str, str]


class ChatErpQaRequest(BaseModel):
    """主数据问答：服务端仅允许在调用 ERP 查询工具后组织答案。"""

    message: str = Field(..., min_length=1, max_length=4000)
    org_id: str
    user_id: Optional[str] = None


class ChatErpQaResponse(BaseModel):
    answer: str
    erp_tools_used: List[str] = Field(default_factory=list)


class ToolUi(BaseModel):
    type: str
    data: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool: str
    status: str
    message: str
    ingestion_id: Optional[str] = None
    ui: Optional[ToolUi] = None
    ingestion: Optional[IngestionResponse] = None
    draft: Optional["CreateDraftResponse"] = None


class ChatToolMessage(BaseModel):
    role: Literal["assistant", "system", "user"] = "assistant"
    content: str
    ui: Optional[ToolUi] = None


class ChatTaskState(BaseModel):
    type: str
    ingestion_id: Optional[str] = None
    status: Optional[str] = None


class ChatMessageRequest(BaseModel):
    session_id: Optional[str] = None
    message: str = ""
    org_id: str
    user_id: Optional[str] = None
    active_task_id: Optional[str] = None
    tool: Optional[str] = None
    action: Optional[
        Literal[
            "get_status",
            "submit_missing_fields",
            "confirm_preview",
            "create_draft",
            "cancel",
        ]
    ] = None
    fields: Dict[str, str] = Field(default_factory=dict)
    preview_data: Optional[OrderPreviewData] = None


class ChatMessageResponse(BaseModel):
    session_id: Optional[str] = None
    messages: List[ChatToolMessage] = Field(default_factory=list)
    active_task: Optional[ChatTaskState] = None
    tool_result: Optional[ToolResult] = None
    ui: Optional[ToolUi] = None


class AssistantLlmProbeRequest(BaseModel):
    message: str = "查物料 M001"
    org_id: str = "org-test"
    user_id: Optional[str] = None
    active_task_id: Optional[str] = None


class AssistantLlmProbeResponse(BaseModel):
    enabled: bool
    api_key_configured: bool
    model: str
    base_url: str
    ok: bool
    attempted: bool = False
    tool_name: Optional[str] = None
    action: Optional[str] = None
    arguments: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    error: Optional[str] = None


class AssistantSessionResponse(BaseModel):
    session_id: str
    messages: List[ChatToolMessage] = Field(default_factory=list)
    active_task: Optional[ChatTaskState] = None
    ui: Optional[ToolUi] = None


class CreateDraftResponse(BaseModel):
    # 创建草稿成功时返回的结构。
    # 除草稿号和链接外，还包含 idempotency_key，方便调用方与审计系统关联同一次写入操作。
    ingestion_id: str
    status: IngestionStatus
    draft_no: str
    draft_url: str
    idempotency_key: str


class SaveCustomerRequest(BaseModel):
    """调用写侧 ERP 保存客户：扁平字段经适配层映射后写入默认 `{ "customer": { ... } }` 请求体。"""

    org_id: str = Field(..., min_length=1)
    user_id: Optional[str] = None
    fields: Dict[str, str] = Field(default_factory=dict)

    @field_validator("fields")
    @classmethod
    def _fields_need_content(cls, v: Dict[str, str]) -> Dict[str, str]:
        if not v or not any((x or "").strip() for x in v.values()):
            raise ValueError("fields must contain at least one non-empty value")
        return v


class SaveCustomerResponse(BaseModel):
    customer_no: str
    customer_url: str


IngestionResponse.model_rebuild()
ToolResult.model_rebuild()
