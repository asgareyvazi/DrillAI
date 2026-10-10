"""Application configuration.

Single source of truth for runtime settings (12-factor style, environment driven).
Every integration is behind a provider switch so that the platform runs with zero
external services for development/CI while remaining production-shaped:

* ``DRILLAI_LLM_PROVIDER=stub|openai_compatible|ollama``
* ``DRILLAI_EMBEDDING_PROVIDER=deterministic|openai_compatible|ollama``
* ``DRILLAI_VECTOR_BACKEND=memory|pgvector``
* ``DRILLAI_BLOB_BACKEND=filesystem|memory``
* ``DRILLAI_EVENT_BUS=memory``  (durable store is always the DB outbox)
* ``DRILLAI_EMAIL_PROVIDER=disabled|smtp``, ``DRILLAI_TELEGRAM_PROVIDER=disabled|bot_api``
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DRILLAI_",
        env_file=(".env", "../.env"),
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ platform
    app_name: str = "DrillAI Well Engineering Intelligence Platform"
    environment: Literal["development", "test", "staging", "production"] = "development"
    api_prefix: str = "/api/v1"
    debug: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    # Comma separated list of origins; "*" only acceptable outside production.
    cors_origins: str = "*"
    # Preview/dev hosts must be accepted by the API in sandboxed environments.
    allowed_hosts: str = "*"

    # ------------------------------------------------------------------ persistence
    database_url: str = "sqlite+aiosqlite:///./.data/drillai.db"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 5
    run_migrations_on_start: bool = False

    # ------------------------------------------------------------------ storage
    blob_backend: Literal["filesystem", "memory"] = "filesystem"
    blob_root: Path = Field(default=REPO_ROOT / "backend" / ".data" / "blobs")
    max_upload_bytes: int = 200 * 1024 * 1024  # 200 MB per upload

    # ------------------------------------------------------------------ ai providers
    llm_provider: Literal["stub", "openai_compatible", "ollama"] = "stub"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    llm_default_model: str = "stub-reasoner-1"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    # Record prompt/response content on spans? Privacy default is off (OTel GenAI guidance).
    llm_capture_content: bool = False

    embedding_provider: Literal["deterministic", "openai_compatible", "ollama"] = "deterministic"
    embedding_model: str = "deterministic-hash-v1"
    embedding_dimensions: int = 256

    vector_backend: Literal["memory", "pgvector"] = "memory"

    # ------------------------------------------------------------------ retrieval
    retrieval_default_top_k: int = 8
    retrieval_max_top_k: int = 50
    retrieval_chunk_chars: int = 1200
    retrieval_chunk_overlap_chars: int = 150

    # ------------------------------------------------------------------ workflow runtime
    workflow_max_steps: int = 500
    workflow_default_node_timeout_seconds: float = 60.0
    workflow_run_timeout_seconds: float = 3600.0
    workflow_approval_ttl_hours: int = 72
    #: Poll interval for the run-event WebSocket tail (the durable log stays the run_events table).
    run_event_stream_poll_seconds: float = 1.0

    # --- the live well feed ------------------------------------------------------------------------
    #: How often the feed reads the outbox for new events. The socket tails a table; this is the tail's
    #: latency, not a queue's polling.
    live_stream_poll_seconds: float = 0.5
    #: How often the feed tells the client where the stream is even when nothing happened. A client uses
    #: this to notice a range it missed without having received a gap frame.
    live_stream_heartbeat_seconds: float = 15.0
    #: The most events one tick will turn into frames. The database is the buffer, so this bounds the
    #: work per tick rather than the data the platform keeps.
    live_stream_max_events_per_tick: int = 500
    #: A client more than this far behind is told the range it lost (a `gap` frame) and resyncs, instead
    #: of the server replaying an arbitrarily long history through one socket.
    live_stream_max_backlog: int = 5000
    #: How much current state a `snapshot` frame carries.
    live_stream_snapshot_alerts: int = 100
    live_stream_snapshot_channels: int = 200

    # --- telemetry freshness and lateness ---------------------------------------------------------
    # The policy lives here, not in the client: the browser and the API must answer "is this value
    # fresh?" the same way, and a threshold written into a React component is a second opinion that
    # drifts from the server's. `fresh` up to this many seconds since the last measurement, `stale`
    # beyond it; a value older than `stale_seconds` is only ever rendered as stale.
    telemetry_fresh_seconds: float = 30.0
    telemetry_stale_seconds: float = 300.0
    # A measurement that arrives this long after it was taken is *late*: the reading may be perfectly
    # good, but a client that treats it as the current value would be showing yesterday's number as
    # today's.
    telemetry_late_seconds: float = 120.0
    # How many channels one latest-values read resolves, and how many points one page returns.
    telemetry_max_latest_channels: int = 200
    telemetry_max_window_points: int = 2000
    telemetry_max_batch_points: int = 5000

    # ------------------------------------------------------------------ security
    auth_enabled: bool = True
    # Demo/dev bootstrap identity (never used when auth_enabled and a token is supplied).
    dev_org_slug: str = "demo-operator"
    secret_backend: Literal["env", "database"] = "env"
    secret_master_key: str = ""  # required for the database secret backend

    # ------------------------------------------------------------------ integrations
    email_provider: Literal["disabled", "smtp"] = "disabled"
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "drillai@localhost"
    telegram_provider: Literal["disabled", "bot_api"] = "disabled"
    telegram_bot_token: str = ""
    telegram_default_chat_id: str = ""
    webhook_timeout_seconds: float = 15.0
    scheduler_enabled: bool = True
    scheduler_poll_seconds: float = 5.0

    # ------------------------------------------------------------------ connectors
    witsml_connector_enabled: bool = True
    etp_connector_enabled: bool = True
    connector_allow_loopback: bool = False
    connector_allowed_hosts: str = ""
    connector_worker_lease_seconds: float = 30.0
    connector_worker_poll_seconds: float = 1.0
    connector_max_response_bytes: int = 5 * 1024 * 1024  # 5 MB per protocol response
    connector_request_timeout_seconds: float = 10.0

    # ------------------------------------------------------------------ seed
    seed_on_start: bool = False
    seed_demo_dataset: bool = False

    # ------------------------------------------------------------------ end-to-end fault injection
    # Enables the deterministic failure routes the browser suite needs (a real timeout, a malformed
    # body, an unhandled fault). The app factory refuses to start when this is set in production, so
    # the flag cannot open a debug endpoint on a deployment.
    e2e_faults: bool = False

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value:
            raise ValueError("database_url must not be empty")
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def connector_allowed_host_list(self) -> list[str]:
        return [host.strip().lower() for host in self.connector_allowed_hosts.split(",") if host.strip()]

    def sqlalchemy_url(self) -> str:
        """Return a SQLAlchemy async URL (SQLite paths are made absolute)."""
        url = self.database_url
        if url.startswith("sqlite+aiosqlite:///"):
            raw = url.removeprefix("sqlite+aiosqlite:///")
            if raw and raw != ":memory:" and not raw.startswith("/"):
                absolute = (REPO_ROOT / "backend" / raw).resolve()
                absolute.parent.mkdir(parents=True, exist_ok=True)
                return f"sqlite+aiosqlite:///{absolute}"
        return url

    def ensure_directories(self) -> None:
        if self.blob_backend == "filesystem":
            self.blob_root.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
