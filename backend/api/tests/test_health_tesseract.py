from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routes import health


def test_health_includes_tesseract_probe() -> None:
    h = health()
    assert h.status == "ok"
    assert isinstance(h.tesseract_available, bool)
    assert h.tesseract_resolution is None or isinstance(h.tesseract_resolution, str)
    assert h.tesseract_cmd is None or isinstance(h.tesseract_cmd, str)
    assert h.extraction_profiles_dir is None or isinstance(h.extraction_profiles_dir, str)
    assert isinstance(h.extraction_profile_json_count, int)
    assert h.extraction_profile_json_count >= 0
    assert h.ocr_engine is None or isinstance(h.ocr_engine, str)
    assert isinstance(h.ocr_http_url_configured, bool)
    assert isinstance(h.ocr_engine_auto_fallback, bool)
    assert isinstance(h.paddleocr_importable, bool)
    assert isinstance(h.paddleocr_initializable, bool)
    assert isinstance(h.paddleocr_init_status, str)
    assert isinstance(h.paddle_ocr_lang, str)
    assert isinstance(h.paddle_ocr_use_gpu, bool)
    assert isinstance(h.tesseract_ocr_lang, str)
    assert isinstance(h.tesseract_languages, list)
    assert isinstance(h.tesseract_has_chi_sim, bool)
    assert isinstance(h.ocr_chinese_ready, bool)
    assert isinstance(h.aliyun_ocr_configured, bool)
    assert isinstance(h.mineru_enabled, bool)
    assert isinstance(h.mineru_api_base, str)
    assert isinstance(h.mineru_model, str)
    assert isinstance(h.erp_client_mode, str)
    assert isinstance(h.erp_sale_order_page_enabled, bool)
    assert isinstance(h.erp_create_body_style, str)
    assert isinstance(h.erp_auth_mode, str)
    assert isinstance(h.erp_customer_save_enabled, bool)
    assert isinstance(h.erp_soft_fail_master_search, bool)
    assert isinstance(h.erp_master_search_query_style, str)
    assert isinstance(h.erp_master_search_datynk_envelope, bool)
    assert isinstance(h.erp_data_base_netloc, str)
    assert isinstance(h.erp_vendors_search_path, str)
    assert isinstance(h.erp_tax_codes_search_path, str)
    assert isinstance(h.erp_qa_report_definitions_count, int)
    assert isinstance(h.llm_extract_enabled, bool)
    assert isinstance(h.llm_router_enabled, bool)
    assert isinstance(h.llm_api_key_configured, bool)
    assert isinstance(h.llm_model, str)
    assert isinstance(h.llm_base_url, str)
    assert isinstance(h.llm_prompt_version, str)
    assert isinstance(h.queue_backend, str)
    assert isinstance(h.queue_name, str)
    assert isinstance(h.queue_available, bool)
    assert h.ingestion_queue_fallback_mode in {"none", "inline", "thread"}
