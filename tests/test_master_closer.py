"""Testes da ETAPA 7 — Mestria Comercial & Humanização Máxima (`tests/test_master_closer.py`).

Cobre os 5 pilares exigidos:
1. Transcrição nativa via SDK oficial `google.genai.Client` (`types.Part.from_bytes`) + fallback Whisper.
2. Formatação visual padrão WhatsApp (`sanitize_whatsapp_formatting`: `**bold**` / `**bold*` -> `*bold*`) e zero meta-linguagem robótica.
3. Chunking semântico (`split_human_chunks`): preserva listas de produtos (`✅`, `•`, `-`, `1️⃣`) inteiras em um único bloco.
4. Inteligência geográfica local (bairros de Belém e Ananindeua como *Tapanã*, *Marambaia*, *Umarizal*, *Icoaraci*, *Cidade Nova*) com entrega grátis e referência à loja da Av. Tavares Bastos, 1246F (Marambaia).
5. Seletor de modelos de IA por cliente (`llm_model`), gestão de webhooks por cliente e reações com emojis (`WahaGateway.react`, `AstraCallsGateway.react`, `pick_reaction_emoji`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakeredis
import httpx
import pytest
from pydantic import SecretStr

from src.agent.graph import LeadConversationStore, SalesAgentRunner, pick_reaction_emoji
from src.agent.nodes import DeterministicSalesChatModel
from src.agent.state import ConversationStage
from src.agent.transcription import AudioTranscriber, normalize_audio_mimetype
from src.config import Settings
from src.core.schemas import (
    IncomingMessage,
    MediaAttachment,
    MessageBatch,
    MessageType,
    Provider,
    sanitize_whatsapp_formatting,
    split_human_chunks,
)
from src.gateways.astracalls import AstraCallsGateway
from src.gateways.base import SendResult, WhatsAppGateway
from src.gateways.waha import WahaGateway
from src.tools.calendar import CalendarService, InMemoryBookingRepository
from src.tools.catalog import CatalogService, resolve_metropolitan_neighborhood
from src.tools.handoff import AIPauseRegistry, HandoffService
from src.tools.tenant_data import JsonTenantDataStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TENANTS_DIR = PROJECT_ROOT / "data" / "tenants"
FIXED_NOW = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return FIXED_NOW


def _make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "environment": "test",
        "allow_demo_data": True,
        "redis_url": "redis://localhost:6379/15",
        "google_api_key": None,
        "openai_api_key": None,
        "gemini_model": "gemini-3.8-flash",
        "gemini_transcribe_model": "gemini-3.8-flash",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def store(tmp_path: Path) -> JsonTenantDataStore:
    for src_file in TENANTS_DIR.glob("*.json"):
        (tmp_path / src_file.name).write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
    return JsonTenantDataStore(tmp_path, allow_demo=True)


@pytest.fixture
async def fake_redis():
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()


class _ReactionRecordingGateway(WhatsAppGateway):
    provider = Provider.WAHA

    def __init__(self, *, audio_bytes: bytes = b"OggS\x00\x02opus-audio-bytes") -> None:
        super().__init__("https://waha.example.com", "key", default_session="Danyllo")
        self._audio_bytes = audio_bytes
        self.reactions: list[tuple[str, str, str, str]] = []
        self.sent_messages: list[str] = []

    def parse_webhook(self, tenant_id, payload):  # pragma: no cover
        raise NotImplementedError

    def verify_signature(self, body, headers):  # pragma: no cover
        return True

    def _auth_headers(self) -> dict[str, str]:
        return {}

    async def mark_seen(self, session_id: str, to: str, message_ids: list[str]) -> None:
        return None

    async def set_typing(self, session_id: str, to: str, typing: bool = True) -> None:
        return None

    async def react(self, session_id: str, chat_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((session_id, chat_id, message_id, emoji))

    async def send_message(self, message) -> SendResult:
        self.sent_messages.append(message.text or "")
        return SendResult(provider=self.provider, message_id=f"OUT_{len(self.sent_messages)}")

    async def download_media(self, message: IncomingMessage) -> bytes:
        return self._audio_bytes


def _build_runner(
    store: JsonTenantDataStore,
    fake_redis,
    *,
    gateway: WhatsAppGateway | None = None,
) -> SalesAgentRunner:
    catalog = CatalogService(store, clock=_fixed_clock)
    booking_repo = InMemoryBookingRepository()
    calendar = CalendarService(store, booking_repo, clock=_fixed_clock)
    pause_registry = AIPauseRegistry(fake_redis)
    handoff = HandoffService(
        store,
        pause_registry,
        lambda _t: None,
        pause_hours=24.0,
        clock=_fixed_clock,
    )
    conv_store = LeadConversationStore(fake_redis)
    return SalesAgentRunner(
        store=store,
        catalog=catalog,
        calendar=calendar,
        handoff=handoff,
        pause_registry=pause_registry,
        conversation_store=conv_store,
        gateway_resolver=(lambda _p: gateway) if gateway is not None else None,
        crm_resolver=lambda _t: None,
        llm=DeterministicSalesChatModel(),
        settings=_make_settings(),
        clock=_fixed_clock,
    )


# =========================================================================== #
# 1. Transcrição Nativa com `google.genai.Client` (`types.Part.from_bytes`)
# =========================================================================== #
async def test_native_gemini_transcription_uses_google_genai_client_and_part_from_bytes(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict[str, Any] = {}

    class FakeModels:
        def generate_content(self, *, model: str, contents: list[Any]) -> Any:
            captured["model"] = model
            captured["contents"] = contents
            return SimpleNamespace(text='  "Oi! Quero saber se vocês entregam colchão no Tapanã."  ')

    class FakeGenAIClient:
        def __init__(self, *, api_key: str) -> None:
            captured["api_key"] = api_key
            self.models = FakeModels()

    monkeypatch.setattr("google.genai.Client", FakeGenAIClient)

    settings = _make_settings(
        google_api_key=SecretStr("AIza-fake-gemini-key"),
        gemini_transcribe_model="gemini-3.8-flash",
    )
    transcriber = AudioTranscriber(settings)
    gw = _ReactionRecordingGateway(audio_bytes=b"OggS\x00\x02opus-voice-note-belem")

    msg = IncomingMessage(
        message_id="MSG_AUDIO_77",
        tenant_id="companhia-dos-sonhos",
        provider=Provider.WAHA,
        session_id="Danyllo",
        contact_phone="5591988887777",
        type=MessageType.AUDIO,
        media=MediaAttachment(
            url="http://localhost:3000/api/files/Danyllo/MSG_AUDIO_77.oga",
            mimetype="audio/ogg; codecs=opus",
        ),
    )

    transcribed_msg = await transcriber.transcribe_message(msg, gateway=gw)
    assert transcribed_msg.transcription == "Oi! Quero saber se vocês entregam colchão no Tapanã."
    assert captured["api_key"] == "AIza-fake-gemini-key"
    assert captured["model"] == "gemini-3.8-flash"
    assert len(captured["contents"]) == 2

    part = captured["contents"][0]
    assert part.inline_data is not None
    assert part.inline_data.mime_type == "audio/ogg"
    assert part.inline_data.data == b"OggS\x00\x02opus-voice-note-belem"
    assert normalize_audio_mimetype("audio/ogg; codecs=opus") == "audio/ogg"


# =========================================================================== #
# 2. Sanitizador de Asteriscos do WhatsApp e Zero Meta-Linguagem Robótica
# =========================================================================== #
def test_sanitize_whatsapp_formatting_converts_double_and_broken_asterisks_to_single():
    raw = (
        "Opa! Seja bem-vindo à **Companhia dos Sonhos**!\n\n"
        "✅ **Colchão Casal Molas Ensacadas Premium* (`CS-ENS-CASAL`)\n"
        "Conforto individualizado e *zero transferência** de movimento.\n"
        "💰 **R$ 2.899,00** em até **12x de R$ 241,58 sem juros**"
    )
    cleaned = sanitize_whatsapp_formatting(raw)
    assert "**" not in cleaned
    assert "*Companhia dos Sonhos*" in cleaned
    assert "✅ *Colchão Casal Molas Ensacadas Premium* (`CS-ENS-CASAL`)" in cleaned
    assert "*zero transferência*" in cleaned
    assert "💰 *R$ 2.899,00* em até *12x de R$ 241,58 sem juros*" in cleaned


async def test_ad_lead_greeting_has_zero_robotic_meta_language_and_uses_whatsapp_cards(
    store: JsonTenantDataStore,
    fake_redis,
):
    runner = _build_runner(store, fake_redis)
    batch = MessageBatch(
        tenant_id="companhia-dos-sonhos",
        contact_phone="5591988881111",
        session_id="Danyllo",
        messages=[
            IncomingMessage(
                message_id="MSG_AD_1",
                tenant_id="companhia-dos-sonhos",
                provider=Provider.WAHA,
                session_id="Danyllo",
                contact_phone="5591988881111",
                contact_name="D Lobo",
                type=MessageType.TEXT,
                text="colchão de casal a partir de R$349 ... MENSAGEM DO CLIENTE: Ola Gostaria ver outros modelos",
            )
        ],
        first_received_at=FIXED_NOW,
        flushed_at=FIXED_NOW,
    )

    res = await runner.handle_batch(batch)
    assert res.reply_text is not None
    assert "**" not in res.reply_text

    lowered = res.reply_text.lower()
    # Zero meta-linguagem robótica
    assert "vi que você veio pelo nosso anúncio" not in lowered
    assert "recebi sua mensagem da campanha" not in lowered
    assert "vi que você marcou" not in lowered

    # Saudação natural + referência à loja da Tavares Bastos + cards estilo WhatsApp
    assert "d lobo" in lowered
    assert "tavares bastos" in lowered
    assert "✅ *Colchão Molas Ensacadas Prime Casal*" in res.reply_text
    assert "💰 *R$ 2.899,00*" in res.reply_text


# =========================================================================== #
# 3. Chunking Semântico: Listas de Produtos NUNCA São Cortadas ao Meio
# =========================================================================== #
def test_semantic_split_human_chunks_keeps_product_list_together_in_single_chunk():
    full_response = (
        "Opa, D Lobo! Tudo bom? Seja muito bem-vindo à Companhia dos Sonhos! 😊 "
        "Show de bola, temos vários modelos maravilhosos aqui na nossa loja da *Tavares Bastos (Marambaia)*!\n\n"
        "Temos opções incríveis aqui na loja! Dá uma olhadinha nos nossos principais destaques:\n\n"
        "✅ *Colchão Casal Molas Ensacadas Premium* (`CS-ENS-CASAL`) (138x188x30 cm)\n"
        "Molas ensacadas individualmente com pillow top em espuma viscoelástica.\n"
        "💰 *R$ 2.899,00* em até *12x de R$ 241,58 sem juros* (ou *R$ 2.609,10* à vista)\n\n"
        "✅ *Colchão Queen Molas Ensacadas Royal* (`CS-ENS-QUEEN`) (158x198x32 cm)\n"
        "Molas ensacadas de alta densidade com tecido em malha belga.\n"
        "💰 *R$ 3.499,00* em até *12x de R$ 291,58 sem juros* (ou *R$ 3.149,10* à vista)\n\n"
        "✅ *Colchão Casal Ortopédico D45 Firme* (`CS-ORT-CASAL`) (138x188x26 cm)\n"
        "Espuma D45 certificada pelo INMETRO com suporte ortopédico.\n"
        "💰 *R$ 1.890,00* em até *10x de R$ 189,00 sem juros* (ou *R$ 1.701,00* à vista)\n\n"
        "Para eu te indicar o modelo perfeito para o seu descanso: qual *medida* você precisa "
        "(solteiro, casal, queen ou king) e qual a sua preferência de *conforto*?"
    )

    chunks = split_human_chunks(full_response)
    assert len(chunks) == 3

    # Bloco 1: Saudação + introdução direta
    assert "Opa, D Lobo!" in chunks[0]
    assert "✅" not in chunks[0]

    # Bloco 2: Lista COMPLETA dos 3 produtos junta em uma única mensagem!
    assert chunks[1].count("✅") == 3
    assert "CS-ENS-CASAL" in chunks[1]
    assert "CS-ENS-QUEEN" in chunks[1]
    assert "CS-ORT-CASAL" in chunks[1]

    # Bloco 3: Pergunta consultiva de fechamento
    assert "qual *medida* você precisa" in chunks[2]
    assert "✅" not in chunks[2]


# =========================================================================== #
# 4. Inteligência Geográfica Local: Bairros de Belém & Região Metropolitana
# =========================================================================== #
@pytest.mark.parametrize(
    ("query_text", "expected_neighborhood", "expected_city"),
    [
        ("Vocês entregam no Tapanã?", "Tapanã", "Belém"),
        ("Sou do Umarizal, qual o valor do frete?", "Umarizal", "Belém"),
        ("Moro em Icoaraci, entrega lá?", "Icoaraci", "Belém"),
        ("Entregam na Cidade Nova?", "Cidade Nova", "Ananindeua"),
    ],
)
async def test_resolve_metropolitan_neighborhood_recognizes_belem_and_ananindeua(
    store: JsonTenantDataStore,
    query_text: str,
    expected_neighborhood: str,
    expected_city: str,
):
    resolved = resolve_metropolitan_neighborhood(query_text)
    assert resolved is not None
    assert resolved == (expected_neighborhood, expected_city)

    catalog = CatalogService(store, clock=_fixed_clock)
    res = await catalog.get_delivery_info("companhia-dos-sonhos", query_text)
    assert res.ok is True
    assert res.requires_human is False
    assert res.data["neighborhood"] == expected_neighborhood
    assert res.data["parent_city"] == expected_city
    assert res.data["fee_formatted"] == "Grátis"
    assert res.data["lead_time_formatted"] == "1 a 2 dias úteis"
    assert "Av. Tavares Bastos, 1246F" in res.data["store_reference"]


async def test_agent_answers_tapana_neighborhood_with_free_shipping_and_tavares_bastos_store(
    store: JsonTenantDataStore,
    fake_redis,
):
    runner = _build_runner(store, fake_redis)
    batch = MessageBatch(
        tenant_id="companhia-dos-sonhos",
        contact_phone="5591988882222",
        session_id="Danyllo",
        messages=[
            IncomingMessage(
                message_id="MSG_TAPANA_1",
                tenant_id="companhia-dos-sonhos",
                provider=Provider.WAHA,
                session_id="Danyllo",
                contact_phone="5591988882222",
                contact_name="Carlos",
                type=MessageType.TEXT,
                text="Vocês entregam no Tapanã? Qual o prazo?",
            )
        ],
        first_received_at=FIXED_NOW,
        flushed_at=FIXED_NOW,
    )

    res = await runner.handle_batch(batch)
    assert res.human_handoff_requested is False
    assert res.stage == ConversationStage.OFFER_CATALOG.value
    assert res.reply_text is not None
    assert "Tapanã" in res.reply_text
    assert "Grátis" in res.reply_text
    assert "1 a 2 dias úteis" in res.reply_text
    assert "Av. Tavares Bastos, 1246F" in res.reply_text
    assert "Marambaia" in res.reply_text


# =========================================================================== #
# 5. Seletor de Modelos por Cliente, Reações com Emojis e Gestão de Webhooks
# =========================================================================== #
async def test_waha_and_astracalls_gateways_send_emoji_reactions():
    waha_requests: list[tuple[str, str, dict[str, Any]]] = []
    astra_requests: list[tuple[str, str, dict[str, Any]]] = []

    def waha_handler(request: httpx.Request) -> httpx.Response:
        waha_requests.append((request.method, request.url.path, json.loads(request.content.decode())))
        return httpx.Response(200, json={"status": "ok"})

    def astra_handler(request: httpx.Request) -> httpx.Response:
        astra_requests.append((request.method, request.url.path, json.loads(request.content.decode())))
        return httpx.Response(200, json={"status": "ok"})

    waha_client = httpx.AsyncClient(transport=httpx.MockTransport(waha_handler))
    astra_client = httpx.AsyncClient(transport=httpx.MockTransport(astra_handler))
    try:
        waha = WahaGateway("https://waha.lobo.test", "secret", default_session="Danyllo", client=waha_client)
        astra = AstraCallsGateway("https://astra.lobo.test", "secret", default_session="sess1", client=astra_client)

        await waha.react("Danyllo", "5591988887777", "MSG_W_1", "👍")
        await astra.react("sess1", "5591988887777", "MSG_A_1", "❤️")

        assert waha_requests == [
            (
                "POST",
                "/api/sendReaction",
                {
                    "session": "Danyllo",
                    "chatId": "5591988887777@c.us",
                    "messageId": "MSG_W_1",
                    "text": "👍",
                },
            )
        ]
        assert astra_requests == [
            (
                "POST",
                "/api/sessions/sess1/messages/reaction",
                {
                    "to": "5591988887777",
                    "messageId": "MSG_A_1",
                    "emoji": "❤️",
                },
            )
        ]
    finally:
        await waha_client.aclose()
        await astra_client.aclose()


async def test_per_tenant_llm_model_and_sparse_emoji_reactions_in_runner(
    store: JsonTenantDataStore,
    fake_redis,
):
    # Configura modelo específico para a Companhia dos Sonhos
    td = store.get("companhia-dos-sonhos")
    td_payload = td.model_dump(mode="json")
    td_payload["tenant"]["llm_model"] = "gemini-2.5-pro"
    store.save(td.model_validate(td_payload))

    gw = _ReactionRecordingGateway()
    runner = _build_runner(store, fake_redis, gateway=gw)

    # Turno 1: Pergunta normal de catálogo -> NÃO deve reagir com emoji
    b1 = MessageBatch(
        tenant_id="companhia-dos-sonhos",
        contact_phone="5591988883333",
        session_id="Danyllo",
        messages=[
            IncomingMessage(
                message_id="MSG_NORM_1",
                tenant_id="companhia-dos-sonhos",
                provider=Provider.WAHA,
                session_id="Danyllo",
                contact_phone="5591988883333",
                chat_id="5591988883333@c.us",
                type=MessageType.TEXT,
                text="Quanto custa o colchão de casal de molas ensacadas?",
            )
        ],
        first_received_at=FIXED_NOW,
        flushed_at=FIXED_NOW,
    )
    r1 = await runner.handle_batch(b1)
    assert r1.lead.attributes.get("llm_model") == "gemini-2.5-pro"
    assert gw.reactions == []

    # Turno 2: Cliente agradece e elogia ("Show, muito obrigado!") -> Reage com 🙏
    b2 = MessageBatch(
        tenant_id="companhia-dos-sonhos",
        contact_phone="5591988883333",
        session_id="Danyllo",
        messages=[
            IncomingMessage(
                message_id="MSG_THANKS_2",
                tenant_id="companhia-dos-sonhos",
                provider=Provider.WAHA,
                session_id="Danyllo",
                contact_phone="5591988883333",
                chat_id="5591988883333@c.us",
                type=MessageType.TEXT,
                text="Show, muito obrigado! Combinado!",
            )
        ],
        first_received_at=FIXED_NOW,
        flushed_at=FIXED_NOW,
    )
    await runner.handle_batch(b2)
    assert len(gw.reactions) == 1
    assert gw.reactions[0] == ("Danyllo", "5591988883333@c.us", "MSG_THANKS_2", "🙏")

    # Valida também os seletores diretos de emoji
    assert pick_reaction_emoji("Perfeito, amei as opções!") == "❤️"
    assert pick_reaction_emoji("Combinado, fechado então!") == "👍"
    assert pick_reaction_emoji("Qual o tamanho do colchão queen?") is None


# =========================================================================== #
# 6. Filtro Estrito de Tamanho (King != Queen) e Zero Alucinação de Estoque
# =========================================================================== #
async def test_king_models_query_returns_only_king_in_stock_and_never_hallucinates_queen_or_stockout(
    store: JsonTenantDataStore,
    fake_redis,
):
    catalog = CatalogService(store, clock=_fixed_clock)
    # Mesmo buscando "colchão king", NUNCA deve retornar Queen, Casal ou Solteiro
    search_res = await catalog.search_catalog("companhia-dos-sonhos", "colchão king")
    assert search_res.ok is True
    results = search_res.data["results"]
    assert len(results) >= 2
    for item in results:
        assert item["attributes"]["tamanho"] == "king"
        assert item["in_stock"] is True
        assert "queen" not in item["name"].lower()

    runner = _build_runner(store, fake_redis)
    batch = MessageBatch(
        tenant_id="companhia-dos-sonhos",
        contact_phone="5591988884444",
        session_id="Danyllo",
        messages=[
            IncomingMessage(
                message_id="MSG_KING_1",
                tenant_id="companhia-dos-sonhos",
                provider=Provider.WAHA,
                session_id="Danyllo",
                contact_phone="5591988884444",
                contact_name="Roberto",
                type=MessageType.TEXT,
                text="Quais os valores e modelos da King?",
            )
        ],
        first_received_at=FIXED_NOW,
        flushed_at=FIXED_NOW,
    )

    turn = await runner.handle_batch(batch)
    assert turn.human_handoff_requested is False
    assert turn.reply_text is not None
    reply_lower = turn.reply_text.lower()

    # Deve mostrar os modelos King com preços reais
    assert "Colchão Molas Ensacadas Prime King" in turn.reply_text
    assert "R$ 4.299,00" in turn.reply_text
    assert "Colchão Ortopédico D45 Premium King" in turn.reply_text
    assert "R$ 3.699,00" in turn.reply_text

    # NUNCA deve oferecer Queen quando o cliente pediu King, nem inventar aviso de esgotado/fábrica
    assert "queen" not in reply_lower
    assert "aviso importante" not in reply_lower
    assert "esgotado" not in reply_lower
    assert "última unidade" not in reply_lower
    assert "encomenda" not in reply_lower
    # Como o cliente já pediu King, não deve perguntar novamente qual medida ele precisa
    assert "qual *medida* você precisa" not in reply_lower


async def test_product_inquiry_blocks_calendar_tools_and_split_chunks_never_reorders_paragraphs(
    store: JsonTenantDataStore,
    fake_redis,
):
    from langchain_core.messages import AIMessage, HumanMessage
    from src.agent.nodes import _has_scheduling_signal
    from src.agent.prompts import build_system_prompt

    # 1. Pergunta pura de produto/estoque NÃO habilita ferramentas de agenda (impossível inserir datas soltas!)
    assert _has_scheduling_signal("Vocês tem o king ainda?") is False
    assert _has_scheduling_signal("Quais os valores e modelos da King?") is False
    # Já pedido de visita/horário habilita normalmente
    assert _has_scheduling_signal("Quero agendar uma visita na loja amanhã") is True

    # 2. split_human_chunks NUNCA reordena parágrafos (nunca puxa lista do fim para o meio dos produtos)
    text_with_two_lists = (
        "Temos sim, D Lobo! Temos duas opções no tamanho King:\n\n"
        "✅ *Colchão Molas Ensacadas Prime King*\n💰 *R$ 4.299,00*\n\n"
        "✅ *Colchão Ortopédico D45 Premium King*\n💰 *R$ 3.699,00*\n\n"
        "Se quiser vir experimentar aqui na loja da Tavares Bastos amanhã, tenho horários livres:\n\n"
        "1. *quinta-feira, 12/11 às 09:00*\n2. *quinta-feira, 12/11 às 12:00*"
    )
    chunks = split_human_chunks(text_with_two_lists)
    joined_order = "\n\n".join(chunks)
    assert joined_order.index("Se quiser vir experimentar") < joined_order.index("quinta-feira, 12/11")

    # 3. Engenharia de System Prompt com tags XML e Memória Ativa da Sessão
    tenant = store.get("companhia-dos-sonhos")
    runner = _build_runner(store, fake_redis)
    res = await runner.handle_batch(
        MessageBatch(
            tenant_id="companhia-dos-sonhos",
            contact_phone="5591988885555",
            session_id="Danyllo",
            messages=[
                IncomingMessage(
                    message_id="MSG_K_ST_1",
                    tenant_id="companhia-dos-sonhos",
                    provider=Provider.WAHA,
                    session_id="Danyllo",
                    contact_phone="5591988885555",
                    contact_name="D Lobo",
                    type=MessageType.TEXT,
                    text="Vocês tem o king ainda?",
                )
            ],
            first_received_at=FIXED_NOW,
            flushed_at=FIXED_NOW,
        )
    )
    assert all(t.tool != "check_available_slots" for t in res.executed_tools)
    assert "09:00" not in (res.reply_text or "")

    prompt_turn2 = build_system_prompt(
        tenant,
        res.lead,
        stage=ConversationStage.OFFER_CATALOG,
        now=FIXED_NOW,
        history=[HumanMessage(content="Vocês tem o king ainda?"), AIMessage(content=res.reply_text or "Temos sim!")],
    )
    assert "<role_and_identity>" in prompt_turn2
    assert "<voice_and_conversation_pacing>" in prompt_turn2
    assert "<tool_calling_policy>" in prompt_turn2
    assert "<anti_hallucination_guardrails>" in prompt_turn2
    assert "<pre_response_self_check>" in prompt_turn2
    assert "<conversation_memory_and_context>" in prompt_turn2
    assert "<brazilian_humanizer_writing_rules>" in prompt_turn2
    assert "ja_cumprimentado_nesta_sessao=sim" in prompt_turn2


# =========================================================================== #
# 7. Humanizer PT-BR, Categorias/Nichos Customizados, Planos e Follow-up
# =========================================================================== #
async def test_humanizer_pt_br_custom_niche_plans_and_automatic_followup(
    store: JsonTenantDataStore,
    fake_redis,
):
    from datetime import timedelta
    from langchain_core.messages import AIMessage, HumanMessage
    from src.agent.followup import FollowUpService
    from src.agent.prompts import build_system_prompt
    from src.core.schemas import LeadProfile, LeadStatus, Segment, TenantConfig
    from src.tools.tenant_data import TenantData

    # 1. Limpeza de travessão literário de IA (em-dash) + preservação do negrito nativo
    raw_ai_text = "Temos **2 opções** King — ambas com entrega grátis — na nossa loja."
    assert sanitize_whatsapp_formatting(raw_ai_text) == "Temos *2 opções* King, ambas com entrega grátis, na nossa loja."

    # 2. Criação de cliente em categoria específica (ex.: Odontologia / Energia Solar) + Plano + Instruções Customizadas
    custom_tenant = TenantData(
        demo_data=False,
        tenant=TenantConfig(
            tenant_id="clinica-sorriso",
            display_name="Clínica Sorriso Prime",
            segment=Segment.DENTAL,
            niche_name="Implantes e Lentes de Contato Dental",
            custom_instructions="Sempre pergunte se o paciente já possui radiografia panorâmica recente.",
            plan_tier="enterprise",
            monthly_turn_limit=5000,
            followup_enabled=True,
            followup_delay_minutes=60,
            followup_max_attempts=2,
        ),
    )
    store.save(custom_tenant, create=True)

    lead = LeadProfile(
        tenant_id="clinica-sorriso",
        phone="5591988886666",
        name="Dra. Fernanda",
        status=LeadStatus.QUALIFIED,
        last_interaction_at=FIXED_NOW - timedelta(minutes=90),
    )
    prompt = build_system_prompt(custom_tenant, lead, now=FIXED_NOW)
    assert "nicho: `Implantes e Lentes de Contato Dental`" in prompt
    assert "<tenant_custom_instructions>" in prompt
    assert "radiografia panorâmica recente" in prompt

    # 3. Teste do motor de Follow-up Automático
    conv_store = LeadConversationStore(fake_redis)
    pause_registry = AIPauseRegistry(fake_redis)
    await conv_store.save(
        lead,
        [
            HumanMessage(content="Oi, gostaria de saber sobre implantes"),
            AIMessage(content="Olá, Dra. Fernanda! Você já possui radiografia panorâmica recente?"),
        ],
    )

    gw = _ReactionRecordingGateway()
    followup_svc = FollowUpService(
        store=store,
        conversation_store=conv_store,
        pause_registry=pause_registry,
        gateway_resolver=lambda _p: gw,
        clock=_fixed_clock,
    )

    # Disparo real do sweep
    sweep1 = await followup_svc.run_sweep(tenant_id="clinica-sorriso")
    assert sweep1["eligible_count"] == 1
    assert sweep1["sent_count"] == 1
    assert len(gw.sent_messages) == 1
    assert "Fernanda" in gw.sent_messages[0]
    assert "Clínica Sorriso Prime" in gw.sent_messages[0]

    # Segunda execução imediata não reenvia pois last_interaction_at foi atualizado para FIXED_NOW
    sweep2 = await followup_svc.run_sweep(tenant_id="clinica-sorriso")
    assert sweep2["sent_count"] == 0


# =========================================================================== #
# 8. Auditoria de Segurança (Security Hardening Regression Suite)
# =========================================================================== #
def test_security_audit_hardening_controls(tmp_path):
    import shutil
    from fastapi.testclient import TestClient
    from pydantic import ValidationError
    from main import create_app
    from src.config import Settings
    from src.gateways.astracalls import AstraCallsGateway
    from src.gateways.waha import WahaGateway
    from src.tools.tenant_data import GHLTenantConfig

    # 1. Proteção Anti-Exfiltração de Segredos no GHLTenantConfig (Finding 1)
    with pytest.raises(ValidationError):
        GHLTenantConfig(token_setting="admin_token", location_setting="ghl_vizzu_location_id")
    with pytest.raises(ValidationError):
        GHLTenantConfig(token_setting="openai_api_key", location_setting="ghl_vizzu_location_id")
    valid_ghl = GHLTenantConfig(token_setting="ghl_vizzu_api_token", location_setting="ghl_vizzu_location_id")
    assert valid_ghl.token_setting == "ghl_vizzu_api_token"

    # 2. Proteção contra Path Traversal em download_media (Finding 3)
    assert WahaGateway._is_safe_media_path("/api/files/default/audio.ogg") is True
    assert WahaGateway._is_safe_media_path("/api/files/../../etc/passwd") is False
    assert WahaGateway._is_safe_media_path("/api/server/stop") is False
    assert AstraCallsGateway._is_safe_media_path("/api/sessions/s1/messages/m1/media") is True
    assert AstraCallsGateway._is_safe_media_path("/api/sessions/../../admin") is False

    # 3. Security Headers, Mascaramento de webhook_secret e Limite de Payload 2MB (Findings 2, 4, 5)
    tenants_tmp = tmp_path / "tenants"
    shutil.copytree(PROJECT_ROOT / "data" / "tenants", tenants_tmp)
    settings = Settings(
        _env_file=None,
        environment="development",
        redis_backend="fake",
        tenant_data_dir=str(tenants_tmp),
        sandbox_config_path=str(tmp_path / "sandbox.json"),
        waha_base_url="https://waha.lobo.test",
        waha_api_key="waha-test-key",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        health_res = client.get("/health")
        assert health_res.status_code == 200
        assert health_res.headers.get("X-Content-Type-Options") == "nosniff"
        assert health_res.headers.get("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'" in (health_res.headers.get("Content-Security-Policy") or "")

        # Configura um webhook_secret e verifica se a API mascara o valor na listagem
        put_res = client.put(
            "/api/admin/webhooks/loboads",
            json={
                "whatsapp_provider": "waha",
                "whatsapp_session_id": "Danyllo",
                "webhook_secret": "segredo_super_confidencial_9988",
            },
        )
        assert put_res.status_code == 200
        put_data = put_res.json()
        assert put_data["webhook_secret_configured"] is True
        assert put_data["webhook_secret_masked"] == "••••••••9988"
        assert "webhook_secret" not in put_data

        # Reenviar a máscara não deve sobrescrever o segredo original no arquivo
        put_masked = client.put(
            "/api/admin/webhooks/loboads",
            json={
                "whatsapp_provider": "waha",
                "whatsapp_session_id": "Danyllo",
                "webhook_secret": "••••••••9988",
            },
        )
        assert put_masked.status_code == 200
        assert app.state.tenants.get("loboads").tenant.webhook_secret == "segredo_super_confidencial_9988"

        # Restaura webhook_secret = None no loboads para não afetar o arquivo do repo
        client.put(
            "/api/admin/webhooks/loboads",
            json={"whatsapp_provider": "waha", "whatsapp_session_id": None, "webhook_secret": ""},
        )

        # Payload acima de 2MB no webhook deve retornar HTTP 413
        oversized = b"x" * (2 * 1024 * 1024 + 100)
        too_large_res = client.post(
            "/webhook/loboads/waha",
            content=oversized,
            headers={"Content-Type": "application/json"},
        )
        assert too_large_res.status_code == 413



