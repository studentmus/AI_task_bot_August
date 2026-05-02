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

    radicale_calendar_dir: str = "data/radicale/collections"

    log_level: str = "INFO"

    allowed_user_id: int | None = None


settings = Settings()
