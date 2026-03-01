"""
Centralised configuration loaded from environment / .env file.
Uses pydantic-settings for validation and type coercion.
"""

from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── VAPI ────────────────────────────────────────────────────
    vapi_api_key: str = Field(default="", description="VAPI API key")
    vapi_phone_number_id: str = Field(default="", description="VAPI outbound phone number ID")
    vapi_assistant_id: str = Field(default="", description="Pre-created assistant ID (blank = auto-create)")
    vapi_base_url: str = Field(default="https://api.vapi.ai")

    # ── Webhook ─────────────────────────────────────────────────
    webhook_base_url: str = Field(default="https://web-production-61074.up.railway.app")
    webhook_secret: str = Field(default="change_me")

    # ── Calling rules ───────────────────────────────────────────
    calling_window_start: str = Field(default="07:00")
    calling_window_end: str = Field(default="22:00")
    calling_timezone: str = Field(default="Europe/London")

    max_concurrent_calls: int = Field(default=5, ge=1, le=50)
    max_calls_per_hour: int = Field(default=50, ge=1)
    max_calls_per_day: int = Field(default=200, ge=1)

    max_retries: int = Field(default=2, ge=0, le=5)
    retry_delay_minutes: int = Field(default=60, ge=1)

    # ── Paths ───────────────────────────────────────────────────
    database_path: Path = Field(default=Path("data/calls.db"))
    input_csv_dir: Path = Field(default=Path("data/input"))
    output_csv_dir: Path = Field(default=Path("data/output"))
    log_dir: Path = Field(default=Path("data/logs"))
    suppression_list_path: Path = Field(default=Path("data/suppression_list.csv"))

    # ── Server ──────────────────────────────────────────────────
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)

    def ensure_dirs(self) -> None:
        """Create required directories if they don't exist."""
        for d in [
            self.input_csv_dir,
            self.output_csv_dir,
            self.log_dir,
            self.database_path.parent,
        ]:
            d.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Factory – cached at module level after first call."""
    return Settings()  # type: ignore[call-arg]
