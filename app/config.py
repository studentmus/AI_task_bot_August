from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    deepseek_api_key: str
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"

    db_path: str = "data/tasks.db"
    task_timezone: str = "Europe/Copenhagen"
    
    # === НОВЫЕ ПЕРЕМЕННЫЕ ДОЛЖНЫ БЫТЬ ЗДЕСЬ ===
    radicale_url: str = "http://127.0.0.1:5232/ivan/2D327C64-9361-4EF5-97E6-80E948B58D8D/"
    radicale_user: str = "ivan"
    radicale_pass: str = "shh123"

    log_level: str = "INFO"
    allowed_user_id: int | None = None

settings = Settings()