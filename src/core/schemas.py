"""Modelos Pydantic unificados (Mensagens, Leads e Tenants).

Estes schemas são a "língua franca" interna do sistema: todo payload de provedor
(WAHA, AstraCalls, Meta Cloud) é normalizado para `IncomingMessage`; toda resposta
sai como `OutgoingMessage`; o contexto do cliente vive em `LeadProfile`.

LGPD: modelos com telefone mascaram o número em `repr()`/`str()` e expõem
`log_context()` para logging seguro (sem conteúdo nem PII crua).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Tipos e utilitários
# --------------------------------------------------------------------------- #
def _clean_slug(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


TenantId = Annotated[
    str,
    BeforeValidator(_clean_slug),  # normaliza ANTES da checagem do pattern
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$"),
]
"""Identificador slug do tenant (ex.: `vizzu`, `companhia-dos-sonhos`)."""

_JID_SUFFIX = re.compile(r"[@:].*$")
_NON_DIGITS = re.compile(r"\D")
_TAG_SEPARATORS = re.compile(r"[\s\-]+")

WHATSAPP_TEXT_LIMIT = 65_536


def normalize_phone(value: str) -> str:
    """Normaliza telefone/JID do WhatsApp para dígitos E.164 sem `+`.

    Aceita: `+55 (91) 98888-7777`, `5591988887777@c.us`,
    `5591988887777:12@s.whatsapp.net` (JID com device do whatsmeow).
    """
    if not isinstance(value, str):
        raise TypeError("telefone deve ser string")
    raw = value.strip()
    if "@g.us" in raw:
        raise ValueError("JIDs de grupo não são suportados como contato")
    digits = _NON_DIGITS.sub("", _JID_SUFFIX.sub("", raw))
    if not 10 <= len(digits) <= 15:
        raise ValueError("telefone deve conter entre 10 e 15 dígitos (E.164)")
    return digits


def mask_phone(phone: str | None) -> str:
    """Anonimiza telefone para logs: `5591988887777` -> `55*******7777`."""
    if not phone:
        return "<sem-telefone>"
    digits = _NON_DIGITS.sub("", phone)
    if len(digits) <= 6:
        return "*" * len(digits)
    return f"{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"


def normalize_tag(tag: str) -> str:
    """`Visita Agendada` -> `visita_agendada`."""
    normalized = _TAG_SEPARATORS.sub("_", tag.strip().lower()).strip("_")
    if not normalized:
        raise ValueError("tag vazia")
    return normalized


def _ensure_utc(value: datetime) -> datetime:
    """Datas sem fuso são assumidas como UTC; todas são convertidas para UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class _PhoneMaskedModel(BaseModel):
    """Mascara campos de telefone em `repr()`/`str()` (LGPD)."""

    _phone_fields: ClassVar[tuple[str, ...]] = ()

    def __repr_args__(self):  # type: ignore[override]
        for key, value in super().__repr_args__():
            if key in self._phone_fields and isinstance(value, str):
                yield key, mask_phone(value)
            else:
                yield key, value


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Provider(StrEnum):
    WAHA = "waha"
    ASTRACALLS = "astracalls"
    META_CLOUD = "meta_cloud"
    SIMULATOR = "simulator"  # usado em testes/simulação local


class MessageType(StrEnum):
    TEXT = "text"
    AUDIO = "audio"
    IMAGE = "image"
    VIDEO = "video"
    DOCUMENT = "document"
    STICKER = "sticker"
    LOCATION = "location"
    UNKNOWN = "unknown"


MEDIA_MESSAGE_TYPES: frozenset[MessageType] = frozenset(
    {
        MessageType.AUDIO,
        MessageType.IMAGE,
        MessageType.VIDEO,
        MessageType.DOCUMENT,
        MessageType.STICKER,
    }
)
OUTGOING_MESSAGE_TYPES: frozenset[MessageType] = frozenset(
    {
        MessageType.TEXT,
        MessageType.AUDIO,
        MessageType.IMAGE,
        MessageType.VIDEO,
        MessageType.DOCUMENT,
    }
)


class LeadStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    QUALIFIED = "qualified"
    VISIT_SCHEDULED = "visit_scheduled"
    HUMAN_HANDOFF = "human_handoff"
    WON = "won"
    LOST = "lost"


