"""Operações de controle de conversa compartilhadas entre webhook e painel.

- `reset`: encerra a sessão de teste do Sandbox, apaga memória/histórico, remove a pausa
  da IA e descarta mensagens pendentes no buffer (comando `/parar` ou botão do painel).
- `resume_ai`: reativa a IA após handoff (pausa no Redis + flags/tag do lead + tag no CRM).
- `pause_ai`: pausa a IA manualmente para um contato (atendimento humano).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from src.agent.graph import LeadConversationStore
from src.buffer.message_queue import MessageBuffer
from src.core.exceptions import LoboError
from src.core.sandbox import SandboxGuard
from src.core.schemas import LeadProfile, LeadStatus, StandardTag, mask_phone, normalize_phone
from src.crm.base import CRMConnector
from src.tools.handoff import AIPauseRegistry
from src.tools.tenant_data import TenantData, TenantDataStore

logger = logging.getLogger("lobo.admin.control")


class ConversationControl:
    def __init__(
        self,
        *,
        store: TenantDataStore,
        sandbox: SandboxGuard,
        conversation_store: LeadConversationStore,
        pause_registry: AIPauseRegistry,
        buffer: MessageBuffer | None = None,
        crm_resolver: Callable[[TenantData], CRMConnector | None] | None = None,
        pause_hours: float = 24.0,
    ) -> None:
        self._store = store
        self._sandbox = sandbox
        self._conv = conversation_store
        self._pause = pause_registry
        self._buffer = buffer
        self._crm_resolver = crm_resolver or (lambda _t: None)
        self._pause_hours = pause_hours

    async def reset(self, tenant_id: str, phone: str, *, session_id: str | None = None) -> dict[str, Any]:
        phone = normalize_phone(phone)
        result: dict[str, Any] = {"tenant_id": tenant_id, "phone": phone, "errors": []}
        result["sandbox_session_ended"] = await self._sandbox.end_session(tenant_id, phone)
        result["memory_cleared"] = await self._conv.delete(tenant_id, phone)
        try:
            await self._pause.resume(tenant_id, phone)
            result["ai_resumed"] = True
        except LoboError:
            result["ai_resumed"] = False
            result["errors"].append("pause_store")
        if self._buffer is not None and session_id:
            try:
                result["pending_discarded"] = await self._buffer.discard(tenant_id, session_id, phone)
            except LoboError:
                result["errors"].append("buffer")
        logger.info("Conversa resetada tenant=%s contato=%s", tenant_id, mask_phone(phone))
        return result

    async def resume_ai(self, tenant_id: str, phone: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        errors: list[str] = []
        try:
            await self._pause.resume(tenant_id, phone)
        except LoboError:
            errors.append("pause_store")

        lead, _ = await self._conv.load(tenant_id, phone)
        lead.human_active = False
        if lead.status is LeadStatus.HUMAN_HANDOFF:
            lead.status = LeadStatus.IN_PROGRESS
        lead.remove_tag(StandardTag.HUMAN_HANDOFF)
        await self._conv.update_lead(lead)

        crm_synced = await self._crm_remove_handoff_tag(lead, errors)
        logger.info("IA reativada tenant=%s contato=%s crm_synced=%s", tenant_id, mask_phone(phone), crm_synced)
        return {"tenant_id": tenant_id, "phone": phone, "ai_paused": False, "crm_synced": crm_synced, "errors": errors}

    async def pause_ai(self, tenant_id: str, phone: str, *, hours: float | None = None) -> dict[str, Any]:
        phone = normalize_phone(phone)
        hours = hours or self._pause_hours
        until = await self._pause.pause(tenant_id, phone, reason="manual_admin", hours=hours)
        lead, _ = await self._conv.load(tenant_id, phone)
        lead.mark_human_handoff()
        await self._conv.update_lead(lead)
        logger.info("IA pausada manualmente tenant=%s contato=%s", tenant_id, mask_phone(phone))
        return {"tenant_id": tenant_id, "phone": phone, "ai_paused": True, "paused_until": until.isoformat()}

    async def _crm_remove_handoff_tag(self, lead: LeadProfile, errors: list[str]) -> bool:
        # Só toca o CRM se o contato já existir lá (não cria contatos/conversas ao reativar).
        if not (lead.crm_contact_id or lead.crm_conversation_id):
            return False
        try:
            crm = self._crm_resolver(self._store.get(lead.tenant_id))
            if crm is None:
                return False
            await crm.remove_tags(lead, [StandardTag.HUMAN_HANDOFF])
            return True
        except Exception as exc:  # noqa: BLE001 - CRM é best-effort aqui
            errors.append(f"crm:{type(exc).__name__}")
            return False


__all__ = ["ConversationControl"]

