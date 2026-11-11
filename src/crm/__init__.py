"""Conectores de CRM e fábrica por tenant."""

from __future__ import annotations

import httpx
from pydantic import SecretStr

from src.config import Settings
from src.core.exceptions import ConfigurationError
from src.core.schemas import CRMProvider
from src.crm.base import CRMConnector
from src.crm.chatwoot import ChatwootConnector
from src.crm.ghl import GHLConnector
from src.tools.tenant_data import TenantData

__all__ = ["CRMConnector", "ChatwootConnector", "GHLConnector", "create_crm"]


def create_crm(
    tenant: TenantData,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> CRMConnector | None:
    """Instancia o CRM do tenant. Retorna `None` se o tenant não usa CRM."""
    provider = tenant.tenant.crm_provider
    if provider is CRMProvider.NONE:
        return None

    if provider is CRMProvider.CHATWOOT:
        cfg = tenant.crm.chatwoot
        if cfg is None or not settings.chatwoot_enabled:
            raise ConfigurationError("Chatwoot não configurado (CHATWOOT_BASE_URL / CHATWOOT_API_TOKEN)", tenant_id=tenant.tenant_id)
        assert settings.chatwoot_api_token is not None
        return ChatwootConnector(
            settings.chatwoot_base_url or "",
            settings.chatwoot_api_token,
            account_id=cfg.account_id,
            inbox_id=cfg.inbox_id,
            handoff_team_id=cfg.handoff_team_id,
            handoff_assignee_id=cfg.handoff_assignee_id,
            bot_agent_ids=cfg.bot_agent_ids,
            assignee_means_human=cfg.assignee_means_human,
            create_missing=cfg.create_missing,
            client=client,
        )

    if provider is CRMProvider.GHL:
        cfg_ghl = tenant.crm.ghl
        if cfg_ghl is None:
            raise ConfigurationError("GHL não configurado para o tenant", tenant_id=tenant.tenant_id)
        if not (cfg_ghl.token_setting.startswith("ghl_") and cfg_ghl.token_setting.endswith("_token")):
            raise ConfigurationError(
                "GHL: token_setting inválido (deve seguir o padrão ghl_*_token)",
                tenant_id=tenant.tenant_id,
            )
        if not (cfg_ghl.location_setting.startswith("ghl_") and cfg_ghl.location_setting.endswith("_location_id")):
            raise ConfigurationError(
                "GHL: location_setting inválido (deve seguir o padrão ghl_*_location_id)",
                tenant_id=tenant.tenant_id,
            )
        token = getattr(settings, cfg_ghl.token_setting, None)
        location = getattr(settings, cfg_ghl.location_setting, None) or cfg_ghl.location_id
        if not isinstance(token, SecretStr) or not location:
            raise ConfigurationError(
                f"GHL: defina {cfg_ghl.token_setting.upper()} e {cfg_ghl.location_setting.upper()} no .env",
                tenant_id=tenant.tenant_id,
            )
        return GHLConnector(token, str(location), handoff_user_id=cfg_ghl.handoff_user_id, client=client)

    raise ConfigurationError(f"CRM não suportado: {provider.value}", tenant_id=tenant.tenant_id)

