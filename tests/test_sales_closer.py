"""Suíte de testes da Reengenharia Comercial: SDR / Closer Consultivo Master.

Valida os 5 cenários reais de vendas exigidos pela especificação:
1. Lead de Anúncio ("colchão de casal a partir de R$349 ... MENSAGEM DO CLIENTE: Ola Gostaria ver outros modelos")
   -> acolhe mencionando o anúncio, apresenta 2–3 destaques (carros-chefes) e faz pergunta de qualificação (zero handoff).
2. Pedido de Agendamento Direto ("Olá, vi a propaganda e gostaria de agendar minha avaliação" / "Quero agendar uma consulta")
   -> NÃO joga horários na lata nem transfere no escuro; faz triagem consultiva de especialidade e público/idade primeiro.
3. Lead Recorrente -> reconhece pelo nome com intimidade ("Oi, Carlos! Que bom falar com você de novo!") e pergunta como ajudar.
4. Quebra de Objeção de Preço ("Achei caro") -> argumenta durabilidade, saúde da coluna, entrega grátis e 10x sem juros (zero handoff).
5. Escalação Elegante ("Quero 30% de desconto à vista no Pix") -> zero jargão de robô ou "atendente humano"; consulta o gerente comercial.
6. Seletor Dinâmico de Modelos de IA (`llm_model`) na interface e por cliente sem necessidade de restart.
"""

from __future__ import annotations

from datetime import datetime, timezone

import fakeredis
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from main import create_app
from src.agent.graph import LeadConversationStore, SalesAgentRunner
from src.agent.nodes import DeterministicSalesChatModel, create_gemini_chat_model
from src.agent.prompts import HUMAN_PERSONA_RULES, build_system_prompt
from src.agent.state import AgentIntent, ConversationStage
from src.config import PROJECT_ROOT, Settings
from src.core.schemas import (
    IncomingMessage,
    LeadProfile,
    LeadStatus,
    MessageBatch,
    Provider,
)
from src.tools.calendar import CalendarService
from src.tools.catalog import FEATURED_FALLBACK_INSTRUCTION, CatalogService
from src.tools.handoff import AIPauseRegistry, HandoffReason, HandoffService
from src.tools.tenant_data import JsonTenantDataStore

NOW = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
FORBIDDEN_ROBOT_JARGON = (
    "sou uma inteligencia artificial",
    "sou um robo",
    "sou uma ia",
    "atendente humano",
    "transferir seu atendimento",
    "vou transferir",
    "banco de dados",
)


@pytest.fixture
def store() -> JsonTenantDataStore:
    return JsonTenantDataStore(PROJECT_ROOT / "data" / "tenants", allow_demo=True)


@pytest.fixture
def fake_redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


def _build_runner(store: JsonTenantDataStore, fake_redis) -> SalesAgentRunner:
    pause = AIPauseRegistry(fake_redis)
    return SalesAgentRunner(
        store=store,
        catalog=CatalogService(store, clock=lambda: NOW),
        calendar=CalendarService(store, clock=lambda: NOW),
        handoff=HandoffService(store, pause, lambda _t: None, clock=lambda: NOW),
        pause_registry=pause,
        conversation_store=LeadConversationStore(fake_redis),
        llm=DeterministicSalesChatModel(),
        clock=lambda: NOW,
    )


def _batch(
    tenant_id: str,
    text: str,
    *,
    phone: str = "5591988887777",
    contact_name: str | None = "Carlos",
    msg_id: str = "m1",
) -> MessageBatch:
    return MessageBatch(
        tenant_id=tenant_id,
        session_id="default",
        contact_phone=phone,
        messages=[
            IncomingMessage(
                message_id=msg_id,
                tenant_id=tenant_id,
                provider=Provider.ASTRACALLS,
                session_id="default",
                contact_phone=phone,
                contact_name=contact_name,
                text=text,
            )
        ],
        first_received_at=NOW,
        flushed_at=NOW,
    )


def _assert_zero_robot_jargon(reply_text: str) -> None:
    lowered = (
        reply_text.lower()
        .replace("ê", "e")
        .replace("ô", "o")
        .replace("ã", "a")
        .replace("á", "a")
    )
    for forbidden in FORBIDDEN_ROBOT_JARGON:
        assert forbidden not in lowered, f"Jargão proibido detectado na resposta: {forbidden!r}"


