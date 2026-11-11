"""Gerenciador de conexão e sessão assíncrona do PostgreSQL (SQLAlchemy 2.0 + asyncpg).

Suporta criação de engine sob demanda a partir de `Settings.async_database_url`
e converte falhas de banco em `DatabaseError` (com `handoff_recommended=True`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import Settings, get_settings
from src.core.exceptions import ConfigurationError, DatabaseError
from src.database.models import Base

logger = logging.getLogger("lobo.database")


class DatabaseManager:
    """Encapsula `AsyncEngine` e `async_sessionmaker` para acesso multi-tenant seguro."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        if not database_url:
            raise ConfigurationError("DATABASE_URL não configurada")
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None, *, echo: bool = False) -> DatabaseManager:
        s = settings or get_settings()
        if not s.async_database_url:
            raise ConfigurationError("DATABASE_URL não configurada no ambiente")
        return cls(s.async_database_url, echo=echo)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Fornece `AsyncSession` com commit/rollback automático e tradução de exceção."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                logger.error("Erro SQLAlchemy (%s)", type(exc).__name__)
                raise DatabaseError(f"falha no banco de dados: {type(exc).__name__}") from exc
            except Exception:
                await session.rollback()
                raise

    async def create_tables(self) -> None:
        try:
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        except SQLAlchemyError as exc:
            raise DatabaseError(f"falha ao criar tabelas: {type(exc).__name__}") from exc

    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def aclose(self) -> None:
        await self._engine.dispose()


__all__ = ["DatabaseManager"]

