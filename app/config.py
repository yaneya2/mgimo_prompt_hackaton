"""Validated environment settings. API credentials are never logged."""
from pathlib import Path
from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", hide_input_in_errors=True,
    )

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error", "critical"] = "info"
    data_dir: Path = Path("data")
    stage_factory: str | None = None
    worker_poll_seconds: float = Field(default=0.5, gt=0, le=10)
    model_provider: str | None = "codex"
    model_name: str | None = None
    model_api_key: SecretStr | None = None
    model_timeout_seconds: float = Field(default=60, gt=0, le=300)
    model_max_tokens: int = Field(default=2048, ge=1, le=32768)
    model_max_retries: int = Field(default=1, ge=0, le=3)
    codex_command: str = Field(default="codex", min_length=1)
    model_input_cost_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=20, decimal_places=9)
    model_output_cost_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False, max_digits=20, decimal_places=9)
    model_cost_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    model_pricing_version: str | None = None

    @field_validator("data_dir", mode="before")
    @classmethod
    def nonempty_path(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("APP_DATA_DIR must not be empty")
        return value

    @field_validator("model_provider", "model_name", "model_api_key", "stage_factory", mode="before")
    @classmethod
    def empty_is_unset(cls, value):
        return None if value == "" else value

    @field_validator("model_input_cost_per_million", "model_output_cost_per_million", "model_pricing_version", mode="before")
    @classmethod
    def empty_pricing_is_unset(cls, value):
        return None if value == "" else value
