"""Testes de `src/core/schemas.py` e `src/core/exceptions.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.core.exceptions import (
    CRMError,
    LoboError,
    MessageBufferError,
    PayloadNormalizationError,
    PriceNotAvailableError,
)
from src.core.schemas import (
    IncomingMessage,
    LeadProfile,
    LeadStatus,
    MediaAttachment,
    MessageType,
    OutgoingMessage,
    Provider,
    Segment,
    StandardTag,
    TenantConfig,
    mask_phone,
    normalize_phone,
    normalize_tag,
)

PHONE = "5591988887777"


def incoming(**overrides) -> IncomingMessage:
    data = {
        "message_id": "msg-1",
        "tenant_id": "companhia-dos-sonhos",
        "provider": Provider.WAHA,
        "session_id": "default",
        "contact_phone": f"{PHONE}@c.us",
        "text": "Olá, boa tarde",
    }
    data.update(overrides)
    return IncomingMessage(**data)


# --------------------------------------------------------------------------- #
# Utilitários
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        "5591988887777",
        "+55 (91) 98888-7777",
        "5591988887777@c.us",
        "5591988887777@s.whatsapp.net",
        "5591988887777:12@s.whatsapp.net",
    ],
)
def test_normalize_phone_variants(raw):
    assert normalize_phone(raw) == PHONE


@pytest.mark.parametrize("raw", ["123", "1234567890123456", "120363000000000000@g.us", ""])
def test_normalize_phone_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_mask_phone():
    assert mask_phone(PHONE) == "55*******7777"
    assert mask_phone(None) == "<sem-telefone>"
    assert mask_phone("1234") == "****"


def test_normalize_tag():
    assert normalize_tag("  Visita Agendada ") == "visita_agendada"
    assert normalize_tag("atendimento-humano") == "atendimento_humano"
    with pytest.raises(ValueError):
        normalize_tag("   ")


# --------------------------------------------------------------------------- #
# IncomingMessage
# --------------------------------------------------------------------------- #
def test_incoming_text_message_normalized():
    msg = incoming()
    assert msg.contact_phone == PHONE
    assert msg.type is MessageType.TEXT
    assert msg.content_for_llm == "Olá, boa tarde"
    assert msg.timestamp.tzinfo is not None


def test_incoming_tenant_id_is_slug_and_lowercased():
    assert incoming(tenant_id="  Vizzu ").tenant_id == "vizzu"
    with pytest.raises(ValidationError):
        incoming(tenant_id="tenant com espaço")


def test_incoming_text_requires_text():
    with pytest.raises(ValidationError, match="exige `text`"):
        incoming(text="   ")


def test_incoming_audio_requires_media_and_transcription_flow():
    with pytest.raises(ValidationError, match="exige `media`"):
        incoming(type=MessageType.AUDIO, text=None)

    audio = incoming(
        type=MessageType.AUDIO,
        text=None,
        media={"url": "https://waha.example.com/files/a.ogg", "mimetype": "audio/ogg"},
    )
    assert audio.needs_transcription
    assert audio.content_for_llm == "[áudio sem transcrição]"

    transcribed = audio.with_transcription("  quero um colchão king  ")
    assert transcribed is not audio
    assert not transcribed.needs_transcription
    assert transcribed.content_for_llm == "quero um colchão king"


def test_incoming_image_with_caption():
    msg = incoming(
        type=MessageType.IMAGE,
        text=None,
        media=MediaAttachment(provider_media_id="abc", caption="foto do terreno"),
    )
    assert msg.content_for_llm == "[imagem] foto do terreno"


def test_media_requires_some_source():
    with pytest.raises(ValidationError):
        MediaAttachment(mimetype="image/jpeg")


def test_incoming_location():
    with pytest.raises(ValidationError):
        incoming(type=MessageType.LOCATION, text=None)
    msg = incoming(
        type=MessageType.LOCATION,
        text=None,
        location={"latitude": -1.45, "longitude": -48.5, "address": "Belém - PA"},
    )
    assert msg.content_for_llm == "[localização] Belém - PA"


def test_incoming_is_immutable():
    msg = incoming()
    with pytest.raises(ValidationError):
        msg.text = "alterado"


def test_incoming_timestamp_converted_to_utc():
    naive = datetime(2026, 11, 10, 12, 0, 0)
    assert incoming(timestamp=naive).timestamp == naive.replace(tzinfo=UTC)

    belem = timezone(timedelta(hours=-3))
    aware = datetime(2026, 11, 10, 9, 0, 0, tzinfo=belem)
    assert incoming(timestamp=aware).timestamp.hour == 12

    epoch = incoming(timestamp=1_762_800_000).timestamp
    assert epoch.tzinfo is not None


def test_incoming_repr_and_logs_mask_phone_and_hide_raw():
    msg = incoming(raw_payload={"from": f"{PHONE}@c.us", "secret": "x"})
    for text in (repr(msg), str(msg)):
        assert PHONE not in text
        assert "55*******7777" in text
        assert "raw_payload" not in text

    ctx = msg.log_context()
    assert ctx["contact"] == "55*******7777"
    assert "Olá" not in str(ctx)  # conteúdo não vai para logs
    assert "raw_payload" not in msg.model_dump()  # excluído da serialização


def test_incoming_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        incoming(foo="bar")


# --------------------------------------------------------------------------- #
# OutgoingMessage
# --------------------------------------------------------------------------- #
def outgoing(**overrides) -> OutgoingMessage:
    data = {
        "tenant_id": "splash-piscinas",
        "provider": Provider.ASTRACALLS,
        "session_id": "sess-1",
        "contact_phone": PHONE,
        "text": "Olá! Em qual cidade você está?",
    }
    data.update(overrides)
    return OutgoingMessage(**data)


def test_outgoing_text_ok_and_masked():
    msg = outgoing()
    assert PHONE not in repr(msg)
    assert msg.log_context()["text_length"] == len(msg.text)


def test_outgoing_validations():
    with pytest.raises(ValidationError):
        outgoing(text="")
    with pytest.raises(ValidationError, match="exige `media`"):
        outgoing(type=MessageType.IMAGE, text=None)
    with pytest.raises(ValidationError, match="não suportado"):
        outgoing(type=MessageType.STICKER, media={"url": "https://x/y.webp"})
    with pytest.raises(ValidationError):
        outgoing(text="a" * 65_537)


def test_outgoing_typing_duration():
    assert outgoing(text="oi").effective_typing_seconds == 1.0  # piso
    assert outgoing(text="a" * 1000).effective_typing_seconds == 6.0  # teto
    assert outgoing(text="a" * 100).effective_typing_seconds == 3.0
    assert outgoing(typing_seconds=2.5).effective_typing_seconds == 2.5
    assert outgoing(simulate_typing=False).effective_typing_seconds == 0.0


# --------------------------------------------------------------------------- #
# LeadProfile
# --------------------------------------------------------------------------- #
def test_lead_profile_defaults_and_tags():
    lead = LeadProfile(
        tenant_id="vizzu",
        phone="+55 91 98888-7777",
        tags=["Qualificado", "qualificado", "Meta Ads"],
    )
    assert lead.phone == PHONE
    assert lead.status is LeadStatus.NEW
    assert lead.tags == ["qualificado", "meta_ads"]
    assert not lead.ai_paused
    assert not lead.is_returning

    lead.add_tag("Visita Agendada")
    lead.add_tag(StandardTag.VISIT_SCHEDULED)
    assert lead.tags.count("visita_agendada") == 1
    lead.remove_tag("Meta-Ads")
    assert "meta_ads" not in lead.tags


def test_lead_assignment_is_validated():
    lead = LeadProfile(tenant_id="vizzu", phone=PHONE)
    with pytest.raises(ValidationError):
        lead.status = "inexistente"
    lead.tags = ["Novo Lead"]
    assert lead.tags == ["novo_lead"]


def test_lead_handoff_pauses_ai():
    lead = LeadProfile(tenant_id="vizzu", phone=PHONE, status=LeadStatus.IN_PROGRESS)
    lead.mark_human_handoff()
    assert lead.ai_paused
    assert lead.status is LeadStatus.HUMAN_HANDOFF
    assert lead.has_tag(StandardTag.HUMAN_HANDOFF)


def test_lead_repr_masks_phone():
    lead = LeadProfile(tenant_id="vizzu", phone=PHONE, name="Maria")
    assert PHONE not in repr(lead)
    assert lead.log_context()["contact"] == "55*******7777"


def test_lead_touch_updates_interaction():
    lead = LeadProfile(
        tenant_id="vizzu",
        phone=PHONE,
        last_interaction_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    lead.touch()
    assert lead.last_interaction_at.year >= 2026


# --------------------------------------------------------------------------- #
# TenantConfig
# --------------------------------------------------------------------------- #
def test_tenant_config():
    t = TenantConfig(
        tenant_id="companhia-dos-sonhos",
        display_name="Companhia dos Sonhos",
        segment=Segment.MATTRESS,
        timezone="America/Belem",
        debounce_silence_seconds=5,
    )
    assert t.tz.key == "America/Belem"
    with pytest.raises(ValidationError, match="timezone"):
        TenantConfig(tenant_id="x1", display_name="X", timezone="Marte/Olympus")
    with pytest.raises(ValidationError):
        TenantConfig(
            tenant_id="x1",
            display_name="X",
            debounce_silence_seconds=8,
            debounce_max_window_seconds=6,
        )


# --------------------------------------------------------------------------- #
# Exceções
# --------------------------------------------------------------------------- #
def test_exceptions_handoff_policy():
    assert MessageBufferError().handoff_recommended
    assert CRMError().handoff_recommended
    assert PriceNotAvailableError().handoff_recommended
    assert not PayloadNormalizationError().handoff_recommended
    assert not issubclass(MessageBufferError, BufferError)  # não colide com builtin


def test_exception_formatting():
    err = CRMError("Chatwoot 502", tenant_id="vizzu", context={"status": 502})
    assert str(err) == "[tenant=vizzu] Chatwoot 502"
    assert isinstance(err, LoboError)
    assert err.to_dict() == {
        "error": "CRMError",
        "message": "Chatwoot 502",
        "tenant_id": "vizzu",
        "handoff_recommended": True,
        "context": {"status": 502},
    }

