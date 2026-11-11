"""Conectores de WhatsApp e fábrica a partir das configurações."""

from __future__ import annotations

import httpx

from src.config import Settings
from src.core.exceptions import ConfigurationError
from src.core.schemas import Provider
from src.gateways.astracalls import AstraCallsGateway
from src.gateways.base import IgnoredEvent, ParsedWebhook, SendResult, WhatsAppGateway
from src.gateways.waha import WahaGateway

__all__ = [
    "AstraCallsGateway",
    "IgnoredEvent",
    "ParsedWebhook",
    "SendResult",
    "WahaGateway",
    "WhatsAppGateway",
    "create_gateway",
]


def create_gateway(
    provider: Provider | str,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> WhatsAppGateway:
    """Instancia o gateway do provedor usando as credenciais do `.env`."""
    provider = Provider(provider)
    if provider is Provider.WAHA:
        if not settings.waha_enabled:
            raise ConfigurationError("WAHA não configurado (WAHA_BASE_URL / WAHA_API_KEY)")
        return WahaGateway(
            settings.waha_base_url or "",
            settings.waha_api_key,
            default_session=settings.waha_session,
            webhook_secret=settings.waha_webhook_hmac_key,
            client=client,
        )
    if provider is Provider.ASTRACALLS:
        if not settings.astracalls_enabled or not settings.astracalls_session_id:
            raise ConfigurationError(
                "AstraCalls não configurado (ASTRACALLS_BASE_URL / ASTRACALLS_API_KEY / ASTRACALLS_SESSION_ID)"
            )
        return AstraCallsGateway(
            settings.astracalls_base_url or "",
            settings.astracalls_api_key,
            default_session=settings.astracalls_session_id,
            client_id=settings.astracalls_client_id,
            webhook_secret=settings.astracalls_webhook_secret,
            client=client,
        )
    raise ConfigurationError(f"Provedor de WhatsApp não suportado: {provider.value}")

