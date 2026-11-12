"""Motor de Régua de Follow-up Progressiva e Humanizada para leads inativos no WhatsApp.

Como funciona a Cadência Progressiva Real (não usa tempo fixo repetido):
- Etapa 1 (`warm_check` — ex.: 2h ou 30min após silêncio): Retomada leve sobre a dúvida/produto consultado.
- Etapa 2 (`value_reinforce` — ex.: +1 dia / 24h após o 1º toque): Reforço de diferencial (parcelamento sem juros,
  frete grátis, reembolso ou horário na agenda da semana).
- Etapa 3 (`objection_break` — ex.: +3 dias / 72h após o 2º toque): Pergunta consultiva curta para destravar
  objeção ou saber se o cliente ainda busca a solução.
- Etapa 4 (`breakup` — ex.: +7 dias após o 3º toque): Encerramento elegante sem pressão ("vou encerrar seu
  atendimento por aqui para não incomodar, mas continuo à disposição").

Regras de Segurança:
1. Só avalia tenants ativos com `followup_enabled=True` e cujo plano permita follow-up (ou quando `force=True`).
2. Só envia para leads em `in_progress` ou `qualified` cuja última mensagem tenha sido da IA (`last_role in ("ia", "ai")`).
3. NUNCA envia se a IA estiver pausada (`lead.ai_paused` ou `pause_registry.is_paused`), nem em `visit_scheduled` / `human_handoff`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage

from src.agent.graph import LeadConversationStore
from src.core.schemas import (
    PLAN_SPECS,
    FollowUpStep,
    LeadProfile,
    LeadStatus,
    OutgoingMessage,
    Provider,
    Segment,
    TenantConfig,
    default_followup_steps,
    mask_phone,
    utcnow,
)
from src.gateways.base import WhatsAppGateway
from src.tools.handoff import AIPauseRegistry
from src.tools.tenant_data import TenantDataStore

logger = logging.getLogger("lobo.agent.followup")


def _extract_first_name(raw_name: str | None) -> str:
    parts = (raw_name or "").strip().split()
    if not parts:
        return ""
    if parts[0].lower().rstrip(".") in ("dr", "dra", "sr", "sra", "prof", "profa") and len(parts) > 1:
        first_name = f"{parts[0]} {parts[1]}"
    else:
        first_name = parts[0]
    if first_name.lower() in ("lead", "cliente", "não", "nao"):
        return ""
    return first_name


def resolve_effective_steps(t_cfg: TenantConfig) -> list[FollowUpStep]:
    """Retorna a lista de etapas progressivas do tenant, respeitando o teto do plano e overrides.

    A tabela de etapas do painel (`followup_steps`) é a fonte da verdade: se ela foi configurada, todas as
    suas etapas valem (limitadas ao teto do plano). `followup_max_attempts` só limita a régua padrão legada.
    """
    explicit_steps = bool(t_cfg.followup_steps) and "followup_steps" in t_cfg.model_fields_set
    steps = list(t_cfg.followup_steps) if t_cfg.followup_steps else default_followup_steps()
    if not steps:
        steps = default_followup_steps()
    # Compatibilidade: se o usuário/teste alterou `followup_delay_minutes` do padrão 120 e manteve o step 1 em 120
    if t_cfg.followup_delay_minutes != 120 and steps[0].delay_minutes == 120:
        steps[0] = steps[0].model_copy(update={"delay_minutes": t_cfg.followup_delay_minutes})

    plan_spec = PLAN_SPECS.get(t_cfg.plan_tier) or PLAN_SPECS["pro"]
    max_plan_steps = int(plan_spec.get("max_followup_steps") or 4)
    attempts_cap = len(steps) if explicit_steps else max(1, t_cfg.followup_max_attempts)
    limit = min(len(steps), attempts_cap, max(1, max_plan_steps))
    return steps[:limit]


def build_followup_message(
    lead: LeadProfile,
    display_name: str,
    segment: Segment,
    *,
    step: FollowUpStep | None = None,
) -> str:
    """Gera a mensagem da etapa atual da régua (1º toque, +1 dia, +3 dias ou +7 dias break-up)."""
    first_name = _extract_first_name(lead.name)
    greeting = f"Oi, {first_name}! Tudo bem?" if first_name else "Oi! Tudo bem por aí?"
    strategy = step.strategy if step is not None else "warm_check"

    if step is not None and step.custom_message and step.custom_message.strip():
        msg = step.custom_message.strip()
        msg = msg.replace("{nome}", first_name or "tudo bem").replace("{empresa}", display_name)
        return msg

    size = lead.attributes.get("tamanho")
    specialty = lead.attributes.get("especialidade")
    city = lead.attributes.get("bairro") or lead.attributes.get("cidade")

    # Etapa 4 — Break-up / Encerramento elegante (+7 dias)
    if strategy == "breakup":
        return (
            f"Oi{f', {first_name}' if first_name else ''}! Tudo joia? 😊 "
            f"Como não conseguimos continuar por aqui, vou encerrar seu atendimento na *{display_name}* "
            "para não te incomodar com mensagens. Quando quiser retomar, é só me mandar um oi que te atendo na hora!"
        )

    # Etapa 3 — Quebra de objeção / Reativação (+3 dias)
    if strategy == "objection_break":
        if segment is Segment.MATTRESS:
            item_ref = f"o colchão *{size.capitalize()}*" if size else "o colchão novo"
            return (
                f"Oi{f', {first_name}' if first_name else ''}, tudo bom? Sei que a semana é corrida! "
                f"Você ainda está pesquisando {item_ref} aqui na *{display_name}* ou decidiu deixar mais para frente?"
            )
        if segment is Segment.PSYCHOLOGY:
            return (
                f"Oi{f', {first_name}' if first_name else ''}! Passando com carinho aqui da *{display_name}* "
                "para saber se você ainda gostaria de ver um horário para este mês ou se prefere que eu te chame mais adiante."
            )
        return (
            f"Oi{f', {first_name}' if first_name else ''}, tudo joia? Sei que a rotina é corrida! "
            f"Você ainda tem interesse em avançar com a *{display_name}* agora ou prefere deixar para o próximo mês?"
        )

    # Etapa 2 — Reforço de Valor / Condição (+1 dia)
    if strategy == "value_reinforce":
        if segment is Segment.MATTRESS:
            return (
                f"{greeting} 😊 Passando para avisar que ainda consigo manter a condição em até *10x sem juros* "
                f"(ou desconto à vista no PIX) e *entrega grátis* aqui na *{display_name}*. "
                "Prefere garantir pelo WhatsApp ou dar uma passada rápida na loja para testar?"
            )
        if segment is Segment.PSYCHOLOGY:
            return (
                f"{greeting} 😊 Aqui na *{display_name}* abrimos novos horários na agenda desta semana "
                "(e emitimos recibo para reembolso no plano de saúde). Quer que eu veja o melhor dia para você?"
            )
        if segment is Segment.POOLS:
            return (
                f"{greeting} 🏊‍♂️ Nossa equipe técnica da *{display_name}* vai estar na sua região esta semana. "
                "Quer aproveitar para agendarmos a avaliação do seu espaço sem compromisso?"
            )
        return (
            f"{greeting} 😊 Passando aqui da *{display_name}* porque ainda consigo segurar aquela condição "
            "que conversamos ontem. Ficou alguma dúvida para avançarmos?"
        )

    # Etapa 1 — Retomada Quente no mesmo dia (warm_check)
    if segment is Segment.MATTRESS:
        if size:
            return (
                f"{greeting} 😊 Passando rapidinho porque ainda tenho o modelo *{size.capitalize()}* "
                f"a pronta-entrega aqui na *{display_name}* com frete grátis. "
                "Ficou alguma dúvida sobre as medidas ou prefere que eu separe ele para você?"
            )
        return (
            f"{greeting} 😊 Passando para saber se conseguiu ver as opções de colchão que te mandei "
            f"aqui da *{display_name}*. Qual medida fica melhor para o seu quarto hoje?"
        )

    if segment is Segment.PSYCHOLOGY:
        if specialty:
            return (
                f"{greeting} 😊 Passando com carinho aqui da *{display_name}* para saber se ficou alguma dúvida "
                f"sobre o atendimento de *{specialty}*. Se quiser, posso verificar os horários desta semana para você."
            )
        return (
            f"{greeting} 😊 Passando aqui da *{display_name}* para saber se posso te ajudar com alguma dúvida "
            "sobre nossos atendimentos e horários disponíveis."
        )

    if segment is Segment.POOLS:
        loc_part = f" aí em *{city}*" if city else ""
        return (
            f"{greeting} 🏊‍♂️ Passando aqui da *{display_name}* para saber se conseguiu medir o espaço do terreno{loc_part}. "
            "Quer que eu separe um horário para nossa visita técnica?"
        )

    return (
        f"{greeting} 😊 Passando aqui da *{display_name}* para saber se ficou alguma dúvida sobre o que conversamos. "
        "Consegue falar rapidinho por aqui?"
    )


class FollowUpService:
    """Serviço de varredura e disparo da Régua Progressiva de Follow-up."""

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
        """Avalia conversas ativas e envia (ou simula em `dry_run=True`) a próxima etapa da régua."""
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
            plan_spec = PLAN_SPECS.get(t_cfg.plan_tier) or PLAN_SPECS["pro"]
            if not plan_spec.get("followup_allowed", True) and not force:
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "plan_not_allowed"})
                continue

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

            steps = resolve_effective_steps(t_cfg)
            followup_count = int(lead.attributes.get("followup_count") or 0)
            if followup_count >= len(steps):
                skipped.append({"tenant_id": lead.tenant_id, "phone": mask_phone(lead.phone), "reason": "max_attempts_reached"})
                continue

            current_step = steps[followup_count]
            required_minutes = current_step.delay_minutes
            elapsed_minutes = (now - lead.last_interaction_at).total_seconds() / 60.0
            if elapsed_minutes < required_minutes and not force:
                skipped.append(
                    {
                        "tenant_id": lead.tenant_id,
                        "phone": mask_phone(lead.phone),
                        "reason": "delay_not_reached",
                        "step": followup_count + 1,
                        "strategy": current_step.strategy,
                        "elapsed_minutes": round(elapsed_minutes, 1),
                        "required_minutes": required_minutes,
                    }
                )
                continue

            text = build_followup_message(lead, t_cfg.display_name, t_cfg.segment, step=current_step)
            candidate_info = {
                "tenant_id": lead.tenant_id,
                "phone": lead.phone,
                "phone_masked": mask_phone(lead.phone),
                "name": lead.name,
                "attempt": followup_count + 1,
                "total_steps": len(steps),
                "step_label": current_step.label,
                "strategy": current_step.strategy,
                "required_minutes": required_minutes,
                "elapsed_minutes": round(elapsed_minutes, 1),
                "message": text,
            }
            eligible.append(candidate_info)

            if dry_run:
                continue

            try:
                provider = t_cfg.whatsapp_provider
                raw_provider = lead.attributes.get("_wa_provider")
                if raw_provider:
                    with contextlib.suppress(ValueError):
                        provider = Provider(raw_provider)
                gateway = self._gateway_resolver(provider)
                session_id = (
                    lead.attributes.get("_wa_session") or t_cfg.whatsapp_session_id or gateway.default_session
                )
                outgoing = OutgoingMessage(
                    tenant_id=lead.tenant_id,
                    provider=provider,
                    session_id=session_id,
                    contact_phone=lead.phone,
                    chat_id=lead.attributes.get("_wa_chat_id") or None,
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
                lead.attributes["last_followup_strategy"] = current_step.strategy
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


class FollowUpScheduler:
    """Executa a varredura da régua automaticamente em segundo plano (antes só rodava pelo painel).

    - Intervalo configurável (`FOLLOWUP_SWEEP_INTERVAL_SECONDS`, padrão 60s): um toque de "1 min" sai entre
      1 e 2 minutos após o silêncio do lead.
    - Trava distribuída no Redis (`SET NX EX`): com 2+ réplicas/workers, só uma varre por ciclo (sem duplicar envio).
    - Falhas nunca derrubam o loop; o status fica disponível para o painel (`status()`).
    """

    LOCK_KEY = "lobo:followup:sweep_lock"

    def __init__(
        self,
        service: FollowUpService,
        *,
        redis: Any | None = None,
        interval_seconds: float = 60.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._service = service
        self._redis = redis
        self.interval_seconds = max(10.0, float(interval_seconds))
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.total_sent = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="followup-scheduler")
            logger.info("Régua de follow-up automática ativa (varredura a cada %.0fs)", self.interval_seconds)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _acquire_lock(self) -> bool:
        if self._redis is None:
            return True
        try:
            ttl = max(5, int(self.interval_seconds) - 2)
            return bool(await self._redis.set(self.LOCK_KEY, "1", nx=True, ex=ttl))
        except Exception:  # noqa: BLE001 - sem Redis, segue (instância única)
            return True

    async def run_once(self) -> dict[str, Any] | None:
        if not await self._acquire_lock():
            return None
        try:
            result = await self._service.run_sweep()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            logger.exception("Falha na varredura automática de follow-up")
            return None
        self.last_run_at = self._clock()
        self.last_result = {k: result.get(k) for k in ("evaluated", "eligible_count", "sent_count")}
        self.last_error = None
        self.total_sent += int(result.get("sent_count") or 0)
        if result.get("sent_count"):
            logger.info("Follow-up automático: %s mensagem(ns) enviada(s)", result["sent_count"])
        return result

    async def _loop(self) -> None:
        await asyncio.sleep(5)  # deixa o app terminar de subir
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_seconds)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "total_sent": self.total_sent,
        }


def reset_followup_cadence(lead: LeadProfile) -> bool:
    """Lead respondeu: a régua recomeça do 1º toque na próxima vez que ele silenciar."""
    changed = False
    for key in ("followup_count", "last_followup_at", "last_followup_strategy"):
        if lead.attributes.pop(key, None) is not None:
            changed = True
    return changed


__all__ = [
    "FollowUpScheduler",
    "FollowUpService",
    "build_followup_message",
    "reset_followup_cadence",
    "resolve_effective_steps",
]
