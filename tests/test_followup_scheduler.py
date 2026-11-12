"""Régua de follow-up AUTOMÁTICA: agendador em segundo plano, canal real do lead, reset quando o lead responde."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fakeredis
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src.agent.followup import FollowUpScheduler, FollowUpService, resolve_effective_steps
from src.agent.graph import LeadConversationStore
from src.core.schemas import FollowUpStep, IncomingMessage, LeadProfile, LeadStatus, Provider
from src.gateways.base import SendResult, WhatsAppGateway
from src.tools.handoff import AIPauseRegistry
from src.tools.tenant_data import JsonTenantDataStore

TENANTS_DIR = Path(__file__).resolve().parents[1] / "data" / "tenants"
NOW = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
TENANT = "companhia-dos-sonhos"


@pytest.fixture
def store(tmp_path: Path) -> JsonTenantDataStore:
    for src_file in TENANTS_DIR.glob("*.json"):
        (tmp_path / src_file.name).write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
    s = JsonTenantDataStore(tmp_path, allow_demo=True)
    td = s.get(TENANT)
    steps = [
        FollowUpStep(step=1, delay_minutes=1, label="1º", strategy="warm_check"),
        FollowUpStep(step=2, delay_minutes=1440, label="2º", strategy="value_reinforce"),
        FollowUpStep(step=3, delay_minutes=4320, label="3º", strategy="objection_break"),
        FollowUpStep(step=4, delay_minutes=10080, label="4º", strategy="breakup"),
    ]
    # Reproduz o JSON real: 4 etapas na tabela, mas followup_max_attempts=1 (legado)
    s.save(td.model_copy(update={"tenant": td.tenant.model_copy(update={
        "followup_enabled": True, "followup_max_attempts": 1, "followup_steps": steps})}))
    return JsonTenantDataStore(tmp_path, allow_demo=True)


@pytest.fixture
async def redis():
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()


class _Gw(WhatsAppGateway):
    provider = Provider.WAHA

    def __init__(self) -> None:
        super().__init__("https://waha.example.com", "k", default_session="default")
        self.sent: list[Any] = []

    def parse_webhook(self, tenant_id, payload):  # pragma: no cover
        raise NotImplementedError

    def verify_signature(self, body, headers):  # pragma: no cover
        return True

    def _auth_headers(self) -> dict[str, str]:
        return {}

    async def set_typing(self, session_id: str, to: str, typing: bool = True) -> None:
        return None

    async def mark_seen(self, session_id: str, to: str, message_ids: list[str]) -> None:
        return None

    async def send_message(self, message) -> SendResult:
        self.sent.append(message)
        return SendResult(provider=self.provider, message_id=f"OUT_{len(self.sent)}")

    async def download_media(self, message: IncomingMessage) -> bytes:  # pragma: no cover
        return b""


async def _seed(conv: LeadConversationStore, minutes_ago: float) -> LeadProfile:
    lead = LeadProfile(
        tenant_id=TENANT,
        phone="5591982054450",
        name="Danyllo",
        status=LeadStatus.QUALIFIED,
        last_interaction_at=NOW - timedelta(minutes=minutes_ago),
        attributes={"_wa_provider": "waha", "_wa_session": "LoboAds", "_wa_chat_id": "229308354326776@lid"},
    )
    await conv.save(lead, [HumanMessage(content="Tem casal?"), AIMessage(content="Temos sim! Qual prefere?")])
    return lead


def test_table_steps_are_the_source_of_truth(store):
    steps = resolve_effective_steps(store.get(TENANT).tenant)
    assert [s.delay_minutes for s in steps] == [1, 1440, 4320, 10080]  # ignora max_attempts=1 legado


async def test_scheduler_sends_first_touch_automatically_on_lead_channel(store, redis, monkeypatch):
    monkeypatch.setattr("src.gateways.base.asyncio.sleep", lambda *_a, **_k: _noop())
    conv = LeadConversationStore(redis)
    await _seed(conv, minutes_ago=2)
    gw = _Gw()
    current = {"now": NOW}
    service = FollowUpService(
        store=store, conversation_store=conv, pause_registry=AIPauseRegistry(redis),
        gateway_resolver=lambda _p: gw, clock=lambda: current["now"],
    )
    scheduler = FollowUpScheduler(service, redis=redis, interval_seconds=60, clock=lambda: current["now"])

    result = await scheduler.run_once()
    assert result is not None and result["sent_count"] == 1
    msg = gw.sent[0]
    assert msg.session_id == "LoboAds" and msg.chat_id == "229308354326776@lid"  # mesmo canal do lead
    assert "Danyllo" in msg.text
    assert scheduler.status()["total_sent"] == 1

    # Mesmo ciclo em outra réplica: a trava do Redis impede envio duplicado
    assert await scheduler.run_once() is None

    # 2º toque só depois de +1 dia do 1º envio
    await redis.delete(FollowUpScheduler.LOCK_KEY)
    current["now"] = NOW + timedelta(hours=2)
    assert (await scheduler.run_once())["sent_count"] == 0
    await redis.delete(FollowUpScheduler.LOCK_KEY)
    current["now"] = NOW + timedelta(days=1, minutes=1)
    assert (await scheduler.run_once())["sent_count"] == 1
    assert len(gw.sent) == 2


async def _noop() -> None:
    return None

