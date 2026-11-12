"""Testes unitários do motor agêntico (`src/agent/`) e modelos ORM (`src/database/`)."""

from __future__ import annotations

from datetime import datetime, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable

from main import create_app
from src.agent import (
    AgentIntent,
    AudioTranscriber,
    ConversationStage,
    DeterministicSalesChatModel,
    JEVGuard,
    LeadConversationStore,
    SalesAgentRunner,
    build_system_prompt,
    build_tenant_tools,
    extract_lead_attributes,
    verify_no_unbacked_prices,
)
from src.config import PROJECT_ROOT, Settings
from src.core.exceptions import ConfigurationError, TranscriptionError
from src.core.schemas import (
    IncomingMessage,
    LeadProfile,
    MediaAttachment,
    MessageBatch,
    MessageType,
    Provider,
    Segment,
)
from src.database import Conversation, DatabaseManager, MessageLog, Tenant
from src.tools.calendar import CalendarService, InMemoryBookingRepository
from src.tools.catalog import CatalogService
from src.tools.handoff import AIPauseRegistry, HandoffReason, HandoffService
from src.tools.results import ToolResult, ToolStatus
from src.tools.tenant_data import JsonTenantDataStore

UTC = timezone.utc
PHONE = "5591988887777"
NOW = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)


@pytest.fixture
def store():
    return JsonTenantDataStore(PROJECT_ROOT / "data" / "tenants", allow_demo=True)


@pytest.fixture
def fake_redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


# =========================================================================== #
# JEV System One & Guardrail Anti-Alucinação
# =========================================================================== #
def test_jev_detects_clinical_emergency_human_request_and_discount():
    jev = JEVGuard()

    crisis = jev.analyze("Estou pensando em suicídio, não aguento mais viver", segment=Segment.PSYCHOLOGY)
    assert crisis.immediate_handoff is True
    assert crisis.handoff_reason is HandoffReason.CLINICAL_EMERGENCY
    assert crisis.stage is ConversationStage.HANDOFF

    human = jev.analyze("Quero falar com um atendente humano por favor")
    assert human.immediate_handoff is True
    assert human.handoff_reason is HandoffReason.CUSTOMER_REQUEST

    discount = jev.analyze("Consegue fazer 20% de desconto ou cobrir oferta da concorrência?")
    assert discount.immediate_handoff is True
    assert discount.handoff_reason is HandoffReason.DISCOUNT_NEGOTIATION


def test_jev_routes_stages_and_extracts_attributes():
    jev = JEVGuard()

    cat = jev.analyze("Queria ver um colchão king de molas ensacadas para entrega em Ananindeua")
    assert cat.intent is AgentIntent.CATALOG_PRICE
    assert cat.stage is ConversationStage.OFFER_CATALOG
    assert cat.extracted_attributes == {
        "cidade": "Ananindeua",
        "tamanho": "king",
        "tecnologia": "molas ensacadas",
    }

    book = jev.analyze("Quero agendar visita na loja amanhã")
    assert book.intent is AgentIntent.BOOKING
    assert book.stage is ConversationStage.BOOKING

    deliv = jev.analyze("Qual o prazo de entrega para Castanhal?")
    assert deliv.intent is AgentIntent.DELIVERY
    assert deliv.stage is ConversationStage.OFFER_CATALOG

    biz = jev.analyze("Qual o endereço e horário de funcionamento?")
    assert biz.intent is AgentIntent.BUSINESS_INFO
    assert biz.stage is ConversationStage.OFFER_CATALOG

    disc = jev.analyze("Olá, bom dia!")
    assert disc.intent is AgentIntent.DISCOVERY
    assert disc.stage is ConversationStage.DISCOVERY

    psy_attrs = extract_lead_attributes("Busco avaliação neuropsicológica para meu filho (criança)", Segment.PSYCHOLOGY)
    assert psy_attrs["publico"] == "infantil"
    assert psy_attrs["modalidade_interesse"] == "avaliação neuropsicológica"


def test_verify_no_unbacked_prices():
    tool_res = ToolResult.success(
        "search_catalog",
        "companhia-dos-sonhos",
        {
            "results": [
                {
                    "price_formatted": "R$ 2.899,00",
                    "installments_formatted": "10x de R$ 289,90 sem juros",
                    "cash_price_formatted": "R$ 2.754,05",
                }
            ]
        },
    )
    ok, bad = verify_no_unbacked_prices(
        "O colchão custa R$ 2.899,00 em 10x de R$ 289,90 ou R$ 2.754,05 à vista.",
        [tool_res],
    )
    assert ok is True and bad == []

    ok2, bad2 = verify_no_unbacked_prices(
        "O colchão custa R$ 2.899,00, mas faço por R$ 1.999,00 para fechar agora!",
        [tool_res],
    )
    assert ok2 is False and bad2 == ["R$ 1.999,00"]


