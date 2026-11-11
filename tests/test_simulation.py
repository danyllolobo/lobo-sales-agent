"""Simulação End-to-End (E2E) e Suíte de Testes do Grafo LangGraph (ETAPA 4).

Pode ser executado tanto via pytest quanto diretamente no terminal como script CLI:
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_simulation.py -v
    .\\.venv\\Scripts\\python.exe tests/test_simulation.py
    .\\.venv\\Scripts\\python.exe tests/test_simulation.py --live
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from datetime import datetime, timezone
from pathlib import Path

# Permite rodar `python tests/test_simulation.py` diretamente a partir da raiz
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import fakeredis
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable

from src.agent import (
    AudioTranscriber,
    ConversationStage,
    DeterministicSalesChatModel,
    LeadConversationStore,
    SalesAgentRunner,
    TurnResult,
    create_gemini_chat_model,
)
from src.buffer.message_queue import MessageBuffer
from src.config import Settings, get_settings
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
    mask_phone,
)
from src.crm import create_crm
from src.crm.base import CRMConnector
from src.gateways.base import SendResult, WhatsAppGateway
from src.tools.calendar import CalendarService, InMemoryBookingRepository
from src.tools.catalog import CatalogService
from src.tools.handoff import (
    EMERGENCY_MESSAGE,
    AIPauseRegistry,
    HandoffReason,
    HandoffService,
)
from src.tools.tenant_data import JsonTenantDataStore

UTC = timezone.utc
LEAD_PHONE = "5591988887777"
FIXED_NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)  # Terça, 10/11/2026 12:00 (Belém)


def fixed_clock() -> datetime:
    return FIXED_NOW


class SimulatorGateway(WhatsAppGateway):
    """Gateway em memória para capturar `composing` e `OutgoingMessage` na simulação."""

    provider = Provider.ASTRACALLS

    def __init__(self) -> None:
        super().__init__("https://sim.local", default_session="sessao-principal")
        self.typing_events: list[tuple[str, str, bool]] = []
        self.sent_messages: list[OutgoingMessage] = []

    def parse_webhook(self, tenant_id: str, payload):  # pragma: no cover
        raise NotImplementedError

    def verify_signature(self, body: bytes, headers) -> bool:
        return True

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        self.sent_messages.append(message)
        return SendResult(provider=self.provider, message_id=f"sim_{len(self.sent_messages)}")

    async def set_typing(self, session_id: str, to: str, typing: bool = True) -> None:
        self.typing_events.append((session_id, to, typing))

    async def mark_seen(self, session_id: str, to: str, message_ids: list[str]) -> None:
        return None

    async def download_media(self, message: IncomingMessage) -> bytes:
        return b""

    async def deliver(self, message: OutgoingMessage) -> SendResult:
        await self.set_typing(message.session_id, message.chat_id or message.contact_phone, True)
        res = await self.send_message(message)
        await self.set_typing(message.session_id, message.chat_id or message.contact_phone, False)
        return res

    def _auth_headers(self) -> dict[str, str]:
        return {}


class FakeCRMConnector(CRMConnector):
    """Conector CRM em memória para inspecionar notas privadas e tags."""

    def __init__(self, *, human_active: bool = False) -> None:
        super().__init__("https://crm.sim.local")
        self.notes: list[tuple[str, str]] = []
        self.tags_history: list[list[str]] = []
        self.human_active_flag = human_active

    async def ensure_contact(self, lead: LeadProfile) -> str:
        lead.crm_contact_id = lead.crm_contact_id or f"crm_{lead.phone[-4:]}"
        return lead.crm_contact_id

    async def add_private_note(self, lead: LeadProfile, content: str) -> None:
        await self.ensure_contact(lead)
        self.notes.append((lead.tenant_id, content))

    async def add_tags(self, lead: LeadProfile, tags: list[str]) -> list[str]:
        for t in tags:
            lead.add_tag(t)
        self.tags_history.append(list(lead.tags))
        return list(lead.tags)

    async def remove_tags(self, lead: LeadProfile, tags: list[str]) -> None:
        for t in tags:
            lead.remove_tag(t)

    async def is_human_active(self, lead: LeadProfile) -> bool:
        return self.human_active_flag or lead.has_tag(StandardTag.HUMAN_HANDOFF)

    def _headers(self) -> dict[str, str]:
        return {}


def build_simulation_harness(
    *,
    llm: BaseChatModel | None = None,
    silence_seconds: float = 0.15,
    max_window_seconds: float = 1.5,
    transcribe_fn=None,
):
    """Monta todo o ecossistema (Store, FakeRedis, Buffer, Tools, CRM, Gateway e Runner)."""
    store = JsonTenantDataStore(PROJECT_ROOT / "data" / "tenants", allow_demo=True)
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    pause_registry = AIPauseRegistry(redis)
    crm = FakeCRMConnector()
    gateway = SimulatorGateway()

    catalog = CatalogService(store, clock=fixed_clock)
    calendar = CalendarService(store, InMemoryBookingRepository(), clock=fixed_clock)
    handoff = HandoffService(
        store,
        pause_registry,
        lambda _t: crm,
        pause_hours=24.0,
        clock=fixed_clock,
    )
    conv_store = LeadConversationStore(redis)
    test_settings = Settings(_env_file=None, redis_backend="fake")
    transcriber = AudioTranscriber(test_settings, transcribe_fn=transcribe_fn)

    turns: list[TurnResult] = []
    turn_event = asyncio.Event()

    async def on_flush(batch: MessageBatch) -> None:
        res = await runner.handle_batch(batch)
        turns.append(res)
        turn_event.set()

    buffer = MessageBuffer(
        redis,
        silence_seconds=silence_seconds,
        max_window_seconds=max_window_seconds,
        on_flush=on_flush,
    )
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
        llm=llm or DeterministicSalesChatModel(),
        settings=test_settings,
        clock=fixed_clock,
    )
    return {
        "store": store,
        "redis": redis,
        "buffer": buffer,
        "runner": runner,
        "gateway": gateway,
        "crm": crm,
        "pause": pause_registry,
        "turns": turns,
        "turn_event": turn_event,
    }


def make_incoming(
    msg_id: str,
    text: str,
    *,
    tenant_id: str = "companhia-dos-sonhos",
    phone: str = LEAD_PHONE,
    name: str = "Carlos Almeida",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=msg_id,
        tenant_id=tenant_id,
        provider=Provider.ASTRACALLS,
        session_id="sessao-principal",
        contact_phone=phone,
        contact_name=name,
        chat_id=f"{phone}@s.whatsapp.net",
        type=MessageType.TEXT,
        text=text,
        ad_referral={"sourceId": "ad_colchao_prime_01", "ctwaClid": "ctwa_999"},
    )


# =========================================================================== #
# Testes E2E Automatizados (pytest)
# =========================================================================== #
async def test_e2e_three_rapid_messages_debounced_into_single_turn_with_real_price_and_delivery():
    """Cenário Principal (MASTER_KICKOFF_PROMPT.md ETAPA 4.3):
    Lead envia 3 mensagens rápidas:
      1. 'Olá, boa tarde'
      2. 'Queria ver um colchão de casal com molas ensacadas'
      3. 'Vocês entregam em Belém?'
    O buffer agrupa as 3 em 1 único turno, a IA consulta catálogo e entrega
    e responde com preço e prazo reais.
    """
    h = build_simulation_harness(silence_seconds=0.12, max_window_seconds=1.5)
    buffer: MessageBuffer = h["buffer"]
    gateway: SimulatorGateway = h["gateway"]
    turns: list[TurnResult] = h["turns"]
    turn_event: asyncio.Event = h["turn_event"]

    messages = [
        "Olá, boa tarde",
        "Queria ver um colchão de casal com molas ensacadas",
        "Vocês entregam em Belém?",
    ]

    try:
        for idx, text in enumerate(messages, start=1):
            msg = make_incoming(f"msg_{idx}", text)
            await buffer.push_message(msg.tenant_id, msg.session_id, msg.contact_phone, msg)
            await asyncio.sleep(0.03)  # Simula digitação rápida (< janela de silêncio)

        await asyncio.wait_for(turn_event.wait(), timeout=3.0)

        # 1. Agrupado em UM único turno
        assert len(turns) == 1
        turn = turns[0]
        assert not turn.silenced
        assert not turn.human_handoff_requested
        assert turn.stage == ConversationStage.OFFER_CATALOG.value

        # 2. Ferramentas determinísticas acionadas
        tool_names = [t.tool for t in turn.executed_tools]
        assert "search_catalog" in tool_names
        assert "get_delivery_info" in tool_names

        # 3. Resposta contém valores e prazos reais exatos da Companhia dos Sonhos
        assert turn.reply_text is not None
        assert "Colchão Molas Ensacadas Prime Casal" in turn.reply_text
        assert "R$ 2.899,00" in turn.reply_text
        assert "10x de R$ 289,90 sem juros" in turn.reply_text
        assert "R$ 2.754,05" in turn.reply_text
        assert "Belém" in turn.reply_text
        assert "Grátis" in turn.reply_text
        assert "1 a 2 dias úteis" in turn.reply_text

        # 4. OutgoingMessage despachado pelo gateway com "composing" antes
        assert len(gateway.sent_messages) == 1
        assert gateway.sent_messages[0].text == turn.reply_text
        assert any(composing is True for _, _, composing in gateway.typing_events)

        # 5. Perfil do lead enriquecido e qualificado
        assert turn.lead.status is LeadStatus.QUALIFIED
        assert turn.lead.has_tag(StandardTag.QUALIFIED)
        assert turn.lead.source == "meta_ads"
        assert turn.lead.attributes["tamanho"] == "casal"
        assert turn.lead.attributes["tecnologia"] == "molas ensacadas"
        assert turn.lead.attributes["cidade"] == "Belém"
    finally:
        await buffer.aclose()


async def test_e2e_booking_flow_offers_two_slots_and_confirms_appointment():
    """Testa o nó de agendamento: oferece 2 opções concretas e confirma horário escolhido."""
    h = build_simulation_harness()
    runner: SalesAgentRunner = h["runner"]
    buffer: MessageBuffer = h["buffer"]

    try:
        # Turno 1: pede horários para quarta-feira (2026-11-11)
        batch1 = MessageBatch(
            tenant_id="companhia-dos-sonhos",
            session_id="sessao-principal",
            contact_phone=LEAD_PHONE,
            messages=[make_incoming("b1", "Quero agendar uma visita à loja no dia 2026-11-11")],
            first_received_at=FIXED_NOW,
            flushed_at=FIXED_NOW,
        )
        res1 = await runner.handle_batch(batch1)
        assert [t.tool for t in res1.executed_tools] == ["check_available_slots"]
        assert "quarta-feira, 11/11 às 09:00" in (res1.reply_text or "")
        assert "quarta-feira, 11/11 às 12:00" in (res1.reply_text or "")

        # Turno 2: confirma o slot das 09:00
        batch2 = MessageBatch(
            tenant_id="companhia-dos-sonhos",
            session_id="sessao-principal",
            contact_phone=LEAD_PHONE,
            messages=[make_incoming("b2", "Perfeito, quero agendar em 2026-11-11T09:00:00-03:00")],
            first_received_at=FIXED_NOW,
            flushed_at=FIXED_NOW,
        )
        res2 = await runner.handle_batch(batch2)
        assert [t.tool for t in res2.executed_tools] == ["create_booking"]
        assert "Agendamento confirmado" in (res2.reply_text or "")
        assert res2.lead.status is LeadStatus.VISIT_SCHEDULED
        assert res2.lead.has_tag(StandardTag.VISIT_SCHEDULED)
    finally:
        await buffer.aclose()


async def test_e2e_vizzu_clinical_emergency_triggers_immediate_handoff_and_silences_next_turns():
    """Em caso de emergência clínica na Vizzu, aciona CVV 188 / SAMU 192, anota no CRM e silencia depois."""
    h = build_simulation_harness()
    runner: SalesAgentRunner = h["runner"]
    crm: FakeCRMConnector = h["crm"]
    buffer: MessageBuffer = h["buffer"]

    try:
        batch_crisis = MessageBatch(
            tenant_id="vizzu",
            session_id="waha-vizzu",
            contact_phone=LEAD_PHONE,
            messages=[make_incoming("v1", "Não aguento mais viver, estou pensando em me machucar", tenant_id="vizzu")],
            first_received_at=FIXED_NOW,
            flushed_at=FIXED_NOW,
        )
        res = await runner.handle_batch(batch_crisis)
        assert res.human_handoff_requested is True
        assert res.handoff_reason == HandoffReason.CLINICAL_EMERGENCY.value
        assert res.reply_text == EMERGENCY_MESSAGE
        assert "188" in res.reply_text and "192" in res.reply_text
        assert len(crm.notes) == 1
        assert crm.notes[0][0] == "vizzu"
        assert "URGENTE" in crm.notes[0][1]

        # Turno seguinte do mesmo lead: IA deve silenciar imediatamente no Nó 1 (node_triage)
        batch_after = MessageBatch(
            tenant_id="vizzu",
            session_id="waha-vizzu",
            contact_phone=LEAD_PHONE,
            messages=[make_incoming("v2", "Oi, alguém aí?", tenant_id="vizzu")],
            first_received_at=FIXED_NOW,
            flushed_at=FIXED_NOW,
        )
        res_after = await runner.handle_batch(batch_after)
        assert res_after.silenced is True
        assert res_after.outgoing_message is None
        assert res_after.reply_text is None
    finally:
        await buffer.aclose()


async def test_e2e_price_guardrail_blocks_hallucinated_price_and_forces_handoff():
    """Se um LLM defeituoso inventar um preço ('R$ 999,00') sem respaldo de ferramenta, o guardrail bloqueia."""

    class HallucinatingLLM(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "hallucinating-llm"

        def bind_tools(self, tools, **kwargs) -> Runnable:
            return self

        def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content="Claro! Esse colchão sai por apenas R$ 999,00 hoje no Pix!")
                    )
                ]
            )

    h = build_simulation_harness(llm=HallucinatingLLM())
    runner: SalesAgentRunner = h["runner"]
    crm: FakeCRMConnector = h["crm"]
    buffer: MessageBuffer = h["buffer"]

    try:
        batch = MessageBatch(
            tenant_id="companhia-dos-sonhos",
            session_id="sessao-principal",
            contact_phone=LEAD_PHONE,
            messages=[make_incoming("h1", "Quanto custa o colchão de casal?")],
            first_received_at=FIXED_NOW,
            flushed_at=FIXED_NOW,
        )
        res = await runner.handle_batch(batch)
        # O preço inventado JAMAIS pode chegar ao cliente
        assert "999" not in (res.reply_text or "")
        assert res.human_handoff_requested is True
        assert res.handoff_reason == HandoffReason.PRICE_UNAVAILABLE.value
        assert len(crm.notes) == 1
        assert "R$ 999,00" in crm.notes[0][1]
    finally:
        await buffer.aclose()


async def test_e2e_audio_message_is_transcribed_before_langgraph():
    """Áudio enviado pelo lead é transcrito antes de entrar no LangGraph."""
    async def fake_stt(audio_bytes: bytes, mimetype: str) -> str:
        assert audio_bytes == b"ogg-audio-payload"
        return "Queria ver um colchão de casal com molas ensacadas e saber se entregam em Belém"

    h = build_simulation_harness(transcribe_fn=fake_stt)
    runner: SalesAgentRunner = h["runner"]
    buffer: MessageBuffer = h["buffer"]

    try:
        audio_msg = IncomingMessage(
            message_id="aud_1",
            tenant_id="companhia-dos-sonhos",
            provider=Provider.ASTRACALLS,
            session_id="sessao-principal",
            contact_phone=LEAD_PHONE,
            type=MessageType.AUDIO,
            media=MediaAttachment(
                data_base64=base64.b64encode(b"ogg-audio-payload").decode("ascii"),
                mimetype="audio/ogg; codecs=opus",
            ),
        )
        batch = MessageBatch(
            tenant_id="companhia-dos-sonhos",
            session_id="sessao-principal",
            contact_phone=LEAD_PHONE,
            messages=[audio_msg],
            first_received_at=FIXED_NOW,
            flushed_at=FIXED_NOW,
        )
        res = await runner.handle_batch(batch)
        assert "R$ 2.899,00" in (res.reply_text or "")
        assert "Grátis" in (res.reply_text or "")
    finally:
        await buffer.aclose()


def test_configured_tenant_crm_ids_and_auto_assignment_flags():
    """Valida as configurações de CRM especificadas para Companhia dos Sonhos, Splash Piscinas e Vizzu."""
    store = JsonTenantDataStore(PROJECT_ROOT / "data" / "tenants", allow_demo=True)

    cs = store.get("companhia-dos-sonhos")
    assert cs.crm.chatwoot is not None
    assert cs.crm.chatwoot.account_id == 8
    assert cs.crm.chatwoot.assignee_means_human is False

    sp = store.get("splash-piscinas")
    assert sp.crm.chatwoot is not None
    assert sp.crm.chatwoot.account_id == 6
    assert sp.crm.chatwoot.assignee_means_human is False

    vz = store.get("vizzu")
    assert vz.crm.ghl is not None
    assert vz.crm.ghl.location_id == "19nHtRjWdzETs8Sv97Pv"

    # Verifica que create_crm usa o location_id configurado no JSON mesmo se não vier no Settings
    s = Settings(_env_file=None, ghl_vizzu_api_token="pit-test-token")
    ghl_conn = create_crm(vz, s)
    assert ghl_conn is not None and ghl_conn.location_id == "19nHtRjWdzETs8Sv97Pv"


# =========================================================================== #
# Execução via Terminal (CLI de Demonstração)
# =========================================================================== #
async def run_cli_simulation(*, use_live_gemini: bool = False) -> TurnResult:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = get_settings()
    llm: BaseChatModel = (
        create_gemini_chat_model(settings) if use_live_gemini else DeterministicSalesChatModel()
    )

    h = build_simulation_harness(llm=llm, silence_seconds=0.5, max_window_seconds=4.0)
    buffer: MessageBuffer = h["buffer"]
    turn_event: asyncio.Event = h["turn_event"]
    turns: list[TurnResult] = h["turns"]

    lead_messages = [
        "Olá, boa tarde",
        "Queria ver um colchão de casal com molas ensacadas",
        "Vocês entregam em Belém?",
    ]

    print("=" * 78)
    print("🐺 LOBO AI SALES AGENT — SIMULAÇÃO E2E (DEBOUNCE + LANGGRAPH + TOOLS)")
    print("=" * 78)
    print("Tenant: companhia-dos-sonhos (Chatwoot account_id=8, assignee_means_human=False)")
    print(f"Contato (LGPD masked): {mask_phone(LEAD_PHONE)}")
    print(f"Modelo LLM: {llm._llm_type}")
    print("-" * 78)

    try:
        for i, text in enumerate(lead_messages, start=1):
            msg = make_incoming(f"cli_msg_{i}", text)
            res = await buffer.push_message(msg.tenant_id, msg.session_id, msg.contact_phone, msg)
            print(f"[+0.{i * 10:02d}s] 📩 Lead enviou msg #{i}: {text!r} (lote={res.batch_size})")
            await asyncio.sleep(0.1)

        print("\n⏳ Aguardando janela de silêncio do Redis Debounce expirar...")
        await asyncio.wait_for(turn_event.wait(), timeout=15.0)
        turn = turns[0]

        print("\n✅ Lote único fechado e processado pelo LangGraph!")
        print(f"   • Turn ID: {turn.turn_id}")
        print(f"   • Estágio: {turn.stage} | Intenção: {turn.intent}")
        print(f"   • Latência total: {turn.latency_ms:.1f} ms")
        print(f"   • Ferramentas executadas ({len(turn.executed_tools)}):")
        for tr in turn.executed_tools:
            print(f"     - {tr.tool} -> status={tr.status.value}")
        print(f"   • Atributos extraídos do lead: {turn.lead.attributes}")
        print(f"   • Tags do lead: {turn.lead.tags}")
        print("-" * 78)
        print("🤖 RESPOSTA ÚNICA ENVIADA NO WHATSAPP:\n")
        print(turn.reply_text)
        print("=" * 78)
        return turn
    finally:
        await buffer.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulação E2E do Lobo AI Sales Agent")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Usa o modelo Google Gemini real configurado no .env em vez do modelo determinístico local",
    )
    args = parser.parse_args()
    asyncio.run(run_cli_simulation(use_live_gemini=args.live))
