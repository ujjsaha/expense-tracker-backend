"""Environment-driven application settings (SRS section 7)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value shipped in .env.example. Treated as "unset" outside development so a
# forgotten copy-paste can never sign real tokens.
PLACEHOLDER_SECRET = "change-me-this-is-not-a-real-secret"

MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Expense Tracker API"
    app_env: Literal["development", "test", "production"] = "development"

    database_url: str = "postgresql+psycopg://postgres:root@localhost:5432/emart"

    jwt_secret_key: str = PLACEHOLDER_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(default=15, gt=0)
    refresh_token_expire_days: int = Field(default=7, gt=0)

    otp_length: int = Field(default=6, ge=4, le=10)
    otp_expire_minutes: int = Field(default=10, gt=0)
    otp_max_attempts: int = Field(default=5, gt=0)
    expose_otp_in_response: bool = False

    bcrypt_rounds: int = Field(default=12, ge=4, le=16)
    log_level: str = "INFO"

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    openai_api_key: str = ""
    chatbot_model: str = "gpt-5"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @model_validator(mode="after")
    def _enforce_deployment_safety(self) -> "Settings":
        """Fail fast rather than boot a deployed app with a known-public secret."""
        if not self.is_development:
            if not self.jwt_secret_key or self.jwt_secret_key == PLACEHOLDER_SECRET:
                raise ValueError(
                    "JWT_SECRET_KEY must be set to a real value when APP_ENV is "
                    f"'{self.app_env}'. Generate one with: "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )
            if len(self.jwt_secret_key) < MIN_SECRET_LENGTH:
                raise ValueError(
                    f"JWT_SECRET_KEY must be at least {MIN_SECRET_LENGTH} characters "
                    f"when APP_ENV is '{self.app_env}'."
                )

        if self.is_production and self.expose_otp_in_response:
            raise ValueError("EXPOSE_OTP_IN_RESPONSE cannot be enabled when APP_ENV=production.")

        return self


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is read once per process."""
    return Settings()