# =========================================================================== #
# System Prompts & Isolamento de Tools
# =========================================================================== #
def test_build_system_prompt_per_segment(store):
    for tid, expected_keyword in [
        ("companhia-dos-sonhos", "LOJA DE COLCHÕES"),
        ("splash-piscinas", "LOJA DE PISCINAS"),
        ("vizzu", "CLÍNICA DE PSICOLOGIA"),
    ]:
        t = store.get(tid)
        lead = LeadProfile(tenant_id=tid, phone=PHONE, name="Ana", attributes={"cidade": "Belém"})
        prompt = build_system_prompt(t, lead, stage=ConversationStage.OFFER_CATALOG, now=NOW)
        assert expected_keyword in prompt
        assert t.tenant.display_name in prompt
        assert "PROIBIDO INVENTAR" in prompt or "ZERO CONFIANÇA EM MEMÓRIA NUMÉRICA" in prompt
        assert "cidade=Belém" in prompt


async def test_bound_tools_enforce_tenant_closure_isolation(store, fake_redis):
    catalog = CatalogService(store, clock=lambda: NOW)
    calendar = CalendarService(store, InMemoryBookingRepository(), clock=lambda: NOW)
    handoff = HandoffService(store, AIPauseRegistry(fake_redis), lambda _t: None, clock=lambda: NOW)
    lead = LeadProfile(tenant_id="vizzu", phone=PHONE)
    recorded: list[ToolResult] = []

    tools = build_tenant_tools(
        tenant_id="vizzu",
        lead=lead,
        catalog=catalog,
        calendar=calendar,
        handoff=handoff,
        recorded_results=recorded,
    )
    by_name = {t.name: t for t in tools}

    # Nenhuma tool expõe `tenant_id` no schema do LLM
    for t in tools:
        schema_props = t.args_schema.model_json_schema().get("properties", {})
        assert "tenant_id" not in schema_props
        assert "lead_phone" not in schema_props

    # Buscar colchão dentro do turno da Vizzu nunca vaza itens de outro tenant (retorna apenas carros-chefes da Vizzu)
    payload = await by_name["search_catalog"].ainvoke({"query": "colchão casal molas ensacadas"})
    assert payload["tenant_id"] == "vizzu"
    assert payload["status"] == ToolStatus.OK.value
    assert payload["requires_human"] is False
    assert payload["data"]["featured_fallback"] is True
    assert all(i["item_id"].startswith("VZ-") for i in payload["data"]["results"])
    assert len(recorded) == 1 and recorded[0].tenant_id == "vizzu"


