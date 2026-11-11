"""Testes das ferramentas determinísticas (`src/tools/`): catálogo, preço, entrega, agenda e handoff."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import fakeredis
import pytest

from src.config import PROJECT_ROOT
from src.core.exceptions import ConfigurationError, CRMError, LoboError
from src.core.schemas import LeadProfile, LeadStatus, StandardTag
from src.tools.calendar import CalendarService, InMemoryBookingRepository, pick_two, slot_label, TimeSlot
from src.tools.catalog import CatalogService, tokenize
from src.tools.handoff import (
    EMERGENCY_MESSAGE,
    AIPauseRegistry,
    HandoffReason,
    HandoffService,
    format_phone_br,
)
from src.tools.results import ToolStatus, format_brl, normalize_text
from src.tools.tenant_data import InMemoryTenantDataStore, JsonTenantDataStore, TenantData

UTC = timezone.utc
PHONE = "5591988887777"
# Terça-feira, 10/11/2026 10:00 em Belém (UTC-3)
NOW = datetime(2026, 11, 10, 13, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def tenant_data(tenant_id: str = "loja-teste", **overrides) -> TenantData:
    raw = {
        "tenant": {
            "tenant_id": tenant_id,
            "display_name": "Loja Teste",
            "segment": "mattress",
            "crm_provider": "none",
            "timezone": "America/Belem",
        },
        "business_info": {"hours": "Seg a sex, 9h às 18h"},
        "catalog": {
            "price_max_age_days": 30,
            "synonyms": {"molas ensacadas": ["pocket"], "casal": ["138x188"]},
            "items": [
                {
                    "item_id": "ENS-CASAL",
                    "name": "Colchão Molas Ensacadas Casal",
                    "category": "colchao",
                    "attributes": {"tamanho": "casal"},
                    "tags": ["molas ensacadas"],
                    "price": {"amount": "2899.00", "cash_amount": "2754.05", "max_installments": 10, "updated_at": "2026-11-01T00:00:00Z"},
                    "variants": [{"code": "com-box", "name": "Com box", "price": {"amount": "3699.00", "max_installments": 10, "updated_at": "2026-11-01T00:00:00Z"}}],
                },
                {
                    "item_id": "ESP-CASAL",
                    "name": "Colchão Espuma Casal",
                    "category": "colchao",
                    "attributes": {"tamanho": "casal"},
                    "price": {"amount": "1000.00", "max_installments": 3, "updated_at": "2026-11-01T00:00:00Z"},
                },
                {
                    "item_id": "ENS-KING",
                    "name": "Colchão Molas Ensacadas King",
                    "category": "colchao",
                    "attributes": {"tamanho": "king"},
                    "price": {"amount": "4299.00", "updated_at": "2026-11-01T00:00:00Z"},
                    "in_stock": False,
                },
                {
                    "item_id": "VELHO",
                    "name": "Travesseiro antigo",
                    "category": "acessorio",
                    "price": {"amount": "99.00", "updated_at": "2026-01-01T00:00:00Z"},
                },
                {"item_id": "CONSULTA", "name": "Base sob medida", "category": "acessorio", "price": {"amount": None}},
            ],
        },
        "delivery": {
            "zones": [
                {"cities": ["Belém", "Ananindeua"], "fee": "0.00", "lead_time_days_min": 1, "lead_time_days_max": 2},
                {"cities": ["Castanhal"], "fee": "90.00", "lead_time_days_min": 3, "lead_time_days_max": 3, "business_days": False},
            ]
        },
        "calendar": {
            "services": [{"service_id": "visita", "name": "Visita", "duration_minutes": 60, "capacity": 1}],
            "working_hours": {"tue": ["09:00-12:00"], "wed": ["09:00-12:00", "14:00-16:00"]},
            "slot_step_minutes": 60,
            "min_notice_minutes": 60,
            "horizon_days": 7,
            "blackout_dates": ["2026-11-12"],
        },
    }
    raw.update(overrides)
    return TenantData.model_validate(raw)


@pytest.fixture
def store():
    other = tenant_data("outra-loja")
    return InMemoryTenantDataStore([tenant_data(), other.model_copy(update={"catalog": other.catalog.model_copy(update={"items": []})})])


@pytest.fixture
def catalog(store):
    return CatalogService(store, clock=clock)


@pytest.fixture
def calendar(store):
    return CalendarService(store, InMemoryBookingRepository(), clock=clock)


# =========================================================================== #
# Utilitários
# =========================================================================== #
def test_format_brl_and_normalize():
    assert format_brl(Decimal("2899")) == "R$ 2.899,00"
    assert format_brl(Decimal("1234567.891")) == "R$ 1.234.567,89"
    assert format_brl(Decimal("0.5")) == "R$ 0,50"
    assert normalize_text("Belém-PA, Pará!") == "belem pa para"


def test_tokenize_removes_stopwords_and_plural():
    assert tokenize("Queria ver um colchão de casal com molas ensacadas") == ["colchao", "casal", "mola", "ensacada"]


# =========================================================================== #
# Catálogo
# =========================================================================== #
async def test_search_ranks_best_match_first_with_validated_price(catalog):
    r = await catalog.search_catalog("loja-teste", "Queria ver um colchão de casal com molas ensacadas")
    assert r.ok
    first = r.data["results"][0]
    assert first["item_id"] == "ENS-CASAL"
    assert first["price_formatted"] == "R$ 2.899,00"
    assert first["installments_formatted"] == "10x de R$ 289,90 sem juros"
    assert first["cash_price_formatted"] == "R$ 2.754,05"
    assert "EXATAMENTE" in r.agent_instruction


async def test_search_uses_synonyms_and_is_deterministic(catalog):
    r1 = await catalog.search_catalog("loja-teste", "colchão pocket 138x188")
    r2 = await catalog.search_catalog("loja-teste", "colchão pocket 138x188")
    assert r1.data == r2.data
    assert r1.data["results"][0]["item_id"] == "ENS-CASAL"


async def test_search_out_of_stock_goes_after_and_stale_price_is_hidden(catalog):
    r = await catalog.search_catalog("loja-teste", keywords=["molas ensacadas"])
    ids = [x["item_id"] for x in r.data["results"]]
    assert ids.index("ENS-CASAL") < ids.index("ENS-KING")

    old = await catalog.search_catalog("loja-teste", "travesseiro")
    item = old.data["results"][0]
    assert item["price_available"] is False and item["price_unavailable_reason"] == "price_outdated"
    assert "price" not in item and "price_formatted" not in item


async def test_search_category_filter_and_no_match(catalog):
    r = await catalog.search_catalog("loja-teste", category="Acessório")
    assert {x["item_id"] for x in r.data["results"]} == {"VELHO", "CONSULTA"}

    none = await catalog.search_catalog("loja-teste", "geladeira frost free")
    assert none.status is ToolStatus.OK and not none.requires_human
    assert none.data["featured_fallback"] is True
    assert "Itens mais procurados e populares do catálogo retornados" in (none.agent_instruction or "")

    empty = await catalog.search_catalog("loja-teste", "   ")
    assert empty.status is ToolStatus.INVALID_INPUT


async def test_search_is_tenant_isolated(catalog):
    r = await catalog.search_catalog("outra-loja", "colchão molas ensacadas casal")
    assert r.status is ToolStatus.NOT_FOUND  # catálogo de outro tenant nunca vaza


async def test_unknown_tenant_returns_safe_error(catalog):
    r = await catalog.search_catalog("tenant-fantasma", "colchão")
    assert r.status is ToolStatus.ERROR and r.requires_human and r.reason == "tenant_not_found"


async def test_price_found_and_variant(catalog):
    r = await catalog.get_realtime_price("loja-teste", "ens-casal")
    assert r.ok and r.data["price"] == Decimal("2899.00") and r.data["in_stock"] is True
    v = await catalog.get_realtime_price("loja-teste", "ENS-CASAL", variant="com-box")
    assert v.data["price_formatted"] == "R$ 3.699,00" and v.data["variant"] == "Com box"
    payload = v.to_llm_payload()
    assert payload["data"]["price"] == "3699.00"  # Decimal serializado como string exata


@pytest.mark.parametrize(
    ("item", "variant", "status", "reason"),
    [
        ("NAO-EXISTE", None, ToolStatus.NOT_FOUND, "item_not_found"),
        ("ENS-CASAL", "sem-box", ToolStatus.NOT_FOUND, "variant_not_found"),
        ("CONSULTA", None, ToolStatus.UNAVAILABLE, "price_on_request"),
        ("VELHO", None, ToolStatus.UNAVAILABLE, "price_outdated"),
    ],
)
async def test_price_never_invented(catalog, item, variant, status, reason):
    r = await catalog.get_realtime_price("loja-teste", item, variant)
    assert r.status is status and r.reason == reason
    assert r.requires_human
    assert "NÃO informe nenhum valor" in r.agent_instruction
    assert "price" not in (r.data or {})


async def test_price_out_of_stock_flagged(catalog):
    r = await catalog.get_realtime_price("loja-teste", "ENS-KING")
    assert r.ok and r.data["in_stock"] is False and "SEM ESTOQUE" in r.agent_instruction


@pytest.mark.parametrize("city", ["Belém", "belem", "Belém - PA", "BELÉM/PA", "Ananindeua"])
async def test_delivery_found(catalog, city):
    r = await catalog.get_delivery_info("loja-teste", city)
    assert r.ok and r.data["fee_formatted"] == "Grátis" and r.data["lead_time_formatted"] == "1 a 2 dias úteis"


async def test_delivery_fee_and_unknown_city(catalog):
    r = await catalog.get_delivery_info("loja-teste", "Castanhal")
    assert r.data["fee_formatted"] == "R$ 90,00" and r.data["lead_time_formatted"] == "3 dias"
    miss = await catalog.get_delivery_info("loja-teste", "Manaus")
    assert miss.status is ToolStatus.NOT_FOUND and miss.requires_human and "NÃO prometa" in miss.agent_instruction


async def test_business_info(catalog):
    assert (await catalog.get_business_info("loja-teste", "hours")).data["content"] == "Seg a sex, 9h às 18h"
    missing = await catalog.get_business_info("loja-teste", "warranty")
    assert missing.status is ToolStatus.NOT_FOUND and missing.requires_human


# =========================================================================== #
# Dados de tenant
# =========================================================================== #
def test_demo_tenant_files_load_and_are_blocked_in_production():
    directory = PROJECT_ROOT / "data" / "tenants"
    store = JsonTenantDataStore(directory)
    assert set(store.tenant_ids()) >= {"companhia-dos-sonhos", "splash-piscinas", "vizzu"}
    with pytest.raises(ConfigurationError, match="DEMONSTRAÇÃO"):
        JsonTenantDataStore(directory, allow_demo=False)


def test_tenant_file_name_must_match_tenant_id(tmp_path):
    (tmp_path / "loja-x.json").write_text(tenant_data("loja-y").model_dump_json(), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="isolamento"):
        JsonTenantDataStore(tmp_path)


def test_tenant_data_validations():
    with pytest.raises(ValueError, match="Chatwoot"):
        tenant_data(tenant={"tenant_id": "x1", "display_name": "X", "crm_provider": "chatwoot"})
    with pytest.raises(ValueError, match="faixa de horário"):
        tenant_data(calendar={"services": [{"service_id": "v", "name": "V", "duration_minutes": 30}], "working_hours": {"mon": ["18:00-09:00"]}})
    bad_price = tenant_data().catalog.items[0].price.model_dump() | {"cash_amount": "9999.00"}
    with pytest.raises(ValueError):
        type(tenant_data().catalog.items[0].price).model_validate(bad_price)


# =========================================================================== #
# Agenda
# =========================================================================== #
async def test_slots_respect_hours_notice_and_suggest_two(calendar):
    r = await calendar.check_available_slots("loja-teste", "2026-11-10")
    assert r.ok
    starts = [s["start"] for s in r.data["slots"]]
    # agora = 10:00; antecedência 60 min => 11:00 é o 1º horário de terça (expediente até 12:00)
    assert starts == ["2026-11-10T11:00:00-03:00"]
    assert len(r.data["suggestions"]) == 1  # só existe 1 horário no dia pedido

    wed = await calendar.check_available_slots("loja-teste", "2026-11-11")
    labels = [s["label"] for s in wed.data["suggestions"]]
    assert labels == ["quarta-feira, 11/11 às 09:00", "quarta-feira, 11/11 às 14:00"]  # ≥3h de diferença
    assert len(wed.data["slots"]) == 5  # 9,10,11 + 14,15


async def test_blackout_and_closed_days_search_forward(calendar):
    r = await calendar.check_available_slots("loja-teste", "2026-11-12")  # quinta bloqueada
    assert r.ok and r.data["no_slots_in_requested_range"] is True
    assert r.data["suggestions"][0]["start"] == "2026-11-17T09:00:00-03:00"  # próxima terça


async def test_horizon_and_past_dates(calendar):
    beyond = await calendar.check_available_slots("loja-teste", "2026-12-30")
    assert beyond.status is ToolStatus.NOT_FOUND and beyond.requires_human
    bad = await calendar.check_available_slots("loja-teste", "amanhã")
    assert bad.status is ToolStatus.INVALID_INPUT


async def test_create_booking_and_prevent_double_booking(calendar):
    ok = await calendar.create_booking("loja-teste", f"{PHONE}@c.us", "2026-11-11T09:00:00", lead_name="Maria")
    assert ok.ok and ok.data["label"] == "quarta-feira, 11/11 às 09:00"
    assert ok.data["lead"] == "55*******7777"

    dup = await calendar.create_booking("loja-teste", "5591977776666", "2026-11-11T12:00:00Z")  # = 09:00 Belém
    assert dup.status is ToolStatus.UNAVAILABLE and not dup.requires_human
    assert dup.data["alternatives"][0]["start"] == "2026-11-11T10:00:00-03:00"

    after = await calendar.check_available_slots("loja-teste", "2026-11-11")
    assert "2026-11-11T09:00:00-03:00" not in [s["start"] for s in after.data["slots"]]


async def test_booking_outside_hours_or_notice_rejected(calendar):
    for slot in ["2026-11-11T13:00:00", "2026-11-10T10:30:00", "2026-11-11T09:15:00"]:
        r = await calendar.create_booking("loja-teste", PHONE, slot)
        assert r.status is ToolStatus.UNAVAILABLE, slot


async def test_concurrent_bookings_only_one_wins(calendar):
    results = await asyncio.gather(
        *[calendar.create_booking("loja-teste", f"55919000000{i:02d}", "2026-11-11T15:00:00") for i in range(5)]
    )
    assert sum(r.ok for r in results) == 1


async def test_booking_invalid_inputs(calendar, store):
    assert (await calendar.create_booking("loja-teste", "123", "2026-11-11T09:00:00")).status is ToolStatus.INVALID_INPUT
    unknown = await calendar.create_booking("loja-teste", PHONE, "2026-11-11T09:00:00", service_id="massagem")
    assert unknown.status is ToolStatus.INVALID_INPUT
    assert unknown.data["available_services"] == [{"service_id": "visita", "name": "Visita"}]

    no_cal = InMemoryTenantDataStore([tenant_data("sem-agenda", calendar=None)])
    r = await CalendarService(no_cal, clock=clock).check_available_slots("sem-agenda", "2026-11-11")
    assert r.status is ToolStatus.NOT_FOUND and r.requires_human


def test_pick_two_and_label():
    tz = timezone(timedelta(hours=-3))
    s = [TimeSlot(datetime(2026, 11, 11, h, tzinfo=tz), datetime(2026, 11, 11, h + 1, tzinfo=tz)) for h in (9, 10, 11)]
    assert [x.start.hour for x in pick_two(s)] == [9, 10]  # nenhum ≥3h depois nem outro dia
    assert pick_two([]) == []
    assert slot_label(datetime(2026, 11, 15, 8, 30)) == "domingo, 15/11 às 08:30"


# =========================================================================== #
# Handoff
# =========================================================================== #
class FakeCRM:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.notes: list[str] = []

    async def escalate_to_human(self, lead: LeadProfile, note: str) -> None:
        if self.fail:
            raise CRMError("Chatwoot fora do ar")
        self.notes.append(note)
        lead.add_tag(StandardTag.HUMAN_HANDOFF)


def make_lead(**kw) -> LeadProfile:
    data = {"tenant_id": "loja-teste", "phone": PHONE, "name": "Maria", "status": LeadStatus.QUALIFIED}
    data.update(kw)
    return LeadProfile(**data)


@pytest.fixture
def pause():
    return AIPauseRegistry(fakeredis.FakeAsyncRedis(decode_responses=True))


async def test_handoff_pauses_ai_notes_crm_and_returns_message(store, pause):
    crm = FakeCRM()
    svc = HandoffService(store, pause, lambda _t: crm, pause_hours=24, clock=clock)
    lead = make_lead(attributes={"tamanho": "casal", "cidade": "Belém"}, source="meta_ads")

    r = await svc.trigger_human_handoff(
        lead,
        reason=HandoffReason.DISCOUNT_NEGOTIATION,
        conversation_summary="Quer colchão casal molas ensacadas, pediu 20% de desconto.",
        suggested_action="Avaliar condição especial no Pix.",
    )
    assert r.ok and r.requires_human
    assert r.data["crm_synced"] is True and r.data["errors"] == []
    assert "equipe comercial" in r.data["customer_message"]
    assert lead.ai_paused and lead.has_tag(StandardTag.HUMAN_HANDOFF)
    assert await pause.is_paused("loja-teste", PHONE)
    assert r.data["ai_paused_until"] == (NOW + timedelta(hours=24)).isoformat()

    note = crm.notes[0]
    assert "Negociação de desconto" in note
    assert "+55 (91) 98888-7777" in note
    assert "Dados coletados: cidade: Belém; tamanho: casal" in note
    assert "Origem: meta_ads" in note
    assert "10/11/2026 10:00" in note  # horário no fuso do tenant


async def test_handoff_survives_crm_and_redis_failures(store):
    class BrokenRedis(fakeredis.FakeAsyncRedis):
        async def set(self, *a, **kw):
            from redis.exceptions import ConnectionError

            raise ConnectionError("down")

    svc = HandoffService(store, AIPauseRegistry(BrokenRedis()), lambda _t: FakeCRM(fail=True), clock=clock)
    lead = make_lead()
    r = await svc.trigger_human_handoff(lead, reason="customer_request", conversation_summary="Quer falar com atendente")
    assert r.ok  # handoff nunca falha
    assert r.data["crm_synced"] is False
    assert set(r.data["errors"]) == {"pause_store", "crm:CRMError"}
    assert lead.ai_paused  # pausa local aplicada mesmo assim
    assert "Já chamei uma pessoa" in r.data["customer_message"]


async def test_emergency_always_gets_crisis_message(pause):
    vizzu = tenant_data("vizzu-teste", tenant={"tenant_id": "vizzu-teste", "display_name": "Vizzu", "segment": "psychology"})
    crm = FakeCRM()
    svc = HandoffService(InMemoryTenantDataStore([vizzu]), pause, lambda _t: crm, clock=clock)
    r = await svc.trigger_human_handoff(
        make_lead(tenant_id="vizzu-teste"),
        reason=HandoffReason.CLINICAL_EMERGENCY,
        conversation_summary="Lead relatou pensamentos de autolesão.",
    )
    assert r.data["customer_message"] == EMERGENCY_MESSAGE
    assert "188" in EMERGENCY_MESSAGE and "192" in EMERGENCY_MESSAGE
    assert crm.notes[0].startswith("🚨 URGENTE")


async def test_handoff_with_unknown_reason_and_no_crm(store, pause):
    svc = HandoffService(store, pause, lambda _t: None, clock=clock)
    r = await svc.trigger_human_handoff(make_lead(), reason="motivo-estranho", conversation_summary="")
    assert r.reason == "other" and r.data["errors"] == ["crm_not_configured"]


async def test_pause_registry_resume(pause):
    await pause.pause("loja-teste", f"{PHONE}@c.us", reason="customer_request", hours=1)
    info = await pause.get("loja-teste", PHONE)
    assert info["reason"] == "customer_request"
    assert not await pause.is_paused("outra-loja", PHONE)  # isolado por tenant
    await pause.resume("loja-teste", PHONE)
    assert not await pause.is_paused("loja-teste", PHONE)


def test_format_phone_br():
    assert format_phone_br("5591988887777") == "+55 (91) 98888-7777"
    assert format_phone_br("559188887777") == "+55 (91) 8888-7777"
    assert format_phone_br("14155550123") == "+14155550123"


def test_lobo_error_is_base():
    assert issubclass(CRMError, LoboError)
    assert date(2026, 11, 12) in tenant_data().calendar.blackout_dates

