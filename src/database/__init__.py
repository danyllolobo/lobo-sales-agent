"""Camada de persistência PostgreSQL (SQLAlchemy 2.0 assíncrono)."""

from __future__ import annotations

from src.database.connection import DatabaseManager
from src.database.models import Base, Conversation, MessageLog, Tenant

__all__ = ["Base", "Conversation", "DatabaseManager", "MessageLog", "Tenant"]

