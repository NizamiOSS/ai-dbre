"""
AI DBRE — Configuration
Loads settings from .env file or environment variables.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LLM Configuration
    llm_provider: str = "anthropic"     # Options: anthropic, openai, ollama
    llm_model: str = "claude-sonnet-4-6"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096

    # API Keys (set the one matching your provider)
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Ollama settings (for local models)
    ollama_base_url: str = "http://localhost:11434"

    # PostgreSQL (agent connection — read-only)
    pg_host: str = "localhost"
    pg_port: int = 5434
    pg_database: str = "dbre_testdb"
    pg_user: str = "dbre_agent"
    pg_password: str = "agent_readonly"

    # PostgreSQL (remediation connection — limited write)
    pg_remediation_user: str = "dbre_remediation"
    pg_remediation_password: str = "remediation_write"

    # Postgresql (agent evaluation)
    eval_db_user: str = "dbre_admin"
    eval_db_password: str = "dbre_admin_pass"

    # Agent behavior
    slow_query_threshold_ms: float = 500.0
    max_results: int = 10

    # Health check thresholds
    health_check_interval_seconds: int = 300
    cache_hit_warning_pct: float = 95.0
    dead_tuple_warning_pct: float = 10.0
    dead_tuple_critical_pct: float = 20.0

    # LangSmith (optional)
    langsmith_api_key: str = ""
    langsmith_project: str = "ai-dbre"
    langchain_tracing_v2: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} "
            f"dbname={self.pg_database} user={self.pg_user} "
            f"password={self.pg_password}"
        )

    @property
    def pg_remediation_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} "
            f"dbname={self.pg_database} user={self.pg_remediation_user} "
            f"password={self.pg_remediation_password}"
        )

    @property
    def pg_url(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )


settings = Settings()
