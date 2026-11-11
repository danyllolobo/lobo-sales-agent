"""JEV System One — Guardrail Determinístico, Classificador de Intenção e Anti-Alucinação.

Atua em duas frentes críticas (ARCHITECTURE.md §1, §2 e §6):
1. **Pré-LLM (System One / Fast Decision):**
   - Detecta emergências clínicas (risco de vida/autolesão) em 0ms e força handoff imediato
     com protocolo CVV (188) / SAMU (192).
   - Detecta pedido explícito de atendente humano ou barganha de desconto fora da tabela.
   - Classifica a intenção e o estágio do funil (`discovery`, `offer_catalog`, `booking`, `handoff`)
     e extrai atributos estruturados do lead (`tamanho`, `tecnologia`, `cidade`, `publico`).
2. **Pós-LLM (Output Guardrail Anti-Alucinação):**
   - `verify_no_unbacked_prices`: inspeciona o texto final gerado pelo modelo e bloqueia
     qualquer valor monetário (`R$ ...`) que não conste literalmente no retorno das
     ferramentas determinísticas executadas na conversa.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agent.state import AgentIntent, ConversationStage
from src.core.schemas import Segment
from src.tools.handoff import HandoffReason
from src.tools.results import ToolResult, normalize_text

logger = logging.getLogger("lobo.jev")

# --------------------------------------------------------------------------- #
# Padrões de segurança (System One)
# --------------------------------------------------------------------------- #
_EMERGENCY_PATTERNS: tuple[str, ...] = (
    "suicidio",
    "suicidar",
    "me matar",
    "tirar minha vida",
    "tirar a minha vida",
    "quero morrer",
    "vontade de morrer",
    "nao quero mais viver",
    "nao aguento mais viver",
    "acabar com minha vida",
    "acabar com a minha vida",
    "acabar com tudo",
    "me machucar",
    "me cortar",
    "automutilacao",
    "overdose",
    "atentar contra minha vida",
    "risco de vida",
)

_HUMAN_REQUEST_PATTERNS: tuple[str, ...] = (
    "falar com atendente",
    "falar com um atendente",
    "falar com uma atendente",
    "falar com humano",
    "falar com um humano",
    "falar com uma pessoa",
    "quero um atendente",
    "quero uma pessoa",
    "quero falar com gente",
    "atendente humano",
    "atendimento humano",
    "passa para um atendente",
    "passar para atendente",
    "transferir para atendente",
    "me transfere",
    "nao quero falar com robo",
    "nao quero robo",
    "sair do robo",
)

_DISCOUNT_BARGAIN_PATTERNS: tuple[str, ...] = (
    "desconto maior",
    "faz mais barato",
    "fazer mais barato",
    "consegue mais desconto",
    "tem um desconto",
    "me da um desconto",
    "dar um desconto",
    "cobrir oferta",
    "cobre o preco",
    "cobre oferta",
    "15 de desconto",
    "20 de desconto",
    "25 de desconto",
    "30 de desconto",
    "metade do preco",
    "baixar esse valor",
    "baixar o preco",
    "tem chorinho",
)

_BOOKING_KEYWORDS: tuple[str, ...] = (
    "agendar",
    "agendamento",
    "marcar visita",
    "marcar horario",
    "marcar um horario",
    "marcar consulta",
    "marcar sessao",
    "marcar triagem",
    "quero marcar",
    "pode marcar",
    "pode agendar",
    "quero agendar",
    "horarios disponiveis",
    "horario disponivel",
    "horario livre",
    "tem vaga",
    "visita tecnica",
    "visitar a loja",
    "ir na loja",
    "conhecer o showroom",
)

_DELIVERY_KEYWORDS: tuple[str, ...] = (
    "entrega",
    "entregam",
    "frete",
    "taxa de entrega",
    "prazo de entrega",
    "enviam para",
    "mandam para",
)

_BUSINESS_INFO_KEYWORDS: tuple[str, ...] = (
    "endereco",
    "onde fica",
    "localizacao",
    "horario de funcionamento",
    "que horas abre",
    "que horas fecha",
    "garantia",
    "forma de pagamento",
    "formas de pagamento",
    "aceita cartao",
    "aceitam cartao",
    "aceita pix",
)

_CATALOG_KEYWORDS: tuple[str, ...] = (
    "colchao",
    "colchoes",
    "mola",
    "molas",
    "ensacada",
    "ensacadas",
    "pocket",
    "espuma",
    "ortopedico",
    "casal",
    "queen",
    "king",
    "solteiro",
    "cama box",
    "piscina",
    "piscinas",
    "fibra",
    "aquecedor",
    "psicoterapia",
    "terapia",
    "psicologo",
    "psicologa",
    "neuropsicologica",
    "neuropsicologia",
    "avaliacao",
    "tdah",
    "autismo",
    "laudo",
    "preco",
    "precos",
    "valor",
    "valores",
    "quanto custa",
    "orcamento",
    "catalogo",
    "parcela",
    "parcelado",
    # Agência (tenant `loboads`)
    "trafego",
    "anuncio",
    "meta ads",
    "google ads",
    "sales agent",
    "agente de ia",
    "automacao",
    "chatbot",
)

_CITY_MAP: dict[str, str] = {
    "belem": "Belém",
    "ananindeua": "Ananindeua",
    "marituba": "Marituba",
    "benevides": "Benevides",
    "santa barbara": "Santa Bárbara do Pará",
    "castanhal": "Castanhal",
    "manaus": "Manaus",
}

_BRL_REGEX = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})|\d+(?:,\d{2})?)")


class JEVDecision(BaseModel):
    """Decisão rápida do System One antes de invocar o LLM principal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: AgentIntent = AgentIntent.DISCOVERY
    stage: ConversationStage = ConversationStage.DISCOVERY
    immediate_handoff: bool = False
    handoff_reason: HandoffReason | None = None
    summary: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    extracted_attributes: dict[str, str] = Field(default_factory=dict)


