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

    # Database — Postgres + pgvector primary, SQLite fallback for dev.
    # The default uses CHANGE_ME placeholders so a misconfigured deploy
    # fails loudly instead of silently running with predictable creds.
    database_url: str = "postgresql+psycopg2://CHANGE_ME:CHANGE_ME@localhost:5432/leasegenie"

    # Celery
    celery_broker_url: str = "memory://"
    celery_result_backend: str = "cache+memory://"
    celery_task_always_eager: bool = True

    # Extractor backend
    extractor_backend: str = "ollama"  # "ollama" or "stub"

    # Ollama chat (extraction)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:32b-instruct-q5_K_M"
    ollama_timeout_seconds: int = 180
    ollama_num_ctx: int = 32768
    ollama_temperature: float = 0.0

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
