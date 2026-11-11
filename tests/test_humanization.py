"""Testes da ETAPA 6 — Humanização no WhatsApp, Áudio Resiliente, Blue Ticks,
Mensagens Picotadas, Contexto de Mensagens Respondidas (Quoted) e Debounce Dinâmico.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fakeredis
import httpx
from fastapi.testclient import TestClient

from main import create_app
from src.agent import (
    AudioTranscriber,
    DeterministicSalesChatModel,
    LeadConversationStore,
    SalesAgentRunner,
    extract_lead_attributes,
    normalize_audio_mimetype,
)
from src.buffer.message_queue import MessageBuffer
from src.config import Settings
from src.core.sandbox import SandboxConfig
from src.core.schemas import (
    IncomingMessage,
    LeadProfile,
    LeadStatus,
    MediaAttachment,
    MessageBatch,
    MessageType,
    OutgoingMessage,
    Provider,
    StandardTag,
    TenantConfig,
    split_human_chunks,
)
from src.crm.base import CRMConnector
from src.gateways.astracalls import AstraCallsGateway
from src.gateways.base import SendResult, WhatsAppGateway
from src.gateways.waha import WahaGateway
from src.tools.calendar import CalendarService, InMemoryBookingRepository
from src.tools.catalog import CatalogService
from src.tools.handoff import AIPauseRegistry, HandoffService
from src.tools.tenant_data import JsonTenantDataStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
FIXED_NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return FIXED_NOW


def _make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "environment": "development",
        "redis_backend": "fake",
        "waha_base_url": "https://waha.loboads.top",
        "waha_api_key": "waha-secret-test",
        "waha_default_session": "Danyllo",
        "astracalls_base_url": "https://api.astracalls.com",
        "astracalls_api_key": "astra-secret-test",
        "astracalls_default_session": "sessao-principal",
        "google_api_key": "gemini-test-key",
        "openai_api_key": "openai-test-key",
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ===========================================================================
# 1. Citação de Mensagens Respondidas (Quoted / Reply Context)
# ===========================================================================


def test_waha_extracts_quoted_text_from_reply_to_and_quoted_msg():
    """WAHA deve extrair quoted_text tanto de replyTo.body quanto de _data.quotedMsg.body."""
    gw = WahaGateway(
        "https://waha.loboads.top",
        "waha-secret-test",
        default_session="Danyllo",
    )

    # Formato 1: payload.replyTo.body
    payload_reply_to = {
        "event": "message",
        "session": "Danyllo",
        "payload": {
            "id": "true_5591988887777@c.us_MSG01",
            "from": "5591988887777@c.us",
            "fromMe": False,
            "body": "Casal",
            "replyTo": {
                "id": "BOT_QUESTION_01",
                "body": "Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?",
            },
        },
    }
    parsed_1 = gw.parse_webhook("companhia-dos-sonhos", payload_reply_to)
    assert len(parsed_1.messages) == 1
    msg_1 = parsed_1.messages[0]
    assert msg_1.quoted_message_id == "BOT_QUESTION_01"
    assert (
        msg_1.quoted_text
        == "Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?"
    )
    assert msg_1.content_for_llm == (
        '[Lead respondeu citando a mensagem: "Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?"]\n'
        "Casal"
    )

    # Formato 2: payload._data.quotedMsg.body (WEBJS / NOWEB / GOWS)
    payload_quoted_msg = {
        "event": "message",
        "session": "Danyllo",
        "payload": {
            "id": "true_5591988887777@c.us_MSG02",
            "from": "5591988887777@c.us",
            "fromMe": False,
            "body": "King",
            "_data": {
                "quotedStanzaID": "BOT_QUESTION_02",
                "quotedMsg": {
                    "body": "Você prefere Solteiro, Casal, Queen ou King?",
                },
            },
        },
    }
    parsed_2 = gw.parse_webhook("companhia-dos-sonhos", payload_quoted_msg)
    assert len(parsed_2.messages) == 1
    msg_2 = parsed_2.messages[0]
    assert msg_2.quoted_message_id == "BOT_QUESTION_02"
    assert msg_2.quoted_text == "Você prefere Solteiro, Casal, Queen ou King?"
    assert msg_2.content_for_llm == (
        '[Lead respondeu citando a mensagem: "Você prefere Solteiro, Casal, Queen ou King?"]\n'
        "King"
    )


def test_astracalls_extracts_quoted_text_from_context_info():
    """AstraCalls deve extrair quoted_text de contextInfo.quotedMessage.conversation / extendedTextMessage."""
    gw = AstraCallsGateway(
        "https://api.astracalls.com",
        "astra-secret-test",
        default_session="sessao-principal",
    )

    payload = {
        "session": "sessao-principal",
        "event": "message",
        "timestamp": 1731250000000,
        "data": {
            "id": "ASTRA_MSG_99",
            "chat": "5591988887777@s.whatsapp.net",
            "sender": "5591988887777@s.whatsapp.net",
            "fromMe": False,
            "pushName": "Maria",
            "isGroup": False,
            "type": "text",
            "text": "King",
            "raw": {
                "extendedTextMessage": {
                    "text": "King",
                    "contextInfo": {
                        "stanzaId": "QUOTED_STANZA_1",
                        "quotedMessage": {
                            "conversation": "Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?"
                        },
                    },
                }
            },
        },
    }
    parsed = gw.parse_webhook("companhia-dos-sonhos", payload)
    assert len(parsed.messages) == 1
    msg = parsed.messages[0]
    assert msg.quoted_message_id == "QUOTED_STANZA_1"
    assert (
        msg.quoted_text
        == "Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?"
    )
    assert msg.content_for_llm == (
        '[Lead respondeu citando a mensagem: "Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?"]\n'
        "King"
    )


def test_quoted_message_does_not_confuse_lead_attribute_extraction():
    """Quando o lead responde 'King' citando uma pergunta que lista solteiro/casal/queen/king,
    o extrator deve capturar 'king' (a resposta do lead) e não 'casal' da pergunta citada.
    """
    msg = IncomingMessage(
        message_id="m_quoted_king",
        tenant_id="companhia-dos-sonhos",
        provider=Provider.WAHA,
        session_id="Danyllo",
        contact_phone="5591988887777",
        contact_name="Carlos",
        chat_id="5591988887777@c.us",
        type=MessageType.TEXT,
        text="King",
        quoted_message_id="q_1",
        quoted_text="Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?",
    )
    attrs = extract_lead_attributes(msg.content_for_llm, "colchoes")
    assert attrs.get("tamanho") == "king"


# ===========================================================================
# 2. Divisão Inteligente de Mensagens Longas (split_human_chunks)
# ===========================================================================


def test_split_human_chunks_keeps_short_messages_intact():
    short = "Olá, Carlos! Tudo bem? Qual tamanho de colchão você procura hoje?"
    chunks = split_human_chunks(short)
    assert chunks == [short]


def test_split_human_chunks_breaks_long_message_into_natural_parts():
    long_reply = (
        "Olá, Mariana! Tudo ótimo por aqui 😊 Separei a melhor opção da Companhia dos Sonhos para você.\n\n"
        "O *Colchão Molas Ensacadas Prime Casal (138x188x30cm)* está saindo por *R$ 2.899,00* "
        "(em até 10x de R$ 289,90 sem juros ou *R$ 2.754,05 à vista* no PIX).\n\n"
        "🚚 Para *Belém*, o frete é *Grátis* e o prazo de entrega é de *1 a 2 dias úteis*.\n\n"
        "Você prefere fechar pelo PIX com desconto ou parcelado no cartão?"
    )
    chunks = split_human_chunks(long_reply, max_chars=400, max_chunks=4)
    assert 2 <= len(chunks) <= 4
    assert all(len(c) <= 400 for c in chunks)
    # Garante que nenhuma palavra foi cortada e todos os blocos estão presentes
    assert "\n\n".join(chunks) == long_reply


def test_split_human_chunks_handles_single_giant_paragraph_without_breaking_words():
    words = ["colchão", "ortopédico", "confortável", "entrega", "rápida", "garantia"]
    giant = " ".join(words * 25)  # ~950 chars sem quebras de linha
    chunks = split_human_chunks(giant, max_chars=350, max_chunks=4)
    assert 2 <= len(chunks) <= 4
    for chunk in chunks[:-1]:
        assert len(chunk) <= 350
    # Todas as palavras preservadas
    assert " ".join(chunks).split() == giant.split()


# ===========================================================================
# 3. Blue Ticks (mark_seen), Delay de Leitura e Envio em Partes no Runner
# ===========================================================================


class _DummyCRM(CRMConnector):
    def __init__(self) -> None:
        super().__init__("https://crm.local")

    async def ensure_contact(self, lead: LeadProfile) -> str:
        return "crm_1"

    async def add_private_note(self, lead: LeadProfile, content: str) -> None:
        return None

    async def add_tags(self, lead: LeadProfile, tags: list[str]) -> list[str]:
        for t in tags:
            lead.add_tag(t)
        return list(lead.tags)

    async def remove_tags(self, lead: LeadProfile, tags: list[str]) -> None:
        for t in tags:
            lead.remove_tag(t)

    async def is_human_active(self, lead: LeadProfile) -> bool:
        return lead.has_tag(StandardTag.HUMAN_HANDOFF)

    def _headers(self) -> dict[str, str]:
        return {}


class RecordingHumanGateway(WhatsAppGateway):
    """Gateway de teste que registra a ordem exata de mark_seen, set_typing e send_message."""

    provider = Provider.WAHA

    def __init__(self) -> None:
        super().__init__("https://human-sim.local", default_session="Danyllo")
        self.events: list[tuple[str, Any]] = []
        self.sent_messages: list[OutgoingMessage] = []

    def parse_webhook(self, tenant_id: str, payload):  # pragma: no cover
        raise NotImplementedError

    def verify_signature(self, body: bytes, headers) -> bool:
        return True

    async def mark_seen(self, session_id: str, to: str, message_ids: list[str]) -> None:
        self.events.append(("mark_seen", list(message_ids)))

    async def set_typing(self, session_id: str, to: str, typing: bool = True) -> None:
        self.events.append(("typing", typing))

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        self.events.append(("send_message", message.text))
        self.sent_messages.append(message)
        return SendResult(provider=self.provider, message_id=f"out_{len(self.sent_messages)}")

    async def download_media(self, message: IncomingMessage) -> bytes:
        return b""

    async def deliver(self, message: OutgoingMessage) -> SendResult:
        await self.set_typing(message.session_id, message.chat_id or message.contact_phone, True)
        try:
            return await self.send_message(message)
        finally:
            await self.set_typing(
                message.session_id, message.chat_id or message.contact_phone, False
            )

    def _auth_headers(self) -> dict[str, str]:
        return {}


async def test_runner_marks_seen_waits_reading_delay_and_dispatches_split_chunks(monkeypatch):
    """Verifica a sequência:
    1) mark_seen com os IDs do lote (Blue Ticks)
    2) delay de leitura humana (500ms-1000ms)
    3) set_typing(True) antes da inferência
    4) envio picotado em partes com simulação de digitação entre cada parte
    5) registro de todas as partes no AIPauseRegistry (anti-eco)
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("src.agent.graph.asyncio.sleep", fake_sleep)

    settings = _make_settings()
    store = JsonTenantDataStore(PROJECT_ROOT / "data" / "tenants", allow_demo=True)
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    pause_registry = AIPauseRegistry(redis)
    crm = _DummyCRM()
    gateway = RecordingHumanGateway()

    catalog = CatalogService(store, clock=_fixed_clock)
    calendar = CalendarService(store, InMemoryBookingRepository(), clock=_fixed_clock)
    handoff = HandoffService(
        store,
        pause_registry,
        lambda _t: crm,
        pause_hours=24.0,
        clock=_fixed_clock,
    )
    conv_store = LeadConversationStore(redis)
    transcriber = AudioTranscriber(settings)
    buffer = MessageBuffer(redis, silence_seconds=1.0, max_window_seconds=5.0)

    runner = SalesAgentRunner(
        store=store,
        catalog=catalog,
        calendar=calendar,
        handoff=handoff,
        pause_registry=pause_registry,
        conversation_store=conv_store,
        transcriber=transcriber,
        gateway_resolver=lambda _p: gateway,
        crm_resolver=lambda _t: crm,
        buffer=buffer,
        llm=DeterministicSalesChatModel(),
        settings=settings,
        clock=_fixed_clock,
    )

    msg = IncomingMessage(
        message_id="MSG_IN_1",
        tenant_id="companhia-dos-sonhos",
        provider=Provider.WAHA,
        session_id="Danyllo",
        contact_phone="5591988887777",
        contact_name="Mariana",
        chat_id="5591988887777@c.us",
        type=MessageType.TEXT,
        text="Oi! Quero saber o preço do colchão de casal de molas ensacadas e o frete para Belém",
    )
    batch = MessageBatch(
        tenant_id="companhia-dos-sonhos",
        contact_phone="5591988887777",
        session_id="Danyllo",
        messages=[msg],
        first_received_at=FIXED_NOW,
        flushed_at=FIXED_NOW,
    )

    try:
        turn = await runner.handle_batch(batch)
        assert turn.reply_text is not None
        assert turn.lead.status is LeadStatus.QUALIFIED

        # 1. O primeiro evento no gateway DEVE ser mark_seen com o ID da mensagem recebida
        assert gateway.events[0] == ("mark_seen", ["MSG_IN_1"])
        # 2. Logo após mark_seen, deve ter ocorrido o delay de leitura (0.5s a 1.0s)
        assert len(sleeps) >= 1
        assert 0.5 <= sleeps[0] <= 1.0
        # 3. Em seguida, set_typing(True) antes da inferência
        assert gateway.events[1] == ("typing", True)

        # 4. Resposta longa foi dividida em múltiplos blocos (2 a 4 partes)
        assert 2 <= len(gateway.sent_messages) <= 4
        assert "\n\n".join(m.text for m in gateway.sent_messages) == turn.reply_text

        # Cada bloco recebe simulação de digitação proporcional (1.5s a 2.5s)
        assert all(m.simulate_typing is True for m in gateway.sent_messages)
        assert all(1.5 <= m.typing_seconds <= 2.5 for m in gateway.sent_messages)

        # 5. Todos os blocos estão registrados no anti-eco para não pausar a IA
        for sent in gateway.sent_messages:
            assert (
                await pause_registry.is_bot_outbound(
                    "companhia-dos-sonhos", "5591988887777", text=sent.text
                )
                is True
            )
    finally:
        await buffer.aclose()
        await redis.aclose()


