"""Trava de segurança obrigatória (Sandbox Guard) para testes em números reais.

Problema: o webhook pode estar conectado ao WhatsApp REAL do usuário (ex.: sessão
`Danyllo`) em pleno horário comercial. A IA não pode sair respondendo clientes ou
contatos pessoais que mandem mensagens normais.

Regras (avaliadas para cada mensagem de ENTRADA, `from_me=False`):
1. `/parar` ou `/reset` (stop keywords) → encerra a sessão de teste do contato e
   reseta memória/pausa. Só é aceito de quem está autorizado (ou com o Sandbox aberto);
   de um contato desconhecido o comando é ignorado em silêncio.
2. Sandbox ATIVO (sempre fora de produção, ou quando `enabled=True` no painel):
   a mensagem só passa se o telefone estiver em `allowed_phones`, OU se houver uma
   sessão de teste ativa (`lobo:sandbox:session:{tenant}:{phone}`), OU se o texto
   contiver uma trigger keyword (`/lobo-sales-agent`, `/lobo`). A keyword abre/renova
   uma sessão de `session_ttl_minutes` e é REMOVIDA do texto antes de ir para a IA.
3. Qualquer outra mensagem é ignorada silenciosamente (nada no buffer, nada no WhatsApp).

Persistência da configuração: Redis (`lobo:sandbox:config`, compartilhado entre réplicas)
+ arquivo JSON (`data/sandbox_config.json`, sobrevive a reinícios sem Redis persistente).
Falhas de Redis NUNCA abrem a trava (fail-closed).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from redis.exceptions import RedisError

from src.core.schemas import IncomingMessage, mask_phone, normalize_phone, utcnow

logger = logging.getLogger("lobo.sandbox")

_REDIS_ERRORS = (RedisError, OSError)

DEFAULT_TRIGGER_KEYWORDS = ["/lobo-sales-agent", "/lobo"]
DEFAULT_STOP_KEYWORDS = ["/parar", "/reset"]
EMPTY_TRIGGER_REPLACEMENT = "Olá, gostaria de atendimento"
RESET_CONFIRMATION_TEXT = "🛑 Sessão de teste do Lobo AI Sales Agent encerrada e memória resetada para este número."


def _clean_keywords(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        kw = str(raw).strip().lower()
        if not kw:
            continue
        if not kw.startswith("/") or len(kw) < 2 or any(c.isspace() for c in kw):
            raise ValueError(f"palavra-chave inválida: {raw!r} (use o formato /palavra, sem espaços)")
        if kw not in out:
            out.append(kw)
    return out


class SandboxConfig(BaseModel):
    """Configuração da trava (editável pelo painel)."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    enabled: bool = True
    allowed_phones: list[str] = Field(default_factory=list)
    trigger_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_TRIGGER_KEYWORDS))
    session_ttl_minutes: int = Field(default=30, ge=1, le=24 * 60)
    stop_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_STOP_KEYWORDS))
    debounce_silence_seconds: float | None = Field(default=None, ge=1.0, le=30.0)
    debounce_max_window_seconds: float | None = Field(default=None, ge=2.0, le=60.0)
    split_messages: bool = True
    updated_at: datetime | None = None

    @field_validator("allowed_phones", mode="before")
    @classmethod
    def _normalize_phones(cls, v: Any) -> list[str]:
        out: list[str] = []
        for raw in v or []:
            phone = normalize_phone(str(raw))
            if phone not in out:
                out.append(phone)
        return out

    @field_validator("trigger_keywords")
    @classmethod
    def _triggers(cls, v: list[str]) -> list[str]:
        cleaned = _clean_keywords(v)
        if not cleaned:
            raise ValueError("informe ao menos uma palavra-chave de ativação")
        return cleaned

    @field_validator("stop_keywords")
    @classmethod
    def _stops(cls, v: list[str]) -> list[str]:
        cleaned = _clean_keywords(v)
        if not cleaned:
            raise ValueError("informe ao menos um comando de parada")
        return cleaned

    @model_validator(mode="after")
    def _validate_debounce(self) -> SandboxConfig:
        s, m = self.debounce_silence_seconds, self.debounce_max_window_seconds
        if s is not None and m is not None and s >= m:
            raise ValueError("debounce_silence_seconds deve ser menor que debounce_max_window_seconds")
        return self


