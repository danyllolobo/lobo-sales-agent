"""Conector GoHighLevel (LeadConnector API v2) — Vizzu Psicologia.

Base: https://services.leadconnectorhq.com
Headers: `Authorization: Bearer <Private Integration Token>`, `Version: 2021-07-28`
- POST   /contacts/upsert          localizar/criar contato por telefone (por location)
- POST   /contacts/{id}/notes      anotação interna
- POST   /contacts/{id}/tags       adicionar tags (não remove as existentes)
- DELETE /contacts/{id}/tags       remover tags
- GET    /contacts/{id}            tags atuais (detecção de humano ativo)
- PUT    /contacts/{id}            atribuir responsável no handoff (`assignedTo`)
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import SecretStr

from src.core.exceptions import CRMError
from src.core.schemas import CRMProvider, LeadProfile, StandardTag
from src.crm.base import CRMConnector

GHL_BASE_URL = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"


class GHLConnector(CRMConnector):
    provider: ClassVar[CRMProvider] = CRMProvider.GHL

    def __init__(
        self,
        api_token: SecretStr | str,
        location_id: str,
        *,
        base_url: str = GHL_BASE_URL,
        handoff_user_id: str | None = None,
        source: str = "Lobo AI Sales Agent",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url, timeout=timeout, client=client)
        if not location_id:
            raise ValueError("GHLConnector: location_id é obrigatório")
        self._token = api_token.get_secret_value() if isinstance(api_token, SecretStr) else api_token
        self.location_id = location_id
        self.handoff_user_id = handoff_user_id
        self.source = source

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Version": GHL_API_VERSION,
            "Accept": "application/json",
        }

    async def ensure_contact(self, lead: LeadProfile) -> str:
        if lead.crm_contact_id:
            return lead.crm_contact_id
        body: dict[str, Any] = {"locationId": self.location_id, "phone": f"+{lead.phone}", "source": self.source}
        if lead.name:
            body["name"] = lead.name
        resp = await self._request("POST", "/contacts/upsert", lead=lead, json=body)
        contact = resp.get("contact") if isinstance(resp, dict) else None
        if not isinstance(contact, dict) or not contact.get("id"):
            raise CRMError("GHL: resposta de upsert sem id de contato", tenant_id=lead.tenant_id)
        lead.crm_contact_id = str(contact["id"])
        return lead.crm_contact_id

    async def add_private_note(self, lead: LeadProfile, content: str) -> None:
        contact_id = await self.ensure_contact(lead)
        await self._request("POST", f"/contacts/{contact_id}/notes", lead=lead, json={"body": content})

    async def add_tags(self, lead: LeadProfile, tags: list[str]) -> list[str]:
        contact_id = await self.ensure_contact(lead)
        clean = self._clean_tags(tags)
        resp = await self._request("POST", f"/contacts/{contact_id}/tags", lead=lead, json={"tags": clean})
        result = resp.get("tags") if isinstance(resp, dict) and isinstance(resp.get("tags"), list) else clean
        for t in clean:
            lead.add_tag(t)
        return [str(t) for t in result]

    async def remove_tags(self, lead: LeadProfile, tags: list[str]) -> None:
        contact_id = await self.ensure_contact(lead)
        clean = self._clean_tags(tags)
        await self._request("DELETE", f"/contacts/{contact_id}/tags", lead=lead, json={"tags": clean})
        for t in clean:
            lead.remove_tag(t)

    async def is_human_active(self, lead: LeadProfile) -> bool:
        contact_id = await self.ensure_contact(lead)
        resp = await self._request("GET", f"/contacts/{contact_id}", lead=lead)
        contact = resp.get("contact", {}) if isinstance(resp, dict) else {}
        tags = [str(t).lower() for t in contact.get("tags") or []]
        return StandardTag.HUMAN_HANDOFF.value in tags

    async def escalate_to_human(self, lead: LeadProfile, note: str) -> None:
        await super().escalate_to_human(lead, note)
        if self.handoff_user_id:
            contact_id = await self.ensure_contact(lead)
            await self._request(
                "PUT", f"/contacts/{contact_id}", lead=lead, json={"assignedTo": self.handoff_user_id}
            )

