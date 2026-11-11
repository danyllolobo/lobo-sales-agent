"""Ferramenta de transferência para atendimento humano (handoff).

Quando acionada (pedido do lead, dúvida fora da base, desconto fora da tabela,
emergência clínica, preço indisponível ou falha técnica):

1. Pausa a IA para o contato IMEDIATAMENTE (Redis, com TTL) — vale para todas as réplicas.
2. Registra nota privada com resumo executivo no CRM do tenant (Chatwoot/GHL),
   aplica a tag `atendimento_humano` e atribui time/agente quando configurado.
3. Devolve a mensagem cordial (determinística) a ser enviada ao lead.

FALLBACK SEGURO: nada aqui levanta exceção. Se o CRM ou o Redis falharem, o lead
ainda recebe a mensagem, a pausa local é aplicada e as falhas vão em `errors`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from src.core.exceptions import LoboError, StateStoreError
from src.core.schemas import LeadProfile, Segment, TenantId, mask_phone, normalize_phone, utcnow
from src.crm.base import CRMConnector
from src.tools.results import ToolResult, ToolStatus
from src.tools.tenant_data import TenantData, TenantDataStore

logger = logging.getLogger("lobo.handoff")


class HandoffReason(StrEnum):
    CUSTOMER_REQUEST = "customer_request"
    UNKNOWN_ANSWER = "unknown_answer"
    DISCOUNT_NEGOTIATION = "discount_negotiation"
    CLINICAL_EMERGENCY = "clinical_emergency"
    PRICE_UNAVAILABLE = "price_unavailable"
    TECHNICAL_FAILURE = "technical_failure"
    OTHER = "other"


REASON_LABELS: dict[HandoffReason, str] = {
    HandoffReason.CUSTOMER_REQUEST: "Cliente pediu para falar com uma pessoa",
    HandoffReason.UNKNOWN_ANSWER: "Dúvida fora da base de conhecimento",
    HandoffReason.DISCOUNT_NEGOTIATION: "Negociação de desconto/condição fora da tabela",
    HandoffReason.CLINICAL_EMERGENCY: "POSSÍVEL EMERGÊNCIA CLÍNICA",
    HandoffReason.PRICE_UNAVAILABLE: "Preço/condição indisponível no sistema",
    HandoffReason.TECHNICAL_FAILURE: "Falha técnica no atendimento automático",
    HandoffReason.OTHER: "Outro motivo",
}

CUSTOMER_MESSAGES: dict[HandoffReason, str] = {
    HandoffReason.CUSTOMER_REQUEST: (
        "Claro! Já chamei uma pessoa da nossa supervisão aqui da equipe para dar sequência "
        "ao seu atendimento. Só um instante! 😊"
    ),
    HandoffReason.UNKNOWN_ANSWER: (
        "Ótima pergunta! Para te passar essa informação com total precisão, vou alinhar agora "
        "mesmo com o meu gerente comercial e já te retorno com os detalhes!"
    ),
    HandoffReason.DISCOUNT_NEGOTIATION: (
        "Essa condição exclusiva foge da minha alçada de consultor. Vou conversar diretamente "
        "com o meu gerente comercial aqui da nossa equipe comercial agora para ver se ele autoriza "
        "essa condição especial para você, combinado? Só um instante enquanto ele valida!"
    ),
    HandoffReason.PRICE_UNAVAILABLE: (
        "Para te passar o orçamento personalizado certinho, vou validar agora mesmo com o meu "
        "gerente comercial e já te retorno por aqui com os detalhes, combinado?"
    ),
    HandoffReason.TECHNICAL_FAILURE: (
        "Tivemos uma rápida instabilidade por aqui, mas já chamei uma pessoa da nossa supervisão "
        "para continuar seu atendimento agora mesmo. Só um instante, por favor!"
    ),
    HandoffReason.OTHER: (
        "Vou alinhar esse detalhe com o meu gerente comercial da nossa equipe e já te retorno "
        "por aqui em instantes!"
    ),
}

PSYCHOLOGY_ESCALATION_MESSAGES: dict[HandoffReason, str] = {
    HandoffReason.UNKNOWN_ANSWER: (
        "Para te direcionar ao profissional com a abordagem mais assertiva para o seu caso, "
        "vou alinhar essa especificidade com a nossa coordenação clínica e já te retorno com os detalhes!"
    ),
    HandoffReason.PRICE_UNAVAILABLE: (
        "Para te direcionar ao profissional com a abordagem mais assertiva para o seu caso, "
        "vou alinhar os detalhes desse protocolo com a nossa coordenação clínica e já te retorno!"
    ),
    HandoffReason.DISCOUNT_NEGOTIATION: (
        "Essa condição especial foge da minha alçada aqui no acolhimento. Vou conversar diretamente "
        "com a nossa coordenação clínica agora para ver se conseguimos autorizar para você, combinado? Só um instante!"
    ),
}

# Emergência: o encaminhamento a serviços de crise vai SEMPRE, independente do CRM.
EMERGENCY_MESSAGE = (
    "Sinto muito que você esteja passando por isso, e obrigado por compartilhar. Já acionei nossa equipe agora. "
    "Se você estiver em risco ou pensando em se machucar, por favor ligue imediatamente para o CVV no 188 "
    "(24 horas, gratuito) ou para o SAMU no 192. Você não está sozinho(a)."
)


class HandoffInput(BaseModel):
    """Contrato (ARCHITECTURE.md §5). `tenant_id`/`lead_phone` vêm do contexto, não do LLM."""

    tenant_id: TenantId
    lead_phone: str
    reason: HandoffReason
    conversation_summary: str = Field(min_length=1, max_length=2000)
    suggested_action: str = Field(default="", max_length=500)


def format_phone_br(phone: str) -> str:
    """`5591988887777` → `+55 (91) 98888-7777` (outros países: `+<dígitos>`)."""
    if phone.startswith("55") and len(phone) in (12, 13):
        ddd, number = phone[2:4], phone[4:]
        return f"+55 ({ddd}) {number[:-4]}-{number[-4:]}"
    return f"+{phone}"


# --------------------------------------------------------------------------- #
# Pausa da IA (estado compartilhado entre réplicas)
# --------------------------------------------------------------------------- #
class AIPauseRegistry:
    """Marca contatos sob atendimento humano. A IA consulta antes de responder."""

    def __init__(self, redis: aioredis.Redis, *, prefix: str = "lobo:pause") -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, tenant_id: str, phone: str) -> str:
        return f"{self._prefix}:{tenant_id}:{normalize_phone(phone)}"

    async def pause(self, tenant_id: str, phone: str, *, reason: str, hours: float, now: datetime | None = None) -> datetime:
        now = now or utcnow()
        until = now + timedelta(hours=hours)
        value = json.dumps({"reason": reason, "paused_at": now.isoformat(), "until": until.isoformat()})
        try:
            await self._redis.set(self._key(tenant_id, phone), value, px=int(hours * 3600 * 1000))
        except (RedisError, OSError) as exc:
            raise StateStoreError("falha ao pausar IA", tenant_id=tenant_id, context={"contact": mask_phone(phone)}) from exc
        return until

    async def get(self, tenant_id: str, phone: str) -> dict[str, Any] | None:
        try:
            raw = await self._redis.get(self._key(tenant_id, phone))
        except (RedisError, OSError) as exc:
            raise StateStoreError("falha ao consultar pausa da IA", tenant_id=tenant_id) from exc
        if raw is None:
            return None
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)

    async def is_paused(self, tenant_id: str, phone: str) -> bool:
        return await self.get(tenant_id, phone) is not None

    async def resume(self, tenant_id: str, phone: str) -> None:
        try:
            await self._redis.delete(self._key(tenant_id, phone))
        except (RedisError, OSError) as exc:
            raise StateStoreError("falha ao retomar IA", tenant_id=tenant_id) from exc

    async def register_bot_outbound(
        self,
        tenant_id: str,
        phone: str,
        *,
        message_id: str | None = None,
        text: str | None = None,
        ttl_seconds: int = 120,
    ) -> None:
        """Registra mensagem enviada pela própria IA para ignorar eco `from_me=True` no webhook."""
        norm_phone = normalize_phone(phone)
        try:
            if message_id:
                key = f"{self._prefix}:echo_id:{tenant_id}:{norm_phone}:{message_id.strip()}"
                await self._redis.set(key, "1", ex=ttl_seconds)
            if text:
                digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]
                key = f"{self._prefix}:echo_txt:{tenant_id}:{norm_phone}:{digest}"
                await self._redis.set(key, "1", ex=ttl_seconds)
        except (RedisError, OSError):
            logger.debug("Não foi possível registrar eco de saída para %s", mask_phone(norm_phone))

    async def is_bot_outbound(
        self,
        tenant_id: str,
        phone: str,
        *,
        message_id: str | None = None,
        text: str | None = None,
    ) -> bool:
        """Retorna True se o evento `from_me` corresponde a uma resposta recém-enviada pela IA."""
        norm_phone = normalize_phone(phone)
        try:
            if message_id:
                key = f"{self._prefix}:echo_id:{tenant_id}:{norm_phone}:{message_id.strip()}"
                if await self._redis.get(key) is not None:
                    return True
            if text:
                digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]
                key = f"{self._prefix}:echo_txt:{tenant_id}:{norm_phone}:{digest}"
                if await self._redis.get(key) is not None:
                    return True
        except (RedisError, OSError):
            return False
        return False


# --------------------------------------------------------------------------- #
# Serviço de handoff
# --------------------------------------------------------------------------- #
CRMResolver = Callable[[TenantData], CRMConnector | None]


class HandoffService:
    def __init__(
        self,
        store: TenantDataStore,
        pause_registry: AIPauseRegistry | None,
        crm_resolver: CRMResolver,
        *,
        pause_hours: float = 24.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._pause = pause_registry
        self._crm_resolver = crm_resolver
        self._pause_hours = pause_hours
        self._clock = clock

    async def trigger_human_handoff(
        self,
        lead: LeadProfile,
        *,
        reason: HandoffReason | str,
        conversation_summary: str,
        suggested_action: str = "",
    ) -> ToolResult:
        tool = "trigger_human_handoff"
        try:
            reason = HandoffReason(reason)
        except ValueError:
            reason = HandoffReason.OTHER
        summary = (conversation_summary or "").strip()[:2000] or "(sem resumo)"
        suggested = (suggested_action or "").strip()[:500]
        errors: list[str] = []

        tenant_data: TenantData | None = None
        try:
            tenant_data = self._store.get(lead.tenant_id)
        except LoboError:
            errors.append("tenant_data")

        # 1) pausa local + compartilhada (antes de qualquer chamada externa)
        lead.mark_human_handoff()
        paused_until: datetime | None = None
        if self._pause is not None:
            try:
                paused_until = await self._pause.pause(
                    lead.tenant_id, lead.phone, reason=reason.value, hours=self._pause_hours, now=self._clock()
                )
            except StateStoreError:
                errors.append("pause_store")

        # 2) CRM: nota privada + tag + atribuição
        crm_synced = False
        if tenant_data is not None:
            try:
                crm = self._crm_resolver(tenant_data)
                if crm is None:
                    errors.append("crm_not_configured")
                else:
                    note = self.build_note(lead, tenant_data, reason, summary, suggested, paused_until)
                    await crm.escalate_to_human(lead, note)
                    crm_synced = True
            except LoboError as exc:
                errors.append(f"crm:{type(exc).__name__}")
            except Exception as exc:  # noqa: BLE001 - handoff nunca pode falhar
                logger.exception("Erro inesperado no CRM durante handoff")
                errors.append(f"crm:{type(exc).__name__}")

        # 3) mensagem ao lead
        segment = tenant_data.tenant.segment if tenant_data else Segment.GENERIC
        customer_message = self.customer_message(reason, segment)

        log = logger.warning if errors else logger.info
        log(
            "Handoff humano reason=%s crm_synced=%s errors=%s %s",
            reason.value, crm_synced, errors, lead.log_context(),
        )
        return ToolResult(
            tool=tool,
            tenant_id=lead.tenant_id,
            status=ToolStatus.OK,
            requires_human=True,
            reason=reason.value,
            data={
                "customer_message": customer_message,
                "crm_synced": crm_synced,
                "ai_paused_until": paused_until.isoformat() if paused_until else None,
                "errors": errors,
            },
            agent_instruction=(
                "Envie ao cliente EXATAMENTE o texto de 'customer_message' e encerre o turno. "
                "A IA está pausada para este contato."
            ),
        )

    @staticmethod
    def customer_message(reason: HandoffReason, segment: Segment) -> str:
        if reason is HandoffReason.CLINICAL_EMERGENCY:
            return EMERGENCY_MESSAGE
        if segment is Segment.PSYCHOLOGY and reason in PSYCHOLOGY_ESCALATION_MESSAGES:
            return PSYCHOLOGY_ESCALATION_MESSAGES[reason]
        return CUSTOMER_MESSAGES.get(reason, CUSTOMER_MESSAGES[HandoffReason.OTHER])

    def build_note(
        self,
        lead: LeadProfile,
        tenant: TenantData,
        reason: HandoffReason,
        summary: str,
        suggested: str,
        paused_until: datetime | None,
    ) -> str:
        """Resumo executivo para o atendente (nota PRIVADA no CRM — telefone completo é permitido aqui)."""
        tz = tenant.tenant.tz
        now = self._clock().astimezone(tz)
        header = (
            "🚨 URGENTE — POSSÍVEL EMERGÊNCIA CLÍNICA. Contato imediato recomendado."
            if reason is HandoffReason.CLINICAL_EMERGENCY
            else "🤖➡️👤 Transferência para atendimento humano"
        )
        lines = [
            header,
            f"Motivo: {REASON_LABELS[reason]}",
            f"Lead: {lead.name or 'nome não informado'} — {format_phone_br(lead.phone)}",
            f"Resumo: {summary}",
        ]
        if suggested:
            lines.append(f"Ação sugerida: {suggested}")
        if lead.attributes:
            collected = "; ".join(f"{k}: {v}" for k, v in sorted(lead.attributes.items()))
            lines.append(f"Dados coletados: {collected}")
        if lead.source:
            lines.append(f"Origem: {lead.source}")
        if lead.tags:
            lines.append(f"Tags: {', '.join(lead.tags)}")
        lines.append(f"Registrado em: {now:%d/%m/%Y %H:%M} ({tenant.tenant.timezone})")
        if paused_until:
            lines.append(f"⏸️ IA pausada para este contato até {paused_until.astimezone(tz):%d/%m %H:%M}.")
        else:
            lines.append("⏸️ IA pausada para este contato.")
        return "\n".join(lines)