class SandboxAction(StrEnum):
    ALLOW = "allow"
    IGNORE = "ignore"
    RESET = "reset"


@dataclass(frozen=True, slots=True)
class SandboxDecision:
    action: SandboxAction
    reason: str
    message: IncomingMessage | None = None  # mensagem (possivelmente reescrita) a seguir para o buffer
    session_started: bool = False

    @property
    def allowed(self) -> bool:
        return self.action is SandboxAction.ALLOW


@dataclass(frozen=True, slots=True)
class SandboxSession:
    tenant_id: str
    phone: str
    expires_in_seconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "phone": self.phone,
            "phone_masked": mask_phone(self.phone),
            "expires_in_seconds": self.expires_in_seconds,
        }


class SandboxGuard:
    """Avalia mensagens de entrada contra a trava de segurança."""

    CONFIG_KEY = "lobo:sandbox:config"
    SESSION_PREFIX = "lobo:sandbox:session"

    def __init__(
        self,
        redis: aioredis.Redis | None,
        *,
        environment: str,
        config_path: Path | None = None,
        seed_phones: Iterable[str] = (),
    ) -> None:
        self._redis = redis
        self.environment = environment
        self._path = Path(config_path) if config_path else None
        self._seed_phones = [normalize_phone(p) for p in seed_phones]
        self._memory_config: SandboxConfig | None = None
        self._memory_sessions: dict[str, datetime] = {}

    # ------------------------------------------------------------------ #
    # Estado da trava
    # ------------------------------------------------------------------ #
    @property
    def forced_by_environment(self) -> bool:
        """Fora de produção a trava é obrigatória, independente do switch."""
        return self.environment != "production"

    def is_active(self, config: SandboxConfig) -> bool:
        return config.enabled or self.forced_by_environment

    @property
    def seed_phones(self) -> list[str]:
        return list(self._seed_phones)

    async def get_config(self) -> SandboxConfig:
        """Redis → arquivo → padrão (com `enabled=True`)."""
        raw: str | None = None
        if self._redis is not None:
            try:
                value = await self._redis.get(self.CONFIG_KEY)
                raw = value.decode() if isinstance(value, bytes) else value
            except _REDIS_ERRORS:
                logger.warning("Redis indisponível ao ler config do Sandbox; usando arquivo/memória")
        if raw:
            try:
                return SandboxConfig.model_validate_json(raw)
            except ValueError:
                logger.error("Config do Sandbox corrompida no Redis; usando arquivo/padrão")
        if self._memory_config is not None:
            return self._memory_config
        config = self._read_file() or SandboxConfig()
        self._memory_config = config
        return config

    async def save_config(self, config: SandboxConfig) -> SandboxConfig:
        config = config.model_copy(update={"updated_at": utcnow()})
        config = SandboxConfig.model_validate(config.model_dump())  # revalida após o copy
        payload = config.model_dump_json()
        self._memory_config = config
        if self._redis is not None:
            try:
                await self._redis.set(self.CONFIG_KEY, payload)
            except _REDIS_ERRORS:
                logger.warning("Redis indisponível ao salvar config do Sandbox; mantida em arquivo/memória")
        self._write_file(payload)
        logger.info(
            "Config do Sandbox atualizada enabled=%s phones=%d triggers=%s ttl=%smin",
            config.enabled, len(config.allowed_phones), config.trigger_keywords, config.session_ttl_minutes,
        )
        return config

    def effective_allowed(self, config: SandboxConfig) -> set[str]:
        return set(config.allowed_phones) | set(self._seed_phones)

    def _read_file(self) -> SandboxConfig | None:
        if self._path is None or not self._path.is_file():
            return None
        try:
            return SandboxConfig.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.error("Arquivo de config do Sandbox inválido (%s); usando padrão seguro", self._path.name)
            return None

    def _write_file(self, payload: str) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(json.loads(payload), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("Não foi possível gravar %s (config mantida no Redis/memória)", self._path)

    # ------------------------------------------------------------------ #
    # Sessões temporárias de teste
    # ------------------------------------------------------------------ #
    def _session_key(self, tenant_id: str, phone: str) -> str:
        return f"{self.SESSION_PREFIX}:{tenant_id}:{normalize_phone(phone)}"

    async def start_session(self, tenant_id: str, phone: str, *, ttl_minutes: int) -> None:
        key = self._session_key(tenant_id, phone)
        self._memory_sessions[key] = utcnow() + timedelta(minutes=ttl_minutes)
        if self._redis is not None:
            try:
                await self._redis.set(key, utcnow().isoformat(), ex=ttl_minutes * 60)
            except _REDIS_ERRORS:
                logger.warning("Redis indisponível ao abrir sessão de teste (mantida em memória)")

    async def has_session(self, tenant_id: str, phone: str) -> bool:
        key = self._session_key(tenant_id, phone)
        if self._redis is not None:
            try:
                return bool(await self._redis.exists(key))
            except _REDIS_ERRORS:
                pass
        expires = self._memory_sessions.get(key)
        return bool(expires and expires > utcnow())

    async def end_session(self, tenant_id: str, phone: str) -> bool:
        key = self._session_key(tenant_id, phone)
        existed = self._memory_sessions.pop(key, None) is not None
        if self._redis is not None:
            try:
                existed = bool(await self._redis.delete(key)) or existed
            except _REDIS_ERRORS:
                logger.warning("Redis indisponível ao encerrar sessão de teste")
        return existed

    async def list_sessions(self) -> list[SandboxSession]:
        sessions: dict[str, SandboxSession] = {}
        now = utcnow()
        for key, expires in list(self._memory_sessions.items()):
            if expires <= now:
                self._memory_sessions.pop(key, None)
                continue
            sessions[key] = self._session_from_key(key, int((expires - now).total_seconds()))
        if self._redis is not None:
            try:
                async for raw_key in self._redis.scan_iter(match=f"{self.SESSION_PREFIX}:*", count=200):
                    key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                    ttl = await self._redis.ttl(key)
                    if ttl and ttl > 0:
                        sessions[key] = self._session_from_key(key, int(ttl))
            except _REDIS_ERRORS:
                logger.warning("Redis indisponível ao listar sessões de teste")
        return sorted(sessions.values(), key=lambda s: (s.tenant_id, s.phone))

    def _session_from_key(self, key: str, ttl: int) -> SandboxSession:
        tenant_id, phone = key[len(self.SESSION_PREFIX) + 1 :].rsplit(":", 1)
        return SandboxSession(tenant_id=tenant_id, phone=phone, expires_in_seconds=ttl)

    # ------------------------------------------------------------------ #
    # Palavras-chave
    # ------------------------------------------------------------------ #
    @staticmethod
    def _keyword_pattern(keyword: str) -> re.Pattern[str]:
        # Início do texto ou espaço antes; depois: fim, espaço ou pontuação (não "-"/letra).
        # Evita falsos positivos como "site.com/lobo" ou "/lobos".
        return re.compile(rf"(?<!\S){re.escape(keyword)}(?![\w-])", re.IGNORECASE)

    def find_trigger(self, text: str | None, config: SandboxConfig) -> tuple[str | None, str]:
        """Retorna `(keyword_encontrada, texto_sem_keywords)`. Keywords maiores primeiro."""
        if not text:
            return None, text or ""
        found: str | None = None
        cleaned = text
        for kw in sorted(config.trigger_keywords, key=len, reverse=True):
            pattern = self._keyword_pattern(kw)
            if pattern.search(cleaned):
                found = found or kw
                cleaned = pattern.sub(" ", cleaned)
        if found is None:
            return None, text
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = "\n".join(line.strip() for line in cleaned.splitlines()).strip()
        cleaned = cleaned.lstrip(",.:;-– ").strip()
        if not re.search(r"\w", cleaned):  # sobrou só pontuação (ex.: "/lobo!")
            cleaned = ""
        return found, cleaned

    @staticmethod
    def is_stop_command(text: str | None, config: SandboxConfig) -> bool:
        if not text:
            return False
        first = text.strip().split(maxsplit=1)
        if not first:
            return False
        token = first[0].lower().rstrip(".!?,;:")
        return token in config.stop_keywords

    @staticmethod
    def _text_of(message: IncomingMessage) -> str | None:
        if message.text:
            return message.text
        if message.media and message.media.caption:
            return message.media.caption
        return None

    @staticmethod
    def _rewrite(message: IncomingMessage, cleaned: str) -> IncomingMessage:
        new_text = cleaned or EMPTY_TRIGGER_REPLACEMENT
        if message.text:
            return message.model_copy(update={"text": new_text})
        if message.media and message.media.caption:
            media = message.media.model_copy(update={"caption": cleaned or None})
            return message.model_copy(update={"media": media})
        return message

    # ------------------------------------------------------------------ #
    # Avaliação
    # ------------------------------------------------------------------ #
    async def evaluate(self, message: IncomingMessage) -> SandboxDecision:
        if message.from_me:
            # Respostas do próprio número são tratadas pelo fluxo de handoff, nunca pela IA.
            return SandboxDecision(SandboxAction.IGNORE, "from_me")

        config = await self.get_config()
        active = self.is_active(config)
        text = self._text_of(message)
        tenant_id, phone = message.tenant_id, message.contact_phone

        whitelisted = phone in self.effective_allowed(config)
        has_session = await self.has_session(tenant_id, phone) if active else False

        if self.is_stop_command(text, config):
            if active and not (whitelisted or has_session):
                logger.info("Sandbox: comando de parada de contato não autorizado ignorado %s", mask_phone(phone))
                return SandboxDecision(SandboxAction.IGNORE, "sandbox_locked")
            return SandboxDecision(SandboxAction.RESET, "stop_keyword")

        keyword, cleaned = self.find_trigger(text, config)

        if not active:
            msg = self._rewrite(message, cleaned) if keyword else message
            return SandboxDecision(SandboxAction.ALLOW, "sandbox_disabled", msg)

        if keyword:
            await self.start_session(tenant_id, phone, ttl_minutes=config.session_ttl_minutes)
            logger.info(
                "Sandbox: sessão de teste liberada por %smin tenant=%s contato=%s",
                config.session_ttl_minutes, tenant_id, mask_phone(phone),
            )
            return SandboxDecision(
                SandboxAction.ALLOW, "trigger_keyword", self._rewrite(message, cleaned), session_started=True
            )
        if whitelisted:
            return SandboxDecision(SandboxAction.ALLOW, "allowed_phone", message)
        if has_session:
            return SandboxDecision(SandboxAction.ALLOW, "active_session", message)

        logger.info("Sandbox: mensagem ignorada (trava ativa) tenant=%s contato=%s", tenant_id, mask_phone(phone))
        return SandboxDecision(SandboxAction.IGNORE, "sandbox_locked")

    async def status(self) -> dict[str, Any]:
        config = await self.get_config()
        return {
            "config": config.model_dump(mode="json"),
            "active": self.is_active(config),
            "forced_by_environment": self.forced_by_environment,
            "environment": self.environment,
            "seed_phones": self.seed_phones,
            "sessions": [s.to_dict() for s in await self.list_sessions()],
        }


__all__ = [
    "DEFAULT_STOP_KEYWORDS",
    "DEFAULT_TRIGGER_KEYWORDS",
    "EMPTY_TRIGGER_REPLACEMENT",
    "RESET_CONFIRMATION_TEXT",
    "SandboxAction",
    "SandboxConfig",
    "SandboxDecision",
    "SandboxGuard",
    "SandboxSession",
]
