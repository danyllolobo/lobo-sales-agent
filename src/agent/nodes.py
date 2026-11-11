"""Nós do Grafo LangGraph, Binding Seguro de Ferramentas e Modelo Determinístico de Fallback.

Arquitetura dos nós (`ARCHITECTURE.md` §4 e `MASTER_KICKOFF_PROMPT.md` ETAPA 4):
1. `node_triage`: Validação de Handoff (silencia se humano ativo no estado/Redis/CRM),
   enriquecimento do `LeadProfile` e triagem rápida via `JEVGuard`.
2. `node_discovery`: Qualificação consultiva e mapeamento da dor por segmento.
3. `node_offer_catalog`: Raciocínio comercial com chamada às ferramentas determinísticas
   de catálogo, preço em tempo real, entrega e informações do negócio.
4. `node_booking_conversion`: Verificação de disponibilidade (2 opções concretas) e
   criação atômica de agendamento.
5. `node_handoff`: Transferência humana com nota privada no CRM, tag `atendimento_humano`,
   pausa imediata da IA no Redis e mensagem cordial (ou protocolo CVV 188 em emergências).
6. `node_dispatch`: Construção do `OutgoingMessage` pronto para envio pelo gateway.

Segurança Multi-tenant:
- As ferramentas expostas ao LLM via `build_tenant_tools` NÃO aceitam `tenant_id` nem
  `lead_phone` como parâmetros livres: ambos são injetados por closure a partir do
  contexto autenticado do turno.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from src.agent.jev_guard import (
    JEVGuard,
    extract_lead_attributes,
    verify_no_unbacked_prices,
)
from src.agent.prompts import build_system_prompt
from src.agent.state import AgentState, ConversationStage
from src.config import Settings
from src.core.exceptions import LoboError
from src.core.schemas import (
    LeadProfile,
    LeadStatus,
    MessageBatch,
    OutgoingMessage,
    Provider,
    Segment,
    StandardTag,
    utcnow,
)
from src.crm.base import CRMConnector
from src.tools.calendar import CalendarService
from src.tools.catalog import CatalogService
from src.tools.handoff import (
    EMERGENCY_MESSAGE,
    AIPauseRegistry,
    HandoffReason,
    HandoffService,
)
from src.tools.results import ToolResult, ToolStatus, normalize_text
from src.tools.tenant_data import BusinessInfoType, TenantData, TenantDataStore

logger = logging.getLogger("lobo.agent.nodes")

MAX_TOOL_ITERATIONS = 6


# --------------------------------------------------------------------------- #
# Schemas de entrada das ferramentas expostas ao LLM (sem tenant_id/lead_phone)
# --------------------------------------------------------------------------- #
class _SearchCatalogArgs(BaseModel):
    query: str = Field(
        default="",
        description="Texto livre do que o cliente procura (ex.: 'colchão de casal molas ensacadas', 'piscina 6x3', 'psicoterapia adulto').",
    )
    category: str | None = Field(
        default=None,
        description="Categoria opcional (ex.: 'colchao', 'piscina', 'servico', 'acessorio').",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Lista opcional de palavras-chave adicionais.",
    )


class _RealtimePriceArgs(BaseModel):
    item_id: str = Field(
        min_length=1,
        description="Código exato do item no catálogo (ex.: 'CS-ENS-CASAL', 'SP-FIB-600', 'VZ-PSI-ADULTO').",
    )
    variant: str | None = Field(
        default=None,
        description="Código opcional da variante (ex.: 'com-box', 'pacote-4').",
    )


class _DeliveryInfoArgs(BaseModel):
    city: str = Field(
        min_length=2,
        description="Nome da cidade para consultar taxa e prazo de entrega (ex.: 'Belém', 'Ananindeua', 'Castanhal').",
    )


class _BusinessInfoArgs(BaseModel):
    info_type: BusinessInfoType = Field(
        description="Tipo de informação institucional: 'hours', 'location', 'warranty' ou 'payment_methods'."
    )


class _CheckAvailableSlotsArgs(BaseModel):
    date: str = Field(
        description="Data inicial desejada no formato AAAA-MM-DD (ex.: '2026-11-11')."
    )
    service_id: str | None = Field(
        default=None,
        description="ID do serviço (ex.: 'visita_loja', 'visita_tecnica', 'triagem_online'). Se omitido, usa o serviço padrão do tenant.",
    )
    end_date: str | None = Field(
        default=None,
        description="Data final opcional no formato AAAA-MM-DD.",
    )


class _CreateBookingArgs(BaseModel):
    slot_datetime: str = Field(
        description="Data e hora ISO-8601 exata do slot escolhido (ex.: '2026-11-11T09:00:00-03:00')."
    )
    lead_name: str | None = Field(
        default=None,
        description="Nome do cliente para o agendamento.",
    )
    service_id: str | None = Field(
        default=None,
        description="ID do serviço agendado.",
    )


class _TriggerHandoffArgs(BaseModel):
    reason: HandoffReason = Field(
        description=(
            "Motivo da transferência humana: 'customer_request', 'unknown_answer', "
            "'discount_negotiation', 'clinical_emergency', 'price_unavailable' ou 'technical_failure'."
        )
    )
    conversation_summary: str = Field(
        min_length=1,
        max_length=2000,
        description="Resumo executivo claro do que o cliente precisa para o atendente humano.",
    )
    suggested_action: str = Field(
        default="",
        max_length=500,
        description="Próximo passo recomendado para o atendente humano.",
    )


# --------------------------------------------------------------------------- #
# Fábrica de ferramentas com tenant_id e lead_phone injetados por closure
# --------------------------------------------------------------------------- #
def build_tenant_tools(
    *,
    tenant_id: str,
    lead: LeadProfile,
    catalog: CatalogService,
    calendar: CalendarService,
    handoff: HandoffService,
    recorded_results: list[ToolResult],
) -> list[StructuredTool]:
    """Cria as ferramentas LangChain vinculadas de forma imutável ao `tenant_id` e `lead` do turno."""

    async def _search_catalog(
        query: str = "",
        category: str | None = None,
        keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        res = await catalog.search_catalog(
            tenant_id,
            query or None,
            category=category,
            keywords=keywords,
        )
        recorded_results.append(res)
        return res.to_llm_payload()

    async def _get_realtime_price(
        item_id: str,
        variant: str | None = None,
    ) -> dict[str, Any]:
        res = await catalog.get_realtime_price(tenant_id, item_id, variant=variant)
        recorded_results.append(res)
        return res.to_llm_payload()

    async def _get_delivery_info(city: str) -> dict[str, Any]:
        res = await catalog.get_delivery_info(tenant_id, city)
        recorded_results.append(res)
        return res.to_llm_payload()

    async def _get_business_info(info_type: BusinessInfoType) -> dict[str, Any]:
        res = await catalog.get_business_info(tenant_id, info_type)
        recorded_results.append(res)
        return res.to_llm_payload()

    async def _check_available_slots(
        date: str,
        service_id: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        res = await calendar.check_available_slots(
            tenant_id,
            date,
            service_id=service_id,
            end_date=end_date,
        )
        recorded_results.append(res)
        return res.to_llm_payload()

    async def _create_booking(
        slot_datetime: str,
        lead_name: str | None = None,
        service_id: str | None = None,
    ) -> dict[str, Any]:
        effective_name = (lead_name or lead.name or "").strip() or None
        res = await calendar.create_booking(
            tenant_id,
            lead.phone,
            slot_datetime,
            service_id=service_id,
            lead_name=effective_name,
        )
        if res.ok:
            if effective_name and not lead.name:
                lead.name = effective_name
            lead.status = LeadStatus.VISIT_SCHEDULED
            lead.add_tag(StandardTag.VISIT_SCHEDULED)
        recorded_results.append(res)
        return res.to_llm_payload()

    async def _trigger_human_handoff(
        reason: HandoffReason,
        conversation_summary: str,
        suggested_action: str = "",
    ) -> dict[str, Any]:
        res = await handoff.trigger_human_handoff(
            lead,
            reason=reason,
            conversation_summary=conversation_summary,
            suggested_action=suggested_action,
        )
        recorded_results.append(res)
        return res.to_llm_payload()

    return [
        StructuredTool.from_function(
            coroutine=_search_catalog,
            name="search_catalog",
            description=(
                "Busca produtos ou serviços no catálogo do tenant atual e retorna preços formatados, "
                "parcelamento, valor à vista e disponibilidade de estoque."
            ),
            args_schema=_SearchCatalogArgs,
        ),
        StructuredTool.from_function(
            coroutine=_get_realtime_price,
            name="get_realtime_price",
            description=(
                "Consulta o preço em tempo real de um item específico pelo código (item_id) "
                "e variante opcional."
            ),
            args_schema=_RealtimePriceArgs,
        ),
        StructuredTool.from_function(
            coroutine=_get_delivery_info,
            name="get_delivery_info",
            description=(
                "Consulta se o tenant entrega na cidade informada, retornando a taxa de entrega "
                "(fee_formatted) e o prazo em dias (lead_time_formatted)."
            ),
            args_schema=_DeliveryInfoArgs,
        ),
        StructuredTool.from_function(
            coroutine=_get_business_info,
            name="get_business_info",
            description=(
                "Consulta informações oficiais do estabelecimento: horário ('hours'), endereço ('location'), "
                "garantia ('warranty') ou formas de pagamento ('payment_methods')."
            ),
            args_schema=_BusinessInfoArgs,
        ),
        StructuredTool.from_function(
            coroutine=_check_available_slots,
            name="check_available_slots",
            description=(
                "Verifica horários disponíveis na agenda do tenant a partir de uma data (AAAA-MM-DD), "
                "retornando sempre 2 sugestões concretas em 'suggestions'."
            ),
            args_schema=_CheckAvailableSlotsArgs,
        ),
        StructuredTool.from_function(
            coroutine=_create_booking,
            name="create_booking",
            description=(
                "Confirma e grava um agendamento para o cliente no horário ISO escolhido (slot_datetime)."
            ),
            args_schema=_CreateBookingArgs,
        ),
        StructuredTool.from_function(
            coroutine=_trigger_human_handoff,
            name="trigger_human_handoff",
            description=(
                "Transfere imediatamente o atendimento para um especialista humano, registrando nota "
                "privada no CRM e pausando a IA."
            ),
            args_schema=_TriggerHandoffArgs,
        ),
    ]


# --------------------------------------------------------------------------- #
# Modelo determinístico de conversa com Tool Calling nativo (Testes/Simulação)
# --------------------------------------------------------------------------- #
_ISO_SLOT_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2}|Z)?)\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_ITEM_CODE_RE = re.compile(r"\b([A-Z]{2}-[A-Z]{3}-[A-Z0-9]+)\b")


class DeterministicSalesChatModel(BaseChatModel):
    """Implementação `BaseChatModel` determinística com suporte real a `bind_tools`.

    Permite executar todo o ciclo do LangGraph (SystemMessage -> HumanMessage ->
    AIMessage(tool_calls) -> execução das `StructuredTool`s -> ToolMessage ->
    AIMessage final) sem depender de rede externa em testes automatizados e na
    simulação local, garantindo latência zero e reprodutibilidade de 100%.
    """

    model_name: str = "gemini-3.8-flash"
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "lobo-deterministic-sales-model"

    def bind_tools(self, tools: list[BaseTool | Any], **kwargs: Any) -> Runnable[Any, Any]:
        names = [getattr(t, "name", str(t)) for t in tools]
        return self.model_copy(update={"bound_tool_names": names})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        ai_msg = self._step(messages)
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])

    def _step(self, messages: list[BaseMessage]) -> AIMessage:
        # Localiza a última mensagem humana e eventuais ToolMessages subsequentes
        last_human_idx = -1
        for idx in range(len(messages) - 1, -1, -1):
            if isinstance(messages[idx], HumanMessage):
                last_human_idx = idx
                break

        user_text = str(messages[last_human_idx].content) if last_human_idx >= 0 else ""
        sys_text = next((str(m.content) for m in messages if isinstance(m, SystemMessage)), "")
        trailing_tools = [
            m for m in messages[last_human_idx + 1 :] if isinstance(m, ToolMessage)
        ] if last_human_idx >= 0 else []

        if not trailing_tools:
            tool_calls = self._plan_tool_calls(user_text, sys_text)
            if tool_calls:
                return AIMessage(content="", tool_calls=tool_calls)
            return AIMessage(content=self._discovery_reply(user_text, sys_text))

        return self._synthesize_from_tools(user_text, sys_text, trailing_tools)

    def _plan_tool_calls(self, user_text: str, sys_text: str) -> list[dict[str, Any]]:
        norm = f" {normalize_text(user_text)} "
        calls: list[dict[str, Any]] = []

        def add_call(name: str, args: dict[str, Any]) -> None:
            if not self.bound_tool_names or name in self.bound_tool_names:
                calls.append({"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:10]}"})

        # 0. Objeção de preço ("Achei caro", "Tá caro") -> quebra de objeção consultiva (sem chamar tools de handoff)
        if self._is_price_objection(norm):
            return calls

        # 1. Código explícito de item (ex.: CS-ENS-CASAL)
        code_match = _ITEM_CODE_RE.search(user_text.upper())
        if code_match and any(k in norm for k in (" preco ", " valor ", " custa ", " box ", " variante ")):
            variant = "com-box" if " box " in norm else None
            add_call("get_realtime_price", {"item_id": code_match.group(1), "variant": variant})

        # 2. Busca no catálogo (específica ou exploratória/anúncio)
        catalog_triggers = (
            "colchao", "colchoes", "mola", "molas", "ensacada", "ensacadas", "pocket",
            "espuma", "ortopedico", "casal", "queen", "king", "solteiro",
            "piscina", "piscinas", "fibra", "aquecedor",
            "psicoterapia", "terapia", "psicologo", "psicologa", "neuropsicologica", "tdah", "autismo", "laudo",
            "geladeira", "travesseiro",
            # Exploratórias / Anúncios ("outros modelos", "quais as opções", etc.)
            "outros modelos", "outro modelo", "quais modelos", "quais as opcoes", "quais opcoes",
            "o que voces tem", "tem promocao", "quero saber mais", "to pesquisando", "estou pesquisando",
            # Agência (tenant `loboads`)
            "trafego", "anuncio", "meta ads", "google ads", "sales agent", "agente de ia", "automacao", "chatbot",
        )
        is_booking_request = any(
            k in norm
            for k in (
                "agendar", "agendamento", "marcar consulta", "marcar avaliacao",
                "marcar sessao", "marcar visita", "quero marcar", "quero agendar",
                "gostaria de agendar",
            )
        )
        # Se for apenas um pedido de agendamento vindo de anúncio ("vi no anúncio e quero agendar minha avaliação"),
        # prioriza a triagem de agendamento em vez de buscar a palavra "anuncio" no catálogo.
        has_non_ad_catalog_trigger = any(
            (f" {t} " in norm or t in norm)
            for t in catalog_triggers
            if t != "anuncio"
        )
        if has_non_ad_catalog_trigger or ("anuncio" in norm and not is_booking_request):
            # Se a mensagem tem cabeçalho de anúncio ("... MENSAGEM DO CLIENTE: Ola Gostaria ver outros modelos")
            # e o cliente pede "outros modelos" / "quais opções", prioriza o pedido do cliente na busca
            ad_client_split = re.split(r"mensagem do cliente\s*:", user_text, maxsplit=1, flags=re.IGNORECASE)
            if len(ad_client_split) == 2 and any(
                k in normalize_text(ad_client_split[1])
                for k in ("outros modelos", "outro modelo", "quais modelos", "quais as opcoes", "quais opcoes", "o que voces tem")
            ):
                query_text = ad_client_split[1].strip()
            else:
                # Extrai a linha/trecho relevante de produto para uma busca limpa
                non_quoted_product_lines = [
                    line.strip()
                    for line in user_text.splitlines()
                    if not line.strip().startswith("[Lead respondeu citando")
                    and any(t in normalize_text(line) for t in catalog_triggers)
                ]
                product_lines = non_quoted_product_lines or [
                    line.strip()
                    for line in user_text.splitlines()
                    if any(t in normalize_text(line) for t in catalog_triggers)
                ]
                query_text = " ".join(product_lines) if product_lines else user_text
            add_call("search_catalog", {"query": query_text})

        # 3. Entrega / Frete
        attrs = extract_lead_attributes(user_text)
        city = attrs.get("cidade")
        if any(k in norm for k in (" entrega", " entregam", " frete", " enviam")):
            target_city = city
            if not target_city:
                # Tenta capturar palavra após "em " ou "para "
                m = re.search(r"\b(?:em|para)\s+([A-Za-zÀ-ÿ]+(?:\s+[A-Za-zÀ-ÿ]+)?)", user_text)
                if m:
                    target_city = m.group(1).strip("?.!, ")
            if target_city:
                add_call("get_delivery_info", {"city": target_city})

        # 4. Informações do negócio
        if any(k in norm for k in ("horario de funcionamento", "que horas abre", "horas funciona")):
            add_call("get_business_info", {"info_type": "hours"})
        elif any(k in norm for k in ("endereco", "onde fica", "localizacao")):
            add_call("get_business_info", {"info_type": "location"})
        elif "garantia" in norm:
            add_call("get_business_info", {"info_type": "warranty"})
        elif any(k in norm for k in ("forma de pagamento", "formas de pagamento", "aceita pix", "aceita cartao")):
            add_call("get_business_info", {"info_type": "payment_methods"})

        # 5. Agenda (confirmação de slot ISO ou consulta de disponibilidade APÓS triagem)
        slot_match = _ISO_SLOT_RE.search(user_text)
        if slot_match and any(k in norm for k in ("agendar", "marcar", "confirmo", "quero esse", "fechar", "reservar")):
            add_call("create_booking", {"slot_datetime": slot_match.group(1)})
        elif any(k in norm for k in ("agendar", "agendamento", "marcar", "horario disponivel", "horarios disponiveis", "visita tecnica", "visitar a loja")):
            date_match = _ISO_DATE_RE.search(user_text)
            has_explicit_day = bool(date_match) or any(
                d in norm for d in (" amanha ", " hoje ", " segunda", " terca", " quarta", " quinta", " sexta", " sabado")
            )
            has_qualification = any(
                k in sys_text
                for k in ("tamanho=", "publico=", "modalidade_interesse=", "medidas_interesse=", "cidade=", "Status do lead: qualified")
            ) or bool(attrs.get("tamanho") or attrs.get("publico") or attrs.get("medidas_interesse"))

            # Regra do Funil Obrigatório (Seção 3): só consulta a agenda se o lead já passou pela triagem
            # OU se especificou uma data/dia concreto para verificar horários.
            if has_explicit_day or has_qualification:
                if date_match:
                    target_date = date_match.group(1)
                else:
                    sys_date_match = _ISO_DATE_RE.search(sys_text)
                    base_date = (
                        datetime.strptime(sys_date_match.group(1), "%Y-%m-%d").date()
                        if sys_date_match
                        else utcnow().date()
                    )
                    target_date = (base_date + timedelta(days=1)).isoformat()
                add_call("check_available_slots", {"date": target_date})

        return calls

    @staticmethod
    def _is_price_objection(norm: str) -> bool:
        return any(
            p in norm
            for p in (
                "achei caro",
                "ta caro",
                "esta caro",
                "muito caro",
                "ficou caro",
                "valor alto",
                "preco alto",
                "acima do meu orcamento",
                "pesado no orcamento",
                "fora do meu orcamento",
            )
        )

    def _discovery_reply(self, user_text: str, sys_text: str) -> str:
        norm = f" {normalize_text(user_text)} "

        # 1. Quebra de Objeção de Preço ("Achei caro") — SDR/Closer Consultivo Master
        if self._is_price_objection(norm):
            if "segmento: `psychology`" in sys_text:
                return (
                    "Compreendo perfeitamente o seu ponto! 😊 Cuidar da saúde emocional é um investimento "
                    "direto na sua qualidade de vida e bem-estar a longo prazo.\n\n"
                    "Aqui na Vizzu, além de contarmos com especialistas dedicados e **emissão de recibo para reembolso "
                    "no plano de saúde**, temos a opção do **pacote mensal** que reduz o valor por sessão e parcelamento "
                    "facilitado. Prefere que eu te explique como funciona o pacote mensal ou o reembolso?"
                )
            if "segmento: `pools`" in sys_text:
                return (
                    "Entendo perfeitamente! 😊 Uma piscina de fibra da Splash é um patrimônio com **alta durabilidade**, "
                    "**5 anos de garantia estrutural**, valorização imediata do seu imóvel e lazer para toda a família.\n\n"
                    "Além disso, temos **entrega grátis** na Região Metropolitana e parcelamento facilitado em até "
                    "**10x sem juros** (ou em até 12x no cartão). Quer que eu simule uma condição parcelada que fique "
                    "confortável no seu planejamento?"
                )
            return (
                "Entendo perfeitamente o seu ponto! 😊 Um colchão de qualidade é um investimento diário na "
                "**saúde da sua coluna** e no seu descanso, com **alta durabilidade** para muitos anos de uso e "
                "**3 anos de garantia de fábrica**.\n\n"
                "Para ficar bem leve no seu orçamento, você conta com **entrega grátis** na Região Metropolitana "
                "e facilidade de pagamento em até **10x sem juros** no cartão (ou desconto especial à vista no PIX). "
                "Prefere aproveitar o parcelamento em **10x sem juros** ou quer conhecer também nossa linha de espuma "
                "de alta densidade com excelente custo-benefício?"
            )

        # 2. Reconhecimento de Lead Recorrente ("returning_contact=sim")
        lead_name_match = re.search(r"- Nome do lead:\s*(.+?)\.", sys_text)
        lead_name = lead_name_match.group(1).strip() if lead_name_match else ""
        if lead_name.lower() in ("não informado", "nao informado", "lead playground"):
            lead_name = ""

        is_returning = "returning_contact=sim" in sys_text or (
            "(recorrente)" in sys_text and "returning_contact=nao" not in sys_text
        )

        # 3. Triagem Consultiva Obrigatória antes do Agendamento (quando o lead pede agendamento no escuro)
        is_booking_request = any(
            k in norm
            for k in (
                "agendar", "agendamento", "marcar consulta", "marcar avaliacao",
                "marcar sessao", "marcar visita", "quero marcar", "quero agendar",
                "gostaria de agendar",
            )
        )
        has_ad_context = "Origem do lead: Anúncio" in sys_text or "origem_anuncio=meta_ads" in sys_text
        ad_headline_match = re.search(r"ad_headline=([^,\n.]+)", sys_text)
        ad_topic = ad_headline_match.group(1).strip() if ad_headline_match else None

        if is_booking_request:
            ad_intro = (
                f"Vi que você veio pelo nosso anúncio sobre **{ad_topic}** — vai ser um enorme prazer te atender! "
                if ad_topic
                else (
                    "Vi que você veio pelo nosso anúncio — vai ser um enorme prazer te atender! "
                    if has_ad_context
                    else ""
                )
            )
            if "segmento: `psychology`" in sys_text:
                return (
                    f"Olá{f', {lead_name}' if lead_name else ''}! Seja muito bem-vindo(a) à Vizzu Psicologia! 😊 "
                    f"{ad_intro}Com certeza vamos organizar o seu agendamento!\n\n"
                    "Para eu separar o profissional com a **especialidade** ideal para o seu horário: "
                    "o atendimento seria para **você (adulto)** ou para um **filho/dependente (qual a idade)**? "
                    "E você busca **psicoterapia** ou **avaliação neuropsicológica** (e já fez terapia antes ou seria a primeira vez)?"
                )
            if "segmento: `pools`" in sys_text:
                return (
                    f"Olá{f', {lead_name}' if lead_name else ''}! Seja muito bem-vindo(a) à Splash Piscinas! 🏊‍♂️ "
                    f"{ad_intro}Maravilha! Antes de agendarmos a visita técnica no seu espaço:\n\n"
                    "Você fala de qual **cidade** e já tem uma ideia do **tamanho de piscina** que cabe no seu terreno (ex.: 6x3m, 7x3,5m ou 8x4m)?"
                )
            return (
                f"Olá{f', {lead_name}' if lead_name else ''}! Seja muito bem-vindo(a) à Companhia dos Sonhos! 😊 "
                f"{ad_intro}Perfeito, nossa loja tem um showroom incrível para você testar!\n\n"
                "Para eu já separar as melhores opções para quando você chegar: qual **medida** você precisa "
                "(solteiro, casal, queen ou king) e você busca algo mais **firme para a coluna** ou mais **macio com molas ensacadas**?"
            )

        if is_returning:
            greeting_name = f", {lead_name}" if lead_name else ""
            return (
                f"Oi{greeting_name}! Que bom falar com você de novo! 😊\n\n"
                "Como posso te ajudar hoje?"
            )

        if has_ad_context:
            topic_str = f" sobre **{ad_topic}**" if ad_topic else ""
            if "segmento: `psychology`" in sys_text:
                return (
                    f"Olá, tudo bem? Seja muito bem-vindo(a) à Vizzu Psicologia! 😊 "
                    f"Vi que você veio pelo nosso anúncio{topic_str}. Vai ser um enorme prazer te atender!\n\n"
                    "Para eu te direcionar ao especialista ideal: o atendimento seria para **você (adulto)** "
                    "ou para **criança/adolescente (qual a idade)**? E você busca **psicoterapia** ou **avaliação neuropsicológica**?"
                )
            if "segmento: `pools`" in sys_text:
                return (
                    f"Olá, tudo bem? Seja muito bem-vindo(a) à Splash Piscinas! 🏊‍♂️ "
                    f"Vi que você veio pelo nosso anúncio{topic_str}. Vai ser um enorme prazer te atender!\n\n"
                    "Para eu preparar as melhores opções: de qual **cidade** você fala e qual é o **espaço aproximado** disponível no seu terreno?"
                )
            return (
                f"Olá, tudo bem? Seja muito bem-vindo(a) à Companhia dos Sonhos! 😊 "
                f"Vi que você veio pelo nosso anúncio{topic_str}. Vai ser um enorme prazer te atender!\n\n"
                "Para eu separar as melhores opções para você: qual **medida** você procura (solteiro, casal, queen ou king) "
                "e você prefere um colchão mais **firme para a coluna** ou mais **macio com molas ensacadas**?"
            )

        if "segmento: `psychology`" in sys_text:
            return (
                "Olá! Seja muito bem-vindo(a) à Vizzu Psicologia. 😊\n\n"
                "Para eu te direcionar da melhor forma: você busca atendimento para **adulto ou infantil (criança)**? "
                "E seria para **psicoterapia regular** ou **avaliação neuropsicológica**?"
            )
        if "segmento: `pools`" in sys_text:
            return (
                "Olá! Tudo bem? Seja bem-vindo(a) à Splash Piscinas! 🏊‍♂️\n\n"
                "Para eu preparar seu orçamento: de qual **cidade** você fala e qual é o **espaço aproximado** "
                "disponível no terreno? Se puder enviar uma foto da área, também ajuda bastante!"
            )
        if "segmento: `generic`" in sys_text:
            name_match = re.search(r"empresa \*\*(.+?)\*\*", sys_text)
            company = name_match.group(1) if name_match else "nossa empresa"
            return (
                f"Olá! Tudo bem? Seja muito bem-vindo(a) à **{company}**! 😊\n\n"
                "Me conta um pouco sobre o seu negócio: qual é o seu **segmento** e qual é o principal "
                "**desafio comercial** hoje (gerar mais clientes, atender mais rápido no WhatsApp ou organizar o funil)?"
            )
        return (
            "Olá, boa tarde! Seja muito bem-vindo(a)! 😊\n\n"
            "Para eu te indicar o colchão ideal: qual **tamanho** você procura (solteiro, casal, queen ou king) "
            "e você prefere um modelo mais **firme** para a coluna ou com **molas ensacadas** e pillow top?"
        )

    def _synthesize_from_tools(
        self,
        user_text: str,
        sys_text: str,
        tool_messages: list[ToolMessage],
    ) -> AIMessage:
        payloads: list[dict[str, Any]] = []
        for tm in tool_messages:
            try:
                data = json.loads(tm.content) if isinstance(tm.content, str) else tm.content
                if isinstance(data, dict):
                    payloads.append(data)
            except ValueError:
                continue

        # Se já houve handoff executado na tool, retorna a mensagem ao cliente
        for p in payloads:
            if p.get("tool") == "trigger_human_handoff" and isinstance(p.get("data"), dict):
                msg = p["data"].get("customer_message")
                if msg:
                    return AIMessage(content=str(msg))

        # Se alguma ferramenta exige humano (ex.: item sem preço ou fora da área), aciona handoff
        needs_human = next((p for p in payloads if p.get("requires_human")), None)
        if needs_human is not None:
            reason_raw = str(needs_human.get("reason") or "")
            if "price" in reason_raw or needs_human.get("tool") == "get_realtime_price":
                h_reason = HandoffReason.PRICE_UNAVAILABLE.value
            else:
                h_reason = HandoffReason.UNKNOWN_ANSWER.value
            if not self.bound_tool_names or "trigger_human_handoff" in self.bound_tool_names:
                already_called = any(p.get("tool") == "trigger_human_handoff" for p in payloads)
                if not already_called:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "trigger_human_handoff",
                                "args": {
                                    "reason": h_reason,
                                    "conversation_summary": f"Cliente solicitou item/condição que requer confirmação humana ({reason_raw}). Pedido original: {user_text[:300]}",
                                    "suggested_action": "Confirmar disponibilidade/valor com o cliente.",
                                },
                                "id": f"call_{uuid.uuid4().hex[:10]}",
                            }
                        ],
                    )

        norm = f" {normalize_text(user_text)} "
        lead_name_match = re.search(r"- Nome do lead:\s*(.+?)\.", sys_text)
        lead_name = lead_name_match.group(1).strip() if lead_name_match else ""
        if lead_name.lower() in ("não informado", "nao informado", "lead playground"):
            lead_name = ""

        has_ad_context = (
            "Origem do lead: Anúncio" in sys_text
            or "origem_anuncio=meta_ads" in sys_text
            or "mensagem do cliente" in norm
            or "vi no anuncio" in norm
            or "vi a propaganda" in norm
        )
        ad_headline_match = re.search(r"ad_headline=([^,\n.]+)", sys_text)
        ad_topic = ad_headline_match.group(1).strip() if ad_headline_match else None
        if ad_topic:
            ad_topic = re.sub(r"(?:a partir de\s*)?R\$\s*[\d.,]+", "", ad_topic, flags=re.IGNORECASE).strip(" .,-–:")

        is_returning = "returning_contact=sim" in sys_text or (
            "(recorrente)" in sys_text and "returning_contact=nao" not in sys_text
        )

        if has_ad_context:
            topic_str = f" sobre **{ad_topic}**" if ad_topic else ""
            greeting = (
                f"Olá{f', {lead_name}' if lead_name else ''}, tudo bem? Seja muito bem-vindo(a)! 😊 "
                f"Vi que você veio pelo nosso anúncio{topic_str} e vai ser um enorme prazer te atender!"
            )
        elif is_returning and lead_name:
            greeting = f"Oi, {lead_name}! Que bom falar com você de novo! 😊"
        else:
            greeting = "Olá, boa tarde! Tudo bem? 😊"

        parts: list[str] = [greeting]
        presented_featured_highlights = False

        for p in payloads:
            tool_name = p.get("tool")
            data = p.get("data") or {}
            if tool_name == "search_catalog" and p.get("status") == "ok":
                results = data.get("results") or []
                is_featured_fallback = bool(data.get("featured_fallback"))
                is_multi_highlight = is_featured_fallback or any(
                    k in norm
                    for k in ("outros modelos", "outro modelo", "quais modelos", "quais as opcoes", "quais opcoes", "o que voces tem")
                )
                if results and is_multi_highlight and len(results) >= 2:
                    presented_featured_highlights = True
                    highlights: list[str] = []
                    for item in results[:3]:
                        i_name = item.get("name", "Produto")
                        i_id = item.get("item_id", "")
                        i_attrs = item.get("attributes") or {}
                        i_medidas = i_attrs.get("medidas")
                        i_firmeza = i_attrs.get("firmeza")
                        i_details = ", ".join(
                            filter(None, [i_medidas, f"firmeza {i_firmeza}" if i_firmeza else None])
                        )
                        line = f"• **{i_name}** (`{i_id}`)"
                        if i_details:
                            line += f" ({i_details})"
                        if item.get("price_available"):
                            p_fmt = item.get("price_formatted")
                            inst_fmt = item.get("installments_formatted")
                            cash_fmt = item.get("cash_price_formatted")
                            line += f" — por **{p_fmt}**"
                            if inst_fmt:
                                line += f" (em até **{inst_fmt}**)"
                            if cash_fmt:
                                line += f" ou **{cash_fmt}** à vista"
                        highlights.append(line)
                    parts.append(
                        "Separei algumas das nossas **opções mais procuradas do catálogo** para você conhecer:\n"
                        + "\n".join(highlights)
                    )
                elif results:
                    top = results[0]
                    name = top.get("name", "Produto")
                    item_id = top.get("item_id", "")
                    attrs = top.get("attributes") or {}
                    medidas = attrs.get("medidas")
                    firmeza = attrs.get("firmeza")
                    details = ", ".join(filter(None, [medidas, f"firmeza {firmeza}" if firmeza else None]))
                    header = f"Temos o **{name}** (`{item_id}`)"
                    if details:
                        header += f" ({details})"
                    if top.get("price_available"):
                        price_fmt = top.get("price_formatted")
                        inst_fmt = top.get("installments_formatted")
                        cash_fmt = top.get("cash_price_formatted")
                        price_line = f"por **{price_fmt}**"
                        if inst_fmt:
                            price_line += f" (em até **{inst_fmt}**)"
                        if cash_fmt:
                            price_line += f" ou **{cash_fmt}** à vista"
                        parts.append(f"{header} {price_line}.")
                        if top.get("price_note"):
                            parts.append(f"📌 *Observação:* {top['price_note']}")
                    else:
                        parts.append(
                            f"{header}. O valor deste item depende de avaliação personalizada com nossa equipe."
                        )

            elif tool_name == "get_realtime_price" and p.get("status") == "ok":
                name = data.get("name", "Item")
                variant = data.get("variant")
                label = f"{name} ({variant})" if variant else name
                price_fmt = data.get("price_formatted")
                inst_fmt = data.get("installments_formatted")
                cash_fmt = data.get("cash_price_formatted")
                line = f"O valor do **{label}** é **{price_fmt}**"
                if inst_fmt:
                    line += f" ({inst_fmt})"
                if cash_fmt:
                    line += f" ou **{cash_fmt}** à vista"
                if not data.get("in_stock", True):
                    line += " *(atualmente sem estoque para pronta-entrega)*"
                parts.append(f"{line}.")

            elif tool_name == "get_delivery_info" and p.get("status") == "ok":
                city = data.get("city", "sua cidade")
                fee_fmt = data.get("fee_formatted", "")
                lead_fmt = data.get("lead_time_formatted", "")
                notes = data.get("notes")
                deliv_line = (
                    f"🚚 **Entrega em {city}:** frete **{fee_fmt}**, com prazo de **{lead_fmt}**."
                )
                if notes:
                    deliv_line += f" ({notes})"
                parts.append(deliv_line)

            elif tool_name == "get_business_info" and p.get("status") == "ok":
                content = data.get("content")
                if content:
                    parts.append(f"ℹ️ {content}")

            elif tool_name == "check_available_slots" and p.get("status") == "ok":
                service_name = (data.get("service") or {}).get("name", "atendimento")
                suggestions = data.get("suggestions") or []
                if len(suggestions) >= 2:
                    s1, s2 = suggestions[0]["label"], suggestions[1]["label"]
                    parts.append(
                        f"📅 Para **{service_name}**, tenho estas 2 opções de horário disponíveis:\n"
                        f"1️⃣ **{s1}**\n"
                        f"2️⃣ **{s2}**\n"
                        f"Qual desses horários fica melhor para você?"
                    )
                elif len(suggestions) == 1:
                    s1 = suggestions[0]["label"]
                    parts.append(
                        f"📅 Para **{service_name}**, temos disponível **{s1}**. Posso confirmar para você?"
                    )

            elif tool_name == "create_booking" and p.get("status") == "ok":
                service_name = data.get("service", "Atendimento")
                label = data.get("label", "")
                parts.append(
                    f"✅ Agendamento confirmado para **{service_name}** em **{label}**! Te esperamos lá."
                )

        if len(parts) == 1:
            parts.append("Como posso te ajudar hoje?")
        elif presented_featured_highlights:
            if "segmento: `psychology`" in sys_text:
                parts.append(
                    "Para eu te indicar a modalidade e o profissional ideal: o atendimento seria para **adulto ou criança**, "
                    "e você busca **psicoterapia** ou **avaliação neuropsicológica**?"
                )
            elif "segmento: `pools`" in sys_text:
                parts.append(
                    "Para eu te indicar o projeto ideal: de qual **cidade** você fala e qual é o **espaço aproximado** "
                    "disponível no seu terreno?"
                )
            elif "segmento: `generic`" in sys_text:
                parts.append(
                    "Para eu te indicar o plano ideal: qual é o seu **segmento** e o seu principal objetivo comercial hoje?"
                )
            else:
                parts.append(
                    "Para eu te indicar o modelo perfeito para o seu descanso: qual **medida** você precisa "
                    "(solteiro, casal, queen ou king) e qual a sua preferência de **conforto** (mais firme para a coluna ou mais macio com molas ensacadas)?"
                )
        elif not any(p.get("tool") in ("check_available_slots", "create_booking") for p in payloads):
            if "segmento: `generic`" in sys_text:
                parts.append("Quer agendar uma reunião rápida de diagnóstico para montarmos o plano ideal para você?")
            else:
                parts.append("Gostaria de agendar uma visita à nossa loja para experimentar pessoalmente ou prefere já garantir o seu?")

        return AIMessage(content="\n\n".join(parts))


def create_gemini_chat_model(settings: Settings, model_name: str | None = None) -> BaseChatModel:
    """Instancia o `ChatGoogleGenerativeAI` se `GOOGLE_API_KEY` estiver configurada;
    caso contrário, retorna `DeterministicSalesChatModel` com o `model_name` selecionado."""
    selected_model = (model_name or settings.gemini_model or "gemini-3.8-flash").strip()
    if settings.gemini_enabled and settings.google_api_key is not None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=selected_model,
            google_api_key=settings.google_api_key.get_secret_value(),
            temperature=0.2,
        )
    return DeterministicSalesChatModel(model_name=selected_model)


# --------------------------------------------------------------------------- #
# Implementação dos Nós do LangGraph
# --------------------------------------------------------------------------- #
def _extract_ai_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                chunks.append(str(part.get("text", "")))
        return "\n".join(chunks).strip()
    return str(content or "").strip()


def _extract_last_user_text(state: AgentState) -> str:
    batch: MessageBatch | None = state.get("batch")
    if batch is not None:
        return batch.combined_text
    messages = state.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return _extract_ai_text(msg.content)
    return ""


class AgentNodes:
    """Agrupa os nós executáveis do `StateGraph` com suas dependências injetadas."""

    def __init__(
        self,
        *,
        store: TenantDataStore,
        catalog: CatalogService,
        calendar: CalendarService,
        handoff: HandoffService,
        pause_registry: AIPauseRegistry | None = None,
        crm_resolver: Callable[[TenantData], CRMConnector | None] | None = None,
        llm: BaseChatModel | None = None,
        jev_guard: JEVGuard | None = None,
        check_crm_human_active: bool = False,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._calendar = calendar
        self._handoff = handoff
        self._pause = pause_registry
        self._crm_resolver = crm_resolver or (lambda _t: None)
        self._llm = llm or DeterministicSalesChatModel()
        self._jev = jev_guard or JEVGuard()
        self._check_crm_human = check_crm_human_active
        self._clock = clock

    # -------------------------------------------------------------- Nó 1: Triagem & Validação de Handoff
    async def node_triage(self, state: AgentState) -> dict[str, Any]:
        """Nó 1: Verifica se um atendente humano já assumiu (silêncio imediato) e classifica a intenção."""
        lead = state["lead"]
        tenant_id = state["tenant_id"]
        batch: MessageBatch | None = state.get("batch")

        # 1. Verifica pausa local no LeadProfile
        if lead.ai_paused or lead.has_tag(StandardTag.HUMAN_HANDOFF):
            logger.info("IA silenciada (lead.ai_paused) %s", lead.log_context())
            return {
                "silenced": True,
                "silence_reason": "lead_human_active",
                "current_stage": ConversationStage.SILENCED,
                "reply_text": None,
                "outgoing_message": None,
            }

        # 2. Verifica pausa distribuída no Redis (AIPauseRegistry)
        if self._pause is not None:
            try:
                if await self._pause.is_paused(tenant_id, lead.phone):
                    lead.mark_human_handoff()
                    logger.info("IA silenciada (AIPauseRegistry) %s", lead.log_context())
                    return {
                        "silenced": True,
                        "silence_reason": "redis_pause_active",
                        "current_stage": ConversationStage.SILENCED,
                        "reply_text": None,
                        "outgoing_message": None,
                    }
            except LoboError as exc:
                logger.warning("Falha ao consultar AIPauseRegistry (%s), prosseguindo com cautela", exc.message)

        tenant = self._store.get(tenant_id)

        # 3. Opcionalmente verifica no CRM se há humano ativo (respeitando assignee_means_human)
        if self._check_crm_human:
            try:
                crm = self._crm_resolver(tenant)
                if crm is not None and await crm.is_human_active(lead):
                    lead.mark_human_handoff()
                    logger.info("IA silenciada (CRM human_active) %s", lead.log_context())
                    return {
                        "silenced": True,
                        "silence_reason": "crm_human_active",
                        "current_stage": ConversationStage.SILENCED,
                        "reply_text": None,
                        "outgoing_message": None,
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning("Não foi possível consultar is_human_active no CRM: %s", type(exc).__name__)

        # 4. Atualiza metadados do lead a partir do lote e identifica Lead Novo vs. Lead Recorrente
        was_returning = (
            lead.status is not LeadStatus.NEW
            or bool(lead.tags)
            or any(isinstance(m, AIMessage) for m in (state.get("messages") or []))
        )
        lead.attributes["returning_contact"] = "sim" if was_returning else "nao"
        lead.touch()
        if lead.status is LeadStatus.NEW:
            lead.status = LeadStatus.IN_PROGRESS
        if batch is not None:
            if batch.contact_name and not lead.name:
                lead.name = batch.contact_name
            for m in batch.messages:
                if m.ad_referral and not lead.source:
                    lead.source = "meta_ads"
                    lead.attributes.setdefault("ctwa_source", m.ad_referral.get("sourceId") or "meta_ads")

        user_text = _extract_last_user_text(state)
        decision = self._jev.analyze(user_text, segment=tenant.tenant.segment)
        if decision.extracted_attributes:
            lead.attributes.update(decision.extracted_attributes)
            if decision.extracted_attributes.get("origem_anuncio") == "meta_ads" and not lead.source:
                lead.source = "meta_ads"

        updates: dict[str, Any] = {
            "lead": lead,
            "silenced": False,
            "intent": decision.intent,
            "jev_decision": decision.model_dump(mode="json"),
        }

        if decision.immediate_handoff and decision.handoff_reason is not None:
            updates.update(
                {
                    "current_stage": ConversationStage.HANDOFF,
                    "human_handoff_requested": True,
                    "handoff_reason": decision.handoff_reason.value,
                    "handoff_summary": f"{decision.summary} Mensagem do cliente: {user_text[:400]}",
                    "handoff_suggested_action": (
                        "Contato imediato de crise."
                        if decision.handoff_reason is HandoffReason.CLINICAL_EMERGENCY
                        else "Assumir atendimento no WhatsApp."
                    ),
                }
            )
        else:
            updates["current_stage"] = decision.stage

        return updates

    # -------------------------------------------------------------- Nós 2a/2b/2c: Raciocínio Comercial & Tool Calling
    async def node_discovery(self, state: AgentState) -> dict[str, Any]:
        """Nó de Descoberta & Necessidade (ARCHITECTURE.md §4.2)."""
        return await self._run_commercial_stage(state, stage=ConversationStage.DISCOVERY)

    async def node_offer_catalog(self, state: AgentState) -> dict[str, Any]:
        """Nó de Apresentação de Catálogo, Preço & Entrega (ARCHITECTURE.md §4.3)."""
        return await self._run_commercial_stage(state, stage=ConversationStage.OFFER_CATALOG)

    async def node_booking_conversion(self, state: AgentState) -> dict[str, Any]:
        """Nó de Agendamento & Conversão (ARCHITECTURE.md §4.4)."""
        return await self._run_commercial_stage(state, stage=ConversationStage.BOOKING)

    async def _run_commercial_stage(
        self,
        state: AgentState,
        *,
        stage: ConversationStage,
    ) -> dict[str, Any]:
        tenant_id = state["tenant_id"]
        lead = state["lead"]
        tenant = self._store.get(tenant_id)
        user_text = _extract_last_user_text(state)

        recorded_results: list[ToolResult] = []
        bound_tools = build_tenant_tools(
            tenant_id=tenant_id,
            lead=lead,
            catalog=self._catalog,
            calendar=self._calendar,
            handoff=self._handoff,
            recorded_results=recorded_results,
        )
        tools_by_name = {t.name: t for t in bound_tools}

        system_prompt = build_system_prompt(tenant, lead, stage=stage, now=self._clock())
        history = list(state.get("messages") or [])
        if not history or not isinstance(history[-1], HumanMessage):
            if user_text:
                history.append(HumanMessage(content=user_text))

        working_messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            *[m for m in history if not isinstance(m, SystemMessage)],
        ]
        new_messages: list[BaseMessage] = []

        effective_llm = self._llm
        if isinstance(effective_llm, DeterministicSalesChatModel) and tenant.tenant.llm_model:
            effective_llm = effective_llm.model_copy(update={"model_name": tenant.tenant.llm_model})
        elif hasattr(effective_llm, "model") and tenant.tenant.llm_model:
            try:
                effective_llm = effective_llm.model_copy(update={"model": tenant.tenant.llm_model})
            except Exception:  # noqa: BLE001
                pass

        model_with_tools = effective_llm.bind_tools(bound_tools)
        iterations = 0
        final_ai_text = ""

        try:
            while iterations < MAX_TOOL_ITERATIONS:
                iterations += 1
                ai_response = await model_with_tools.ainvoke(working_messages)
                if not isinstance(ai_response, AIMessage):
                    ai_response = AIMessage(content=_extract_ai_text( getattr(ai_response, "content", ai_response) ))

                working_messages.append(ai_response)
                new_messages.append(ai_response)

                tool_calls = getattr(ai_response, "tool_calls", None) or []
                if not tool_calls:
                    final_ai_text = _extract_ai_text(ai_response.content)
                    break

                for tc in tool_calls:
                    tc_name = tc.get("name", "")
                    tc_args = tc.get("args") or {}
                    tc_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    tool_obj = tools_by_name.get(tc_name)
                    if tool_obj is None:
                        err_result = ToolResult.invalid(tc_name or "unknown", tenant_id, "ferramenta desconhecida")
                        recorded_results.append(err_result)
                        payload = err_result.to_llm_payload()
                    else:
                        try:
                            payload = await tool_obj.ainvoke(tc_args)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Erro ao invocar tool %s: %s", tc_name, type(exc).__name__)
                            err_result = ToolResult.invalid(tc_name, tenant_id, f"argumentos inválidos: {exc}")
                            recorded_results.append(err_result)
                            payload = err_result.to_llm_payload()

                    tool_msg = ToolMessage(
                        content=json.dumps(payload, ensure_ascii=False),
                        tool_call_id=tc_id,
                        name=tc_name,
                    )
                    working_messages.append(tool_msg)
                    new_messages.append(tool_msg)

        except Exception as exc:  # noqa: BLE001 - REGRA INVIOLÁVEL 4 (FALLBACK SEGURO)
            logger.error(
                "Falha no modelo de IA (%s) %s — acionando fallback seguro para humano",
                type(exc).__name__,
                lead.log_context(),
            )
            return {
                "lead": lead,
                "current_stage": ConversationStage.HANDOFF,
                "executed_tools": recorded_results,
                "tool_iterations": iterations,
                "human_handoff_requested": True,
                "handoff_reason": HandoffReason.TECHNICAL_FAILURE.value,
                "handoff_summary": f"Falha técnica ao consultar LLM ({type(exc).__name__}). Última mensagem: {user_text[:300]}",
                "handoff_suggested_action": "Retomar atendimento humano imediatamente.",
                "reply_text": None,
            }

        # Qualificação automática do lead quando catálogo/preço foi consultado com sucesso
        if any(r.ok and r.tool in ("search_catalog", "get_realtime_price") for r in recorded_results):
            if lead.status is LeadStatus.IN_PROGRESS:
                lead.status = LeadStatus.QUALIFIED
            lead.add_tag(StandardTag.QUALIFIED)

        # Verifica se `trigger_human_handoff` já foi executada pelo modelo
        handoff_tool_result = next(
            (r for r in recorded_results if r.tool == "trigger_human_handoff"),
            None,
        )
        if handoff_tool_result is not None:
            cust_msg = (handoff_tool_result.data or {}).get("customer_message")
            return {
                "lead": lead,
                "messages": new_messages,
                "current_stage": ConversationStage.HANDOFF,
                "executed_tools": recorded_results,
                "tool_iterations": iterations,
                "human_handoff_requested": True,
                "handoff_reason": handoff_tool_result.reason or HandoffReason.OTHER.value,
                "handoff_completed": True,
                "reply_text": final_ai_text or cust_msg,
            }

        # Verifica se alguma ferramenta exigiu humano (`requires_human=True`) e o LLM não transferiu
        unresolved_human = self._find_unresolved_human_requirement(recorded_results)
        if unresolved_human is not None:
            reason_code = self._map_tool_reason_to_handoff(unresolved_human)
            return {
                "lead": lead,
                "messages": new_messages,
                "current_stage": ConversationStage.HANDOFF,
                "executed_tools": recorded_results,
                "tool_iterations": iterations,
                "human_handoff_requested": True,
                "handoff_reason": reason_code.value,
                "handoff_summary": (
                    f"Ferramenta `{unresolved_human.tool}` sinalizou necessidade de validação humana "
                    f"(motivo: {unresolved_human.reason}). Mensagem do lead: {user_text[:300]}"
                ),
                "handoff_suggested_action": "Confirmar informação/preço e responder ao cliente.",
                "reply_text": None,
            }

        # Guardrail Pós-LLM: bloqueia qualquer preço (`R$ ...`) que não tenha saído das tools!
        all_tools = [*(state.get("executed_tools") or []), *recorded_results]
        prices_ok, unbacked = verify_no_unbacked_prices(final_ai_text, all_tools)
        if not prices_ok:
            logger.error(
                "Guardrail anti-alucinação bloqueou preços não validados %s %s",
                unbacked,
                lead.log_context(),
            )
            return {
                "lead": lead,
                "messages": new_messages,
                "current_stage": ConversationStage.HANDOFF,
                "executed_tools": recorded_results,
                "tool_iterations": iterations,
                "human_handoff_requested": True,
                "handoff_reason": HandoffReason.PRICE_UNAVAILABLE.value,
                "handoff_summary": (
                    f"Guardrail bloqueou valor monetário não validado pela ferramenta ({', '.join(unbacked)}). "
                    f"Pedido do cliente: {user_text[:300]}"
                ),
                "handoff_suggested_action": "Informar o orçamento oficial validado ao cliente.",
                "reply_text": None,
            }

        return {
            "lead": lead,
            "messages": new_messages,
            "current_stage": stage,
            "executed_tools": recorded_results,
            "tool_iterations": iterations,
            "human_handoff_requested": False,
            "reply_text": final_ai_text,
        }

    # -------------------------------------------------------------- Nó 3: Handoff Humano
    async def node_handoff(self, state: AgentState) -> dict[str, Any]:
        """Nó de Transferência Humana: garante nota privada no CRM, pausa no Redis e mensagem segura."""
        lead = state["lead"]
        reason_raw = state.get("handoff_reason") or HandoffReason.OTHER.value
        try:
            reason = HandoffReason(reason_raw)
        except ValueError:
            reason = HandoffReason.OTHER

        new_tools: list[ToolResult] = []
        customer_msg: str | None = None

        if not state.get("handoff_completed"):
            user_text = _extract_last_user_text(state)
            summary = state.get("handoff_summary") or f"Atendimento transferido para humano. Última mensagem: {user_text[:400]}"
            suggested = state.get("handoff_suggested_action") or "Assumir conversa com o cliente."
            res = await self._handoff.trigger_human_handoff(
                lead,
                reason=reason,
                conversation_summary=summary,
                suggested_action=suggested,
            )
            new_tools.append(res)
            customer_msg = (res.data or {}).get("customer_message")
        else:
            existing_handoff = next(
                (r for r in reversed(state.get("executed_tools") or []) if r.tool == "trigger_human_handoff"),
                None,
            )
            if existing_handoff and isinstance(existing_handoff.data, dict):
                customer_msg = existing_handoff.data.get("customer_message")

        # Em emergência clínica ou falha técnica, a mensagem determinística homologada é obrigatória
        segment = Segment.GENERIC
        try:
            segment = self._store.get(state["tenant_id"]).tenant.segment
        except Exception:  # noqa: BLE001
            pass

        if reason is HandoffReason.CLINICAL_EMERGENCY:
            final_text = EMERGENCY_MESSAGE
        elif reason in (HandoffReason.TECHNICAL_FAILURE, HandoffReason.PRICE_UNAVAILABLE):
            final_text = customer_msg or self._handoff.customer_message(reason, segment)
        else:
            final_text = state.get("reply_text") or customer_msg or self._handoff.customer_message(reason, segment)

        return {
            "lead": lead,
            "current_stage": ConversationStage.HANDOFF,
            "executed_tools": new_tools,
            "human_handoff_requested": True,
            "handoff_reason": reason.value,
            "handoff_completed": True,
            "reply_text": final_text,
        }

    # -------------------------------------------------------------- Nó 4: Despacho da Resposta
    async def node_dispatch(self, state: AgentState) -> dict[str, Any]:
        """Nó de Despacho: empacota `reply_text` em um `OutgoingMessage` tipado."""
        if state.get("silenced") or not (state.get("reply_text") or "").strip():
            return {"outgoing_message": None}

        reply_text = str(state["reply_text"]).strip()
        turn_id = state.get("turn_id")
        batch: MessageBatch | None = state.get("batch")
        lead = state["lead"]

        if batch is not None:
            outgoing = OutgoingMessage.text_reply(batch, reply_text, turn_id=turn_id)
        else:
            tenant = self._store.get(state["tenant_id"])
            outgoing = OutgoingMessage(
                tenant_id=state["tenant_id"],
                provider=tenant.tenant.whatsapp_provider or Provider.SIMULATOR,
                session_id=state.get("session_id") or "default",
                contact_phone=lead.phone,
                text=reply_text,
                turn_id=turn_id,
            )

        return {"outgoing_message": outgoing}

    # -------------------------------------------------------------- Helpers
    @staticmethod
    def _find_unresolved_human_requirement(results: list[ToolResult]) -> ToolResult | None:
        for res in results:
            if not res.requires_human:
                continue
            if res.tool == "search_catalog":
                # Se uma busca falhou mas outra busca no mesmo turno encontrou itens com preço, não precisa handoff
                if any(r.tool == "search_catalog" and r.ok for r in results):
                    continue
            return res
        for res in results:
            if res.tool == "search_catalog" and res.ok and isinstance(res.data, dict):
                items = res.data.get("results") or []
                if items and all(not item.get("price_available", True) for item in items):
                    return ToolResult.needs_human(
                        "search_catalog",
                        res.tenant_id,
                        ToolStatus.UNAVAILABLE,
                        "price_on_request",
                    )
        return None

    @staticmethod
    def _map_tool_reason_to_handoff(res: ToolResult) -> HandoffReason:
        reason = (res.reason or "").lower()
        if "price" in reason or res.tool == "get_realtime_price":
            return HandoffReason.PRICE_UNAVAILABLE
        if "unexpected" in reason or "tool_failure" in reason or res.status is ToolStatus.ERROR:
            return HandoffReason.TECHNICAL_FAILURE
        return HandoffReason.UNKNOWN_ANSWER


__all__ = [
    "AgentNodes",
    "DeterministicSalesChatModel",
    "build_tenant_tools",
    "create_gemini_chat_model",
]