# =========================================================================== #
# Cenário 1 — Lead de Anúncio ("Ola Gostaria ver outros modelos")
# =========================================================================== #
async def test_scenario_1_ad_lead_wants_other_models_shows_featured_highlights_zero_handoff(
    store: JsonTenantDataStore,
    fake_redis,
):
    runner = _build_runner(store, fake_redis)
    ad_message = "colchão de casal a partir de R$349 ... MENSAGEM DO CLIENTE: Ola Gostaria ver outros modelos"

    res = await runner.handle_batch(_batch("companhia-dos-sonhos", ad_message))

    # 1. Zero handoff!
    assert res.human_handoff_requested is False
    assert res.stage == ConversationStage.OFFER_CATALOG.value
    assert res.reply_text is not None
    _assert_zero_robot_jargon(res.reply_text)

    # 2. Acolhe com naturalidade humana de loja (sem meta-linguagem robótica como "Vi que você veio pelo nosso anúncio")
    reply_lower = res.reply_text.lower()
    assert "seja muito bem-vindo" in reply_lower
    assert "vi que você veio pelo nosso anúncio" not in reply_lower
    assert res.lead.source == "meta_ads"

    # 3. Apresenta 2 ou 3 destaques do catálogo (carros-chefes) com preços validados da tool
    catalog_calls = [t for t in res.executed_tools if t.tool == "search_catalog"]
    assert len(catalog_calls) == 1
    assert catalog_calls[0].ok is True
    assert catalog_calls[0].requires_human is False
    assert catalog_calls[0].data.get("featured_fallback") is True
    assert FEATURED_FALLBACK_INSTRUCTION in (catalog_calls[0].agent_instruction or "")

    assert "CS-ENS-CASAL" in res.reply_text
    assert "CS-ENS-QUEEN" in res.reply_text
    assert "R$ 2.899,00" in res.reply_text
    assert "R$ 3.499,00" in res.reply_text

    # 4. Faz pergunta consultiva de qualificação (medida e preferência de conforto/firmeza)
    assert "medida" in reply_lower
    assert "conforto" in reply_lower or "firme" in reply_lower
    assert "?" in res.reply_text


# =========================================================================== #
# Cenário 2 — Pedido de Agendamento Direto ("Quero agendar uma consulta")
# =========================================================================== #
async def test_scenario_2_direct_booking_request_performs_triage_before_slots(
    store: JsonTenantDataStore,
    fake_redis,
):
    runner = _build_runner(store, fake_redis)

    # Entrada na Vizzu Psicologia sem qualificação prévia nem data explícita
    res = await runner.handle_batch(
        _batch(
            "vizzu",
            "Olá, vi a propaganda e gostaria de agendar minha avaliação",
            contact_name="Mariana",
        )
    )

    # 1. Zero handoff e NÃO joga horários na lata antes da triagem!
    assert res.human_handoff_requested is False
    assert not any(t.tool == "check_available_slots" for t in res.executed_tools)
    assert res.reply_text is not None
    _assert_zero_robot_jargon(res.reply_text)

    # 2. Faz a triagem consultiva de especialidade e público/idade primeiro
    reply_lower = res.reply_text.lower()
    assert "especialidade" in reply_lower or "profissional" in reply_lower
    assert "adulto" in reply_lower
    assert "filho" in reply_lower or "dependente" in reply_lower or "idade" in reply_lower
    assert "psicoterapia" in reply_lower
    assert "avaliação neuropsicológica" in reply_lower or "avaliacao neuropsicologica" in reply_lower


# =========================================================================== #
# Cenário 3 — Lead Recorrente ("Oi, bom dia")
# =========================================================================== #
async def test_scenario_3_returning_lead_greeted_by_name_with_intimacy(
    store: JsonTenantDataStore,
    fake_redis,
):
    runner = _build_runner(store, fake_redis)
    phone = "5591981112222"

    # Turno 1: primeiro contato do Carlos
    first = await runner.handle_batch(
        _batch(
            "companhia-dos-sonhos",
            "Oi, queria ver um colchão de casal de molas ensacadas",
            phone=phone,
            contact_name="Carlos",
            msg_id="m_turn_1",
        )
    )
    assert first.human_handoff_requested is False
    assert first.lead.status is LeadStatus.QUALIFIED

    # Turno 2: Carlos retorna mandando "Oi, bom dia"
    second = await runner.handle_batch(
        _batch(
            "companhia-dos-sonhos",
            "Oi, bom dia",
            phone=phone,
            contact_name="Carlos",
            msg_id="m_turn_2",
        )
    )

    assert second.human_handoff_requested is False
    assert second.reply_text is not None
    _assert_zero_robot_jargon(second.reply_text)
    assert second.lead.attributes.get("returning_contact") == "sim"
    assert "Oi, Carlos! Que bom falar com você de novo!" in second.reply_text
    assert "Como posso te ajudar hoje?" in second.reply_text


# =========================================================================== #
# Cenário 4 — Quebra de Objeção de Preço ("Achei caro")
# =========================================================================== #
async def test_scenario_4_price_objection_argues_value_and_installments_zero_handoff(
    store: JsonTenantDataStore,
    fake_redis,
):
    runner = _build_runner(store, fake_redis)

    res = await runner.handle_batch(
        _batch("companhia-dos-sonhos", "Achei caro", contact_name="Roberto")
    )

    # 1. Zero handoff — mantém a conversa ativa!
    assert res.human_handoff_requested is False
    assert res.intent == AgentIntent.PRICE_OBJECTION.value
    assert res.stage == ConversationStage.OFFER_CATALOG.value
    assert res.reply_text is not None
    _assert_zero_robot_jargon(res.reply_text)

    # 2. Argumenta sobre durabilidade, saúde da coluna, entrega grátis e 10x sem juros
    reply_lower = res.reply_text.lower()
    assert "durabilidade" in reply_lower
    assert "saúde da sua coluna" in reply_lower or "coluna" in reply_lower
    assert "entrega grátis" in reply_lower or "entrega gratis" in reply_lower
    assert "10x sem juros" in reply_lower


