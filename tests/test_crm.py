"""Testes dos conectores de CRM (`src/crm/`): Chatwoot e GoHighLevel. HTTP simulado."""

from __future__ import annotations

import json

import httpx
import pytest

from src.config import Settings
from src.core.exceptions import ConfigurationError, CRMError
from src.core.schemas import LeadProfile, StandardTag
from src.crm import ChatwootConnector, GHLConnector, create_crm
from src.tools.tenant_data import TenantData

PHONE = "5591988887777"
CW = "https://chat.example.com"
ACC = "/api/v1/accounts/7"


class FakeServer:
    """Roteia (método, caminho) → (status, json) e grava as requisições."""

    def __init__(self, routes: dict[tuple[str, str], object]):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"error": "not found"})
        route = self.routes[key]
        status, body = route if isinstance(route, tuple) else (200, route)
        return httpx.Response(status, json=body)

    def bodies(self, method: str, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.calls if r.method == method and r.url.path == path]

    def called(self, method: str, path: str) -> int:
        return sum(1 for r in self.calls if r.method == method and r.url.path == path)


def lead(**kw) -> LeadProfile:
    data = {"tenant_id": "companhia-dos-sonhos", "phone": PHONE, "name": "Maria"}
    data.update(kw)
    return LeadProfile(**data)


def chatwoot(server: FakeServer, **kw) -> ChatwootConnector:
    kw.setdefault("account_id", 7)
    kw.setdefault("inbox_id", 3)
    return ChatwootConnector(CW, "cw-token", client=httpx.AsyncClient(transport=httpx.MockTransport(server)), **kw)


def cw_routes(extra: dict | None = None) -> dict:
    routes = {
        ("GET", f"{ACC}/contacts/search"): {"payload": [{"id": 11, "phone_number": "+5591900001111"}, {"id": 42, "phone_number": f"+{PHONE}"}]},
        ("GET", f"{ACC}/contacts/42/conversations"): {
            "payload": [
                {"id": 100, "inbox_id": 3, "status": "resolved"},
                {"id": 150, "inbox_id": 9, "status": "open"},
                {"id": 120, "inbox_id": 3, "status": "pending"},
            ]
        },
        ("POST", f"{ACC}/conversations/120/messages"): {"id": 1},
        ("GET", f"{ACC}/conversations/120/labels"): {"payload": ["lead_meta"]},
        ("POST", f"{ACC}/conversations/120/labels"): {"payload": []},
        ("POST", f"{ACC}/conversations/120/toggle_status"): {"payload": {"success": True}},
        ("POST", f"{ACC}/conversations/120/assignments"): {"id": 5},
        ("GET", f"{ACC}/conversations/120"): {"id": 120, "labels": [], "meta": {"assignee": None}},
    }
    routes.update(extra or {})
    return routes


# =========================================================================== #
# Chatwoot
# =========================================================================== #
async def test_chatwoot_finds_contact_and_best_conversation():
    server = FakeServer(cw_routes())
    cw, ld = chatwoot(server), lead()
    assert await cw.ensure_conversation(ld) == "120"  # inbox 3, não resolvida, mais recente
    assert ld.crm_contact_id == "42" and ld.crm_conversation_id == "120"
    search = server.calls[0]
    assert search.headers["api_access_token"] == "cw-token"
    assert search.url.params["q"] == PHONE

    await cw.ensure_conversation(ld)  # ids cacheados no lead: sem novas chamadas
    assert len(server.calls) == 2


async def test_chatwoot_private_note():
    server = FakeServer(cw_routes())
    await chatwoot(server).add_private_note(lead(), "Resumo do atendimento")
    assert server.bodies("POST", f"{ACC}/conversations/120/messages") == [
        {"content": "Resumo do atendimento", "message_type": "outgoing", "private": True}
    ]


async def test_chatwoot_tags_are_merged_not_replaced():
    server = FakeServer(cw_routes())
    ld = lead()
    merged = await chatwoot(server).add_tags(ld, ["Qualificado", "lead_meta"])
    assert merged == ["lead_meta", "qualificado"]
    assert server.bodies("POST", f"{ACC}/conversations/120/labels") == [{"labels": ["lead_meta", "qualificado"]}]
    assert ld.has_tag("qualificado")


async def test_chatwoot_no_label_post_when_nothing_changes():
    server = FakeServer(cw_routes())
    await chatwoot(server).add_tags(lead(), ["lead_meta"])
    assert server.called("POST", f"{ACC}/conversations/120/labels") == 0


