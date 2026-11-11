"""Interface abstrata dos conectores de WhatsApp (WAHA, AstraCalls, Meta Cloud...).

Cada gateway:
1. Normaliza o payload bruto do webhook para `IncomingMessage` (`parse_webhook`).
2. Valida a assinatura HMAC do webhook (`verify_signature`).
3. Envia o status "digitando..." (`set_typing`) e a resposta final (`send_message`).
4. Marca mensagens como lidas e baixa mídias recebidas (para transcrição/visão).

Falhas de rede/HTTP viram `GatewayError` (handoff_recommended=True) com contexto
mascarado (LGPD), nunca derrubando o servidor.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr

from src.core.exceptions import GatewayError
from src.core.http import request_json
from src.core.schemas import IncomingMessage, OutgoingMessage, Provider, mask_phone

logger = logging.getLogger("lobo.gateway")

# JIDs que nunca representam um lead 1:1
_NON_CONTACT_SUFFIXES = ("@g.us", "@broadcast", "@newsletter")


@dataclass(frozen=True, slots=True)
class IgnoredEvent:
    """Evento de webhook descartado na normalização (com motivo, para métricas/logs)."""

    reason: str
    event: str | None = None
    message_id: str | None = None


@dataclass(slots=True)
class ParsedWebhook:
    messages: list[IncomingMessage] = field(default_factory=list)
    ignored: list[IgnoredEvent] = field(default_factory=list)

    def ignore(self, reason: str, event: str | None = None, message_id: str | None = None) -> ParsedWebhook:
        self.ignored.append(IgnoredEvent(reason=reason, event=event, message_id=message_id))
        return self


@dataclass(frozen=True, slots=True)
class SendResult:
    provider: Provider
    message_id: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _secret(value: SecretStr | str | None) -> str | None:
    if value is None:
        return None
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def is_non_contact_jid(jid: str) -> bool:
    return jid == "status@broadcast" or jid.endswith(_NON_CONTACT_SUFFIXES)


def dig(mapping: Any, *path: str) -> Any:
    """Acesso seguro a dicts aninhados: `dig(p, "_data", "key", "id")`."""
    for key in path:
        if not isinstance(mapping, Mapping):
            return None
        mapping = mapping.get(key)
    return mapping


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Busca de header case-insensitive (funciona com dict comum ou Starlette Headers)."""
    lowered = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lowered), None)


def to_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def epoch_to_datetime(value: Any) -> datetime | None:
    """Aceita epoch em segundos ou milissegundos."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 1e11:  # milissegundos
        ts /= 1000
    return datetime.fromtimestamp(ts, tz=UTC)


class WhatsAppGateway(ABC):
    provider: ClassVar[Provider]

    def __init__(
        self,
        base_url: str,
        api_key: SecretStr | str | None = None,
        *,
        default_session: str,
        webhook_secret: SecretStr | str | None = None,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError(f"{type(self).__name__}: base_url é obrigatório")
        self.base_url = base_url.rstrip("/")
        self.default_session = default_session
        self._api_key = _secret(api_key)
        self._webhook_secret = _secret(webhook_secret)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------------ #
    # Contrato
    # ------------------------------------------------------------------ #
    @abstractmethod
    def parse_webhook(self, tenant_id: str, payload: Mapping[str, Any]) -> ParsedWebhook:
        """Normaliza o payload bruto em zero ou mais `IncomingMessage`."""

    @abstractmethod
    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        """Valida a assinatura do webhook. Sem segredo configurado → sempre True."""

    @abstractmethod
    async def set_typing(self, session_id: str, to: str, typing: bool = True) -> None:
        """Liga/desliga o "digitando..." no chat `to` (telefone ou JID nativo)."""

    @abstractmethod
    async def send_message(self, message: OutgoingMessage) -> SendResult:
        """Envia a resposta (texto ou mídia)."""

    @abstractmethod
    async def mark_seen(self, session_id: str, to: str, message_ids: list[str]) -> None:
        """Marca mensagens como lidas."""

    @abstractmethod
    async def download_media(self, message: IncomingMessage) -> bytes:
        """Baixa os bytes da mídia de uma mensagem recebida."""

    @abstractmethod
    def _auth_headers(self) -> dict[str, str]: ...

    # ------------------------------------------------------------------ #
    # Comportamento comum
    # ------------------------------------------------------------------ #
    @staticmethod
    def destination(message: OutgoingMessage | IncomingMessage) -> str:
        """Endereço de destino: JID nativo quando conhecido, senão o telefone."""
        return message.chat_id or message.contact_phone

    @contextlib.asynccontextmanager
    async def typing(self, session_id: str, to: str) -> AsyncIterator[None]:
        """Mantém "digitando..." durante um bloco (ex.: enquanto o LLM pensa). Best-effort."""
        await self._safe_typing(session_id, to, True)
        try:
            yield
        finally:
            await self._safe_typing(session_id, to, False)

    async def deliver(self, message: OutgoingMessage) -> SendResult:
        """Fluxo humanizado: "digitando..." → pausa proporcional ao texto → envio."""
        to = self.destination(message)
        delay = message.effective_typing_seconds
        if delay > 0:
            await self._safe_typing(message.session_id, to, True)
            await asyncio.sleep(delay)
        try:
            return await self.send_message(message)
        finally:
            if delay > 0:
                await self._safe_typing(message.session_id, to, False)

    async def _safe_typing(self, session_id: str, to: str, typing: bool) -> None:
        """Presença é cosmética: falhas não podem impedir a resposta."""
        try:
            await self.set_typing(session_id, to, typing)
        except GatewayError as exc:
            logger.warning("Falha ao enviar presença (%s) contato=%s", exc.message, mask_phone(to))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expect_json: bool = True,
        absolute_url: str | None = None,
        log_contact: str | None = None,
    ) -> Any:
        merged_headers = {**self._auth_headers(), **(dict(headers) if headers else {})}
        return await request_json(
            self._client,
            method,
            absolute_url or f"{self.base_url}{path}",
            error_cls=GatewayError,
            service=self.provider.value,
            path=path,
            headers=merged_headers,
            json=json,
            expect_json=expect_json,
            log_contact=log_contact,
        )

    @staticmethod
    def _hmac_matches(secret: str | None, body: bytes, received: str | None, algo: str, prefix: str = "") -> bool:
        if not secret:
            return True  # verificação desativada (sem segredo configurado)
        if not received:
            return False
        digest = hmac.new(secret.encode(), body, getattr(hashlib, algo)).hexdigest()
        return hmac.compare_digest(f"{prefix}{digest}", received.strip().lower())

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
