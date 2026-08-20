"""Runtime configuration, entirely driven by environment variables.

Every setting below is documented in ``.env.example`` and in the README.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration.

    Field names map to upper-case environment variables, so ``database_url``
    is set through ``DATABASE_URL``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- storage -----------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///./data/lamal.db",
        description="SQLAlchemy URL. SQLite by default; set a postgresql+psycopg:// "
        "URL to use PostgreSQL (requires the 'postgres' extra).",
    )

    # --- HTTP server -------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    root_path: str = Field(
        default="",
        description="Set when served behind a reverse proxy on a sub-path, e.g. '/lamal'.",
    )
    cors_origins: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins, or '*'.",
    )
    rate_limit_per_minute: int = Field(
        default=0,
        ge=0,
        description="Request cap per client per minute, applied per API key when "
        "authentication is on and per IP otherwise. 0 disables rate limiting.",
    )

    # --- access control ----------------------------------------------------
    auth_enabled: bool = Field(
        default=False,
        description="Require an API key on every endpoint except /health. Off by default "
        "so a fresh 'docker compose up -d' still serves. Mint keys with "
        "'lamal-api key create'.",
    )
    api_key_header: str = Field(
        default="X-API-Key",
        description="Header carrying the API key. 'Authorization: Bearer <key>' is always "
        "accepted as well.",
    )

    # --- data synchronisation ---------------------------------------------
    sync_cron: str = Field(
        default="",
        description="Five-field cron expression for the built-in scheduler, e.g. "
        "'0 4 * * *'. Empty disables the scheduler; use an external cron instead.",
    )
    sync_on_startup: bool = Field(
        default=False,
        description="Run a sync when the API boots. Handy for a one-command Docker start.",
    )
    sync_years: str = Field(
        default="",
        description="Premium years to keep in sync, e.g. '2024-2026' or '2025,2026'. "
        "Empty means: whatever year the live file currently carries. Older years are "
        "fetched from the official yearly archives.",
    )
    http_timeout_seconds: float = Field(default=180.0, gt=0)
    ca_bundle: str = Field(
        default="",
        description="Path to a PEM file of trusted CA certificates. Set this when the host "
        "sits behind a TLS-inspecting corporate proxy, whose root CA is absent from the "
        "bundled certifi store. Empty uses the default trust store.",
    )
    user_agent: str = Field(
        default="lamal-api/0.1 (+https://github.com/mahmoud2344/lamal-api)",
        description="Sent to the federal servers so they can identify the client.",
    )

    # --- misc --------------------------------------------------------------
    log_level: str = "INFO"
    max_page_size: int = Field(default=500, gt=0)

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def requested_years(self) -> list[int] | None:
        """Parse ``SYNC_YEARS`` into an explicit year list.

        Accepts ``'2026'``, ``'2024,2026'`` and ranges like ``'2024-2026'``.
        Returns ``None`` when unset, meaning "track the live file".
        """
        spec = self.sync_years.strip()
        if not spec:
            return None
        years: set[int] = set()
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo_s, _, hi_s = part.partition("-")
                lo, hi = int(lo_s), int(hi_s)
                if lo > hi:
                    raise ValueError(f"SYNC_YEARS range is inverted: {part!r}")
                years.update(range(lo, hi + 1))
            else:
                years.add(int(part))
        return sorted(years)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
