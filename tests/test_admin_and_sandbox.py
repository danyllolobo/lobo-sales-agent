"""ETAPA 5 — Trava de segurança (SandboxGuard), webhook protegido e API do painel administrativo."""

from __future__ import annotations

import functools
import json
import shutil
import time
from pathlib import Path

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from main import create_app
from src.config import PROJECT_ROOT, Settings
from src.core.sandbox import (
    EMPTY_TRIGGER_REPLACEMENT,
    RESET_CONFIRMATION_TEXT,
    SandboxAction,
    SandboxConfig,
    SandboxGuard,
)
from src.core.schemas import IncomingMessage, LeadProfile, MessageBatch, Provider, StandardTag
from src.gateways.base import SendResult
from src.gateways.waha import WahaGateway

LEAD = "5591988887777"
STRANGER = "5591911112222"
TENANTS_SRC = PROJECT_ROOT / "data" / "tenants"


# =========================================================================== #
# Helpers
# =========================================================================== #
def incoming(text: str | None, *, phone: str = LEAD, tenant: str = "loboads", msg_id: str = "m1") -> IncomingMessage:
    return IncomingMessage(
        message_id=msg_id, tenant_id=tenant, provider=Provider.WAHA, session_id="Danyllo",
        contact_phone=phone, text=text,
    )


def guard(*, environment: str = "development", path: Path | None = None, seed=(), redis=None) -> SandboxGuard:
    return SandboxGuard(
        redis if redis is not None else fakeredis.FakeAsyncRedis(decode_responses=True),
        environment=environment, config_path=path, seed_phones=seed,
    )


class RecordingWaha(WahaGateway):
    """WAHA real (parser/HMAC) com envio gravado em memória — nada sai para a rede."""

    def __init__(self) -> None:
        super().__init__("https://waha.example.com", "k", default_session="default")
        self.sent = []

    async def send_message(self, message):
        self.sent.append(message)
        return SendResult(provider=Provider.WAHA, message_id=f"sent_{len(self.sent)}")

    async def set_typing(self, session_id, to, typing=True):
        return None


def waha_payload(msg_id: str, body: str, *, phone: str = LEAD, from_me: bool = False) -> dict:
    payload = {
        "id": msg_id, "timestamp": 1762800000, "from": f"{phone}@c.us", "fromMe": from_me,
        "body": body, "hasMedia": False, "_data": {"notifyName": "Lead Teste"},
    }
    if from_me:
        payload["to"] = f"{phone}@c.us"
    return {"event": "message.any" if from_me else "message", "session": "default", "payload": payload}


@pytest.fixture
def tenants_dir(tmp_path: Path) -> Path:
    target = tmp_path / "tenants"
    shutil.copytree(TENANTS_SRC, target)
    return target


@pytest.fixture
def make_settings(tmp_path: Path, tenants_dir: Path):
    def _make(**kw) -> Settings:
        base = {
            "redis_backend": "fake",
            "waha_base_url": "https://waha.example.com",
            "waha_api_key": "k",
            "debounce_silence_seconds": 1.0,
            "debounce_max_window_seconds": 3.0,
            "tenant_data_dir": tenants_dir,
            "sandbox_config_path": tmp_path / "sandbox_config.json",
        }
        base.update(kw)
        return Settings(_env_file=None, **base)

    return _make


def run(client: TestClient, fn, *args, **kwargs):
    """Executa uma corrotina no event loop do app (objetos do app.state)."""
    return client.portal.call(functools.partial(fn, *args, **kwargs))


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.05)


# =========================================================================== #
# SandboxGuard (unitário)
# =========================================================================== #
async def test_unknown_contact_is_ignored_silently():
    d = await guard().evaluate(incoming("Oi, quanto custa?", phone=STRANGER))
    assert d.action is SandboxAction.IGNORE and d.reason == "sandbox_locked" and d.message is None


async def test_whitelisted_phone_is_allowed_without_keyword():
    g = guard(seed=["+55 (91) 97777-6666"])
    await g.save_config(SandboxConfig(allowed_phones=[LEAD]))
    d = await g.evaluate(incoming("Quero um orçamento"))
    assert d.action is SandboxAction.ALLOW and d.reason == "allowed_phone"
    assert d.message.text == "Quero um orçamento"
    seeded = await g.evaluate(incoming("oi", phone="5591977776666"))
    assert seeded.allowed  # número do .env (SANDBOX_ALLOWED_PHONES)


