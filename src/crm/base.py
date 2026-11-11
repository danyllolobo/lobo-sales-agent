"""Interface abstrata dos conectores de CRM (Chatwoot, GoHighLevel...).

Operações mínimas para a FASE 1:
- garantir o contato (e cachear os ids no `LeadProfile`);
- anotação interna privada (resumo para o atendente);
- tags (`qualificado`, `visita_agendada`, `atendimento_humano`...);
- detectar se um humano já assumiu a conversa (para pausar a IA);
- escalar para humano (nota + tag + atribuição, conforme o CRM).

Falhas viram `CRMError` (handoff_recommended=True) com contexto mascarado.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx

from src.core.exceptions import CRMError
from src.core.http import request_json
from src.core.schemas import CRMProvider, LeadProfile, StandardTag, normalize_tag

logger = logging.getLogger("lobo.crm")


class CRMConnector(ABC):
    provider: ClassVar[CRMProvider]

    def __init__(self, base_url: str, *, timeout: float = 15.0, client: httpx.AsyncClient | None = None) -> None:
        if not base_url:
            raise ValueError(f"{type(self).__name__}: base_url é obrigatório")
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    # ------------------------------------------------------------------ #
    # Contrato
    # ------------------------------------------------------------------ #
    @abstractmethod
    async def ensure_contact(self, lead: LeadProfile) -> str:
        """Localiza/cria o contato e grava `lead.crm_contact_id`. Retorna o id."""

    @abstractmethod
    async def add_private_note(self, lead: LeadProfile, content: str) -> None:
        """Anotação interna, visível apenas para a equipe."""

    @abstractmethod
    async def add_tags(self, lead: LeadProfile, tags: list[str]) -> list[str]:
        """Adiciona tags (sem remover as existentes). Retorna as tags resultantes quando disponível."""

    @abstractmethod
    async def remove_tags(self, lead: LeadProfile, tags: list[str]) -> None: ...

    @abstractmethod
    async def is_human_active(self, lead: LeadProfile) -> bool:
        """True se um atendente humano assumiu a conversa (a IA deve silenciar)."""

    @abstractmethod
    def _headers(self) -> dict[str, str]: ...

    # ------------------------------------------------------------------ #
    # Comportamento comum
    # ------------------------------------------------------------------ #
    async def escalate_to_human(self, lead: LeadProfile, note: str) -> None:
        """Handoff padrão: nota privada + tag `atendimento_humano`. CRMs podem estender."""
        await self.add_private_note(lead, note)
        await self.add_tags(lead, [StandardTag.HUMAN_HANDOFF])

    @staticmethod
    def _clean_tags(tags: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_tag(t) for t in tags))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        lead: LeadProfile | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await request_json(
            self._client,
            method,
            f"{self.base_url}{path}",
            error_cls=CRMError,
            service=self.provider.value,
            path=path,
            headers=self._headers(),
            json=json,
            params=params,
            log_contact=lead.phone if lead else None,
            tenant_id=lead.tenant_id if lead else None,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

