"""Runtime configuration. Every setting can be supplied as a KAIZEVAL_* environment variable."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAIZEVAL_", env_file=".env", extra="ignore")

    env: Literal["production", "development", "test"] = "production"
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".kaizeval")
    database_url: str | None = None
    secret_key: str | None = None

    # Hard instance-level boundary. When true, no workspace can reach a public network
    # destination, whatever its own settings say. Default is fail-closed.
    offline: bool = True
    # Comma-separated host names that are treated as internal even though they contain dots
    # (for example "llm.corp.internal"). Only consulted for model and agent endpoints.
    egress_allowlist: str = ""

    storage_backend: Literal["filesystem", "s3"] = "filesystem"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "kaizeval"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    # inline: a worker thread inside the API process. external: run `kaizeval worker`.
    # eager: run jobs synchronously inside the request (tests only).
    worker_mode: Literal["inline", "external", "eager"] = "inline"
    worker_poll_seconds: float = 0.5
    job_max_attempts: int = 3
    # External workers cannot tell a crashed peer from a busy one, so a job is treated as orphaned
    # only after it has been 'running' for this long.
    job_stale_minutes: int = 720

    allow_signup: bool = True
    token_ttl_minutes: int = 720
    cors_origins: str = ""
    log_level: str = "INFO"
    max_upload_mb: int = 25

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'kaizeval.db').as_posix()}"

    @property
    def resolved_secret_key(self) -> str:
        """Signing/encryption key. Generated once and stored next to the data when not supplied."""
        if self.secret_key:
            return self.secret_key
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key_file = self.data_dir / "secret.key"
        if not key_file.exists():
            key_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
            try:
                key_file.chmod(0o600)
            except OSError:
                pass
        return key_file.read_text(encoding="utf-8").strip()

    @property
    def allowlist_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.egress_allowlist.split(",") if h.strip()}

    @property
    def storage_dir(self) -> Path:
        path = self.data_dir / "objects"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Drop cached settings (used by tests and by the CLI after it sets environment variables)."""
    get_settings.cache_clear()
