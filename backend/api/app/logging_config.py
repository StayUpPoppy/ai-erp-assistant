import logging
from pathlib import Path
import re


class RedactFilter(logging.Filter):
    # 日志脱敏过滤器：针对常见业务敏感字段做键值级打码，
    # 防止 vendor_code/invoice_no/tax_no 等标识直接明文落日志，
    # 满足“可审计 + 最小暴露”的治理要求。
    _patterns = [
        re.compile(r"(vendor_code=)([^ ]+)", re.IGNORECASE),
        re.compile(r"(invoice_no=)([^ ]+)", re.IGNORECASE),
        re.compile(r"(tax_no=)([^ ]+)", re.IGNORECASE),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern in self._patterns:
            msg = pattern.sub(r"\1***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging() -> logging.Logger:
    # 统一初始化 API 日志：
    # - 输出到文件，便于审计留痕与问题回溯；
    # - 输出到控制台，便于本地开发实时观察；
    # - 两种输出都挂同一套 formatter 与脱敏过滤器，保证格式一致。
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "api.log"

    logger = logging.getLogger("ai_erp_api")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactFilter())
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(RedactFilter())
    logger.addHandler(stream_handler)
    return logger