class Segment(StrEnum):
    PSYCHOLOGY = "psychology"  # Clínicas de Psicologia / Saúde Mental (Vizzu)
    MATTRESS = "mattress"  # Lojas de Colchões / Estofados (Companhia dos Sonhos)
    POOLS = "pools"  # Piscinas / Lazer / Construção (Splash Piscinas)
    DENTAL = "dental"  # Clínicas Odontológicas / Harmonização
    AESTHETICS = "aesthetics"  # Clínicas de Estética / Dermatologia / Spa
    CLINIC = "clinic"  # Clínicas Médicas / Fisioterapia / Nutrição / Exames
    REAL_ESTATE = "real_estate"  # Imobiliárias / Construtoras / Corretores
    AUTOMOTIVE = "automotive"  # Concessionárias / Seminovos / Estética Automotiva
    EDUCATION = "education"  # Escolas / Faculdades / Cursos / Infoprodutos
    LAW = "law"  # Escritórios de Advocacia / Consultoria Jurídica
    RETAIL = "retail"  # Varejo Geral / Móveis / Eletro / Moda / Óticas
    GENERIC = "generic"  # B2B / Agências / Serviços / Qualquer outro nicho


PlanTier = Literal["starter", "pro", "enterprise"]
FollowUpStrategy = Literal["warm_check", "value_reinforce", "objection_break", "breakup"]

PLAN_SPECS: dict[str, dict[str, Any]] = {
    "starter": {
        "code": "starter",
        "name": "Plano Essencial",
        "subtitle": "Para consultórios e pequenas lojas",
        "price_brl": 790,
        "setup_brl": 2500,
        "max_whatsapp_numbers": 1,
        "monthly_turn_limit": 800,
        "max_catalog_items": 15,
        "followup_allowed": False,
        "max_followup_steps": 0,
        "crm_allowed": False,
        "external_api_allowed": False,
        "features": [
            "1 número de WhatsApp",
            "Até 800 conversas/mês",
            "Compreensão de áudios",
            "Catálogo de até 15 itens",
            "Horário noturno e fins de semana",
        ],
    },
    "pro": {
        "code": "pro",
        "name": "Plano Pro Vendas",
        "subtitle": "Para varejo, clínicas, colchões e piscinas",
        "price_brl": 1690,
        "setup_brl": 4500,
        "max_whatsapp_numbers": 2,
        "monthly_turn_limit": 2500,
        "max_catalog_items": None,
        "followup_allowed": True,
        "max_followup_steps": 4,
        "crm_allowed": True,
        "external_api_allowed": True,
        "features": [
            "1 a 2 números de WhatsApp",
            "Até 2.500 conversas/mês",
            "Compreensão nativa de áudios",
            "Catálogo completo ilimitado",
            "Inteligência de frete por bairro",
            "Régua de follow-up progressiva (até 4 etapas)",
            "Integração CRM (Chatwoot / GoHighLevel)",
        ],
    },
    "enterprise": {
        "code": "enterprise",
        "name": "Escala Enterprise",
        "subtitle": "Para franquias e redes com filiais",
        "price_brl": 2990,
        "setup_brl": None,
        "max_whatsapp_numbers": 4,
        "monthly_turn_limit": 6000,
        "max_catalog_items": None,
        "followup_allowed": True,
        "max_followup_steps": 6,
        "crm_allowed": True,
        "external_api_allowed": True,
        "features": [
            "Até 4 números / departamentos",
            "Até 6.000 conversas/mês",
            "Catálogo completo ilimitado",
            "Integração com ERP / Estoque / API Externa",
            "Régua de follow-up completa (até 6 etapas)",
            "Modelos de Raciocínio Avançado (Gemini Pro / GPT-4o)",
        ],
    },
}


class FollowUpStep(BaseModel):
    """Etapa individual da régua progressiva de follow-up comercial."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step: int = Field(default=1, ge=1, le=6)
    delay_minutes: int = Field(default=120, ge=1, le=43200)  # de 1 min (teste) até 30 dias (43.200 min)
    label: str = "Retomada"
    strategy: FollowUpStrategy = "warm_check"
    custom_message: str | None = None


def default_followup_steps() -> list[FollowUpStep]:
    """Cadência comercial progressiva padrão: 2h -> +1 dia -> +3 dias -> +7 dias."""
    return [
        FollowUpStep(
            step=1,
            delay_minutes=120,
            label="1º Toque — Mesmo Dia (Retomada Quente)",
            strategy="warm_check",
        ),
        FollowUpStep(
            step=2,
            delay_minutes=1440,
            label="2º Toque — Dia Seguinte (+1 dia)",
            strategy="value_reinforce",
        ),
        FollowUpStep(
            step=3,
            delay_minutes=4320,
            label="3º Toque — Reativação Consultiva (+3 dias)",
            strategy="objection_break",
        ),
        FollowUpStep(
            step=4,
            delay_minutes=10080,
            label="4º Toque — Despedida Sem Pressão (+7 dias)",
            strategy="breakup",
        ),
    ]


class CRMProvider(StrEnum):
    CHATWOOT = "chatwoot"
    GHL = "ghl"
    NONE = "none"


class StandardTag(StrEnum):
    """Tags canônicas aplicadas no CRM."""

    QUALIFIED = "qualificado"
    VISIT_SCHEDULED = "visita_agendada"
    HUMAN_HANDOFF = "atendimento_humano"


# --------------------------------------------------------------------------- #
# Mensagens
# --------------------------------------------------------------------------- #
class MediaAttachment(BaseModel):
    """Mídia anexada. Ao menos uma forma de obtenção é obrigatória."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str | None = None
    data_base64: str | None = Field(default=None, repr=False)
    provider_media_id: str | None = None
    mimetype: str | None = None
    filename: str | None = None
    caption: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _require_source(self) -> MediaAttachment:
        if not (self.url or self.data_base64 or self.provider_media_id):
            raise ValueError("mídia exige `url`, `data_base64` ou `provider_media_id`")
        return self


class GeoLocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str | None = None
    address: str | None = None


class IncomingMessage(_PhoneMaskedModel):
    """Mensagem recebida, neutra em relação ao provedor de WhatsApp."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    _phone_fields: ClassVar[tuple[str, ...]] = ("contact_phone",)

    message_id: str = Field(min_length=1)
    tenant_id: TenantId
    provider: Provider
    session_id: str = Field(min_length=1)
    contact_phone: str
    contact_name: str | None = None
    # JID nativo do provedor para responder exatamente no mesmo chat
    # (ex.: `5591988887777@c.us`). Evita problemas com o 9º dígito BR.
    chat_id: str | None = Field(default=None, repr=False)

    type: MessageType = MessageType.TEXT
    text: str | None = None
    media: MediaAttachment | None = None
    location: GeoLocation | None = None
    transcription: str | None = None
    quoted_message_id: str | None = None
    quoted_text: str | None = None
    # Origem "Click to WhatsApp" (anúncio Meta Ads): ctwaClid, sourceId, sourceUrl...
    ad_referral: dict[str, Any] | None = None

    from_me: bool = False
    timestamp: datetime = Field(default_factory=utcnow)
    raw_payload: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)

    @field_validator("contact_phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    @field_validator("text", "contact_name", "transcription", "quoted_text")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _validate_by_type(self) -> IncomingMessage:
        if self.type is MessageType.TEXT and not self.text:
            raise ValueError("mensagem de texto exige `text`")
        if self.type in MEDIA_MESSAGE_TYPES and self.media is None:
            raise ValueError(f"mensagem do tipo `{self.type}` exige `media`")
        if self.type is MessageType.LOCATION and self.location is None:
            raise ValueError("mensagem de localização exige `location`")
        return self

    @property
    def content_for_llm(self) -> str:
        """Representação textual da mensagem para o motor de IA."""
        caption = self.media.caption if self.media and self.media.caption else None
        match self.type:
            case MessageType.TEXT:
                base = self.text or ""
            case MessageType.AUDIO:
                base = self.transcription or "[áudio sem transcrição]"
            case MessageType.IMAGE:
                base = f"[imagem] {caption}" if caption else "[imagem enviada pelo cliente]"
            case MessageType.VIDEO:
                base = f"[vídeo] {caption}" if caption else "[vídeo enviado pelo cliente]"
            case MessageType.DOCUMENT:
                name = self.media.filename if self.media and self.media.filename else "documento"
                base = f"[documento: {name}] {caption or ''}".strip()
            case MessageType.STICKER:
                base = "[figurinha]"
            case MessageType.LOCATION:
                loc = self.location
                label = (loc.address or loc.name) if loc else None
                coords = f" (coordenadas: {loc.latitude:.6f}, {loc.longitude:.6f})" if loc else ""
                # Com endereço legível, ele basta; sem endereço, as coordenadas permitem geocodificação reversa
                base = f"[localização] {label}" if label else f"[localização compartilhada{coords}]"
            case _:
                base = self.text or "[mensagem não suportada]"

        if self.quoted_text:
            return f'[Lead respondeu citando a mensagem: "{self.quoted_text}"]\n{base}'
        return base

    @property
    def needs_transcription(self) -> bool:
        return self.type is MessageType.AUDIO and not self.transcription

    def with_transcription(self, transcription: str) -> IncomingMessage:
        """Retorna uma cópia (modelo imutável) com a transcrição preenchida."""
        return self.model_copy(update={"transcription": transcription.strip() or None})

    def log_context(self) -> dict[str, Any]:
        """Contexto seguro para logs: sem conteúdo e com telefone mascarado."""
        return {
            "message_id": self.message_id,
            "tenant_id": self.tenant_id,
            "provider": self.provider.value,
            "type": self.type.value,
            "contact": mask_phone(self.contact_phone),
            "content_length": len(self.content_for_llm),
            "from_me": self.from_me,
        }


class OutgoingMessage(_PhoneMaskedModel):
    """Resposta a ser despachada pelo gateway de WhatsApp."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    _phone_fields: ClassVar[tuple[str, ...]] = ("contact_phone",)

    tenant_id: TenantId
    provider: Provider
    session_id: str = Field(min_length=1)
    contact_phone: str
    chat_id: str | None = Field(default=None, repr=False)  # JID nativo de destino (opcional)

    type: MessageType = MessageType.TEXT
    text: str | None = Field(default=None, max_length=WHATSAPP_TEXT_LIMIT)
    media: MediaAttachment | None = None
    reply_to_message_id: str | None = None

    simulate_typing: bool = True
    typing_seconds: float | None = Field(default=None, ge=0, le=15)
    turn_id: str | None = None  # correlação com a auditoria do turno

    @field_validator("contact_phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @model_validator(mode="after")
    def _validate_by_type(self) -> OutgoingMessage:
        if self.type not in OUTGOING_MESSAGE_TYPES:
            raise ValueError(f"tipo de saída não suportado: `{self.type}`")
        if self.type is MessageType.TEXT and not self.text:
            raise ValueError("resposta de texto exige `text`")
        if self.type is not MessageType.TEXT and self.media is None:
            raise ValueError(f"resposta do tipo `{self.type}` exige `media`")
        return self

    @property
    def effective_typing_seconds(self) -> float:
        """Duração do "digitando..." — explícita ou estimada pelo tamanho do texto."""
        if not self.simulate_typing:
            return 0.0
        if self.typing_seconds is not None:
            return self.typing_seconds
        chars = len(self.text or "")
        return round(min(max(chars * 0.03, 1.0), 6.0), 2)

    def log_context(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "provider": self.provider.value,
            "type": self.type.value,
            "contact": mask_phone(self.contact_phone),
            "text_length": len(self.text or ""),
            "turn_id": self.turn_id,
        }

    @classmethod
    def text_reply(cls, to: IncomingMessage | MessageBatch, text: str, **kwargs: Any) -> OutgoingMessage:
        """Cria uma resposta de texto para o mesmo tenant/sessão/chat da mensagem recebida."""
        src = to.last_message if isinstance(to, MessageBatch) else to
        return cls(
            tenant_id=src.tenant_id,
            provider=src.provider,
            session_id=src.session_id,
            contact_phone=src.contact_phone,
            chat_id=src.chat_id,
            text=text,
            **kwargs,
        )


# --------------------------------------------------------------------------- #
# Lote do buffer (debounce)
# --------------------------------------------------------------------------- #
class FlushReason(StrEnum):
    SILENCE = "silence"  # janela de silêncio atingida
    MAX_WINDOW = "max_window"  # teto máximo de segurança atingido
    MANUAL = "manual"  # flush forçado


class MessageBatch(_PhoneMaskedModel):
    """Mensagens agrupadas de um mesmo contato, entregues como UM turno à IA."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    _phone_fields: ClassVar[tuple[str, ...]] = ("contact_phone",)

    tenant_id: TenantId
    session_id: str = Field(min_length=1)
    contact_phone: str
    messages: list[IncomingMessage] = Field(min_length=1)
    first_received_at: datetime
    flushed_at: datetime = Field(default_factory=utcnow)
    flush_reason: FlushReason = FlushReason.SILENCE

    @field_validator("contact_phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("first_received_at", "flushed_at")
    @classmethod
    def _validate_dates(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    @model_validator(mode="after")
    def _validate_isolation(self) -> MessageBatch:
        for m in self.messages:
            if m.tenant_id != self.tenant_id or m.contact_phone != self.contact_phone:
                raise ValueError("lote contém mensagens de outro tenant/contato")
        return self

    @property
    def last_message(self) -> IncomingMessage:
        return self.messages[-1]

    @property
    def provider(self) -> Provider:
        return self.last_message.provider

    @property
    def message_ids(self) -> list[str]:
        return [m.message_id for m in self.messages]

    @property
    def contact_name(self) -> str | None:
        return next((m.contact_name for m in reversed(self.messages) if m.contact_name), None)

    @property
    def combined_text(self) -> str:
        """Texto único do turno (uma linha por mensagem), na ordem de chegada."""
        return "\n".join(c for m in self.messages if (c := m.content_for_llm))

    @property
    def needs_transcription(self) -> bool:
        return any(m.needs_transcription for m in self.messages)

    @property
    def wait_seconds(self) -> float:
        return round((self.flushed_at - self.first_received_at).total_seconds(), 3)

    def log_context(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "contact": mask_phone(self.contact_phone),
            "size": len(self.messages),
            "types": [m.type.value for m in self.messages],
            "flush_reason": self.flush_reason.value,
            "wait_seconds": self.wait_seconds,
        }


# --------------------------------------------------------------------------- #
# Lead
# --------------------------------------------------------------------------- #
class LeadProfile(_PhoneMaskedModel):
    """Contexto comercial do cliente final, isolado por tenant."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid", str_strip_whitespace=True)
    _phone_fields: ClassVar[tuple[str, ...]] = ("phone",)

    tenant_id: TenantId
    phone: str
    name: str | None = None
    status: LeadStatus = LeadStatus.NEW
    tags: list[str] = Field(default_factory=list)
    human_active: bool = False

    source: str | None = None  # ex.: "meta_ads", "organico"
    crm_contact_id: str | None = None
    crm_conversation_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)  # dados descobertos (tamanho, cidade...)

    first_seen_at: datetime = Field(default_factory=utcnow)
    last_interaction_at: datetime = Field(default_factory=utcnow)

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: str) -> str:
        return normalize_phone(v)

    @field_validator("first_seen_at", "last_interaction_at")
    @classmethod
    def _validate_dates(cls, v: datetime) -> datetime:
        return _ensure_utc(v)

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, tags: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_tag(t) for t in tags))

    @property
    def ai_paused(self) -> bool:
        """IA deve silenciar se um humano assumiu a conversa."""
        return self.human_active or self.status is LeadStatus.HUMAN_HANDOFF

    @property
    def is_returning(self) -> bool:
        return self.status is not LeadStatus.NEW

    def has_tag(self, tag: str) -> bool:
        return normalize_tag(tag) in self.tags

    def add_tag(self, tag: str) -> None:
        if not self.has_tag(tag):
            self.tags = [*self.tags, tag]

    def remove_tag(self, tag: str) -> None:
        target = normalize_tag(tag)
        self.tags = [t for t in self.tags if t != target]

    def mark_human_handoff(self) -> None:
        self.human_active = True
        self.status = LeadStatus.HUMAN_HANDOFF
        self.add_tag(StandardTag.HUMAN_HANDOFF)

    def touch(self) -> None:
        self.last_interaction_at = utcnow()

    def log_context(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "contact": mask_phone(self.phone),
            "status": self.status.value,
            "tags": self.tags,
            "human_active": self.human_active,
        }


