"""Configuração tipada da aplicação, carregada do `.env` via pydantic-settings.

Regras:
- Todo segredo é `SecretStr` (nunca aparece em `repr`, logs ou tracebacks).
- URLs são normalizadas sem barra final para facilitar o join de endpoints.
- `get_settings()` é cacheado: use-o como dependência no FastAPI.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

Environment = Literal["development", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
RedisBackend = Literal["auto", "redis", "fake"]


def _normalize_url(value: str | None, *, schemes: tuple[str, ...]) -> str | None:
    """Remove espaços/barra final e valida o esquema. Strings vazias viram `None`."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.lower().startswith(tuple(f"{s}://" for s in schemes)):
        raise ValueError(f"URL deve usar um dos esquemas {schemes}")
    return value.rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Aplicação ---
    project_name: str = "Lobo AI Sales Agent"
    environment: Environment = "development"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"

    # --- LLMs ---
    google_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.8-flash"
    gemini_transcribe_model: str = "gemini-3.8-flash"
    openai_api_key: SecretStr | None = None

    # --- JEV / TypeSafe (System One) ---
    typesafe_api_key: SecretStr | None = None
    jev_enabled: bool = False

    # --- Banco & Cache ---
    redis_url: str = "redis://localhost:6379/0"
    # auto  = tenta REDIS_URL; se indisponível FORA de produção, usa fakeredis em memória
    # redis = exige Redis real (recomendado na VPS/Easypanel)
    # fake  = sempre fakeredis (testes / dev sem Docker)
    redis_backend: RedisBackend = "auto"
    database_url: str = "postgresql://postgres:postgres@localhost:5432/lobo_sales_agent"

    # --- Gateways WhatsApp ---
    waha_base_url: str | None = None
    waha_api_key: SecretStr | None = None
    waha_session: str = "default"
    waha_webhook_hmac_key: SecretStr | None = None  # valida X-Webhook-Hmac (SHA-512)

    astracalls_base_url: str | None = None
    astracalls_session_id: str | None = None
    astracalls_client_id: str | None = None
    astracalls_api_key: SecretStr | None = None
    astracalls_webhook_secret: SecretStr | None = None  # valida X-Webhook-Signature (SHA-256)

    # --- CRMs ---
    chatwoot_base_url: str | None = None
    chatwoot_api_token: SecretStr | None = None

    ghl_vizzu_api_token: SecretStr | None = None
    ghl_vizzu_location_id: str | None = None

    # --- Debounce & Humanização (padrão global; pode ser sobrescrito por tenant) ---
    debounce_silence_seconds: float = Field(default=4.0, ge=1.0, le=30.0)
    debounce_max_window_seconds: float = Field(default=10.0, ge=2.0, le=60.0)
    split_messages: bool = True

    # --- Geolocalização aberta (ViaCEP + OpenStreetMap), sem chave de API ---
    # Desligada automaticamente em ENVIRONMENT=test.
    geo_lookup_enabled: bool = True

    # --- Dados dos tenants & handoff ---
    tenant_data_dir: Path = PROJECT_ROOT / "data" / "tenants"
    allow_demo_data: bool | None = None
    ai_pause_hours: float = Field(default=24.0, gt=0, le=24 * 30)  # IA silenciada após handoff

    # --- Trava de segurança (Sandbox) ---
    # Fora de produção o Sandbox é SEMPRE aplicado; em produção vale o switch do painel.
    sandbox_config_path: Path | None = PROJECT_ROOT / "data" / "sandbox_config.json"
    # Números sempre liberados (somados à whitelist do painel). Ex.: "5591988887777,5591977776666"
    sandbox_allowed_phones: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Painel administrativo ---
    # Se definido, as rotas /api/admin/* exigem o header `X-Admin-Token` (ou Bearer).
    admin_token: SecretStr | None = None
    admin_password: SecretStr | None = None

    # ------------------------------------------------------------------ #
    # Validadores
    # ------------------------------------------------------------------ #
    @field_validator(
        "astracalls_session_id",
        "astracalls_client_id",
        "ghl_vizzu_location_id",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator(
        "google_api_key",
        "openai_api_key",
        "typesafe_api_key",
        "waha_api_key",
        "waha_webhook_hmac_key",
        "astracalls_api_key",
        "astracalls_webhook_secret",
        "chatwoot_api_token",
        "ghl_vizzu_api_token",
        "admin_token",
        "admin_password",
        mode="before",
    )
    @classmethod
    def _empty_secret_to_none(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v or v.startswith("your_"):
                return None
        return v

    @field_validator("sandbox_allowed_phones", mode="before")
    @classmethod
    def _split_phones(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
        return v

    @field_validator("waha_base_url", "astracalls_base_url", "chatwoot_base_url")
    @classmethod
    def _validate_http_url(cls, v: str | None) -> str | None:
        return _normalize_url(v, schemes=("http", "https"))

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        normalized = _normalize_url(v, schemes=("redis", "rediss", "unix"))
        if normalized is None:
            raise ValueError("REDIS_URL é obrigatório")
        return normalized

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("postgresql://", "postgres://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL deve ser uma URL PostgreSQL")
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> Settings:
        if self.debounce_silence_seconds >= self.debounce_max_window_seconds:
            raise ValueError(
                "DEBOUNCE_SILENCE_SECONDS deve ser menor que DEBOUNCE_MAX_WINDOW_SECONDS"
            )
        if self.is_production and self.google_api_key is None:
            raise ValueError("GOOGLE_API_KEY é obrigatório em produção")
        return self

    # ------------------------------------------------------------------ #
    # Propriedades derivadas
    # ------------------------------------------------------------------ #
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def geo_enabled(self) -> bool:
        return self.geo_lookup_enabled and self.environment != "test"

    @property
    def effective_allow_demo(self) -> bool:
        if self.allow_demo_data is not None:
            return self.allow_demo_data
        return True

    @property
    def async_database_url(self) -> str:
        """URL no formato exigido pelo SQLAlchemy async + asyncpg."""
        url = self.database_url
        for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+asyncpg://" + url[len(prefix):]
        return url  # pragma: no cover - já validado

    @property
    def waha_enabled(self) -> bool:
        return bool(self.waha_base_url and self.waha_api_key)

    @property
    def astracalls_enabled(self) -> bool:
        return bool(self.astracalls_base_url and self.astracalls_api_key)

    @property
    def chatwoot_enabled(self) -> bool:
        return bool(self.chatwoot_base_url and self.chatwoot_api_token)

    @property
    def ghl_vizzu_enabled(self) -> bool:
        return bool(self.ghl_vizzu_api_token and self.ghl_vizzu_location_id)

    @property
    def gemini_enabled(self) -> bool:
        return self.google_api_key is not None

    @property
    def openai_enabled(self) -> bool:
        return self.openai_api_key is not None

    @property
    def whisper_fallback_enabled(self) -> bool:
        return self.openai_api_key is not None

    @property
    def admin_auth_required(self) -> bool:
        return self.admin_token is not None or self.admin_password is not None

    def safe_summary(self) -> dict[str, Any]:
        """Resumo seguro para logs/health-check: sem segredos nem credenciais de URL."""
        return {
            "project_name": self.project_name,
            "environment": self.environment,
            "gemini_model": self.gemini_model,
            "jev_enabled": self.jev_enabled,
            "redis_backend": self.redis_backend,
            "integrations": {
                "gemini": self.google_api_key is not None,
                "whisper_fallback": self.whisper_fallback_enabled,
                "waha": self.waha_enabled,
                "astracalls": self.astracalls_enabled,
                "chatwoot": self.chatwoot_enabled,
                "ghl_vizzu": self.ghl_vizzu_enabled,
            },
            "debounce": {
                "silence_seconds": self.debounce_silence_seconds,
                "max_window_seconds": self.debounce_max_window_seconds,
            },
        }


@lru_cache
def get_settings() -> Settings:
    """Instância única (cacheada) das configurações."""
    return Settings()

