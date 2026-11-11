"""Testes dos conectores WAHA e AstraCalls (`src/gateways/`).

Nenhuma chamada de rede real: o HTTP é interceptado por `httpx.MockTransport`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from src.config import Settings
from src.core.exceptions import ConfigurationError, GatewayError
from src.core.schemas import MediaAttachment, MessageType, OutgoingMessage, Provider
from src.gateways import AstraCallsGateway, WahaGateway, create_gateway

TENANT = "splash-piscinas"
PHONE = "5591988887777"
WAHA_URL = "https://waha.example.com"
ASTRA_URL = "https://chamadas.example.com"


class Recorder:
    """Transporte HTTP falso que registra as requisições."""

    def __init__(self, status: int = 200, json_body=None, content: bytes | None = None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.json_body = json_body if json_body is not None else {"id": "out-1"}
        self.content = content

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.content is not None:
            return httpx.Response(self.status, content=self.content)
        return httpx.Response(self.status, json=self.json_body)

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def waha(rec: Recorder | None = None, **kw) -> WahaGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec or Recorder()))
    return WahaGateway(WAHA_URL, "waha-key", default_session="default", client=client, **kw)


def astra(rec: Recorder | None = None, **kw) -> AstraCallsGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec or Recorder()))
    return AstraCallsGateway(
        ASTRA_URL, "astra-key", default_session="sess-1", client_id="lobo", client=client, **kw
    )


def outgoing(provider: Provider, **kw) -> OutgoingMessage:
    data = {
        "tenant_id": TENANT,
        "provider": provider,
        "session_id": "default" if provider is Provider.WAHA else "sess-1",
        "contact_phone": PHONE,
        "text": "Em qual cidade você está?",
    }
    data.update(kw)
    return OutgoingMessage(**data)


# =========================================================================== #
# WAHA — normalização
# =========================================================================== #
def waha_event(payload: dict, event: str = "message") -> dict:
    return {"event": event, "session": "default", "engine": "WEBJS", "payload": payload}


def waha_payload(**kw) -> dict:
    p = {
        "id": "false_5591988887777@c.us_AAAA",
        "timestamp": 1762800000,
        "from": f"{PHONE}@c.us",
        "fromMe": False,
        "to": "5591900000000@c.us",
        "body": "Quero orçamento de piscina",
        "hasMedia": False,
        "_data": {"notifyName": "Carlos", "type": "chat"},
    }
    p.update(kw)
    return p


def test_waha_parse_text():
    parsed = waha().parse_webhook(TENANT, waha_event(waha_payload()))
    assert not parsed.ignored
    (m,) = parsed.messages
    assert m.provider is Provider.WAHA
    assert m.tenant_id == TENANT
    assert m.contact_phone == PHONE
    assert m.chat_id == f"{PHONE}@c.us"
    assert m.contact_name == "Carlos"
    assert m.type is MessageType.TEXT
    assert m.text == "Quero orçamento de piscina"
    assert m.timestamp.year == 2025
    assert m.session_id == "default"


def test_waha_parse_voice_note():
    p = waha_payload(
        body="",
        hasMedia=True,
        media={"url": "http://localhost:3000/api/files/default/a.oga", "mimetype": "audio/ogg; codecs=opus"},
        _data={"type": "ptt", "duration": "7", "notifyName": "Carlos"},
    )
    (m,) = waha().parse_webhook(TENANT, waha_event(p)).messages
    assert m.type is MessageType.AUDIO
    assert m.needs_transcription
    assert m.media.duration_seconds == 7.0
    assert m.media.url.endswith("a.oga")


def test_waha_parse_image_with_caption_and_sticker():
    p = waha_payload(
        body="foto do quintal",
        hasMedia=True,
        media={"url": None, "mimetype": "image/jpeg"},
        _data={"type": "image"},
    )
    (m,) = waha().parse_webhook(TENANT, waha_event(p)).messages
    assert m.type is MessageType.IMAGE
    assert m.media.caption == "foto do quintal"
    assert m.media.provider_media_id == p["id"]  # sem URL → id para download
    assert m.content_for_llm == "[imagem] foto do quintal"

    sticker = waha_payload(body="", hasMedia=True, media={"mimetype": "image/webp"}, _data={})
    (s,) = waha().parse_webhook(TENANT, waha_event(sticker)).messages
    assert s.type is MessageType.STICKER


def test_waha_parse_location():
    p = waha_payload(body="", location={"latitude": -1.45, "longitude": -48.49, "description": "Belém"})
    (m,) = waha().parse_webhook(TENANT, waha_event(p)).messages
    assert m.type is MessageType.LOCATION
    assert m.content_for_llm == "[localização] Belém"


def test_waha_from_me_uses_recipient_as_contact():
    p = waha_payload(fromMe=True, **{"from": "5591900000000@c.us", "to": f"{PHONE}@c.us"})
    (m,) = waha().parse_webhook(TENANT, waha_event(p, "message.any")).messages
    assert m.from_me and m.contact_phone == PHONE and m.contact_name is None


def test_waha_lid_resolution():
    p = waha_payload(**{"from": "123456789012345@lid"})
    p["_data"] = {"key": {"remoteJidAlt": f"{PHONE}@s.whatsapp.net"}, "pushName": "Ana"}
    (m,) = waha().parse_webhook(TENANT, waha_event(p)).messages
    assert m.contact_phone == PHONE
    assert m.chat_id == "123456789012345@lid"  # responde no chat exato
    assert m.contact_name == "Ana"

    unresolved = waha_payload(**{"from": "123456789012345@lid"}, _data={})
    parsed = waha().parse_webhook(TENANT, waha_event(unresolved))
    assert not parsed.messages and parsed.ignored[0].reason == "lid_unresolved"


@pytest.mark.parametrize(
    ("event", "payload", "reason"),
    [
        ("session.status", {"status": "WORKING"}, "event_not_supported"),
        ("message", waha_payload(**{"from": "120363000000000000@g.us"}), "not_a_contact"),
        ("message", waha_payload(**{"from": "status@broadcast"}), "not_a_contact"),
        ("message", waha_payload(body="", hasMedia=False), "unsupported_message"),
        ("message", {"no_id": True}, "invalid_payload"),
        ("message", waha_payload(**{"from": "123@c.us"}), "invalid_contact"),
    ],
)
def test_waha_ignored_events(event, payload, reason):
    parsed = waha().parse_webhook(TENANT, waha_event(payload, event))
    assert parsed.messages == []
    assert parsed.ignored[0].reason == reason


def test_waha_raw_payload_not_serialized():
    (m,) = waha().parse_webhook(TENANT, waha_event(waha_payload())).messages
    assert m.raw_payload["event"] == "message"
    assert "raw_payload" not in m.model_dump_json()


# =========================================================================== #
# WAHA — envio
# =========================================================================== #
async def test_waha_send_text():
    rec = Recorder(json_body={"id": {"_serialized": "true_x@c.us_BBB"}})
    result = await waha(rec).send_message(outgoing(Provider.WAHA, reply_to_message_id="msg-9"))
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path == "/api/sendText"
    assert req.headers["X-Api-Key"] == "waha-key"
    assert rec.body() == {
        "session": "default",
        "chatId": f"{PHONE}@c.us",
        "text": "Em qual cidade você está?",
        "reply_to": "msg-9",
    }
    assert result.message_id == "true_x@c.us_BBB"


async def test_waha_send_uses_native_chat_id():
    rec = Recorder()
    await waha(rec).send_message(outgoing(Provider.WAHA, chat_id="559188887777@c.us"))
    assert rec.body()["chatId"] == "559188887777@c.us"  # sem o 9º dígito, como o WhatsApp registrou


async def test_waha_send_image_and_voice():
    rec = Recorder()
    gw = waha(rec)
    await gw.send_message(
        outgoing(
            Provider.WAHA,
            type=MessageType.IMAGE,
            text="Modelo Splash 6x3",
            media=MediaAttachment(url="https://cdn.example.com/p.jpg", mimetype="image/jpeg"),
        )
    )
    assert rec.paths[-1] == "/api/sendImage"
    assert rec.body()["file"] == {"mimetype": "image/jpeg", "url": "https://cdn.example.com/p.jpg"}
    assert rec.body()["caption"] == "Modelo Splash 6x3"

    await gw.send_message(
        outgoing(Provider.WAHA, type=MessageType.AUDIO, text=None, media={"data_base64": "AAA", "mimetype": "audio/ogg"})
    )
    assert rec.paths[-1] == "/api/sendVoice"
    assert rec.body()["file"]["data"] == "AAA" and rec.body()["convert"] is True


async def test_waha_typing_seen_and_deliver(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("src.gateways.base.asyncio.sleep", fake_sleep)
    rec = Recorder()
    gw = waha(rec)
    await gw.mark_seen("default", PHONE, ["m1"])
    await gw.deliver(outgoing(Provider.WAHA, typing_seconds=2.0))
    assert rec.paths == ["/api/sendSeen", "/api/startTyping", "/api/sendText", "/api/stopTyping"]
    assert rec.body(0) == {"session": "default", "chatId": f"{PHONE}@c.us", "messageIds": ["m1"]}
    assert sleeps == [2.0]


async def test_deliver_still_sends_when_typing_fails(monkeypatch):
    monkeypatch.setattr("src.gateways.base.asyncio.sleep", lambda s: _noop())

    def handler(request: httpx.Request) -> httpx.Response:
        if "Typing" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, json={"id": "ok"})

    gw = WahaGateway(
        WAHA_URL, "k", default_session="default", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    result = await gw.deliver(outgoing(Provider.WAHA))
    assert result.message_id == "ok"


async def _noop():
    return None


async def test_waha_http_error_raises_gateway_error_without_pii():
    gw = waha(Recorder(status=502))
    with pytest.raises(GatewayError) as exc_info:
        await gw.send_message(outgoing(Provider.WAHA))
    err = exc_info.value
    assert err.handoff_recommended
    assert err.context["status_code"] == 502
    assert PHONE not in str(err.to_dict())
    assert err.context["contact"] == "55*******7777"


async def test_waha_network_error_raises_gateway_error():
    def boom(request):
        raise httpx.ConnectError("refused")

    gw = WahaGateway(WAHA_URL, "k", default_session="default", client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(GatewayError, match="erro de rede"):
        await gw.set_typing("default", PHONE)


async def test_waha_download_media_rewrites_host():
    rec = Recorder(content=b"OGGDATA")
    gw = waha(rec)
    p = waha_payload(
        body="",
        hasMedia=True,
        media={"url": "http://localhost:3000/api/files/default/a.oga?x=1", "mimetype": "audio/ogg"},
        _data={"type": "ptt"},
    )
    (m,) = gw.parse_webhook(TENANT, waha_event(p)).messages
    assert await gw.download_media(m) == b"OGGDATA"
    req = rec.requests[0]
    assert str(req.url) == f"{WAHA_URL}/api/files/default/a.oga?x=1"  # nunca vai para outro host
    assert req.headers["X-Api-Key"] == "waha-key"


def test_waha_signature():
    body = b'{"event":"message"}'
    sig = hmac.new(b"segredo", body, hashlib.sha512).hexdigest()
    gw = waha(webhook_secret="segredo")
    assert gw.verify_signature(body, {"x-webhook-hmac": sig})
    assert not gw.verify_signature(body, {"X-Webhook-Hmac": "0" * 128})
    assert not gw.verify_signature(body, {})
    assert waha().verify_signature(body, {})  # sem segredo → verificação desativada


# =========================================================================== #
# AstraCalls — normalização
# =========================================================================== #
def astra_event(data: dict, event: str = "message") -> dict:
    return {"session": "sess-1", "event": event, "timestamp": 1762800000123, "data": data}


def astra_data(**kw) -> dict:
    d = {
        "id": "3EB0ABC",
        "chat": f"{PHONE}@s.whatsapp.net",
        "sender": f"{PHONE}:12@s.whatsapp.net",
        "fromMe": False,
        "pushName": "Juliana",
        "timestamp": 1762800000123,
        "isGroup": False,
        "type": "text",
        "text": "Quanto custa o colchão king?",
        "raw": {"conversation": "Quanto custa o colchão king?"},
    }
    d.update(kw)
    return d


def test_astra_parse_text_with_ad_referral():
    referral = {"ctwaClid": "abc", "sourceId": "120210000", "sourceType": "ad"}
    (m,) = astra().parse_webhook(TENANT, astra_event(astra_data(referral=referral, quotedMessageId="q1"))).messages
    assert m.provider is Provider.ASTRACALLS
    assert m.contact_phone == PHONE
    assert m.chat_id == f"{PHONE}@s.whatsapp.net"
    assert m.contact_name == "Juliana"
    assert m.text == "Quanto custa o colchão king?"
    assert m.ad_referral["ctwaClid"] == "abc"
    assert m.quoted_message_id == "q1"
    assert m.session_id == "sess-1"
    assert m.timestamp.microsecond == 123000  # epoch em ms


def test_astra_parse_audio_from_raw():
    data = astra_data(
        type="audio",
        text="",
        raw={"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": 12, "PTT": True, "fileLength": "20480"}},
    )
    (m,) = astra().parse_webhook(TENANT, astra_event(data)).messages
    assert m.type is MessageType.AUDIO
    assert m.media.duration_seconds == 12.0
    assert m.media.size_bytes == 20480
    assert m.media.provider_media_id == "3EB0ABC"
    assert m.media.url is None  # CDN criptografado não é exposto


def test_astra_parse_image_and_document():
    img = astra_data(type="image", text="meu terreno", raw={"imageMessage": {"mimetype": "image/jpeg"}})
    (m,) = astra().parse_webhook(TENANT, astra_event(img)).messages
    assert m.type is MessageType.IMAGE and m.media.caption == "meu terreno"

    doc = astra_data(
        type="document",
        text="",
        raw={"documentWithCaptionMessage": {"message": {"documentMessage": {"fileName": "planta.pdf", "mimetype": "application/pdf"}}}},
    )
    (d,) = astra().parse_webhook(TENANT, astra_event(doc)).messages
    assert d.media.filename == "planta.pdf"
    assert d.content_for_llm == "[documento: planta.pdf]"


def test_astra_parse_location():
    data = astra_data(
        type="location",
        text="",
        raw={"locationMessage": {"degreesLatitude": -1.45, "degreesLongitude": -48.49, "address": "Ananindeua"}},
    )
    (m,) = astra().parse_webhook(TENANT, astra_event(data)).messages
    assert m.location.address == "Ananindeua"


def test_astra_button_reply_becomes_text():
    data = astra_data(type="text", text="Quero agendar visita", raw={"buttonsResponseMessage": {}})
    (m,) = astra().parse_webhook(TENANT, astra_event(data)).messages
    assert m.type is MessageType.TEXT


def test_astra_lid_chat_uses_sender_phone():
    data = astra_data(chat="60838698344603@lid", sender=f"{PHONE}@s.whatsapp.net")
    (m,) = astra().parse_webhook(TENANT, astra_event(data)).messages
    assert m.contact_phone == PHONE and m.chat_id == "60838698344603@lid"

    unresolved = astra_data(chat="60838698344603@lid", sender="60838698344603@lid")
    parsed = astra().parse_webhook(TENANT, astra_event(unresolved))
    assert parsed.ignored[0].reason == "lid_unresolved"


@pytest.mark.parametrize(
    ("event", "data", "reason"),
    [
        ("receipt", {"id": "x"}, "event_not_supported"),
        ("message", astra_data(isGroup=True), "not_a_contact"),
        ("message", astra_data(chat="120363000000000000@g.us"), "not_a_contact"),
        ("message", astra_data(type="poll", text=""), "unsupported_message"),
        ("message", astra_data(type="location", text="", raw={}), "invalid_payload"),
        ("message", {"chat": "x"}, "invalid_payload"),
    ],
)
def test_astra_ignored_events(event, data, reason):
    parsed = astra().parse_webhook(TENANT, astra_event(data, event))
    assert parsed.messages == [] and parsed.ignored[0].reason == reason


def test_astra_from_me_has_no_contact_name():
    (m,) = astra().parse_webhook(TENANT, astra_event(astra_data(fromMe=True, pushName="Loja"))).messages
    assert m.from_me and m.contact_name is None


# =========================================================================== #
# AstraCalls — envio
# =========================================================================== #
async def test_astra_send_text():
    rec = Recorder(json_body={"id": "3EB0OUT", "to": f"{PHONE}@s.whatsapp.net", "timestamp": 1})
    result = await astra(rec).send_message(outgoing(Provider.ASTRACALLS, reply_to_message_id="3EB0ABC"))
    req = rec.requests[0]
    assert req.url.path == "/api/sessions/sess-1/messages/text"
    assert req.headers["X-API-Key"] == "astra-key"
    assert req.headers["X-Client-Id"] == "lobo"
    assert rec.body() == {"to": PHONE, "text": "Em qual cidade você está?", "quotedMessageId": "3EB0ABC"}
    assert result.message_id == "3EB0OUT"


async def test_astra_send_media():
    rec = Recorder()
    gw = astra(rec)
    await gw.send_message(
        outgoing(
            Provider.ASTRACALLS,
            type=MessageType.DOCUMENT,
            text="Tabela de preços",
            media=MediaAttachment(url="https://cdn.example.com/t.pdf", mimetype="application/pdf", filename="tabela.pdf"),
        )
    )
    assert rec.paths[-1] == "/api/sessions/sess-1/messages/document"
    assert rec.body() == {
        "to": PHONE,
        "url": "https://cdn.example.com/t.pdf",
        "mimetype": "application/pdf",
        "filename": "tabela.pdf",
        "caption": "Tabela de preços",
    }
    await gw.send_message(outgoing(Provider.ASTRACALLS, type=MessageType.AUDIO, text=None, media={"data_base64": "T2dn"}))
    assert rec.paths[-1].endswith("/messages/audio") and rec.body()["ptt"] is True


async def test_astra_typing_and_seen():
    rec = Recorder(json_body={"ok": True})
    gw = astra(rec)
    async with gw.typing("sess-1", "60838698344603@lid"):
        pass
    await gw.mark_seen("sess-1", PHONE, [])  # sem ids → não chama a API
    await gw.mark_seen("sess-1", PHONE, ["3EB0ABC"])
    assert rec.paths == [
        "/api/sessions/sess-1/messages/typing",
        "/api/sessions/sess-1/messages/typing",
        "/api/sessions/sess-1/messages/seen",
    ]
    assert rec.body(0) == {"to": "60838698344603@lid", "typing": True}
    assert rec.body(1)["typing"] is False
    assert rec.body(2) == {"to": PHONE, "messageIds": ["3EB0ABC"]}


async def test_astra_download_media():
    rec = Recorder(content=b"\x89PNG")
    gw = astra(rec)
    data = astra_data(type="image", text="", raw={"imageMessage": {"mimetype": "image/png"}})
    (m,) = gw.parse_webhook(TENANT, astra_event(data)).messages
    assert await gw.download_media(m) == b"\x89PNG"
    assert rec.paths == ["/api/sessions/sess-1/messages/3EB0ABC/media"]

    m64 = m.model_copy(update={"media": MediaAttachment(data_base64="data:image/png;base64," + base64.b64encode(b"x").decode())})
    assert await gw.download_media(m64) == b"x"


def test_astra_signature():
    body = b'{"event":"message"}'
    sig = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    gw = astra(webhook_secret="s3cr3t")
    assert gw.verify_signature(body, {"X-Webhook-Signature": sig})
    assert not gw.verify_signature(b"tampered", {"X-Webhook-Signature": sig})


# =========================================================================== #
# Fábrica
# =========================================================================== #
def test_create_gateway_from_settings():
    s = Settings(
        _env_file=None,
        waha_base_url="https://waha.example.com/",
        waha_api_key="k",
        astracalls_base_url="https://astra.example.com",
        astracalls_api_key="k2",
        astracalls_session_id="sess-9",
        astracalls_client_id="lobo",
    )
    w = create_gateway("waha", s)
    a = create_gateway(Provider.ASTRACALLS, s)
    assert isinstance(w, WahaGateway) and w.base_url == "https://waha.example.com"
    assert isinstance(a, AstraCallsGateway) and a.default_session == "sess-9" and a.client_id == "lobo"


def test_create_gateway_requires_configuration():
    with pytest.raises(ConfigurationError):
        create_gateway("waha", Settings(_env_file=None))
    with pytest.raises(ConfigurationError):
        create_gateway(
            "astracalls",
            Settings(_env_file=None, astracalls_base_url="https://a.example.com", astracalls_api_key="k"),
        )
    with pytest.raises(ConfigurationError):
        create_gateway("simulator", Settings(_env_file=None))

