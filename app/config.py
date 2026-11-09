from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = APP_ROOT / ".env"

# Passenger/cron may start from a different working directory. Load the
# application-local .env explicitly so settings are stable everywhere.
load_dotenv(dotenv_path=ENV_FILE, override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "production"
    app_name: str = "Fresh AI Engine"

    # Optional complete SQLAlchemy URL. If empty, DB_* values below are used.
    database_url: str = ""
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""

    admin_user: str = "admin"
    admin_password: str = "change-me"
    fresh_engine_api_keys: str = "dev-key-change-me"
    fresh_chat_engine_key: str = ""  # dedicated Chat->Engine key; do not reuse other app keys
    fresh_chat_reporting_secret: str = ""  # distinct Engine->Chat read-only directory secret

    store_payloads: bool = False
    log_preview_chars: int = 220
    job_timeout_seconds: int = 180
    worker_batch_size: int = 10

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.6-luna"
    # Dedicated image model. Kept separate from the text route so image.generate
    # can never accidentally execute on OPENAI_MODEL.
    openai_image_model: str = "gpt-image-2.5-flare"
    openai_vision_model: str = "gpt-4.1-mini"

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_version: str = "2023-06-01"

    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.5-flash"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"

    enable_mock_provider: bool = True

    customer_session_secret: str = "change-this-customer-session-secret"

    # Fresh Key: shared identity/project/OAuth broker used by Linda, Boban and other Fresh apps.
    fresh_key_encryption_key: str = ""
    fresh_key_public_origin: str = "https://aiengine.freshcrackers.cz"
    fresh_key_allowed_return_hosts: str = ".freshcrackers.cz"

    fresh_key_meta_client_id: str = ""
    fresh_key_meta_client_secret: str = ""
    fresh_key_meta_api_version: str = "v24.0"
    fresh_key_meta_scopes: str = "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish"

    fresh_key_linkedin_client_id: str = ""
    fresh_key_linkedin_client_secret: str = ""
    fresh_key_linkedin_scopes: str = "openid profile email w_member_social"
    fresh_key_linkedin_version: str = "202609"

    fresh_key_google_client_id: str = ""
    fresh_key_google_client_secret: str = ""
    fresh_key_google_scopes: str = "openid email profile https://www.googleapis.com/auth/business.manage"

    @property
    def api_keys(self) -> set[str]:
        return {k.strip() for k in self.fresh_engine_api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    # Explicit _env_file is intentional: cPanel Passenger and cron can have
    # different current working directories.
    return Settings(_env_file=str(ENV_FILE), _env_file_encoding="utf-8")
