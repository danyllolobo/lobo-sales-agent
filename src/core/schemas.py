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
from typing import Annotated, Any, ClassVar
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
    PSYCHOLOGY = "psychology"  # Vizzu Psicologia
    MATTRESS = "mattress"  # Companhia dos Sonhos / Vitória Prime
    POOLS = "pools"  # Splash Piscinas
    GENERIC = "generic"


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

    @field_validator("text", "contact_name", "transcription")
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
                return self.text or ""
            case MessageType.AUDIO:
                return self.transcription or "[áudio sem transcrição]"
            case MessageType.IMAGE:
                return f"[imagem] {caption}" if caption else "[imagem enviada pelo cliente]"
            case MessageType.VIDEO:
                return f"[vídeo] {caption}" if caption else "[vídeo enviado pelo cliente]"
            case MessageType.DOCUMENT:
                name = self.media.filename if self.media and self.media.filename else "documento"
                return f"[documento: {name}] {caption or ''}".strip()
            case MessageType.STICKER:
                return "[figurinha]"
            case MessageType.LOCATION:
                loc = self.location
                label = (loc.address or loc.name) if loc else None
                return f"[localização] {label}" if label else "[localização compartilhada]"
            case _:
                return self.text or "[mensagem não suportada]"

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


# --------------------------------------------------------------------------- #
# Tenant
# --------------------------------------------------------------------------- #
class TenantConfig(BaseModel):
    """Configuração de um cliente da agência (multi-tenant)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: TenantId
    display_name: str = Field(min_length=1)
    segment: Segment = Segment.GENERIC
    whatsapp_provider: Provider = Provider.WAHA
    whatsapp_session_id: str | None = None
    crm_provider: CRMProvider = CRMProvider.NONE
    timezone: str = "America/Sao_Paulo"
    active: bool = True

    # Sobrescrevem os padrões globais de `Settings` quando definidos
    debounce_silence_seconds: float | None = Field(default=None, ge=1.0, le=8.0)
    debounce_max_window_seconds: float | None = Field(default=None, ge=2.0, le=30.0)

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
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
