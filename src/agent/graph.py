"""Montagem e compilação do Grafo LangGraph + Orquestrador de Turno (`SalesAgentRunner`).

Fluxo completo do grafo (`ARCHITECTURE.md` §1 e §4):
    START
      │
      ▼
  node_triage ──(silenced=True)──────────────────────────────────► END (silêncio)
      │
      ├──(human_handoff_requested=True)──────────┐
      ├──(stage=discovery)─────► node_discovery ─┤
      ├──(stage=offer_catalog)─► node_offer_catalog ─┼─(handoff?)─► node_handoff
      └──(stage=booking)───────► node_booking_conversion ┘              │
                                         │ (ok)                         │
                                         ▼                              ▼
                                    node_dispatch ◄─────────────────────┘
                                         │
                                         ▼
                                        END
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    messages_from_dict,
    messages_to_dict,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from redis.exceptions import RedisError

from src.agent.jev_guard import JEVGuard
from src.agent.nodes import AgentNodes, create_gemini_chat_model
from src.agent.state import AgentIntent, AgentState, ConversationStage, TurnResult
from src.agent.transcription import AudioTranscriber
from src.buffer.message_queue import MessageBuffer
from src.config import Settings, get_settings
from src.core.schemas import (
    LeadProfile,
    MessageBatch,
    MessageType,
    OutgoingMessage,
    Provider,
    mask_phone,
    normalize_phone,
    split_human_chunks,
    utcnow,
)
from src.crm.base import CRMConnector
from src.gateways.base import WhatsAppGateway
from src.tools.calendar import CalendarService
from src.tools.catalog import CatalogService
from src.tools.handoff import (
    CUSTOMER_MESSAGES,
    AIPauseRegistry,
    HandoffReason,
    HandoffService,
)
from src.tools.results import ToolResult, normalize_text
from src.tools.tenant_data import TenantData, TenantDataStore

logger = logging.getLogger("lobo.agent.graph")

MAX_STORED_MESSAGES = 40


# --------------------------------------------------------------------------- #
# Roteadores condicionais do StateGraph
# --------------------------------------------------------------------------- #
def route_after_triage(state: AgentState) -> str:
    """Decide o destino após `node_triage`."""
    if state.get("silenced"):
        return END
    if state.get("human_handoff_requested"):
        return "node_handoff"
    stage = state.get("current_stage")
    if stage == ConversationStage.OFFER_CATALOG:
        return "node_offer_catalog"
    if stage == ConversationStage.BOOKING:
        return "node_booking_conversion"
    return "node_discovery"


def route_after_commercial(state: AgentState) -> str:
    """Decide se vai para `node_handoff` ou direto para `node_dispatch`."""
    if state.get("human_handoff_requested"):
        return "node_handoff"
    return "node_dispatch"


def build_sales_graph(nodes: AgentNodes) -> CompiledStateGraph:
    """Constrói e compila o `StateGraph` determinístico do Lobo AI Sales Agent."""
    workflow = StateGraph(AgentState)

    workflow.add_node("node_triage", nodes.node_triage)
    workflow.add_node("node_discovery", nodes.node_discovery)
    workflow.add_node("node_offer_catalog", nodes.node_offer_catalog)
    workflow.add_node("node_booking_conversion", nodes.node_booking_conversion)
    workflow.add_node("node_handoff", nodes.node_handoff)
    workflow.add_node("node_dispatch", nodes.node_dispatch)

    workflow.add_edge(START, "node_triage")
    workflow.add_conditional_edges(
        "node_triage",
        route_after_triage,
        {
            "node_discovery": "node_discovery",
            "node_offer_catalog": "node_offer_catalog",
            "node_booking_conversion": "node_booking_conversion",
            "node_handoff": "node_handoff",
            END: END,
        },
    )

    for stage_node in ("node_discovery", "node_offer_catalog", "node_booking_conversion"):
        workflow.add_conditional_edges(
            stage_node,
            route_after_commercial,
            {
                "node_handoff": "node_handoff",
                "node_dispatch": "node_dispatch",
            },
        )

    workflow.add_edge("node_handoff", "node_dispatch")
    workflow.add_edge("node_dispatch", END)

    return workflow.compile()


# --------------------------------------------------------------------------- #
# Persistência de contexto de conversa por tenant + telefone (Redis + Memória)
# --------------------------------------------------------------------------- #
class LeadConversationStore:
    """Guarda `LeadProfile` e histórico recente de mensagens isolados por `(tenant_id, phone)`."""

    def __init__(
        self,
        redis: aioredis.Redis | None = None,
        *,
        prefix: str = "lobo:conv",
        ttl_seconds: int = 7 * 24 * 3600,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._memory: dict[str, str] = {}

    def _key(self, tenant_id: str, phone: str) -> str:
        return f"{self._prefix}:{tenant_id}:{normalize_phone(phone)}"

    async def load(self, tenant_id: str, phone: str) -> tuple[LeadProfile, list[BaseMessage]]:
        norm_phone = normalize_phone(phone)
        key = self._key(tenant_id, norm_phone)
        raw: str | bytes | None = None
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
            except (RedisError, OSError):
                raw = self._memory.get(key)
        else:
            raw = self._memory.get(key)

        if not raw:
            return LeadProfile(tenant_id=tenant_id, phone=norm_phone), []

        try:
            payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            lead = LeadProfile.model_validate(payload["lead"])
            if lead.tenant_id != tenant_id:
                return LeadProfile(tenant_id=tenant_id, phone=norm_phone), []
            msgs = messages_from_dict(payload.get("messages") or [])
            return lead, msgs
        except Exception:  # noqa: BLE001
            return LeadProfile(tenant_id=tenant_id, phone=norm_phone), []

    async def save(self, lead: LeadProfile, messages: list[BaseMessage]) -> None:
        key = self._key(lead.tenant_id, lead.phone)
        # Mantém apenas HumanMessage e AIMessage de texto final para histórico enxuto
        compact_msgs = [
            m
            for m in messages
            if isinstance(m, HumanMessage)
            or (isinstance(m, AIMessage) and not getattr(m, "tool_calls", None) and m.content)
        ][-MAX_STORED_MESSAGES:]
        serialized = json.dumps(
            {
                "lead": lead.model_dump(mode="json"),
                "messages": messages_to_dict(compact_msgs),
            },
            ensure_ascii=False,
        )
        self._memory[key] = serialized
        if self._redis is not None:
            try:
                await self._redis.set(key, serialized, ex=self._ttl)
            except (RedisError, OSError):
                logger.warning("Não foi possível persistir sessão no Redis; mantida em memória %s", lead.log_context())

    async def update_lead(self, lead: LeadProfile) -> None:
        """Regrava apenas o perfil do lead, preservando o histórico."""
        _, history = await self.load(lead.tenant_id, lead.phone)
        await self.save(lead, history)

    async def delete(self, tenant_id: str, phone: str) -> bool:
        """Apaga perfil + histórico do contato (reset de conversa)."""
        key = self._key(tenant_id, phone)
        existed = self._memory.pop(key, None) is not None
        if self._redis is not None:
            try:
                existed = bool(await self._redis.delete(key)) or existed
            except (RedisError, OSError):
                logger.warning("Não foi possível apagar a conversa no Redis")
        return existed

    async def list_conversations(self, *, tenant_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Lista conversas (lead + resumo do histórico), mais recentes primeiro."""
        keys: set[str] = set(self._memory)
        if self._redis is not None:
            try:
                async for raw_key in self._redis.scan_iter(match=f"{self._prefix}:*", count=200):
                    keys.add(raw_key.decode() if isinstance(raw_key, bytes) else raw_key)
                    if len(keys) >= limit * 2:
                        break
            except (RedisError, OSError):
                logger.warning("Redis indisponível ao listar conversas; usando memória local")

        items: list[dict[str, Any]] = []
        prefix_len = len(self._prefix) + 1
        for key in keys:
            rest = key[prefix_len:]
            if ":" not in rest:
                continue
            key_tenant, phone = rest.rsplit(":", 1)
            if tenant_id and key_tenant != tenant_id:
                continue
            try:
                lead, history = await self.load(key_tenant, phone)
            except (ValueError, TypeError):
                continue
            last = history[-1] if history else None
            items.append(
                {
                    "lead": lead,
                    "message_count": len(history),
                    "last_message": _message_text(last)[:160] if last is not None else None,
                    "last_role": ("lead" if isinstance(last, HumanMessage) else "ia") if last is not None else None,
                }
            )
        items.sort(key=lambda i: i["lead"].last_interaction_at, reverse=True)
        return items[:limit]


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def _extract_turn_tool_calls(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Chamadas de ferramenta feitas pelo LLM DEPOIS da última mensagem do lead (turno atual)."""
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    calls: list[dict[str, Any]] = []
    for msg in messages[last_human + 1 :]:
        if isinstance(msg, AIMessage):
            for tc in getattr(msg, "tool_calls", None) or []:
                calls.append({"name": tc.get("name"), "args": tc.get("args") or {}, "id": tc.get("id")})
    return calls


_GRATITUDE_REACTION_TOKENS: tuple[str, ...] = (
    "obrigado",
    "obrigada",
    "muito obrigado",
    "muito obrigada",
    "valeu",
    "agradeco",
    "grato",
    "grata",
)
_PRAISE_REACTION_TOKENS: tuple[str, ...] = (
    "show",
    "perfeito",
    "maravilha",
    "excelente",
    "amei",
    "top demais",
    "sensacional",
)
_CONFIRMATION_REACTION_TOKENS: tuple[str, ...] = (
    "combinado",
    "fechado",
    "confirmo",
    "confirmado",
    "pode marcar",
    "pode agendar",
)


def pick_reaction_emoji(
    user_text: str,
    *,
    executed_tools: list[ToolResult] | None = None,
) -> str | None:
    """Seleciona um emoji de reação com parcimônia (apenas em confirmações, elogios ou agradecimentos).

    Nunca reage a todas as mensagens: perguntas normais de preço, frete ou catálogo retornam `None`.
    """
    if executed_tools and any(r.ok and r.tool == "create_booking" for r in executed_tools):
        return "👍"

    norm = f" {normalize_text(user_text or '')} "
    if not norm.strip():
        return None

    if any(f" {token} " in norm for token in _GRATITUDE_REACTION_TOKENS):
        return "🙏"
    if any(f" {token} " in norm for token in _PRAISE_REACTION_TOKENS):
        return "❤️"
    if any(f" {token} " in norm for token in _CONFIRMATION_REACTION_TOKENS):
        return "👍"
    return None


# --------------------------------------------------------------------------- #
# Orquestrador de Turno (conecta Buffer -> Transcrição -> LangGraph -> Gateway)
# --------------------------------------------------------------------------- #
GatewayResolver = Callable[[Provider], WhatsAppGateway | None]
CRMResolver = Callable[[TenantData], CRMConnector | None]


class SalesAgentRunner:
    """Executa um turno completo a partir de um `MessageBatch` (ou chamada direta)."""

    def __init__(
        self,
        *,
        store: TenantDataStore,
        catalog: CatalogService,
        calendar: CalendarService,
        handoff: HandoffService,
        pause_registry: AIPauseRegistry | None = None,
        conversation_store: LeadConversationStore | None = None,
        transcriber: AudioTranscriber | None = None,
        gateway_resolver: GatewayResolver | None = None,
        crm_resolver: CRMResolver | None = None,
        buffer: MessageBuffer | None = None,
        llm: BaseChatModel | None = None,
        jev_guard: JEVGuard | None = None,
        settings: Settings | None = None,
        check_crm_human_active: bool = False,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings or get_settings()
        self._store = store
        self._catalog = catalog
        self._calendar = calendar
        self._handoff = handoff
        self._pause = pause_registry
        self._conv_store = conversation_store or LeadConversationStore()
        self._transcriber = transcriber or AudioTranscriber(self._settings)
        self._gateway_resolver = gateway_resolver or (lambda _p: None)
        self._crm_resolver = crm_resolver or (lambda _t: None)
        self._buffer = buffer
        self._clock = clock

        effective_llm = llm or create_gemini_chat_model(self._settings)
        self.nodes = AgentNodes(
            store=store,
            catalog=catalog,
            calendar=calendar,
            handoff=handoff,
            pause_registry=pause_registry,
            crm_resolver=self._crm_resolver,
            llm=effective_llm,
            jev_guard=jev_guard,
            check_crm_human_active=check_crm_human_active,
            clock=clock,
        )
        self.graph = build_sales_graph(self.nodes)

    async def handle_batch(self, batch: MessageBatch, *, acquire_lock: bool = False) -> TurnResult:
        """Processa um lote agrupado pelo debounce de ponta a ponta (FALLBACK SEGURO).

        Nota: quando chamado via `MessageBuffer.on_flush`, o worker do buffer já detém
        a trava distribuída do contato (`_held_lock`), portanto `acquire_lock=False`.
        """
        if acquire_lock and self._buffer is not None:
            async with self._buffer.processing_lock(
                batch.tenant_id, batch.session_id, batch.contact_phone
            ):
                return await self._process_batch_unlocked(batch)
        return await self._process_batch_unlocked(batch)

    async def _process_batch_unlocked(self, batch: MessageBatch) -> TurnResult:
        t0 = time.perf_counter()
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        lead, history = await self._conv_store.load(batch.tenant_id, batch.contact_phone)
        gateway = self._safe_resolve_gateway(batch.provider)

        try:
            # Se o contato ainda não estiver pausado, confirma leitura (blue ticks),
            # aguarda breve pausa de leitura humana (500ms-1000ms), envia "digitando..." e transcreve áudios
            is_paused = lead.ai_paused or (
                self._pause is not None and await self._pause.is_paused(batch.tenant_id, batch.contact_phone)
            )
            if not is_paused:
                if gateway is not None:
                    await self._safe_mark_seen_and_typing(gateway, batch)
                if batch.needs_transcription:
                    batch = await self._transcriber.transcribe_batch(batch, gateway=gateway)

            user_message = HumanMessage(content=batch.combined_text)
            initial_state: AgentState = {
                "turn_id": turn_id,
                "tenant_id": batch.tenant_id,
                "session_id": batch.session_id,
                "batch": batch,
                "lead": lead,
                "messages": [*history, user_message],
                "executed_tools": [],
                "human_handoff_requested": False,
                "handoff_completed": False,
                "silenced": False,
            }

            final_state = await self.graph.ainvoke(initial_state)
            updated_lead: LeadProfile = final_state.get("lead", lead)
            updated_messages: list[BaseMessage] = final_state.get("messages") or [*history, user_message]
            await self._conv_store.save(updated_lead, updated_messages)

            outgoing: OutgoingMessage | None = final_state.get("outgoing_message")
            executed_tools: list[ToolResult] = list(final_state.get("executed_tools") or [])
            if outgoing is not None and not final_state.get("silenced"):
                reaction_emoji = pick_reaction_emoji(batch.combined_text, executed_tools=executed_tools)
                if reaction_emoji and gateway is not None:
                    await self._safe_react(gateway, batch, reaction_emoji)
                await self._dispatch_outgoing(gateway, outgoing)

            latency_ms = (time.perf_counter() - t0) * 1000
            result = TurnResult(
                turn_id=turn_id,
                tenant_id=batch.tenant_id,
                session_id=batch.session_id,
                contact_phone=batch.contact_phone,
                silenced=bool(final_state.get("silenced")),
                silence_reason=final_state.get("silence_reason"),
                intent=str(final_state.get("intent") or AgentIntent.GENERAL.value),
                stage=str(final_state.get("current_stage") or ConversationStage.DISPATCH.value),
                human_handoff_requested=bool(final_state.get("human_handoff_requested")),
                handoff_reason=final_state.get("handoff_reason"),
                executed_tools=list(final_state.get("executed_tools") or []),
                tool_calls=_extract_turn_tool_calls(updated_messages),
                jev_decision=final_state.get("jev_decision"),
                reply_text=final_state.get("reply_text"),
                outgoing_message=outgoing,
                lead=updated_lead,
                latency_ms=latency_ms,
            )
            logger.info("Turno concluído %s", result.log_context())
            return result

        except Exception as exc:  # noqa: BLE001 - REGRA INVIOLÁVEL 4 (FALLBACK SEGURO)
            logger.exception(
                "Erro inesperado ao processar turno tenant=%s contact=%s: %s",
                batch.tenant_id,
                mask_phone(batch.contact_phone),
                type(exc).__name__,
            )
            handoff_res = await self._handoff.trigger_human_handoff(
                lead,
                reason=HandoffReason.TECHNICAL_FAILURE,
                conversation_summary=f"Exceção inesperada no orquestrador ({type(exc).__name__}).",
                suggested_action="Assumir atendimento humano.",
            )
            fallback_text = (handoff_res.data or {}).get(
                "customer_message",
                CUSTOMER_MESSAGES[HandoffReason.TECHNICAL_FAILURE],
            )
            outgoing = OutgoingMessage.text_reply(batch, fallback_text, turn_id=turn_id)
            await self._conv_store.save(lead, history)
            await self._dispatch_outgoing(gateway, outgoing)
            latency_ms = (time.perf_counter() - t0) * 1000
            return TurnResult(
                turn_id=turn_id,
                tenant_id=batch.tenant_id,
                session_id=batch.session_id,
                contact_phone=batch.contact_phone,
                silenced=False,
                intent=AgentIntent.GENERAL.value,
                stage=ConversationStage.HANDOFF.value,
                human_handoff_requested=True,
                handoff_reason=HandoffReason.TECHNICAL_FAILURE.value,
                executed_tools=[handoff_res],
                reply_text=fallback_text,
                outgoing_message=outgoing,
                lead=lead,
                latency_ms=latency_ms,
            )

    def _safe_resolve_gateway(self, provider: Provider) -> WhatsAppGateway | None:
        if provider is Provider.SIMULATOR:
            return None
        try:
            return self._gateway_resolver(provider)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Gateway %s indisponível para despacho direto: %s", provider.value, type(exc).__name__)
            return None

    @staticmethod
    def _reading_delay_seconds(batch: MessageBatch) -> float:
        """Delay de leitura humana proporcional ao tamanho das mensagens (500ms a 1000ms)."""
        chars = len(batch.combined_text or "")
        return round(min(max(0.5 + chars * 0.003, 0.5), 1.0), 3)

    async def _safe_mark_seen(self, gateway: WhatsAppGateway, batch: MessageBatch) -> None:
        try:
            target = batch.last_message.chat_id or batch.contact_phone
            await gateway.mark_seen(batch.session_id, target, batch.message_ids)
        except Exception:  # noqa: BLE001
            logger.debug("Falha não-crítica ao enviar mark_seen para %s", mask_phone(batch.contact_phone))

    async def _safe_set_typing(self, gateway: WhatsAppGateway, batch: MessageBatch) -> None:
        try:
            target = batch.last_message.chat_id or batch.contact_phone
            await gateway.set_typing(batch.session_id, target, True)
        except Exception:  # noqa: BLE001
            logger.debug("Falha não-crítica ao enviar status composing para %s", mask_phone(batch.contact_phone))

    async def _safe_react(self, gateway: WhatsAppGateway, batch: MessageBatch, emoji: str) -> None:
        try:
            target = batch.last_message.chat_id or batch.contact_phone
            msg_id = batch.last_message.message_id
            if msg_id:
                await gateway.react(batch.session_id, target, msg_id, emoji)
        except Exception:  # noqa: BLE001
            logger.debug("Falha não-crítica ao enviar reação (%s) para %s", emoji, mask_phone(batch.contact_phone))

    async def _safe_mark_seen_and_typing(self, gateway: WhatsAppGateway, batch: MessageBatch) -> None:
        """Fluxo humanizado pré-turno: 1) Blue ticks (mark_seen) -> 2) Pausa 500-1000ms -> 3) Digitando..."""
        await self._safe_mark_seen(gateway, batch)
        delay = self._reading_delay_seconds(batch)
        if delay > 0:
            await asyncio.sleep(delay)
        await self._safe_set_typing(gateway, batch)

    def _should_split_messages(self, tenant_id: str) -> bool:
        try:
            return bool(self._store.get(tenant_id).tenant.split_messages)
        except Exception:  # noqa: BLE001
            return bool(self._settings.split_messages)

    @staticmethod
    def _chunk_typing_seconds(chunk_text: str) -> float:
        """Duração de digitação proporcional entre partes (1.5s a 2.5s)."""
        return round(min(max(len(chunk_text) * 0.012, 1.5), 2.5), 2)

    async def _dispatch_outgoing(
        self,
        gateway: WhatsAppGateway | None,
        outgoing: OutgoingMessage,
    ) -> None:
        if self._pause is not None and outgoing.text:
            await self._pause.register_bot_outbound(
                outgoing.tenant_id,
                outgoing.contact_phone,
                text=outgoing.text,
            )

        if outgoing.type is MessageType.TEXT and outgoing.text and self._should_split_messages(outgoing.tenant_id):
            chunks = split_human_chunks(outgoing.text)
        else:
            chunks = [outgoing.text] if outgoing.text else []

        if self._pause is not None and len(chunks) > 1:
            for chunk_text in chunks:
                await self._pause.register_bot_outbound(
                    outgoing.tenant_id,
                    outgoing.contact_phone,
                    text=chunk_text,
                )

        if gateway is None:
            return

        if len(chunks) <= 1:
            try:
                send_res = await gateway.deliver(outgoing)
                if self._pause is not None and send_res.message_id:
                    await self._pause.register_bot_outbound(
                        outgoing.tenant_id,
                        outgoing.contact_phone,
                        message_id=send_res.message_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Falha ao enviar OutgoingMessage no gateway (%s) %s",
                    type(exc).__name__,
                    outgoing.log_context(),
                )
            return

        for idx, chunk_text in enumerate(chunks):
            typing_secs: float | None
            if not outgoing.simulate_typing:
                typing_secs = 0.0
            elif outgoing.typing_seconds is not None:
                typing_secs = outgoing.typing_seconds
            elif idx == 0:
                typing_secs = min(outgoing.effective_typing_seconds, 1.5)
            else:
                typing_secs = self._chunk_typing_seconds(chunk_text)

            part_msg = outgoing.model_copy(
                update={
                    "text": chunk_text,
                    "reply_to_message_id": outgoing.reply_to_message_id if idx == 0 else None,
                    "typing_seconds": typing_secs,
                }
            )
            try:
                send_res = await gateway.deliver(part_msg)
                if self._pause is not None and send_res.message_id:
                    await self._pause.register_bot_outbound(
                        outgoing.tenant_id,
                        outgoing.contact_phone,
                        message_id=send_res.message_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Falha ao enviar parte %d/%d de OutgoingMessage no gateway (%s) %s",
                    idx + 1,
                    len(chunks),
                    type(exc).__name__,
                    part_msg.log_context(),
                )
                break


__all__ = [
    "LeadConversationStore",
    "SalesAgentRunner",
    "build_sales_graph",
    "pick_reaction_emoji",
    "route_after_commercial",
    "route_after_triage",
    "split_human_chunks",
]
