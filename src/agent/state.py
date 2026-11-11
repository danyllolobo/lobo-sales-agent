"""Estado tipado do Grafo Agêntico (LangGraph) e resultado estruturado de turno.

Define:
- `ConversationStage`: estágios do funil conversacional (ARCHITECTURE.md §4).
- `AgentIntent`: intenções comerciais e de segurança detectadas na mensagem.
- `AgentState`: `TypedDict` consumido pelo `StateGraph` do LangGraph, com
  reducers aditivos para `messages` (`add_messages`) e `executed_tools`.
- `TurnResult`: envelope imutável retornado pelo orquestrador ao final de um turno,
  com auditoria completa de ferramentas, decisão do JEV e `OutgoingMessage`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, ClassVar, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import ConfigDict, Field

from src.core.schemas import (
     LeadProfile,
    MessageBatch,
    OutgoingMessage,
    TenantId,
    _PhoneMaskedModel,
    mask_phone,
)
from src.tools.results import ToolResult


class ConversationStage(StrEnum):
    """Estágios do grafo de estados (ARCHITECTURE.md §4)."""

    TRIAGE = "triage"
    DISCOVERY = "discovery"
    OFFER_CATALOG = "offer_catalog"
    BOOKING = "booking"
    HANDOFF = "handoff"
    DISPATCH = "dispatch"
    SILENCED = "silenced"


class AgentIntent(StrEnum):
    """Intenção principal identificada no turno do lead."""

    GREETING = "greeting"
    DISCOVERY = "discovery"
    CATALOG_PRICE = "catalog_price"
    DELIVERY = "delivery"
    BUSINESS_INFO = "business_info"
    BOOKING = "booking"
    DISCOUNT_NEGOTIATION = "discount_negotiation"
    HUMAN_REQUEST = "human_request"
    CLINICAL_EMERGENCY = "clinical_emergency"
    GENERAL = "general"


def add_tool_results(
    existing: list[ToolResult] | None,
    new: list[ToolResult] | None,
) -> list[ToolResult]:
    """Reducer aditivo para acumular `ToolResult` executados ao longo do turno."""
    base = list(existing or [])
    if new:
        base.extend(new)
    return base


class AgentState(TypedDict, total=False):
    """Estado compartilhado entre os nós do LangGraph em cada turno."""

    turn_id: str
    tenant_id: str
    session_id: str
    batch: MessageBatch | None
    lead: LeadProfile

    # Histórico de mensagens da conversa (System/Human/AI/Tool)
    messages: Annotated[list[BaseMessage], add_messages]

    # Roteamento e estágio atual
    intent: AgentIntent | str
    current_stage: ConversationStage | str
    jev_decision: dict[str, Any] | None

    # Auditoria determinística de ferramentas acionadas no turno
    executed_tools: Annotated[list[ToolResult], add_tool_results]
    tool_iterations: int

    # Controle de Handoff Humano e Silêncio da IA
    human_handoff_requested: bool
    handoff_reason: str | None
    handoff_summary: str | None
    handoff_suggested_action: str | None
    handoff_completed: bool

    silenced: bool
    silence_reason: str | None

    # Saída do turno
    reply_text: str | None
    outgoing_message: OutgoingMessage | None


class TurnResult(_PhoneMaskedModel):
    """Resultado consolidado da execução de um turno pelo motor agêntico."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    _phone_fields: ClassVar[tuple[str, ...]] = ("contact_phone",)

    turn_id: str
    tenant_id: TenantId
    session_id: str
    contact_phone: str

    silenced: bool = False
    silence_reason: str | None = None

    intent: str = AgentIntent.GENERAL.value
    stage: str = ConversationStage.DISPATCH.value
    human_handoff_requested: bool = False
    handoff_reason: str | None = None

    executed_tools: list[ToolResult] = Field(default_factory=list)
    # Chamadas de ferramenta emitidas pelo LLM (nome + argumentos) — usado no Raio-X do painel
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    jev_decision: dict[str, Any] | None = None
    reply_text: str | None = None
    outgoing_message: OutgoingMessage | None = None
    lead: LeadProfile
    latency_ms: float = 0.0

    def log_context(self) -> dict[str, Any]:
        """Resumo seguro para logs (sem PII crua nem conteúdo de mensagem)."""
        return {
            "turn_id": self.turn_id,
            "tenant_id": self.tenant_id,
            "contact": mask_phone(self.contact_phone),
            "silenced": self.silenced,
            "silence_reason": self.silence_reason,
            "intent": self.intent,
            "stage": self.stage,
            "human_handoff": self.human_handoff_requested,
            "handoff_reason": self.handoff_reason,
            "tools": [t.tool for t in self.executed_tools],
            "latency_ms": round(self.latency_ms, 2),
        }


__all__ = [
    "AgentIntent",
    "AgentState",
    "ConversationStage",
    "TurnResult",
    "add_tool_results",
]

