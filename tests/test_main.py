"""Testes do app FastAPI (`main.py`): health-check e webhook → buffer."""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from main import create_app
from src.config import Settings
from src.core.schemas import MessageBatch

PHONE = "5591988887777"
PROJECT_TENANTS = Path(__file__).resolve().parents[1] / "data" / "tenants"


def _isolated_tenants_dir() -> Path:
    """Cópia temporária dos tenants SEM overrides de debounce: o teste não depende (nem altera)
    das configurações reais que o usuário ajusta pelo painel."""
    target = Path(tempfile.mkdtemp(prefix="lobo-tenants-"))
    for src in PROJECT_TENANTS.glob("*.json"):
        data = json.loads(src.read_text(encoding="utf-8"))
        data.get("tenant", {}).pop("debounce_silence_seconds", None)
        data.get("tenant", {}).pop("debounce_max_window_seconds", None)
        (target / src.name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return target


def settings(**kw) -> Settings:
    base = {
        "redis_backend": "fake",
        "waha_base_url": "https://waha.example.com",
        "waha_api_key": "k",
        "debounce_silence_seconds": 1.0,
        "debounce_max_window_seconds": 3.0,
        # ETAPA 5: fora de produção a trava Sandbox é obrigatória → o lead de teste fica na whitelist
        "sandbox_allowed_phones": [PHONE],
        "sandbox_config_path": None,
        "tenant_data_dir": _isolated_tenants_dir(),
    }
    base.update(kw)
    return Settings(_env_file=None, **base)


def waha_msg(msg_id: str, body: str, **kw) -> dict:
    payload = {
        "id": msg_id,
        "timestamp": 1762800000,
        "from": f"{PHONE}@c.us",
        "fromMe": False,
        "body": body,
        "hasMedia": False,
        "_data": {"notifyName": "Lead"},
    }
    payload.update(kw)
    return {"event": "message", "session": "default", "payload": payload}


def test_health_endpoint_does_not_leak_secrets():
    s = settings(google_api_key="top-secret-key")
    with TestClient(create_app(s)) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["integrations"]["gemini"] is True
    assert "top-secret-key" not in resp.text


def test_webhook_groups_rapid_messages_into_one_batch():
    batches: list[MessageBatch] = []

    async def on_flush(batch: MessageBatch) -> None:
        batches.append(batch)

    with TestClient(create_app(settings(), on_flush=on_flush)) as client:
        texts = ["Olá, boa tarde", "Queria ver um colchão de casal com molas ensacadas", "Vocês entregam em Belém?"]
        for i, t in enumerate(texts):
            r = client.post("/webhook/companhia-dos-sonhos/waha", json=waha_msg(f"m{i}", t))
            assert r.status_code == 200 and r.json()["accepted"] == 1

        dup = client.post("/webhook/companhia-dos-sonhos/waha", json=waha_msg("m0", texts[0]))
        assert dup.json()["duplicates"] == 1

        deadline = time.monotonic() + 5
        while not batches and time.monotonic() < deadline:
            time.sleep(0.1)

    assert len(batches) == 1
    assert [m.text for m in batches[0].messages] == texts
    assert batches[0].tenant_id == "companhia-dos-sonhos"


def test_webhook_ignores_groups_and_counts_from_me():
    with TestClient(create_app(settings())) as client:
        group = waha_msg("g1", "oi grupo", **{"from": "120363000000000000@g.us"})
        r = client.post("/webhook/companhia-dos-sonhos/waha", json=group)
        assert r.json() == {"accepted": 0, "duplicates": 0, "from_me": 0, "ignored": ["not_a_contact"]}

        mine = waha_msg("f1", "Oi, aqui é a Carla da loja", fromMe=True, to=f"{PHONE}@c.us")
        mine["event"] = "message.any"
        assert client.post("/webhook/companhia-dos-sonhos/waha", json=mine).json()["from_me"] == 1


def test_webhook_validation_errors():
    with TestClient(create_app(settings(waha_webhook_hmac_key="segredo"))) as client:
        body = json.dumps(waha_msg("s1", "oi")).encode()
        assert client.post("/webhook/vizzu/waha", content=body).status_code == 401

        sig = hmac.new(b"segredo", body, hashlib.sha512).hexdigest()
        ok = client.post("/webhook/vizzu/waha", content=body, headers={"X-Webhook-Hmac": sig})
        assert ok.status_code == 200 and ok.json()["accepted"] == 1

        assert client.post("/webhook/Tenant Inválido/waha", json={}).status_code == 422
        assert client.post("/webhook/tenant-fantasma/waha", json={}).status_code == 404  # isolamento
        assert client.post("/webhook/vizzu/telegram", json={}).status_code == 422
        assert client.post("/webhook/vizzu/astracalls", json={}).status_code == 404  # não configurado

    with TestClient(create_app(settings())) as client:
        assert client.post("/webhook/vizzu/waha", content=b"not json").status_code == 400