_LIST_LINE_RE = re.compile(r"^\s*(?:✅|•|[-*]\s+|\d+[.)]\s+|[1-9]️⃣)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_BROKEN_BOLD_RE = re.compile(r"\*{2,}([^*\n]+?)\*{1,}|\*{1,}([^*\n]+?)\*{2,}")
_EM_DASH_SURROUNDED_RE = re.compile(r"\s+—\s+")


def sanitize_whatsapp_formatting(text: str) -> str:
    """Converte negritos Markdown (`**texto**` ou `**texto*`) para o padrão nativo do WhatsApp (`*texto*`)
    e remove vícios visuais de IA (como travessões literários ` — ` no meio de frases).
    """
    if not text:
        return ""

    def _repl(match: re.Match[str]) -> str:
        inner = (match.group(1) or match.group(2) or "").strip()
        return f"*{inner}*" if inner else ""

    cleaned = _BROKEN_BOLD_RE.sub(_repl, text)
    cleaned = _EM_DASH_SURROUNDED_RE.sub(", ", cleaned)
    return cleaned


def _is_list_paragraph(para: str) -> bool:
    lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
    if not lines:
        return False
    return bool(_LIST_LINE_RE.match(lines[0]))


def _split_long_line_by_words(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks


def _split_long_block_by_sentences(block: str, max_chars: int) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(block.strip()) if s.strip()]
    if not sentences:
        return []
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_line_by_words(sentence, max_chars))
            continue
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def _split_paragraph(para: str, max_chars: int) -> list[str]:
    # Se for um bloco de lista semântica (✅, •, -), NUNCA corta a lista ao meio
    if _is_list_paragraph(para):
        return [para]

    if len(para) <= max_chars:
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        if len(lines) >= 3 and not _LIST_LINE_RE.match(lines[0]) and all(_LIST_LINE_RE.match(ln) for ln in lines[1:]):
            if len(para) > 240:
                return [lines[0], "\n".join(lines[1:])]
        return [para]

    if "\n" in para:
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        chunks: list[str] = []
        current = ""
        for line in lines:
            if len(line) > max_chars:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.extend(_split_long_block_by_sentences(line, max_chars))
                continue
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= max_chars:
                current = candidate
            else:
                chunks.append(current)
                current = line
        if current:
            chunks.append(current)
        return chunks

    return _split_long_block_by_sentences(para, max_chars)