async def test_trigger_keyword_opens_30min_session_and_is_stripped():
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    g = guard(redis=redis)
    d = await g.evaluate(incoming("/lobo-sales-agent Quero saber o preço do colchão", phone=STRANGER))
    assert d.action is SandboxAction.ALLOW and d.session_started and d.reason == "trigger_keyword"
    assert d.message.text == "Quero saber o preço do colchão"

    ttl = await redis.ttl(f"lobo:sandbox:session:loboads:{STRANGER}")
    assert 30 * 60 - 5 <= ttl <= 30 * 60

    follow_up = await g.evaluate(incoming("E vocês entregam em Belém?", phone=STRANGER, msg_id="m2"))
    assert follow_up.allowed and follow_up.reason == "active_session"
    assert follow_up.message.text == "E vocês entregam em Belém?"

    other_tenant = await g.evaluate(incoming("oi", phone=STRANGER, tenant="vizzu"))
    assert other_tenant.action is SandboxAction.IGNORE  # sessão isolada por tenant


@pytest.mark.parametrize("text", ["/lobo", "/LOBO", "  /lobo-sales-agent  ", "/lobo-sales-agent!"])
async def test_keyword_alone_becomes_greeting(text):
    d = await guard().evaluate(incoming(text, phone=STRANGER))
    assert d.allowed and d.message.text == EMPTY_TRIGGER_REPLACEMENT


async def test_keyword_in_middle_is_removed_and_lookalikes_do_not_unlock():
    d = await guard().evaluate(incoming("Bom dia /lobo tudo bem?", phone=STRANGER))
    assert d.allowed and d.message.text == "Bom dia tudo bem?"
    for text in ["veja em site.com/lobo", "os /lobos uivam", "/lobo_sales"]:
        blocked = await guard().evaluate(incoming(text, phone=STRANGER))
        assert blocked.action is SandboxAction.IGNORE, text


async def test_stop_command_ends_session_and_relocks():
    g = guard()
    await g.evaluate(incoming("/lobo oi", phone=STRANGER))
    assert await g.has_session("loboads", STRANGER)

    stop = await g.evaluate(incoming("/parar", phone=STRANGER, msg_id="m2"))
    assert stop.action is SandboxAction.RESET
    assert await g.end_session("loboads", STRANGER) is True
    after = await g.evaluate(incoming("oi de novo", phone=STRANGER, msg_id="m3"))
    assert after.action is SandboxAction.IGNORE


async def test_stop_command_from_unknown_contact_is_ignored():
    d = await guard().evaluate(incoming("/reset", phone=STRANGER))
    assert d.action is SandboxAction.IGNORE  # nunca responde a desconhecidos


async def test_production_open_mode_allows_everyone_but_still_strips_keyword():
    g = guard(environment="production")
    await g.save_config(SandboxConfig(enabled=False))
    assert not g.is_active(await g.get_config())
    assert (await g.evaluate(incoming("Oi", phone=STRANGER))).allowed
    d = await g.evaluate(incoming("/lobo Quero preço", phone=STRANGER, msg_id="m2"))
    assert d.allowed and d.message.text == "Quero preço" and not d.session_started


async def test_lock_is_forced_outside_production_even_if_disabled():
    g = guard(environment="staging")
    await g.save_config(SandboxConfig(enabled=False))
    assert g.forced_by_environment and g.is_active(await g.get_config())
    assert (await g.evaluate(incoming("Oi", phone=STRANGER))).action is SandboxAction.IGNORE