async def test_chatwoot_remove_tags():
    routes = cw_routes({("GET", f"{ACC}/conversations/120/labels"): {"payload": ["lead_meta", "atendimento_humano"]}})
    server = FakeServer(routes)
    await chatwoot(server).remove_tags(lead(), ["Atendimento Humano"])
    assert server.bodies("POST", f"{ACC}/conversations/120/labels") == [{"labels": ["lead_meta"]}]


async def test_chatwoot_escalate_reopens_and_assigns_team_then_agent():
    server = FakeServer(cw_routes())
    cw = chatwoot(server, handoff_team_id=2, handoff_assignee_id=8)
    await cw.escalate_to_human(lead(), "nota")
    assert server.bodies("POST", f"{ACC}/conversations/120/messages")[0]["private"] is True
    assert server.bodies("POST", f"{ACC}/conversations/120/labels") == [{"labels": ["lead_meta", "atendimento_humano"]}]
    assert server.bodies("POST", f"{ACC}/conversations/120/toggle_status") == [{"status": "open"}]
    assert server.bodies("POST", f"{ACC}/conversations/120/assignments") == [{"team_id": 2}, {"assignee_id": 8}]


@pytest.mark.parametrize(
    ("conversation", "kwargs", "expected"),
    [
        ({"labels": [], "meta": {"assignee": None}}, {}, False),
        ({"labels": ["atendimento_humano"], "meta": {}}, {}, True),
        ({"labels": [], "meta": {"assignee": {"id": 8}}}, {}, True),
        ({"labels": [], "meta": {"assignee": {"id": 99}}}, {"bot_agent_ids": [99]}, False),  # bot não conta
        ({"labels": [], "meta": {"assignee": {"id": 8}}}, {"assignee_means_human": False}, False),  # auto-atribuição
        ({"labels": ["atendimento_humano"], "meta": {}}, {"assignee_means_human": False}, True),
    ],
)
async def test_chatwoot_is_human_active(conversation, kwargs, expected):
    server = FakeServer(cw_routes({("GET", f"{ACC}/conversations/120"): {"id": 120, **conversation}}))
    assert await chatwoot(server, **kwargs).is_human_active(lead()) is expected


async def test_chatwoot_creates_contact_and_conversation_when_missing():
    routes = {
        ("GET", f"{ACC}/contacts/search"): {"payload": []},
        ("POST", f"{ACC}/contacts"): {"payload": {"contact": {"id": 77}, "contact_inbox": {"source_id": "x"}}},
        ("GET", f"{ACC}/contacts/77/conversations"): {"payload": []},
        ("POST", f"{ACC}/contacts/77/contact_inboxes"): {"source_id": "src-abc"},
        ("POST", f"{ACC}/conversations"): {"id": 555},
    }
    server = FakeServer(routes)
    ld = lead()
    assert await chatwoot(server).ensure_conversation(ld) == "555"
    assert server.bodies("POST", f"{ACC}/contacts") == [{"name": "Maria", "phone_number": f"+{PHONE}", "inbox_id": 3}]
    assert server.bodies("POST", f"{ACC}/conversations") == [
        {"source_id": "src-abc", "inbox_id": 3, "contact_id": 77, "status": "pending"}
    ]


async def test_chatwoot_without_create_missing_raises():
    server = FakeServer({("GET", f"{ACC}/contacts/search"): {"payload": []}})
    with pytest.raises(CRMError, match="contato não encontrado"):
        await chatwoot(server, create_missing=False).ensure_contact(lead())


async def test_chatwoot_http_error_is_masked():
    server = FakeServer({("GET", f"{ACC}/contacts/search"): (401, {"error": "unauthorized"})})
    with pytest.raises(CRMError) as exc_info:
        await chatwoot(server).ensure_contact(lead())
    err = exc_info.value
    assert err.handoff_recommended and err.context["status_code"] == 401
    assert PHONE not in str(err.to_dict()) and err.tenant_id == "companhia-dos-sonhos"


# =========================================================================== #
# GoHighLevel
# =========================================================================== #
GHL = "https://services.leadconnectorhq.com"


def ghl(server: FakeServer, **kw) -> GHLConnector:
    return GHLConnector("pit-token", "loc-123", client=httpx.AsyncClient(transport=httpx.MockTransport(server)), **kw)


def ghl_routes(extra: dict | None = None) -> dict:
    routes = {
        ("POST", "/contacts/upsert"): {"new": False, "contact": {"id": "c-9"}},
        ("POST", "/contacts/c-9/notes"): {"note": {"id": "n1"}},
        ("POST", "/contacts/c-9/tags"): {"tags": ["lead_meta", "qualificado"]},
        ("DELETE", "/contacts/c-9/tags"): {"tags": []},
        ("GET", "/contacts/c-9"): {"contact": {"id": "c-9", "tags": ["lead_meta"]}},
        ("PUT", "/contacts/c-9"): {"contact": {"id": "c-9"}},
    }
    routes.update(extra or {})
    return routes