def split_human_chunks(text: str, max_chars: int = 400, max_chunks: int = 4) -> list[str]:
    """Divide respostas longas em blocos semânticos naturais de WhatsApp.

    Regras de Chunking Semântico:
    - Se o texto contiver uma lista de produtos/serviços (`✅`, `•`, `-`, `1️⃣`),
      todos os itens da lista permanecem JUNTOS em um único bloco (nunca corta a lista ao meio!).
    - Divide entre:
      * Bloco 1: Acolhimento e resposta direta à dúvida.
      * Bloco 2: O card/lista de opções completo.
      * Bloco 3: Pergunta consultiva de fechamento / convite para a loja.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    max_chars = max(80, int(max_chars))
    max_chunks = max(1, int(max_chunks))

    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", cleaned) if p.strip()]

    # 1. Separa cabeçalho introdutório colado na primeira linha de uma lista (ex.: "Destaques:\n• Item 1\n• Item 2")
    expanded_paragraphs: list[str] = []
    for para in raw_paragraphs:
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        first_list_idx = next((i for i, ln in enumerate(lines) if _LIST_LINE_RE.match(ln)), -1)
        if first_list_idx > 0:
            intro_part = "\n".join(lines[:first_list_idx]).strip()
            list_part = "\n".join(lines[first_list_idx:]).strip()
            if intro_part:
                expanded_paragraphs.append(intro_part)
            if list_part:
                expanded_paragraphs.append(list_part)
        else:
            expanded_paragraphs.append(para)

    # 2. Agrupa parágrafos de lista consecutivos (`✅`, `•`, `-`) em um ÚNICO bloco indivisível,
    #    mantendo estritamente a ordem cronológica do texto (NUNCA reordena parágrafos!).
    grouped_paragraphs: list[str] = []
    for para in expanded_paragraphs:
        if _is_list_paragraph(para) and grouped_paragraphs and _is_list_paragraph(grouped_paragraphs[-1]):
            grouped_paragraphs[-1] = f"{grouped_paragraphs[-1]}\n\n{para}"
        else:
            grouped_paragraphs.append(para)

    has_semantic_list = any(_is_list_paragraph(p) for p in grouped_paragraphs)
    if has_semantic_list and len(grouped_paragraphs) > 1:
        # Agrupa parágrafos adjacentes de não-lista mantendo a sequência exata do texto:
        # Ex.: [Saudação + Intro] -> [Lista Completa de Produtos] -> [Fechamento Consultivo]
        ordered_segments: list[str] = []
        for para in grouped_paragraphs:
            if not ordered_segments:
                ordered_segments.append(para)
                continue
            prev_is_list = _is_list_paragraph(ordered_segments[-1])
            curr_is_list = _is_list_paragraph(para)
            if not prev_is_list and not curr_is_list:
                ordered_segments[-1] = f"{ordered_segments[-1]}\n\n{para}"
            else:
                ordered_segments.append(para)

        while len(ordered_segments) > max_chunks:
            # Se ultrapassar max_chunks, une os dois últimos blocos preservando a ordem
            ordered_segments[-2:] = [f"{ordered_segments[-2]}\n\n{ordered_segments[-1]}"]
        return ordered_segments

    chunks: list[str] = []
    for para in grouped_paragraphs:
        chunks.extend(_split_paragraph(para, max_chars))

    # Se houver mais partes que `max_chunks`, agrupa os blocos adjacentes menores que caibam em `max_chars`
    while len(chunks) > max_chunks:
        best_idx = -1
        best_len = max_chars + 1
        for i in range(len(chunks) - 1):
            if _is_list_paragraph(chunks[i]) or _is_list_paragraph(chunks[i + 1]):
                continue
            combined_len = len(chunks[i]) + 2 + len(chunks[i + 1])
            if combined_len <= max_chars and combined_len < best_len:
                best_len = combined_len
                best_idx = i
        if best_idx < 0:
            break
        chunks[best_idx : best_idx + 2] = [f"{chunks[best_idx]}\n\n{chunks[best_idx + 1]}"]

    return chunks


# --------------------------------------------------------------------------- #
# Validação e Extração de Nome Real de Cliente (Anti-Emoji / Anti-Abreviação WhatsApp)
# --------------------------------------------------------------------------- #
_ONLY_LETTERS_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ]+(?:\s+[A-Za-zÀ-ÖØ-öø-ÿ]+)?$")
_VOWELS_RE = re.compile(r"[aeiouáéíóúâêôãõàüAEIOUÁÉÍÓÚÂÊÔÃÕÀÜ]")
_REPEATED_CHAR_RE = re.compile(r"(.)\1\1", re.IGNORECASE)

_SUSPICIOUS_NAME_TOKENS: frozenset[str] = frozenset(
    {
        # Termos genéricos / sistema
        "lead",
        "cliente",
        "visitante",
        "contato",
        "usuario",
        "user",
        "admin",
        "teste",
        "test",
        "playground",
        "whatsapp",
        "aparelho",
        "numero",
        "desconhecido",
        "anonimo",
        "novo",
        "nova",
        # Termos empresariais / comerciais / nichos
        "loja",
        "clinica",
        "consultorio",
        "empresa",
        "comercial",
        "vendas",
        "atendimento",
        "suporte",
        "sac",
        "oficial",
        "grupo",
        "comunidade",
        "equipe",
        "time",
        "marketing",
        "digital",
        "agencia",
        "ads",
        "lobo",
        "loboads",
        "vizzu",
        "splash",
        "sonhos",
        "companhia",
        "ortobom",
        "piscinas",
        "piscina",
        "colchoes",
        "colchao",
        "moveis",
        "estetica",
        "odonto",
        "odontologia",
        "psicologia",
        "psicoterapia",
        "terapia",
        "reabilita",
        "tratabem",
        "imobiliaria",
        "construtora",
        "advocacia",
        "direito",
        "escola",
        "curso",
        "auto",
        "veiculos",
        "oficina",
        "delivery",
        "express",
        "pastel",
        "calcadao",
        "lanches",
        "restaurante",
        "mercado",
        "farmacia",
        "salao",
        "barbearia",
        "studio",
        "estudio",
        "spa",
        # Frases religiosas / familiares / status comuns em perfil de WhatsApp
        "deus",
        "jesus",
        "cristo",
        "senhor",
        "fiel",
        "gratidao",
        "abencoado",
        "abencoada",
        "ungido",
        "ungida",
        "amor",
        "paz",
        "vida",
        "familia",
        "mae",
        "mamae",
        "pai",
        "papai",
        "filho",
        "filha",
        "irmao",
        "irma",
        "tio",
        "tia",
        "status",
        "online",
        "ocupado",
        "disponivel",
        "esined",
        # Saudações, medidas, cidades e palavras de chat (evita falso-positivo ao extrair nome)
        "bom",
        "boa",
        "dia",
        "tarde",
        "noite",
        "ola",
        "opa",
        "oi",
        "tudo",
        "bem",
        "quero",
        "queria",
        "gostaria",
        "preciso",
        "procuro",
        "orcamento",
        "valor",
        "valores",
        "preco",
        "precos",
        "quanto",
        "custa",
        "tem",
        "ainda",
        "hoje",
        "amanha",
        "king",
        "queen",
        "casal",
        "solteiro",
        "molas",
        "espuma",
        "ortopedico",
        "fibra",
        "adulto",
        "infantil",
        "crianca",
        "sim",
        "nao",
        "obrigado",
        "obrigada",
        "valeu",
        "beleza",
        "show",
        "belem",
        "ananindeua",
        "marambaia",
        "tapana",
        "icoaraci",
        "umarizal",
        "castanhal",
        "paragominas",
    }
)

_EXPLICIT_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:meu\s+nome\s+[eé]|me\s+chamo|pode\s+me\s+chamar\s+de|aqui\s+[eé]\s+(?:o|a)|eu\s+sou\s+(?:o|a)|sou\s+(?:o|a))\s+([A-Za-zÀ-ÖØ-öø-ÿ]{3,20})(?:\s+([A-Za-zÀ-ÖØ-öø-ÿ]{3,20}))?\b",
        re.IGNORECASE,
    ),
)


def _ascii_lower(word: str) -> str:
    import unicodedata

    norm = unicodedata.normalize("NFKD", word)
    return "".join(ch for ch in norm if not unicodedata.combining(ch)).lower().strip()


def validate_human_name(raw_name: str | None) -> str | None:
    """Valida se `raw_name` é um nome humano real e confiável (descartando apelidos do WhatsApp
    com emojis, abreviações como `D Lobo`, frases como `Deus E Fiel` ou nomes de empresas).

    Retorna o primeiro nome capitalizado (ex.: `"Carlos"`) quando válido, ou `None` quando suspeito.
    """
    if not raw_name or not isinstance(raw_name, str):
        return None
    cleaned = " ".join(raw_name.strip().split())
    if not cleaned or len(cleaned) < 3 or len(cleaned) > 35:
        return None

    # Qualquer emoji, pontuação, dígito ou símbolo invalida o pushName imediatamente
    if not _ONLY_LETTERS_NAME_RE.fullmatch(cleaned):
        return None

    words = cleaned.split()
    if not words or len(words) > 2:
        return None

    for w in words:
        # Abreviações de 1 ou 2 letras (ex.: "D Lobo", "J Silva", "Do", "De", "E") são descartadas
        if len(w) < 3 or len(w) > 18:
            return None
        if not _VOWELS_RE.search(w) or _REPEATED_CHAR_RE.search(w):
            return None
        if _ascii_lower(w) in _SUSPICIOUS_NAME_TOKENS:
            return None

    return words[0].capitalize()


def extract_spoken_name(user_text: str, *, asked_for_name: bool = False) -> str | None:
    """Extrai o nome informado pelo próprio cliente durante a conversa no WhatsApp."""
    if not user_text or not isinstance(user_text, str):
        return None
    text = user_text.strip()
    if not text:
        return None

    # 1. Padrões explícitos ("Meu nome é Danyllo", "Me chamo Fernanda", "Sou o Carlos")
    for pattern in _EXPLICIT_NAME_PATTERNS:
        m = pattern.search(text)
        if m:
            first_word = m.group(1)
            validated = validate_human_name(first_word)
            if validated:
                return validated

    # 2. Resposta direta logo após o agente perguntar "Como posso te chamar?" / "Qual é o seu nome?"
    if asked_for_name:
        first_clause = re.split(r"[,.;!?\n\r]+", text, maxsplit=1)[0].strip()
        first_clause = re.sub(
            r"^(?:oi+|ol[aá]+|opa+|bom\s+dia|boa\s+tarde|boa\s+noite|[eé]\s+o|[eé]\s+a|sou\s+o|sou\s+a|sou|aqui\s+[eé]\s+o|aqui\s+[eé]\s+a|aqui\s+[eé]|meu\s+nome\s+[eé]|me\s+chamo)\s+",
            "",
            first_clause,
            flags=re.IGNORECASE,
        ).strip()
        tokens = first_clause.split()
        if 1 <= len(tokens) <= 2:
            candidate = validate_human_name(tokens[0])
            if candidate:
                return candidate

    return None


# --------------------------------------------------------------------------- #
# Tenant
# --------------------------------------------------------------------------- #
SUPPORTED_LLM_MODELS: tuple[str, ...] = (
    "default",
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-3-pro",
    "gemini-4-argon",
    "gpt-4o",
    "gpt-4o-mini",
)


class TenantConfig(BaseModel):
    """Configuração de um cliente da agência (multi-tenant)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: TenantId
    display_name: str = Field(min_length=1)
    segment: Segment = Segment.GENERIC
    whatsapp_provider: Provider = Provider.WAHA
    whatsapp_session_id: str | None = None
    webhook_secret: str | None = None
    crm_provider: CRMProvider = CRMProvider.NONE
    timezone: str = "America/Sao_Paulo"
    active: bool = True

    # Modelo de IA do cliente ("default" usa o padrão global; ou modelo específico)
    llm_model: str = Field(default="gemini-3.8-flash", min_length=1)

    # Personalização livre de nicho/categoria e instruções específicas da empresa
    niche_name: str | None = None
    # Camada 3 do System Prompt (DNA exclusivo do cliente)
    custom_instructions: str | None = None
    # Camada 2 do System Prompt: substitui o playbook padrão do segmento apenas para este cliente
    playbook_override: str | None = None

    # Plano comercial e franquia mensal de atendimentos/turnos
    plan_tier: PlanTier = "pro"
    monthly_turn_limit: int | None = Field(default=None, ge=100, le=1_000_000)

    # Configuração da Régua Progressiva de Follow-up para leads inativos
    followup_enabled: bool = False
    followup_delay_minutes: int = Field(default=120, ge=1, le=43200)
    followup_max_attempts: int = Field(default=4, ge=1, le=10)
    followup_steps: list[FollowUpStep] = Field(default_factory=default_followup_steps)

    # Conexão opcional com API Externa / ERP / Webhook de saída (Bling, Asaas, n8n, Make)
    external_webhook_url: str | None = None
    external_api_token: str | None = None

    # Sobrescrevem os padrões globais de `Settings` quando definidos
    debounce_silence_seconds: float | None = Field(default=None, ge=1.0, le=30.0)
    debounce_max_window_seconds: float | None = Field(default=None, ge=2.0, le=60.0)
    split_messages: bool = True

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"timezone inválido: {v}") from exc
        return v

    @model_validator(mode="after")
    def _validate_debounce(self) -> TenantConfig:
        s, m = self.debounce_silence_seconds, self.debounce_max_window_seconds
        if s is not None and m is not None and s >= m:
            raise ValueError("debounce_silence_seconds deve ser menor que debounce_max_window_seconds")
        if self.monthly_turn_limit is None:
            spec = PLAN_SPECS.get(self.plan_tier) or PLAN_SPECS["pro"]
            self.monthly_turn_limit = int(spec["monthly_turn_limit"])
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
