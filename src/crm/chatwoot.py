"""Conector Chatwoot (Application API v1) — ChatCRM / Astrachat.

Endpoints (prefixo `/api/v1/accounts/{account_id}`; header `api_access_token`):
- GET  /contacts/search?q=            localizar contato pelo telefone
- POST /contacts                      criar contato
- GET  /contacts/{id}/conversations   conversas do contato
- POST /contacts/{id}/contact_inboxes + POST /conversations   criar conversa (inbox API)
- POST /conversations/{id}/messages   nota privada (`private: true`)
- GET/POST /conversations/{id}/labels tags (o POST SUBSTITUI a lista → fazemos merge)
- POST /conversations/{id}/toggle_status   reabrir (`open`) no handoff
- POST /conversations/{id}/assignments     atribuir time/agente no handoff
- GET  /conversations/{id}            atribuição atual (detecção de humano ativo)
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr

from src.core.exceptions import CRMError
from src.core.schemas import CRMProvider, LeadProfile, StandardTag
from src.crm.base import CRMConnector

_DIGITS = re.compile(r"\D")


def _payload(resp: Any) -> Any:
    return resp.get("payload", resp) if isinstance(resp, dict) else resp


class ChatwootConnector(CRMConnector):
    provider: ClassVar[CRMProvider] = CRMProvider.CHATWOOT

    def __init__(
        self,
        base_url: str,
        api_token: SecretStr | str,
        *,
        account_id: int,
        inbox_id: int | None = None,
        handoff_team_id: int | None = None,
        handoff_assignee_id: int | None = None,
        bot_agent_ids: list[int] | None = None,
        assignee_means_human: bool = True,
        create_missing: bool = True,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url, timeout=timeout, client=client)
        self._token = api_token.get_secret_value() if isinstance(api_token, SecretStr) else api_token
        self.account_id = account_id
        self.inbox_id = inbox_id
        self.handoff_team_id = handoff_team_id
        self.handoff_assignee_id = handoff_assignee_id
        self.bot_agent_ids = set(bot_agent_ids or [])
        self.assignee_means_human = assignee_means_human
        self.create_missing = create_missing

    def _headers(self) -> dict[str, str]:
        return {"api_access_token": self._token}

    def _p(self, path: str) -> str:
        return f"/api/v1/accounts/{self.account_id}{path}"

    # ------------------------------------------------------------------ #
    # Contato & conversa
    # ------------------------------------------------------------------ #
    async def ensure_contact(self, lead: LeadProfile) -> str:
        if lead.crm_contact_id:
            return lead.crm_contact_id
        found = _payload(await self._request("GET", self._p("/contacts/search"), lead=lead, params={"q": lead.phone}))
        for contact in found if isinstance(found, list) else []:
            if _DIGITS.sub("", str(contact.get("phone_number") or "")) == lead.phone:
                lead.crm_contact_id = str(contact["id"])
                return lead.crm_contact_id
        if not self.create_missing:
            raise CRMError("Chatwoot: contato não encontrado", tenant_id=lead.tenant_id)

        body: dict[str, Any] = {"name": lead.name or f"+{lead.phone}", "phone_number": f"+{lead.phone}"}
        if self.inbox_id:
            body["inbox_id"] = self.inbox_id
        created = _payload(await self._request("POST", self._p("/contacts"), lead=lead, json=body))
        contact = created.get("contact", created) if isinstance(created, dict) else {}
        if not contact.get("id"):
            raise CRMError("Chatwoot: resposta de criação de contato sem id", tenant_id=lead.tenant_id)
        lead.crm_contact_id = str(contact["id"])
        return lead.crm_contact_id

    async def ensure_conversation(self, lead: LeadProfile) -> str:
        if lead.crm_conversation_id:
            return lead.crm_conversation_id
        contact_id = await self.ensure_contact(lead)
        convs = _payload(await self._request("GET", self._p(f"/contacts/{contact_id}/conversations"), lead=lead))
        candidates = [
            c
            for c in (convs if isinstance(convs, list) else [])
            if self.inbox_id is None or c.get("inbox_id") == self.inbox_id
        ]
        if candidates:
            # prefere conversas não resolvidas; entre elas, a mais recente
            best = max(candidates, key=lambda c: (c.get("status") != "resolved", int(c.get("id", 0))))
            lead.crm_conversation_id = str(best["id"])
            return lead.crm_conversation_id

        if not (self.create_missing and self.inbox_id):
            raise CRMError("Chatwoot: conversa não encontrada para o contato", tenant_id=lead.tenant_id)
        ci = await self._request(
            "POST", self._p(f"/contacts/{contact_id}/contact_inboxes"), lead=lead, json={"inbox_id": self.inbox_id}
        )
        source_id = _payload(ci).get("source_id") if isinstance(_payload(ci), dict) else None
        if not source_id:
            raise CRMError("Chatwoot: não foi possível obter source_id do contact_inbox", tenant_id=lead.tenant_id)
        conv = await self._request(
            "POST",
            self._p("/conversations"),
            lead=lead,
            json={"source_id": source_id, "inbox_id": self.inbox_id, "contact_id": int(contact_id), "status": "pending"},
        )
        lead.crm_conversation_id = str(_payload(conv)["id"])
        return lead.crm_conversation_id

    # ------------------------------------------------------------------ #
    # Notas & tags
    # ------------------------------------------------------------------ #
    async def add_private_note(self, lead: LeadProfile, content: str) -> None:
        conv = await self.ensure_conversation(lead)
        await self._request(
            "POST",
            self._p(f"/conversations/{conv}/messages"),
            lead=lead,
            json={"content": content, "message_type": "outgoing", "private": True},
        )

    async def _labels(self, lead: LeadProfile) -> tuple[str, list[str]]:
        conv = await self.ensure_conversation(lead)
        current = _payload(await self._request("GET", self._p(f"/conversations/{conv}/labels"), lead=lead))
        return conv, [str(x) for x in current] if isinstance(current, list) else []

    async def add_tags(self, lead: LeadProfile, tags: list[str]) -> list[str]:
        conv, current = await self._labels(lead)
        merged = list(dict.fromkeys([*current, *self._clean_tags(tags)]))
        if merged != current:
            await self._request("POST", self._p(f"/conversations/{conv}/labels"), lead=lead, json={"labels": merged})
        for t in merged:
            lead.add_tag(t)
        return merged

    async def remove_tags(self, lead: LeadProfile, tags: list[str]) -> None:
        conv, current = await self._labels(lead)
        drop = set(self._clean_tags(tags))
        remaining = [t for t in current if t not in drop]
        if remaining != current:
            await self._request("POST", self._p(f"/conversations/{conv}/labels"), lead=lead, json={"labels": remaining})
        for t in drop:
            lead.remove_tag(t)

    # ------------------------------------------------------------------ #
    # Humano & handoff
    # ------------------------------------------------------------------ #
    async def is_human_active(self, lead: LeadProfile) -> bool:
        conv = await self.ensure_conversation(lead)
        data = await self._request("GET", self._p(f"/conversations/{conv}"), lead=lead)
        if not isinstance(data, dict):
            return False
        if StandardTag.HUMAN_HANDOFF.value in (data.get("labels") or []):
            return True
        if self.assignee_means_human:
            assignee = (data.get("meta") or {}).get("assignee") or {}
            assignee_id = assignee.get("id")
            if assignee_id is not None and int(assignee_id) not in self.bot_agent_ids:
                return True
        return False

    async def escalate_to_human(self, lead: LeadProfile, note: str) -> None:
        await super().escalate_to_human(lead, note)
        conv = await self.ensure_conversation(lead)
        await self._request("POST", self._p(f"/conversations/{conv}/toggle_status"), lead=lead, json={"status": "open"})
        # A API aplica apenas UM dos campos por chamada: time primeiro, depois agente.
        if self.handoff_team_id:
            await self._request(
                "POST", self._p(f"/conversations/{conv}/assignments"), lead=lead, json={"team_id": self.handoff_team_id}
            )
        if self.handoff_assignee_id:
            await self._request(
                "POST",
                self._p(f"/conversations/{conv}/assignments"),
                lead=lead,
                json={"assignee_id": self.handoff_assignee_id},
            )