# =========================================================================== #
# Cenário 5 — Escalação Elegante (Exceção Real: "Quero 30% de desconto à vista no Pix")
# =========================================================================== #
async def test_scenario_5_elegant_escalation_to_commercial_manager_without_robot_jargon(
    store: JsonTenantDataStore,
    fake_redis,
):
    runner = _build_runner(store, fake_redis)

    res = await runner.handle_batch(
        _batch(
            "companhia-dos-sonhos",
            "Quero 30% de desconto à vista no Pix",
            contact_name="Fernanda",
        )
    )

    # 1. Aciona escalação real por negociação fora da alçada
    assert res.human_handoff_requested is True
    assert res.handoff_reason == HandoffReason.DISCOUNT_NEGOTIATION.value
    assert res.reply_text is not None

    # 2. Zero jargão de robô ou "atendente humano" / "vou transferir"
    _assert_zero_robot_jargon(res.reply_text)

    # 3. Informa que vai consultar o gerente comercial
    reply_lower = res.reply_text.lower()
    assert "gerente comercial" in reply_lower

    # 4. Na clínica (Vizzu), escalação menciona a coordenação clínica
    res_clinic = await runner.handle_batch(
        _batch(
            "vizzu",
            "Quero 30% de desconto no pacote mensal",
            phone="5591983334444",
            contact_name="Luciana",
        )
    )
    assert res_clinic.human_handoff_requested is True
    assert res_clinic.reply_text is not None
    _assert_zero_robot_jargon(res_clinic.reply_text)
    assert "coordenação clínica" in res_clinic.reply_text.lower()


# =========================================================================== #
# Seletor Dinâmico de Modelos de IA (`llm_model`) no Backend, Prompt e Painel
# =========================================================================== #
async def test_dynamic_llm_model_selector_per_tenant_and_admin_api(store: JsonTenantDataStore, tmp_path):
    # 1. Verifica que o prompt e o TenantConfig expõem `llm_model`
    comp = store.get("companhia-dos-sonhos")
    assert comp.tenant.llm_model == "gemini-3.8-flash"
    lead = LeadProfile(tenant_id="companhia-dos-sonhos", phone="5591988887777", name="Carlos")
    prompt = build_system_prompt(comp, lead, stage=ConversationStage.DISCOVERY, now=NOW)
    assert "gemini-3.8-flash" in prompt
    assert HUMAN_PERSONA_RULES.splitlines()[1] in prompt

    # 2. Verifica factory `create_gemini_chat_model` com override de modelo
    # Dados isolados: o teste NUNCA grava no sandbox_config.json nem nos tenants reais do projeto
    iso_tenants = tmp_path / "tenants_iso"
    iso_tenants.mkdir()
    for src_file in (Path(__file__).resolve().parents[1] / "data" / "tenants").glob("*.json"):
        (iso_tenants / src_file.name).write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
    settings = Settings(
        _env_file=None,
        redis_backend="fake",
        sandbox_config_path=tmp_path / "sandbox_config.json",
        tenant_data_dir=iso_tenants,
    )
    m_pro = create_gemini_chat_model(settings, model_name="gemini-2.5-pro")
    assert isinstance(m_pro, DeterministicSalesChatModel)
    assert m_pro.model_name == "gemini-2.5-pro"

    # 3. Verifica atualização imediata via API Admin (Aba 1 e Aba 3)
    app = create_app(settings)
    with TestClient(app) as client:
        # Aba 1: altera modelo padrão no Sandbox
        r_sb = client.put("/api/admin/sandbox", json={"llm_model": "gemini-4-argon"})
        assert r_sb.status_code == 200
        assert r_sb.json()["config"]["llm_model"] == "gemini-4-argon"
        assert app.state.settings.gemini_model == "gemini-4-argon"

        # Aba 3: altera modelo específico de um cliente e restaura em seguida
        r_tn = client.get("/api/admin/tenants/companhia-dos-sonhos")
        assert r_tn.status_code == 200
        payload = r_tn.json()
        original_model = payload["tenant"].get("llm_model", "gemini-3.8-flash")
        try:
            payload["tenant"]["llm_model"] = "gemini-2.5-pro"
            r_put = client.put("/api/admin/tenants/companhia-dos-sonhos", json=payload)
            assert r_put.status_code == 200
            assert r_put.json()["summary"]["llm_model"] == "gemini-2.5-pro"
            assert app.state.tenants.get("companhia-dos-sonhos").tenant.llm_model == "gemini-2.5-pro"
        finally:
            payload["tenant"]["llm_model"] = original_model
            client.put("/api/admin/tenants/companhia-dos-sonhos", json=payload)
