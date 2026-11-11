"""Modelos ORM SQLAlchemy 2.0 (Tenant, Conversation, MessageLog) para PostgreSQL.

Cumpre `MASTER_KICKOFF_PROMPT.md` §3 e `ARCHITECTURE.md` §6.3:
- Isolamento multi-tenant (`tenant_id` indexado em todas as tabelas).
- Auditoria de cada turno (`MessageLog` com `tool_calls_json`, `jev_decision_json` e `latency_ms`).
- LGPD: `repr()` de `Conversation` e `MessageLog` mascara o telefone do lead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.core.schemas import LeadStatus, Segment, mask_phone, utcnow


class Base(DeclarativeBase):
    """Base declarativa SQLAlchemy 2.0."""


class Tenant(Base):
    """Registro de cliente da agência (multi-tenant)."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    segment: Mapped[str] = mapped_column(String(32), default=Segment.GENERIC.value, nullable=False)
    whatsapp_provider: Mapped[str] = mapped_column(String(32), default="waha", nullable=False)
    crm_provider: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Sao_Paulo", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    conversations: Mapped[list[Conversation]] = relationship(back_populates="tenant", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Tenant tenant_id={self.tenant_id!r} segment={self.segment!r} active={self.active}>"


class Conversation(Base):
    """Conversa e perfil consolidado do lead, isolado por `tenant_id`."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "contact_phone", name="uq_conversation_tenant_phone"),
        Index("ix_conversation_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    contact_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=LeadStatus.NEW.value, nullable=False)
    human_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    crm_contact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    crm_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    attributes_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_interaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="conversations")
    messages: Mapped[list[MessageLog]] = relationship(back_populates="conversation", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return (
            f"<Conversation tenant_id={self.tenant_id!r} "
            f"contact={mask_phone(self.contact_phone)!r} status={self.status!r}>"
        )


class MessageLog(Base):
    """Log de auditoria de mensagens, chamadas de ferramentas e decisões do JEV."""

    __tablename__ = "message_logs"
    __table_args__ = (
        Index("ix_message_logs_tenant_turn", "tenant_id", "turn_id"),
        Index("ix_message_logs_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)  # "inbound" | "outbound"
    contact_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)

    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tool_calls_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    jev_decision_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    conversation: Mapped[Conversation | None] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return (
            f"<MessageLog tenant_id={self.tenant_id!r} direction={self.direction!r} "
            f"contact={mask_phone(self.contact_phone)!r} turn_id={self.turn_id!r}>"
        )


__all__ = ["Base", "Conversation", "MessageLog", "Tenant"]