def _strip_quoted_header(text: str) -> str:
    """Remove o cabeçalho `[Lead respondeu citando...]` para priorizar o texto do próprio lead."""
    lines = [
        ln
        for ln in text.splitlines()
        if not ln.strip().startswith("[Lead respondeu citando")
    ]
    cleaned = "\n".join(lines).strip()
    return cleaned or text


def extract_lead_attributes(text: str, segment: Segment = Segment.GENERIC) -> dict[str, str]:
    """Extrai atributos comerciais estruturados do texto do lead (determinístico)."""
    unquoted = _strip_quoted_header(text)
    norm = f" {normalize_text(unquoted)} "
    attrs: dict[str, str] = {}

    # Cidades
    for token, canonical_city in _CITY_MAP.items():
        if f" {token} " in norm:
            attrs["cidade"] = canonical_city
            break

    # Tamanho / tecnologia de colchão
    for size, aliases in (
        ("casal", ("casal", "138x188")),
        ("queen", ("queen", "158x198")),
        ("king", ("king", "193x203")),
        ("solteiro", ("solteiro", "88x188")),
    ):
        if any(f" {a} " in norm for a in aliases):
            attrs["tamanho"] = size
            break

    if any(t in norm for t in (" mola ", " molas ", " pocket ", " ensacada ", " ensacadas ")):
        attrs["tecnologia"] = "molas ensacadas"
    elif " ortopedico " in norm or " ortopedica " in norm:
        attrs["tecnologia"] = "ortopédico"
    elif " espuma " in norm or " d33 " in norm:
        attrs["tecnologia"] = "espuma"

    # Piscinas
    for dim, label in (("6x3", "6x3m"), ("6 x 3", "6x3m"), ("7x3", "7x3,5m"), ("8x4", "8x4m")):
        if dim in norm:
            attrs["medidas_interesse"] = label
            break

    # Psicologia
    if segment is Segment.PSYCHOLOGY or any(k in norm for k in (" terapia ", " psicoterapia ", " neuropsicologica ")):
        if any(k in norm for k in (" infantil ", " crianca ", " filho ", " filha ")):
            attrs["publico"] = "infantil"
        elif any(k in norm for k in (" adulto ", " adulta ", " para mim ")):
            attrs["publico"] = "adulto"
        if any(k in norm for k in (" neuropsicologica ", " neuropsicologia ", " tdah ", " autismo ", " tea ", " laudo ")):
            attrs["modalidade_interesse"] = "avaliação neuropsicológica"
        elif any(k in norm for k in (" terapia ", " psicoterapia ", " sessao ", " psicologo ", " psicologa ")):
            attrs["modalidade_interesse"] = "psicoterapia"

    return attrs


