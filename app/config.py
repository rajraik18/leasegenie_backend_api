"""Application configuration loaded from environment / .env."""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # API
    api_v1_prefix: str = "/api/v1"
    debug: bool = False
    project_name: str = "LeaseGenie API"
    # Comma-separated origins (parsed by pydantic-settings into list[str])
    cors_allow_origins: list[str] = ["http://localhost:3000"]
    # Bearer token shared between the reverse proxy / clients and the API.
    # When None, requests are accepted unauthenticated -- ONLY safe for
    # local development. Production deployments MUST set API_KEY.
    api_key: str | None = None

    # Database — Postgres + pgvector primary, SQLite fallback for dev.
    # The default uses CHANGE_ME placeholders so a misconfigured deploy
    # fails loudly instead of silently running with predictable creds.
    database_url: str = "postgresql+psycopg2://CHANGE_ME:CHANGE_ME@localhost:5432/leasegenie"

    # Celery
    celery_broker_url: str = "memory://"
    celery_result_backend: str = "cache+memory://"
    celery_task_always_eager: bool = True
    # Hard / soft per-task limits. soft fires SoftTimeLimitExceeded so the
    # task can clean up; hard kills the worker process.
    celery_task_time_limit: int = 600
    celery_task_soft_time_limit: int = 540
    # How long to keep task results in the Redis backend.
    celery_result_expires_seconds: int = 86400

    # Worker pool — Windows can't use prefork; threads / gevent / solo only.
    # `threads` lets multiple Ollama calls run concurrently inside one
    # worker process; `solo` means strict single-task at a time.
    worker_pool: str = "threads"
    worker_concurrency: int = 4

    # Extractor backend
    extractor_backend: str = "ollama"  # "ollama" or "stub"

    # Ollama chat (extraction)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:32b-instruct-q5_K_M"
    ollama_timeout_seconds: int = 180
    ollama_num_ctx: int = 32768
    ollama_temperature: float = 0.0
    # Per-call retry around the Ollama HTTP client.
    ollama_max_retries: int = 3
    ollama_retry_initial_seconds: float = 2.0

    # Ollama embeddings (vector store)
    ollama_embed_model: str = "nomic-embed-text"
    ollama_embed_dim: int = 768

    # Paths
    upload_dir: Path = Path("./uploads")
    export_dir: Path = Path("./exports")
    brd_path: Path = Path("./data/LeaseGenie_BRD.xlsx")

    # Upload limits — enforced in app/api/v1/extract_pdf.py
    max_upload_size_mb: int = 100      # per-file limit (MB)
    max_upload_total_mb: int = 500     # total across all files in one request (MB)
    max_pdfs_per_request: int = 8      # 1 base lease + 7 amendments per BRD

    # Database connection pool — sized for a single API or worker process.
    db_pool_size: int = 15
    db_max_overflow: int = 20

    # File retention — Celery-beat tasks delete files in upload_dir /
    # export_dir whose mtime is older than these many days. 0 disables.
    upload_retention_days: int = 90
    export_retention_days: int = 30

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql", "postgres"))

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