async def test_ghl_upsert_contact_with_headers():
    server = FakeServer(ghl_routes())
    ld = lead(tenant_id="vizzu")
    assert await ghl(server).ensure_contact(ld) == "c-9"
    req = server.calls[0]
    assert str(req.url).startswith(GHL)
    assert req.headers["Authorization"] == "Bearer pit-token"
    assert req.headers["Version"] == "2021-07-28"
    assert server.bodies("POST", "/contacts/upsert") == [
        {"locationId": "loc-123", "phone": f"+{PHONE}", "source": "Lobo AI Sales Agent", "name": "Maria"}
    ]


async def test_ghl_note_tags_and_remove():
    server = FakeServer(ghl_routes())
    ld = lead(tenant_id="vizzu")
    connector = ghl(server)
    await connector.add_private_note(ld, "Triagem: ansiedade, prefere online")
    tags = await connector.add_tags(ld, ["Qualificado"])
    await connector.remove_tags(ld, ["qualificado"])
    assert server.bodies("POST", "/contacts/c-9/notes") == [{"body": "Triagem: ansiedade, prefere online"}]
    assert server.bodies("POST", "/contacts/c-9/tags") == [{"tags": ["qualificado"]}]
    assert tags == ["lead_meta", "qualificado"]
    assert server.bodies("DELETE", "/contacts/c-9/tags") == [{"tags": ["qualificado"]}]
    assert server.called("POST", "/contacts/upsert") == 1  # id cacheado


async def test_ghl_human_active_and_escalate_with_assignment():
    server = FakeServer(ghl_routes({("GET", "/contacts/c-9"): {"contact": {"tags": ["Atendimento_Humano"]}}}))
    ld = lead(tenant_id="vizzu")
    connector = ghl(server, handoff_user_id="user-1")
    assert await connector.is_human_active(ld) is True
    await connector.escalate_to_human(ld, "nota de handoff")
    assert server.bodies("POST", "/contacts/c-9/tags") == [{"tags": ["atendimento_humano"]}]
    assert server.bodies("PUT", "/contacts/c-9") == [{"assignedTo": "user-1"}]


async def test_ghl_errors():
    with pytest.raises(CRMError, match="sem id"):
        await ghl(FakeServer({("POST", "/contacts/upsert"): {"new": True}})).ensure_contact(lead())
    with pytest.raises(CRMError, match="HTTP 429"):
        await ghl(FakeServer({("POST", "/contacts/upsert"): (429, {})})).ensure_contact(lead())


# =========================================================================== #
# Fábrica
# =========================================================================== #
def tenant(provider: str, crm: dict) -> TenantData:
    return TenantData.model_validate(
        {"tenant": {"tenant_id": "t1x", "display_name": "T", "crm_provider": provider}, "crm": crm}
    )


def test_create_crm_from_tenant_and_settings():
    s = Settings(
        _env_file=None,
        chatwoot_base_url="https://chat.example.com/",
        chatwoot_api_token="tok",
        ghl_vizzu_api_token="pit",
        ghl_vizzu_location_id="loc",
    )
    cw = create_crm(tenant("chatwoot", {"chatwoot": {"account_id": 4, "inbox_id": 2, "assignee_means_human": False}}), s)
    assert isinstance(cw, ChatwootConnector) and cw.account_id == 4 and cw.assignee_means_human is False
    g = create_crm(tenant("ghl", {"ghl": {"token_setting": "ghl_vizzu_api_token", "location_setting": "ghl_vizzu_location_id"}}), s)
    assert isinstance(g, GHLConnector) and g.location_id == "loc"
    assert create_crm(tenant("none", {}), s) is None


def test_create_crm_missing_credentials():
    s = Settings(_env_file=None)
    with pytest.raises(ConfigurationError, match="Chatwoot"):
        create_crm(tenant("chatwoot", {"chatwoot": {"account_id": 1}}), s)
    with pytest.raises(ConfigurationError, match="GHL_VIZZU_API_TOKEN"):
        create_crm(tenant("ghl", {"ghl": {"token_setting": "ghl_vizzu_api_token", "location_setting": "ghl_vizzu_location_id"}}), s)


def test_standard_tag_value():
    assert StandardTag.HUMAN_HANDOFF.value == "atendimento_humano"