class JEVGuard:
    """Motor rápido de decisão e segurança (System One)."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def analyze(self, text: str, *, segment: Segment = Segment.GENERIC) -> JEVDecision:
        """Classifica a mensagem do turno e identifica gatilhos imediatos de handoff."""
        norm = f" {normalize_text(text)} "
        attrs = extract_lead_attributes(text, segment)

        # 1. Emergência clínica (prioridade máxima absoluta)
        for pattern in _EMERGENCY_PATTERNS:
            if pattern in norm:
                logger.warning("JEV detectou possível emergência clínica pattern=%s", pattern)
                return JEVDecision(
                    intent=AgentIntent.CLINICAL_EMERGENCY,
                    stage=ConversationStage.HANDOFF,
                    immediate_handoff=True,
                    handoff_reason=HandoffReason.CLINICAL_EMERGENCY,
                    summary="Lead apresentou sinais de crise aguda / risco à integridade física.",
                    confidence=1.0,
                    extracted_attributes=attrs,
                )

        # 2. Pedido explícito de atendente humano
        for pattern in _HUMAN_REQUEST_PATTERNS:
            if pattern in norm:
                return JEVDecision(
                    intent=AgentIntent.HUMAN_REQUEST,
                    stage=ConversationStage.HANDOFF,
                    immediate_handoff=True,
                    handoff_reason=HandoffReason.CUSTOMER_REQUEST,
                    summary="Lead solicitou falar diretamente com um atendente humano.",
                    confidence=1.0,
                    extracted_attributes=attrs,
                )

        # 3. Negociação de desconto fora da tabela autorizada
        for pattern in _DISCOUNT_BARGAIN_PATTERNS:
            if pattern in norm:
                return JEVDecision(
                    intent=AgentIntent.DISCOUNT_NEGOTIATION,
                    stage=ConversationStage.HANDOFF,
                    immediate_handoff=True,
                    handoff_reason=HandoffReason.DISCOUNT_NEGOTIATION,
                    summary="Lead solicitou desconto ou condição comercial especial fora da tabela.",
                    confidence=0.95,
                    extracted_attributes=attrs,
                )

        # 4. Roteamento de estágio comercial
        has_booking = any(k in norm for k in _BOOKING_KEYWORDS)
        has_catalog = any(f" {k} " in norm or k in norm for k in _CATALOG_KEYWORDS)
        has_delivery = any(k in norm for k in _DELIVERY_KEYWORDS)
        has_biz_info = any(k in norm for k in _BUSINESS_INFO_KEYWORDS)

        if has_booking and not has_catalog:
            return JEVDecision(
                intent=AgentIntent.BOOKING,
                stage=ConversationStage.BOOKING,
                summary="Lead demonstrou intenção de agendar horário/visita.",
                confidence=0.9,
                extracted_attributes=attrs,
            )

        if has_catalog:
            return JEVDecision(
                intent=AgentIntent.CATALOG_PRICE,
                stage=ConversationStage.OFFER_CATALOG,
                summary="Lead consultou catálogo, produto/serviço ou preço.",
                confidence=0.9,
                extracted_attributes=attrs,
            )

        if has_delivery:
            return JEVDecision(
                intent=AgentIntent.DELIVERY,
                stage=ConversationStage.OFFER_CATALOG,
                summary="Lead perguntou sobre entrega, frete ou prazo.",
                confidence=0.9,
                extracted_attributes=attrs,
            )

        if has_biz_info:
            return JEVDecision(
                intent=AgentIntent.BUSINESS_INFO,
                stage=ConversationStage.OFFER_CATALOG,
                summary="Lead consultou informações institucionais (horário, endereço, pagamento, garantia).",
                confidence=0.85,
                extracted_attributes=attrs,
            )

        return JEVDecision(
            intent=AgentIntent.DISCOVERY,
            stage=ConversationStage.DISCOVERY,
            summary="Triagem inicial e descoberta da necessidade do lead.",
            confidence=0.8,
            extracted_attributes=attrs,
        )


# --------------------------------------------------------------------------- #
# Guardrail Pós-LLM: Verificador Anti-Alucinação de Preços
# --------------------------------------------------------------------------- #
def _normalize_brl_token(raw: str) -> str:
    """Normaliza número monetário (`2.899,00` -> `2899,00`; `2899` -> `2899,00`)."""
    clean = raw.strip().replace(".", "")
    if "," not in clean:
        return f"{clean},00"
    integer, _, cents = clean.partition(",")
    return f"{integer},{cents.ljust(2, '0')[:2]}"


def _collect_strings(obj: Any, bucket: list[str]) -> None:
    if isinstance(obj, str):
        bucket.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, bucket)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            _collect_strings(item, bucket)


def extract_authorized_prices(executed_tools: list[ToolResult]) -> set[str]:
    """Extrai todos os valores monetários (`R$ ...`) retornados pelas ferramentas determinísticas."""
    authorized: set[str] = set()
    strings: list[str] = []
    for res in executed_tools:
        if res.ok and res.data is not None:
            _collect_strings(res.to_llm_payload().get("data"), strings)
    for text in strings:
        for match in _BRL_REGEX.findall(text):
            authorized.add(_normalize_brl_token(match))
    return authorized


def verify_no_unbacked_prices(
    reply_text: str | None,
    executed_tools: list[ToolResult],
) -> tuple[bool, list[str]]:
    """Verifica se todo `R$ <valor>` no texto de saída veio de uma ferramenta determinística.

    Retorna `(True, [])` se seguro, ou `(False, [valores_alucinados])` se houver violação.
    """
    if not reply_text:
        return True, []
    mentioned = _BRL_REGEX.findall(reply_text)
    if not mentioned:
        return True, []

    authorized = extract_authorized_prices(executed_tools)
    unbacked: list[str] = []
    for raw in mentioned:
        norm = _normalize_brl_token(raw)
        if norm not in authorized:
            unbacked.append(f"R$ {raw}")
    return len(unbacked) == 0, unbacked


__all__ = [
    "JEVDecision",
    "JEVGuard",
    "extract_authorized_prices",
    "extract_lead_attributes",
    "verify_no_unbacked_prices",
]

