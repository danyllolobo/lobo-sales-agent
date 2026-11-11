"""Motor de Follow-up Automático e Humanizado para leads inativos no WhatsApp.

Regras de Elegibilidade e Segurança:
1. Só avalia tenants ativos com `followup_enabled=True` (ou quando solicitado via `force=True` / `dry_run=True`).
2. Só envia follow-up para leads em estágio `in_progress` ou `qualified` cuja última mensagem na conversa
   tenha sido enviada pela IA (`last_role == "ai"`), ou seja, o cliente parou de responder.
3. NUNCA envia follow-up se a IA estiver pausada (`lead.ai_paused` ou `pause_registry.is_paused`),
   nem se o lead já estiver em `visit_scheduled`, `human_handoff`, `won` ou `lost`.
4. Respeita o limite `followup_max_attempts` (padrão: 1 tentativa) e a janela `followup_delay_minutes`.
5. Registra a mensagem no `pause_registry.register_bot_outbound` antes do disparo para que o eco `from_me`
   do WhatsApp não pause a IA acidentalmente.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage

from src.agent.graph import LeadConversationStore
from src.core.schemas import (
    LeadProfile,
    LeadStatus,
    OutgoingMessage,
    Segment,
    mask_phone,
    utcnow,
)
from src.gateways.base import WhatsAppGateway
from src.tools.handoff import AIPauseRegistry
from src.tools.tenant_data import TenantDataStore

logger = logging.getLogger("lobo.agent.followup")


def build_followup_message(
    lead: LeadProfile,
    display_name: str,
    segment: Segment,
) -> str:
    """Gera uma mensagem curta, leve e 100% humana (padrão humanizer-pt-br + clearmode) de retomada."""
    parts = (lead.name or "").strip().split()
    if not parts:
        first_name = ""
    elif parts[0].lower().rstrip(".") in ("dr", "dra", "sr", "sra", "prof", "profa") and len(parts) > 1:
        first_name = f"{parts[0]} {parts[1]}"
    else:
        first_name = parts[0]
    if first_name.lower() in ("lead", "cliente", "não", "nao"):
        first_name = ""
    greeting = f"Oi, {first_name}! Tudo bem?" if first_name else "Oi! Tudo bem por aí?"

    size = lead.attributes.get("tamanho")
    specialty = lead.attributes.get("especialidade")
    city = lead.attributes.get("bairro") or lead.attributes.get("cidade")

    if segment is Segment.MATTRESS:
        if size:
            return (
                f"{greeting} 😊 Passando rapidinho porque ainda tenho o modelo *{size.capitalize()}* "
                f"separado aqui na *{display_name}* com entrega grátis. "
                "Ficou alguma dúvida sobre as medidas ou prefere que eu segure essa condição para você?"
            )
        return (
            f"{greeting} 😊 Passando para saber se conseguiu ver as opções de colchão que te mandei "
            f"aqui da *{display_name}*. Qual medida fica melhor para o seu quarto hoje?"
        )

    if segment is Segment.PSYCHOLOGY:
        if specialty:
            return (
                f"{greeting} 😊 Passando com carinho aqui da *{display_name}* para saber se ficou alguma dúvida "
                f"sobre o atendimento de *{specialty}*. Se quiser, posso verificar os horários mais tranquilos desta semana para você."
            )
        return (
            f"{greeting} 😊 Passando aqui da *{display_name}* para saber se posso te ajudar com alguma dúvida "
            "sobre nossos atendimentos e horários disponíveis."
        )

    if segment is Segment.POOLS:
        loc_part = f" aí em *{city}*" if city else ""
        return (
            f"{greeting} 🏊‍♂️ Passando aqui da *{display_name}* para saber se conseguiu medir o espaço do terreno{loc_part}. "
            "Quer que eu separe um horário para nossa visita técnica sem compromisso?"
        )

    return (
        f"{greeting} 😊 Passando aqui da *{display_name}* para saber se ficou alguma dúvida sobre o que conversamos. "
        "Consegue falar rapidinho por aqui?"
    )


class FollowUpService:
    """Serviço de varredura e disparo de follow-up automático para leads que silenciaram."""

    def __init__(
        self,
        *,
        store: TenantDataStore,
        conversation_store: LeadConversationStore,
        pause_registry: AIPauseRegistry,
        gateway_resolver: Callable[[Any], WhatsAppGateway],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._conv_store = conversation_store
        self._pause = pause_registry
        self._gateway_resolver = gateway_resolver
        self._clock = clock

    async def run_sweep(
        self,
        *,
        tenant_id: str | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Avalia conversas ativas e envia (ou simula em `dry_run=True`) follow-ups elegíveis."""
        now = self._clock()
        conversations = await self._conv_store.list_conversations(tenant_id=tenant_id)
        evaluated = 0
        eligible: list[dict[str, Any]] = []
        sent: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for item in conversations:
            evaluated += 1
            lead: LeadProfile = item["lead"]
            last_role = item.get("last_role")
            try:
                tenant = self._store.get(lead.tenant_id)
            except Exception:  # noqa: BLE001
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "tenant_inactive"})
                continue

            t_cfg = tenant.tenant
            if not t_cfg.followup_enabled and not force:
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "followup_disabled"})
                continue

            if lead.status not in (LeadStatus.IN_PROGRESS, LeadStatus.QUALIFIED):
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": f"status_{lead.status.value}"})
                continue

            if last_role not in ("ia", "ai"):
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "waiting_ai_not_lead"})
                continue

            if lead.ai_paused or await self._pause.is_paused(lead.tenant_id, lead.phone):
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "ai_paused"})
                continue

            followup_count = int(lead.attributes.get("followup_count") or 0)
            if followup_count >= t_cfg.followup_max_attempts:
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "max_attempts_reached"})
                continue

            elapsed_minutes = (now - lead.last_interaction_at).total_seconds() / 60.0
            if elapsed_minutes < t_cfg.followup_delay_minutes and not force:
                skipped.append(
                    {
                        "tenant_id": lead.tenant_id,
                        "phone": mask_phone(lead.phone),
                        "reason": "delay_not_reached",
                        "elapsed_minutes": round(elapsed_minutes, 1),
                        "required_minutes": t_cfg.followup_delay_minutes,
                    }
                )
                continue

            text = build_followup_message(lead, t_cfg.display_name, t_cfg.segment)
            candidate_info = {
                "tenant_id": lead.tenant_id,
                "phone": lead.phone,
                "phone_masked": mask_phone(lead.phone),
                "name": lead.name,
                "attempt": followup_count + 1,
                "elapsed_minutes": round(elapsed_minutes, 1),
                "message": text,
            }
            eligible.append(candidate_info)

            if dry_run:
                continue

            try:
                gateway = self._gateway_resolver(t_cfg.whatsapp_provider)
                session_id = t_cfg.whatsapp_session_id or gateway.default_session
                outgoing = OutgoingMessage(
                    tenant_id=lead.tenant_id,
                    provider=t_cfg.whatsapp_provider,
                    session_id=session_id,
                    contact_phone=lead.phone,
                    text=text,
                    simulate_typing=True,
                    typing_seconds=1.8,
                )
                await self._pause.register_bot_outbound(lead.tenant_id, lead.phone, text=text)
                res = await gateway.deliver(outgoing)
                if res.message_id:
                    await self._pause.register_bot_outbound(lead.tenant_id, lead.phone, message_id=res.message_id)

                _, history = await self._conv_store.load(lead.tenant_id, lead.phone)
                lead.attributes["followup_count"] = str(followup_count + 1)
                lead.attributes["last_followup_at"] = now.isoformat()
                lead.last_interaction_at = now
                history.append(AIMessage(content=text))
                await self._conv_store.save(lead, history)
                sent.append({**candidate_info, "message_id": res.message_id})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Falha ao enviar follow-up tenant=%s contato=%s: %s",
                    lead.tenant_id,
                    mask_phone(lead.phone),
                    exc,
                )
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "send_error"})

        return {
            "evaluated": evaluated,
            "eligible_count": len(eligible),
            "sent_count": len(sent),
            "dry_run": dry_run,
            "eligible": eligible,
            "sent": sent,
            "skipped": skipped,
        }


__all__ = ["FollowUpService", "build_followup_message"]