async def test_config_is_persisted_to_file_and_reloaded(tmp_path):
    path = tmp_path / "sandbox.json"
    await guard(path=path, redis=None).save_config(
        SandboxConfig(allowed_phones=["+55 91 98888-7777"], trigger_keywords=["/Teste"], session_ttl_minutes=15)
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["allowed_phones"] == [LEAD] and saved["trigger_keywords"] == ["/teste"]

    reloaded = await SandboxGuard(None, environment="development", config_path=path).get_config()
    assert reloaded.allowed_phones == [LEAD] and reloaded.session_ttl_minutes == 15 and reloaded.enabled is True


def test_config_validation():
    with pytest.raises(ValueError, match="palavra-chave"):
        SandboxConfig(trigger_keywords=["lobo"])
    with pytest.raises(ValueError):
        SandboxConfig(allowed_phones=["123"])
    with pytest.raises(ValueError):
        SandboxConfig(session_ttl_minutes=0)
    assert SandboxConfig(allowed_phones=[LEAD, f"{LEAD}@c.us"]).allowed_phones == [LEAD]


async def test_redis_failure_fails_closed():
    class BrokenRedis(fakeredis.FakeAsyncRedis):
        async def get(self, *a, **kw):
            from redis.exceptions import ConnectionError

            raise ConnectionError("down")

        async def exists(self, *a, **kw):
            from redis.exceptions import ConnectionError

            raise ConnectionError("down")

    d = await guard(redis=BrokenRedis()).evaluate(incoming("oi", phone=STRANGER))
    assert d.action is SandboxAction.IGNORE


# =========================================================================== #
# Webhook protegido pela trava
# =========================================================================== #
def test_webhook_ignores_unknown_contact_without_buffering(make_settings):
    app = create_app(make_settings())
    with TestClient(app) as client:
        gw = RecordingWaha()
        app.state.gateways[Provider.WAHA] = gw
        r = client.post("/webhook/loboads/waha", json=waha_payload("s1", "Oi, tudo bem?", phone=STRANGER))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ignored" and body["reason"] == "sandbox_locked" and body["accepted"] == 0
        assert run(client, app.state.buffer.pending_count, "loboads", "default", STRANGER) == 0
        assert gw.sent == []


def test_webhook_trigger_strips_keyword_and_keeps_session(make_settings):
    batches: list[MessageBatch] = []

    async def on_flush(batch: MessageBatch) -> None:
        batches.append(batch)

    app = create_app(make_settings(), on_flush=on_flush)
    with TestClient(app) as client:
        app.state.gateways[Provider.WAHA] = RecordingWaha()
        r = client.post(
            "/webhook/loboads/waha",
            json=waha_payload("t1", "/lobo-sales-agent Quero saber o preço do colchão", phone=STRANGER),
        )
        assert r.json()["accepted"] == 1 and r.json()["sandbox_session_started"] is True
        follow = client.post("/webhook/loboads/waha", json=waha_payload("t2", "E a entrega?", phone=STRANGER))
        assert follow.json()["accepted"] == 1 and "status" not in follow.json()
        wait_for(lambda: batches)

    assert len(batches) == 1
    assert [m.text for m in batches[0].messages] == ["Quero saber o preço do colchão", "E a entrega?"]


def test_webhook_whitelisted_phone_is_accepted(make_settings):
    app = create_app(make_settings(sandbox_allowed_phones=[LEAD]), on_flush=lambda b: _noop())
    with TestClient(app) as client:
        app.state.gateways[Provider.WAHA] = RecordingWaha()
        r = client.post("/webhook/loboads/waha", json=waha_payload("w1", "Oi"))
        assert r.json()["accepted"] == 1


async def _noop() -> None:
    return None


def test_webhook_stop_command_resets_and_confirms(make_settings):
    app = create_app(make_settings(), on_flush=lambda b: _noop())
    with TestClient(app) as client:
        gw = RecordingWaha()
        app.state.gateways[Provider.WAHA] = gw
        client.post("/webhook/loboads/waha", json=waha_payload("r1", "/lobo", phone=STRANGER))
        assert run(client, app.state.sandbox.has_session, "loboads", STRANGER)
        run(client, app.state.pause_registry.pause, "loboads", STRANGER, reason="customer_request", hours=1)

        r = client.post("/webhook/loboads/waha", json=waha_payload("r2", "/reset", phone=STRANGER))
        assert r.json()["status"] == "sandbox_reset" and r.json()["reply_sent"] is True
        assert [m.text for m in gw.sent] == [RESET_CONFIRMATION_TEXT]
        assert not run(client, app.state.sandbox.has_session, "loboads", STRANGER)
        assert not run(client, app.state.pause_registry.is_paused, "loboads", STRANGER)

        # O eco `from_me` da confirmação NÃO pode pausar a IA
        echo = waha_payload("sent_1", RESET_CONFIRMATION_TEXT, phone=STRANGER, from_me=True)
        assert client.post("/webhook/loboads/waha", json=echo).json()["from_me"] == 1
        assert not run(client, app.state.pause_registry.is_paused, "loboads", STRANGER)

        # Sem nova keyword, o contato volta a ser ignorado
        again = client.post("/webhook/loboads/waha", json=waha_payload("r3", "oi", phone=STRANGER))
        assert again.json()["status"] == "ignored"


# =========================================================================== #
# Painel: página, estáticos e API
# =========================================================================== #
def test_admin_panel_and_logos_are_served(make_settings):
    with TestClient(create_app(make_settings())) as client:
        for path in ("/", "/admin"):
            page = client.get(path)
            assert page.status_code == 200 and "text/html" in page.headers["content-type"]
            assert "loboads-logo-dark.png" in page.text and "loboads-logo-light.png" in page.text
        for asset in ("loboads-logo-dark.png", "loboads-logo-light.png"):
            img = client.get(f"/static/assets/{asset}")
            assert img.status_code == 200 and img.headers["content-type"] == "image/png"
        assert client.get("/static/admin/app.js").status_code == 200
        assert client.get("/static/admin/app.css").status_code == 200


def test_overview_and_loboads_tenant(make_settings):
    with TestClient(create_app(make_settings())) as client:
        ov = client.get("/api/admin/overview").json()
        assert {"loboads", "companhia-dos-sonhos", "splash-piscinas", "vizzu"} <= {t["tenant_id"] for t in ov["tenants"]}
        assert ov["auth_required"] is False and ov["sandbox_active"] is True and ov["sandbox_forced"] is True
        assert len(ov["integrations"]) == 6

        lobo = client.get("/api/admin/tenants/loboads").json()
        assert lobo["tenant"]["display_name"] == "LoboAds Marketing Digital"
        assert lobo["tenant"]["segment"] == "generic" and lobo["tenant"]["timezone"] == "America/Belem"
        assert lobo["tenant"]["whatsapp_provider"] == "waha" and lobo["tenant"]["whatsapp_session_id"] == "Danyllo"
        assert lobo["crm"]["chatwoot"]["account_id"] == 4 and lobo["crm"]["chatwoot"]["assignee_means_human"] is False
        assert [i["item_id"] for i in lobo["catalog"]["items"]] == ["LB-TRAFEGO-PRO", "LB-AI-ESSENCIAL", "LB-AI-PRO"]
        assert lobo["calendar"]["services"][0]["service_id"] == "diagnostico_comercial"
        assert client.get("/api/admin/tenants/fantasma").status_code == 404


def test_tenant_edit_applies_new_price_immediately(make_settings, tenants_dir):
    with TestClient(create_app(make_settings())) as client:
        data = client.get("/api/admin/tenants/companhia-dos-sonhos").json()
        item = next(i for i in data["catalog"]["items"] if i["item_id"] == "CS-ENS-CASAL")
        item["price"].update({"amount": "2999.00", "cash_amount": "2849.05"})
        data["catalog"]["items"].append(
            {"item_id": "CS-TRAV-NASA", "name": "Travesseiro Nasa", "category": "acessorio", "price": {"amount": "199.90"}}
        )
        r = client.put("/api/admin/tenants/companhia-dos-sonhos", json=data)
        assert r.status_code == 200, r.text
        saved = r.json()["tenant"]
        new_item = next(i for i in saved["catalog"]["items"] if i["item_id"] == "CS-TRAV-NASA")
        assert new_item["price"]["updated_at"]  # data carimbada automaticamente

        on_disk = json.loads((tenants_dir / "companhia-dos-sonhos.json").read_text(encoding="utf-8"))
        assert next(i for i in on_disk["catalog"]["items"] if i["item_id"] == "CS-ENS-CASAL")["price"]["amount"] == "2999.00"

        sim = client.post(
            "/api/admin/playground/simulate",
            json={"tenant_id": "companhia-dos-sonhos", "messages": ["Queria ver um colchão de casal com molas ensacadas"]},
        ).json()
        assert "R$ 2.999,00" in sim["reply_text"] and "R$ 2.849,05" in sim["reply_text"]


def test_tenant_validation_errors_do_not_touch_file(make_settings, tenants_dir):
    original = (tenants_dir / "splash-piscinas.json").read_text(encoding="utf-8")
    with TestClient(create_app(make_settings())) as client:
        data = client.get("/api/admin/tenants/splash-piscinas").json()
        data["catalog"]["items"][0]["price"]["amount"] = "-10"
        r = client.put("/api/admin/tenants/splash-piscinas", json=data)
        assert r.status_code == 422 and "catalog.items.0.price.amount" in r.json()["detail"]

        data = client.get("/api/admin/tenants/splash-piscinas").json()
        data["tenant"]["tenant_id"] = "outro-cliente"
        assert client.put("/api/admin/tenants/splash-piscinas", json=data).status_code == 422
    assert (tenants_dir / "splash-piscinas.json").read_text(encoding="utf-8") == original


def test_create_tenant_and_duplicate_conflict(make_settings, tenants_dir):
    new = {
        "tenant": {"tenant_id": "loja-nova", "display_name": "Loja Nova", "segment": "generic", "crm_provider": "none"},
        "catalog": {"items": [{"item_id": "LN-1", "name": "Produto 1", "category": "geral", "price": {"amount": "10.00"}}]},
    }
    with TestClient(create_app(make_settings())) as client:
        r = client.post("/api/admin/tenants", json=new)
        assert r.status_code == 201 and r.json()["summary"]["items"] == 1
        assert (tenants_dir / "loja-nova.json").is_file()
        assert client.get("/api/admin/tenants/loja-nova").status_code == 200
        assert client.post("/api/admin/tenants", json=new).status_code == 409
        sim = client.post("/api/admin/playground/simulate", json={"tenant_id": "loja-nova", "messages": ["Olá"]})
        assert sim.status_code == 200  # disponível na hora, sem reiniciar


def test_sandbox_config_api(make_settings, tmp_path):
    with TestClient(create_app(make_settings())) as client:
        st = client.get("/api/admin/sandbox").json()
        assert st["active"] is True and st["config"]["trigger_keywords"] == ["/lobo-sales-agent", "/lobo"]

        upd = client.put("/api/admin/sandbox", json={"trigger_keywords": ["/teste", "/lobo"], "session_ttl_minutes": 15})
        assert upd.status_code == 200 and upd.json()["config"]["session_ttl_minutes"] == 15
        assert client.put("/api/admin/sandbox", json={"trigger_keywords": ["sem-barra"]}).status_code == 422

        added = client.post("/api/admin/sandbox/phones", json={"phone": "+55 (91) 98888-7777"})
        assert added.status_code == 200 and added.json()["config"]["allowed_phones"] == [LEAD]
        assert client.post("/api/admin/sandbox/phones", json={"phone": "abc"}).status_code == 422

        removed = client.delete(f"/api/admin/sandbox/phones/{LEAD}")
        assert removed.status_code == 200 and removed.json()["config"]["allowed_phones"] == []
        assert client.delete(f"/api/admin/sandbox/phones/{LEAD}").status_code == 404

        disabled = client.put("/api/admin/sandbox", json={"enabled": False}).json()
        assert disabled["config"]["enabled"] is False and disabled["active"] is True  # forçado fora de produção

    persisted = json.loads((tmp_path / "sandbox_config.json").read_text(encoding="utf-8"))
    assert persisted["trigger_keywords"] == ["/teste", "/lobo"] and persisted["enabled"] is False


def test_sandbox_session_listing_and_end_via_api(make_settings):
    app = create_app(make_settings(), on_flush=lambda b: _noop())
    with TestClient(app) as client:
        app.state.gateways[Provider.WAHA] = RecordingWaha()
        client.post("/webhook/loboads/waha", json=waha_payload("x1", "/lobo oi", phone=STRANGER))
        sessions = client.get("/api/admin/sandbox").json()["sessions"]
        assert [(s["tenant_id"], s["phone"]) for s in sessions] == [("loboads", STRANGER)]
        assert 0 < sessions[0]["expires_in_seconds"] <= 1800

        ended = client.delete(f"/api/admin/sandbox/sessions/loboads/{STRANGER}").json()
        assert ended["reset"]["sandbox_session_ended"] is True and ended["sessions"] == []


def test_playground_three_messages_xray(make_settings):
    with TestClient(create_app(make_settings())) as client:
        r = client.post(
            "/api/admin/playground/simulate",
            json={
                "tenant_id": "companhia-dos-sonhos",
                "engine": "deterministic",
                "messages": ["Olá, boa tarde", "Queria ver um colchão de casal com molas ensacadas", "Vocês entregam em Belém?"],
            },
        )
        assert r.status_code == 200, r.text
        t = r.json()
        assert t["status"] == "success" and t["batch_size"] == 3 and t["turn_id"].startswith("turn_")
        assert t["stage"] == "offer_catalog" and t["intent"] == "catalog_price"
        tools = {x["tool"]: x for x in t["tools"]}
        assert {"search_catalog", "get_delivery_info"} <= set(tools)
        assert "colchão" in tools["search_catalog"]["args"]["query"]
        assert tools["get_delivery_info"]["args"] == {"city": "Belém"}
        assert tools["get_delivery_info"]["result"]["data"]["fee_formatted"] == "Grátis"
        assert "R$ 2.899,00" in t["reply_text"] and "1 a 2 dias úteis" in t["reply_text"]
        assert "qualificado" in t["lead"]["tags"] and t["lead"]["attributes"]["cidade"] == "Belém"
        assert t["jev_decision"]["intent"] == "catalog_price"

        assert client.post("/api/admin/playground/simulate", json={"tenant_id": "vizzu", "engine": "gemini", "messages": ["oi"]}).status_code == 400
        assert client.post("/api/admin/playground/simulate", json={"tenant_id": "tenant-x", "messages": ["oi"]}).status_code == 404
        assert client.post("/api/admin/playground/simulate", json={"tenant_id": "vizzu", "messages": ["  "]}).status_code == 422


def test_playground_loboads_generic_greeting(make_settings):
    with TestClient(create_app(make_settings())) as client:
        t = client.post("/api/admin/playground/simulate", json={"tenant_id": "loboads", "messages": ["Olá"]}).json()
        assert "LoboAds Marketing Digital" in t["reply_text"] and "colchão" not in t["reply_text"]
        traffic = client.post(
            "/api/admin/playground/simulate",
            json={"tenant_id": "loboads", "phone": "5591900000002", "messages": ["Quanto custa a gestão de tráfego pago?"]},
        ).json()
        assert "Gestão de Tráfego Pago" in traffic["reply_text"] and "R$ 1.500,00" in traffic["reply_text"]


def test_playground_is_isolated_from_real_handoff_and_resettable(make_settings):
    app = create_app(make_settings())
    with TestClient(app) as client:
        body = {"tenant_id": "vizzu", "phone": LEAD, "messages": ["Não aguento mais viver, quero morrer"]}
        t = client.post("/api/admin/playground/simulate", json=body).json()
        assert t["status"] == "handoff" and t["handoff_reason"] == "clinical_emergency" and "188" in t["reply_text"]
        assert not run(client, app.state.pause_registry.is_paused, "vizzu", LEAD)  # atendimento real intocado
        assert run(client, app.state.playground.pause.is_paused, "vizzu", LEAD)
        assert client.get("/api/admin/conversations").json() == []  # não polui as conversas reais

        silenced = client.post("/api/admin/playground/simulate", json={**body, "messages": ["oi?"]}).json()
        assert silenced["status"] == "silenced"

        assert client.delete(f"/api/admin/playground/conversations/vizzu/{LEAD}").json()["memory_cleared"] is True
        assert not run(client, app.state.playground.pause.is_paused, "vizzu", LEAD)


def test_conversations_list_resume_pause_and_reset(make_settings):
    app = create_app(make_settings())
    with TestClient(app) as client:
        lead = LeadProfile(tenant_id="companhia-dos-sonhos", phone=LEAD, name="Maria")
        lead.mark_human_handoff()
        run(client, app.state.conversation_store.save, lead, [])
        run(client, app.state.pause_registry.pause, "companhia-dos-sonhos", LEAD, reason="customer_request", hours=24)

        convs = client.get("/api/admin/conversations").json()
        assert len(convs) == 1 and convs[0]["ai_paused"] is True and convs[0]["pause"]["reason"] == "customer_request"
        assert StandardTag.HUMAN_HANDOFF.value in convs[0]["tags"]

        resumed = client.post(f"/api/admin/conversations/companhia-dos-sonhos/{LEAD}/resume")
        assert resumed.status_code == 200 and resumed.json()["ai_paused"] is False
        after = client.get("/api/admin/conversations?tenant_id=companhia-dos-sonhos").json()[0]
        assert after["ai_paused"] is False and StandardTag.HUMAN_HANDOFF.value not in after["tags"]
        assert after["status"] == "in_progress"
        assert not run(client, app.state.pause_registry.is_paused, "companhia-dos-sonhos", LEAD)

        paused = client.post(f"/api/admin/conversations/companhia-dos-sonhos/{LEAD}/pause", json={"hours": 2})
        assert paused.status_code == 200 and paused.json()["ai_paused"] is True
        assert client.get("/api/admin/conversations").json()[0]["ai_paused"] is True

        assert client.delete(f"/api/admin/conversations/companhia-dos-sonhos/{LEAD}").json()["memory_cleared"] is True
        assert client.get("/api/admin/conversations").json() == []
        assert client.get("/api/admin/conversations?tenant_id=vizzu").json() == []


def test_integrations_status_and_live_checks_mocked(make_settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/profile":
            assert request.headers["api_access_token"] == "cw-secret-token"
            return httpx.Response(200, json={"name": "Danyllo", "accounts": [{"id": 4}, {"id": 8}]})
        if request.url.path == "/api/sessions":
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(404, json={})

    settings = make_settings(chatwoot_base_url="https://chat.example.com", chatwoot_api_token="cw-secret-token")
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        overview = {i["name"]: i for i in client.get("/api/admin/integrations").json()}
        assert overview["chatwoot"]["configured"] and overview["waha"]["configured"]
        assert not overview["gemini"]["configured"] and overview["chatwoot"]["target"] == "chat.example.com"

        cw = client.post("/api/admin/integrations/chatwoot/test").json()
        assert cw["ok"] is True and cw["status"] == "ok" and "contas: 4, 8" in cw["detail"]
        waha = client.post("/api/admin/integrations/waha/test").json()
        assert waha["ok"] is False and waha["status"] == "error" and "HTTP 401" in waha["detail"]
        gem = client.post("/api/admin/integrations/gemini/test").json()
        assert gem["status"] == "not_configured"

        everything = client.post("/api/admin/integrations/test-all")
        assert len(everything.json()) == 6 and "cw-secret-token" not in everything.text
        assert client.post("/api/admin/integrations/telegram/test").status_code == 404


def test_admin_api_token_protection(make_settings):
    with TestClient(create_app(make_settings(admin_token="s3cret-token"))) as client:
        assert client.get("/").status_code == 200  # a página carrega; os dados exigem token
        assert client.get("/api/admin/overview").status_code == 401
        assert client.get("/api/admin/overview", headers={"X-Admin-Token": "errado"}).status_code == 401
        assert client.get("/api/admin/overview", headers={"X-Admin-Token": "s3cret-token"}).status_code == 200
        ok = client.get("/api/admin/overview", headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200 and ok.json()["auth_required"] is True


def test_admin_api_blocked_in_production_without_token(make_settings, tmp_path):
    prod_dir = tmp_path / "prod-tenants"
    prod_dir.mkdir()
    (prod_dir / "loja-real.json").write_text(
        json.dumps({"tenant": {"tenant_id": "loja-real", "display_name": "Loja Real", "crm_provider": "none"}}),
        encoding="utf-8",
    )
    settings = make_settings(environment="production", google_api_key="g-key", tenant_data_dir=prod_dir)
    with TestClient(create_app(settings)) as client:
        r = client.get("/api/admin/overview")
        assert r.status_code == 503 and "ADMIN_TOKEN" in r.json()["detail"]


def test_webhooks_listing_and_gateway_test_send(make_settings):
    app = create_app(make_settings())
    with TestClient(app) as client:
        gw = RecordingWaha()
        app.state.gateways[Provider.WAHA] = gw
        wh = client.get("/api/admin/webhooks", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "salesagent.loboads.top"}).json()
        assert wh["base_url"] == "https://salesagent.loboads.top"
        lobo = next(t for t in wh["tenants"] if t["tenant_id"] == "loboads")
        assert lobo["paths"] == {"waha": "/webhook/loboads/waha", "astracalls": "/webhook/loboads/astracalls"}

        r = client.post(
            "/api/admin/gateway/test-send",
            json={"tenant_id": "loboads", "provider": "waha", "phone": LEAD, "text": "Teste do painel"},
        )
        assert r.status_code == 200 and r.json()["session_id"] == "Danyllo" and r.json()["message_id"] == "sent_1"
        assert gw.sent[0].text == "Teste do painel" and gw.sent[0].session_id == "Danyllo"

        # o eco desse envio não pausa a IA do contato
        client.post("/webhook/loboads/waha", json=waha_payload("sent_1", "Teste do painel", from_me=True))
        assert not run(client, app.state.pause_registry.is_paused, "loboads", LEAD)

        missing = client.post(
            "/api/admin/gateway/test-send",
            json={"tenant_id": "loboads", "provider": "astracalls", "phone": LEAD, "text": "x"},
        )
        assert missing.status_code == 400 and "AstraCalls" in missing.json()["detail"]