# ===========================================================================
# 4. Controle Dinâmico de Debounce (até 30s / 60s) e Persistência no Painel
# ===========================================================================


def test_debounce_validation_accepts_up_to_30s_silence_and_60s_max_window():
    s = _make_settings(debounce_silence_seconds=30.0, debounce_max_window_seconds=60.0)
    assert s.debounce_silence_seconds == 30.0
    assert s.debounce_max_window_seconds == 60.0

    tc = TenantConfig(
        tenant_id="teste-debounce",
        display_name="Teste Debounce",
        whatsapp_provider=Provider.WAHA,
        debounce_silence_seconds=25.0,
        debounce_max_window_seconds=55.0,
        split_messages=False,
    )
    assert tc.debounce_silence_seconds == 25.0
    assert tc.debounce_max_window_seconds == 55.0
    assert tc.split_messages is False

    sb = SandboxConfig(
        debounce_silence_seconds=20.0,
        debounce_max_window_seconds=45.0,
        split_messages=True,
    )
    assert sb.debounce_silence_seconds == 20.0
    assert sb.debounce_max_window_seconds == 45.0


def test_admin_api_updates_debounce_and_split_messages_live(tmp_path):
    """PUT /api/admin/sandbox deve atualizar silence_seconds (até 30s), max_window_seconds (até 60s)
    e split_messages imediatamente em memória (app.state.buffer e app.state.settings) e no disco.
    """
    sandbox_file = tmp_path / "sandbox_config.json"
    settings = _make_settings(sandbox_config_path=str(sandbox_file))
    app = create_app(settings)

    with TestClient(app) as client:
        res = client.put(
            "/api/admin/sandbox",
            json={
                "debounce_silence_seconds": 18.0,
                "debounce_max_window_seconds": 45.0,
                "split_messages": False,
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["config"]["debounce_silence_seconds"] == 18.0
        assert body["config"]["debounce_max_window_seconds"] == 45.0
        assert body["config"]["split_messages"] is False

        # Verifica atualização imediata no MessageBuffer em memória
        assert app.state.buffer.silence_seconds == 18.0
        assert app.state.buffer.max_window_seconds == 45.0
        assert app.state.settings.split_messages is False

        # Verifica persistência no arquivo JSON
        saved = json.loads(sandbox_file.read_text(encoding="utf-8"))
        assert saved["debounce_silence_seconds"] == 18.0
        assert saved["debounce_max_window_seconds"] == 45.0
        assert saved["split_messages"] is False


# ===========================================================================
# 5. Resiliência na Transcrição de Áudio (Download Fallback + Mimetype + Whisper)
# ===========================================================================


async def test_waha_download_media_supports_base64_localhost_rewrite_and_message_endpoint_fallback():
    """Garante que o download_media do WAHA funciona nos 3 cenários reais:
    1) Base64 direto no payload
    2) URL com localhost reescrita para a URL pública do WAHA
    3) Fallback para o endpoint /api/{session}/chats/{chatId}/messages/{messageId}/media quando a URL falha
    """
    raw_ogg = b"OggS\x00\x02fake-opus-voice-note-bytes"
    b64_ogg = base64.b64encode(raw_ogg).decode("ascii")

    # Cenário 1: Base64 direto em media.data_base64
    gw1 = WahaGateway(
        "https://waha.loboads.top",
        "waha-secret-test",
        default_session="Danyllo",
    )
    msg_b64 = IncomingMessage(
        message_id="waha_b64",
        tenant_id="loboads",
        provider=Provider.WAHA,
        session_id="Danyllo",
        contact_phone="5591988887777",
        chat_id="5591988887777@c.us",
        type=MessageType.AUDIO,
        media=MediaAttachment(
            mimetype="audio/ogg; codecs=opus",
            data_base64=f"data:audio/ogg; codecs=opus;base64,{b64_ogg}",
        ),
    )
    assert await gw1.download_media(msg_b64) == raw_ogg
    await gw1.aclose()

    # Cenário 2 e 3: URL inicial falha com 404, fallback busca no endpoint da mensagem
    requested_urls: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        # Verifica envio do header X-Api-Key
        assert request.headers.get("X-Api-Key") == "waha-secret-test"
        if "/api/files/broken.ogg" in request.url.path:
            return httpx.Response(404, text="File expired")
        if "/messages/true_5591988887777%40c.us_MSG99/media" in str(request.url) or (
            "/messages/true_5591988887777@c.us_MSG99/media" in request.url.path
        ):
            # Simula retorno JSON do WAHA contendo base64
            return httpx.Response(
                200,
                json={"mimetype": "audio/ogg; codecs=opus", "data": b64_ogg},
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    gw2 = WahaGateway(
        "https://waha.loboads.top",
        "waha-secret-test",
        default_session="Danyllo",
        client=client,
    )
    msg_fallback = IncomingMessage(
        message_id="true_5591988887777@c.us_MSG99",
        tenant_id="loboads",
        provider=Provider.WAHA,
        session_id="Danyllo",
        contact_phone="5591988887777",
        chat_id="5591988887777@c.us",
        type=MessageType.AUDIO,
        media=MediaAttachment(
            url="http://localhost:3000/api/files/broken.ogg",
            mimetype="audio/ogg; codecs=opus",
        ),
    )
    downloaded = await gw2.download_media(msg_fallback)
    assert downloaded == raw_ogg
    # A primeira tentativa deve ter reescrito localhost:3000 para https://waha.loboads.top
    assert requested_urls[0] == "https://waha.loboads.top/api/files/broken.ogg"
    assert any("/messages/" in u for u in requested_urls[1:])
    await client.aclose()


async def test_audio_transcriber_normalizes_mimetype_falls_back_to_whisper_and_logs(
    caplog, monkeypatch
):
    """Verifica:
    - Limpeza de 'audio/ogg; codecs=opus' -> 'audio/ogg'
    - Fallback automático para OpenAI Whisper quando Gemini falha
    - Logs INFO com tamanho em bytes e texto transcrito
    """
    assert normalize_audio_mimetype("audio/ogg; codecs=opus") == "audio/ogg"
    assert normalize_audio_mimetype("audio/webm;codecs=opus") == "audio/webm"
    assert normalize_audio_mimetype(None) == "audio/ogg"

    raw_audio = b"OggS\x00\x02whatsapp-ptt-voice-sample-12345"
    gemini_seen_mimetypes: list[str] = []

    async def failing_gemini(audio_bytes: bytes, mimetype: str) -> str:
        gemini_seen_mimetypes.append(mimetype)
        raise RuntimeError("Gemini quota or format error simulated")

    async def working_whisper(audio_bytes: bytes, mimetype: str) -> str:
        assert audio_bytes == raw_audio
        assert mimetype == "audio/ogg"
        return "Oi, queria saber o valor do colchão Queen e se entrega em Ananindeua."

    transcriber = AudioTranscriber(_make_settings())
    monkeypatch.setattr(transcriber, "_transcribe_with_gemini", failing_gemini)
    monkeypatch.setattr(transcriber, "_transcribe_with_whisper", working_whisper)

    msg = IncomingMessage(
        message_id="ptt_01",
        tenant_id="companhia-dos-sonhos",
        provider=Provider.WAHA,
        session_id="Danyllo",
        contact_phone="5591988887777",
        chat_id="5591988887777@c.us",
        type=MessageType.AUDIO,
        media=MediaAttachment(
            mimetype="audio/ogg; codecs=opus",
            data_base64=base64.b64encode(raw_audio).decode("ascii"),
        ),
    )

    with caplog.at_level(logging.INFO):
        updated_msg = await transcriber.transcribe_message(msg)

    assert gemini_seen_mimetypes == ["audio/ogg"]
    assert (
        updated_msg.transcription
        == "Oi, queria saber o valor do colchão Queen e se entrega em Ananindeua."
    )
    assert any("Áudio decodificado" in r.message for r in caplog.records)
    assert any("Áudio transcrito via Whisper" in r.message for r in caplog.records)