# =========================================================================== #
# Fallback Seguro do LLM & Handoff Automático por Tool (`requires_human=True`)
# =========================================================================== #
async def test_llm_exception_triggers_safe_technical_failure_handoff(store, fake_redis):
    class CrashingLLM(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "crashing-llm"

        def bind_tools(self, tools, **kwargs) -> Runnable:
            return self

        def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
            raise RuntimeError("Gemini API 503 Service Unavailable")

    pause = AIPauseRegistry(fake_redis)
    runner = SalesAgentRunner(
        store=store,
        catalog=CatalogService(store, clock=lambda: NOW),
        calendar=CalendarService(store, clock=lambda: NOW),
        handoff=HandoffService(store, pause, lambda _t: None, clock=lambda: NOW),
        pause_registry=pause,
        conversation_store=LeadConversationStore(fake_redis),
        llm=CrashingLLM(),
        clock=lambda: NOW,
    )
    batch = MessageBatch(
        tenant_id="splash-piscinas",
        session_id="s1",
        contact_phone=PHONE,
        messages=[
            IncomingMessage(
                message_id="m1",
                tenant_id="splash-piscinas",
                provider=Provider.ASTRACALLS,
                session_id="s1",
                contact_phone=PHONE,
                text="Quanto custa a piscina de 6 metros?",
            )
        ],
        first_received_at=NOW,
        flushed_at=NOW,
    )
    res = await runner.handle_batch(batch)
    assert res.human_handoff_requested is True
    assert res.handoff_reason == HandoffReason.TECHNICAL_FAILURE.value
    assert "instabilidade" in (res.reply_text or "")
    assert await pause.is_paused("splash-piscinas", PHONE)


async def test_item_with_price_on_request_triggers_handoff_without_inventing_price(store, fake_redis):
    pause = AIPauseRegistry(fake_redis)
    runner = SalesAgentRunner(
        store=store,
        catalog=CatalogService(store, clock=lambda: NOW),
        calendar=CalendarService(store, clock=lambda: NOW),
        handoff=HandoffService(store, pause, lambda _t: None, clock=lambda: NOW),
        pause_registry=pause,
        conversation_store=LeadConversationStore(fake_redis),
        llm=DeterministicSalesChatModel(),
        clock=lambda: NOW,
    )
    # Em splash-piscinas, o Aquecedor Solar tem `amount: null` ("sob consulta")
    batch = MessageBatch(
        tenant_id="splash-piscinas",
        session_id="s1",
        contact_phone=PHONE,
        messages=[
            IncomingMessage(
                message_id="m_aqc",
                tenant_id="splash-piscinas",
                provider=Provider.ASTRACALLS,
                session_id="s1",
                contact_phone=PHONE,
                text="Quanto custa o aquecedor solar?",
            )
        ],
        first_received_at=NOW,
        flushed_at=NOW,
    )
    res = await runner.handle_batch(batch)
    assert res.human_handoff_requested is True
    assert "R$" not in (res.reply_text or "")
    assert await pause.is_paused("splash-piscinas", PHONE)


# =========================================================================== #
# Webhook `from_me` Humano vs Eco da IA (`main.py`)
# =========================================================================== #
async def test_from_me_human_pauses_ai_while_bot_echo_does_not():
    s = Settings(
        _env_file=None,
        redis_backend="fake",
        waha_base_url="https://waha.example.com",
        waha_api_key="secret",
        sandbox_config_path=None,
    )
    app = create_app(s)
    with TestClient(app) as client:
        pause: AIPauseRegistry = app.state.pause_registry

        # 1. Registra que a IA acabou de enviar "Olá! Como posso ajudar?" (msg_id="bot_1")
        await pause.register_bot_outbound(
            "vizzu",
            PHONE,
            message_id="bot_1",
            text="Olá! Como posso ajudar?",
        )

        # Eco do próprio bot chegando no webhook `message.any`: NÃO deve pausar a IA
        echo_payload = {
            "event": "message.any",
            "session": "default",
            "payload": {
                "id": "bot_1",
                "timestamp": 1762800000,
                "from": f"{PHONE}@c.us",
                "to": f"{PHONE}@c.us",
                "fromMe": True,
                "body": "Olá! Como posso ajudar?",
                "hasMedia": False,
            },
        }
        r1 = client.post("/webhook/vizzu/waha", json=echo_payload)
        assert r1.status_code == 200 and r1.json()["from_me"] == 1
        assert await pause.is_paused("vizzu", PHONE) is False

        # 2. Atendente humano digita no celular e envia mensagem (`id="human_99"`): DEVE pausar a IA
        human_payload = {
            "event": "message.any",
            "session": "default",
            "payload": {
                "id": "human_99",
                "timestamp": 1762800010,
                "from": f"{PHONE}@c.us",
                "to": f"{PHONE}@c.us",
                "fromMe": True,
                "body": "Oi, aqui é a Dra. Camila da Vizzu, vou continuar seu atendimento!",
                "hasMedia": False,
            },
        }
        r2 = client.post("/webhook/vizzu/waha", json=human_payload)
        assert r2.status_code == 200 and r2.json()["from_me"] == 1
        assert await pause.is_paused("vizzu", PHONE) is True


# =========================================================================== #
# Transcrição de Áudio & Modelos de Banco de Dados
# =========================================================================== #
async def test_audio_transcriber_error_when_no_provider_configured():
    transcriber = AudioTranscriber(Settings(_env_file=None))
    msg = IncomingMessage(
        message_id="a1",
        tenant_id="vizzu",
        provider=Provider.WAHA,
        session_id="default",
        contact_phone=PHONE,
        type=MessageType.AUDIO,
        media=MediaAttachment(data_base64="b2dn", mimetype="audio/ogg"),
    )
    with pytest.raises(TranscriptionError, match="nenhum provedor STT"):
        await transcriber.transcribe_message(msg)


def test_database_models_mask_pii_and_validate_config():
    tenant = Tenant(tenant_id="vizzu", display_name="Vizzu Psicologia", segment="psychology")
    conv = Conversation(tenant_id="vizzu", contact_phone=PHONE, status="qualified")
    log = MessageLog(tenant_id="vizzu", direction="inbound", contact_phone=PHONE, turn_id="t_1", content="Oi")

    assert "vizzu" in repr(tenant)
    assert PHONE not in repr(conv) and "55*******7777" in repr(conv)
    assert PHONE not in repr(log) and "55*******7777" in repr(log)

    with pytest.raises(ConfigurationError):
        DatabaseManager("")
