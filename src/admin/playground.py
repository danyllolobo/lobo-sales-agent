"""Playground do painel: simula turnos completos do agente sem tocar o mundo real.

Isolamento em relação ao atendimento real:
- memória/histórico em `lobo:pg:conv:*` e pausa em `lobo:pg:pause:*` (prefixos próprios);
- agenda em memória própria (simulações não ocupam horários reais);
- handoff SEM CRM (nenhuma nota/tag no Chatwoot/GHL) e provider `simulator` (nada vai ao WhatsApp).

Os dados de catálogo/preço/entrega são os MESMOS do tenant (o objetivo é validá-los).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Literal

import redis.asyncio as aioredis
from langchain_core.language_models.chat_models import BaseChatModel

from src.agent.graph import LeadConversationStore, SalesAgentRunner
from src.agent.nodes import DeterministicSalesChatModel, create_gemini_chat_model
from src.agent.state import TurnResult
from src.config import Settings
from src.core.schemas import IncomingMessage, MessageBatch, MessageType, Provider, normalize_phone, utcnow
from src.tools.calendar import CalendarService
from src.tools.catalog import CatalogService
from src.tools.geo import GeoLookup
from src.tools.handoff import AIPauseRegistry, HandoffService
from src.tools.tenant_data import TenantDataStore

Engine = Literal["deterministic", "gemini"]


class PlaygroundUnavailable(Exception):
    """Motor solicitado não está disponível (ex.: Gemini sem GOOGLE_API_KEY)."""


class Playground:
    SESSION_ID = "playground"

    def __init__(
        self,
        *,
        store: TenantDataStore,
        settings: Settings,
        redis: aioredis.Redis | None,
        injected_llm: BaseChatModel | None = None,
        geo: GeoLookup | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._injected_llm = injected_llm
        self.pause = AIPauseRegistry(redis, prefix="lobo:pg:pause") if redis is not None else None
        self.conversations = LeadConversationStore(redis, prefix="lobo:pg:conv", ttl_seconds=24 * 3600)
        self._catalog = CatalogService(store, geo=geo)
        self._calendar = CalendarService(store)
        self._handoff = HandoffService(store, self.pause, lambda _t: None, pause_hours=settings.ai_pause_hours)
        self._runners: dict[str, SalesAgentRunner] = {}

    @property
    def gemini_available(self) -> bool:
        return self._injected_llm is not None or self._settings.gemini_enabled

    def _llm_for(self, engine: Engine) -> BaseChatModel:
        if engine == "deterministic":
            return DeterministicSalesChatModel()
        if self._injected_llm is not None:
            return self._injected_llm
        if not self._settings.gemini_enabled:
            raise PlaygroundUnavailable("GOOGLE_API_KEY não configurada — use o Motor Determinístico.")
        return create_gemini_chat_model(self._settings)

    def runner(self, engine: Engine) -> SalesAgentRunner:
        if engine not in self._runners:
            self._runners[engine] = SalesAgentRunner(
                store=self._store,
                catalog=self._catalog,
                calendar=self._calendar,
                handoff=self._handoff,
                pause_registry=self.pause,
                conversation_store=self.conversations,
                llm=self._llm_for(engine),
                settings=self._settings,
            )
        return self._runners[engine]

    async def simulate(
        self,
        *,
        tenant_id: str,
        engine: Engine,
        phone: str,
        name: str | None,
        messages: list[str],
    ) -> dict[str, Any]:
        self._store.get(tenant_id)  # valida tenant (TenantNotFoundError → 404)
        runner = self.runner(engine)
        phone = normalize_phone(phone)
        start = utcnow()
        incoming = [
            IncomingMessage(
                message_id=f"pg_{uuid.uuid4().hex[:12]}",
                tenant_id=tenant_id,
                provider=Provider.SIMULATOR,
                session_id=self.SESSION_ID,
                contact_phone=phone,
                contact_name=name or None,
                type=MessageType.TEXT,
                text=text,
                timestamp=start + timedelta(milliseconds=i),
            )
            for i, text in enumerate(messages)
        ]
        batch = MessageBatch(
            tenant_id=tenant_id,
            session_id=self.SESSION_ID,
            contact_phone=phone,
            messages=incoming,
            first_received_at=start,
        )
        turn = await runner.handle_batch(batch)
        return turn_payload(turn, engine=engine, batch_size=len(incoming))

    async def reset(self, tenant_id: str, phone: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        cleared = await self.conversations.delete(tenant_id, phone)
        if self.pause is not None:
            await self.pause.resume(tenant_id, phone)
        return {"tenant_id": tenant_id, "phone": phone, "memory_cleared": cleared}


def turn_payload(turn: TurnResult, *, engine: str, batch_size: int) -> dict[str, Any]:
    """Raio-X do turno: status, estágio, intenção, ferramentas (args + retorno), lead."""
    if turn.silenced:
        status = "silenced"
    elif turn.human_handoff_requested:
        status = "handoff"
    else:
        status = "success"

    pending_calls = list(turn.tool_calls)
    tools: list[dict[str, Any]] = []
    for res in turn.executed_tools:
        idx = next((i for i, c in enumerate(pending_calls) if c.get("name") == res.tool), None)
        args = pending_calls.pop(idx).get("args") if idx is not None else None
        tools.append(
            {
                "tool": res.tool,
                "status": res.status.value,
                "ok": res.ok,
                "requires_human": res.requires_human,
                "reason": res.reason,
                "args": args,
                "result": res.to_llm_payload(),
            }
        )

    lead = turn.lead
    return {
        "status": status,
        "engine": engine,
        "batch_size": batch_size,
        "turn_id": turn.turn_id,
        "latency_ms": round(turn.latency_ms, 1),
        "stage": turn.stage,
        "intent": turn.intent,
        "jev_decision": turn.jev_decision,
        "silenced": turn.silenced,
        "silence_reason": turn.silence_reason,
        "handoff_reason": turn.handoff_reason,
        "reply_text": turn.reply_text,
        "tools": tools,
        "lead": {
            "name": lead.name,
            "phone": lead.phone,
            "status": lead.status.value,
            "tags": lead.tags,
            "attributes": lead.attributes,
            "ai_paused": lead.ai_paused,
        },
    }


__all__ = ["Engine", "Playground", "PlaygroundUnavailable", "turn_payload"]

