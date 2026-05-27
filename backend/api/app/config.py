from pydantic import BaseModel


class Settings(BaseModel):
    # 运行时配置占位模型。
    # 当前仅保留最小字段，后续会扩展 REDIS_URL、DB_URL、MINIO、ERP_ENDPOINT 等，
    # 并统一改为从环境变量加载，满足不同环境（dev/stage/prod）配置隔离。
    app_name: str = "AI ERP Assistant API"
    env: str = "dev"


settings = Settings()
