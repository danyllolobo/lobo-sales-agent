"""Testes do buffer de debounce (`src/buffer/message_queue.py`) com fakeredis."""

from __future__ import annotations

import asyncio
import time

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.buffer.message_queue import MessageBuffer
from src.config import Settings
from src.buffer.redis_client import create_redis_client
from src.core.exceptions import MessageBufferError, TenantIsolationError
from src.core.schemas import FlushReason, IncomingMessage, MessageBatch, Provider

TENANT = "companhia-dos-sonhos"
SESSION = "default"
PHONE = "5591988887777"

SILENCE = 0.3
MAX_WINDOW = 0.9


def msg(text: str, msg_id: str, *, tenant: str = TENANT, phone: str = PHONE, **kw) -> IncomingMessage:
    return IncomingMessage(
        message_id=msg_id,
        tenant_id=tenant,
        provider=Provider.SIMULATOR,
        session_id=SESSION,
        contact_phone=phone,
        text=text,
        **kw,
    )


class Collector:
    """Handler de flush que registra os lotes e mede concorrência."""

    def __init__(self, delay: float = 0.0, fail_first: bool = False) -> None:
        self.batches: list[MessageBatch] = []
        self.delay = delay
        self.fail_first = fail_first
        self.active = 0
        self.max_active = 0

    async def __call__(self, batch: MessageBatch) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.fail_first and not self.batches:
                self.batches.append(batch)
                raise RuntimeError("falha simulada no motor de IA")
            await asyncio.sleep(self.delay)
            self.batches.append(batch)
        finally:
            self.active -= 1


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


def make_buffer(redis, handler=None, **kw) -> MessageBuffer:
    kw.setdefault("silence_seconds", SILENCE)
    kw.setdefault("max_window_seconds", MAX_WINDOW)
    kw.setdefault("lock_poll_seconds", 0.05)
    return MessageBuffer(redis, on_flush=handler, **kw)


async def push(buf: MessageBuffer, m: IncomingMessage):
    return await buf.push_message(m.tenant_id, m.session_id, m.contact_phone, m)


# --------------------------------------------------------------------------- #
async def test_three_rapid_messages_become_one_batch(redis):
    handler = Collector()
    buf = make_buffer(redis, handler)
    texts = [
        "Olá, boa tarde",
        "Queria ver um colchão de casal com molas ensacadas",
        "Vocês entregam em Belém?",
    ]
    start = time.monotonic()
    for i, t in enumerate(texts):
        r = await push(buf, msg(t, f"m{i}"))
        assert r.accepted and r.batch_size == i + 1
        await asyncio.sleep(0.1)

    await buf.drain(timeout=3)
    elapsed = time.monotonic() - start

    assert len(handler.batches) == 1
    batch = handler.batches[0]
    assert [m.text for m in batch.messages] == texts  # ordem preservada
    assert batch.combined_text == "\n".join(texts)
    assert batch.flush_reason is FlushReason.SILENCE
    assert elapsed >= 0.2 + SILENCE * 0.9  # esperou o silêncio após a última msg
    assert await buf.pending_count(TENANT, SESSION, PHONE) == 0


async def test_messages_separated_by_silence_make_two_batches(redis):
    handler = Collector()
    buf = make_buffer(redis, handler)
    await push(buf, msg("oi", "a1"))
    await asyncio.sleep(SILENCE + 0.25)
    await push(buf, msg("tudo bem?", "a2"))
    await buf.drain(timeout=3)
    assert [len(b.messages) for b in handler.batches] == [1, 1]


async def test_max_window_caps_a_lead_that_never_stops_typing(redis):
    handler = Collector()
    buf = make_buffer(redis, handler)
    start = time.monotonic()
    first_flush_at = None
    i = 0
    while time.monotonic() - start < MAX_WINDOW + 0.6:  # digita sem parar
        await push(buf, msg(f"msg {i}", f"t{i}"))
        i += 1
        await asyncio.sleep(0.1)  # < SILENCE: nunca dá silêncio
        if handler.batches and first_flush_at is None:
            first_flush_at = time.monotonic() - start
    await buf.drain(timeout=3)

    assert first_flush_at is not None
    assert first_flush_at <= MAX_WINDOW + 0.25  # teto respeitado
    assert handler.batches[0].flush_reason is FlushReason.MAX_WINDOW
    total = sum(len(b.messages) for b in handler.batches)
    assert total == i  # nenhuma mensagem perdida


async def test_duplicate_message_id_is_ignored(redis):
    handler = Collector()
    buf = make_buffer(redis, handler)
    assert (await push(buf, msg("oi", "dup"))).accepted
    second = await push(buf, msg("oi", "dup"))
    assert not second.accepted and second.reason == "duplicate"
    await buf.drain(timeout=3)
    assert len(handler.batches) == 1 and len(handler.batches[0].messages) == 1


async def test_from_me_messages_are_not_buffered(redis):
    buf = make_buffer(redis, Collector())
    r = await push(buf, msg("resposta do atendente", "fm1", from_me=True))
    assert not r.accepted and r.reason == "from_me"
    assert buf.pending_workers == 0


async def test_tenant_isolation(redis):
    handler = Collector()
    buf = make_buffer(redis, handler)
    m = msg("oi", "x1", tenant="vizzu")
    with pytest.raises(TenantIsolationError):
        await buf.push_message(TENANT, SESSION, PHONE, m)

    # mesmo telefone em tenants diferentes => lotes independentes
    await push(buf, msg("colchão", "c1"))
    await push(buf, msg("terapia", "v1", tenant="vizzu"))
    await buf.drain(timeout=3)
    assert sorted(b.tenant_id for b in handler.batches) == ["companhia-dos-sonhos", "vizzu"]
    assert all(len(b.messages) == 1 for b in handler.batches)


async def test_turns_are_serialized_and_new_messages_accumulate(redis):
    """Enquanto a IA processa o lote 1, as novas mensagens formam o lote 2."""
    handler = Collector(delay=0.6)
    buf = make_buffer(redis, handler)
    await push(buf, msg("primeira", "s1"))
    await asyncio.sleep(SILENCE + 0.15)  # lote 1 já está sendo processado
    await push(buf, msg("segunda", "s2"))
    await asyncio.sleep(0.05)
    await push(buf, msg("terceira", "s3"))
    await buf.drain(timeout=5)

    assert handler.max_active == 1  # nunca dois turnos simultâneos do mesmo contato
    assert [[m.text for m in b.messages] for b in handler.batches] == [
        ["primeira"],
        ["segunda", "terceira"],
    ]


async def test_two_replicas_sharing_redis_process_batch_once(redis):
    """Simula 2 réplicas do servidor recebendo webhooks do mesmo contato."""
    handler = Collector()
    server = fakeredis.FakeServer()
    r1 = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    r2 = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    buf1, buf2 = make_buffer(r1, handler), make_buffer(r2, handler)

    await push(buf1, msg("oi", "r1"))
    await push(buf2, msg("quero preço", "r2"))
    await push(buf1, msg("do king", "r3"))
    await asyncio.gather(buf1.drain(timeout=3), buf2.drain(timeout=3))

    assert len(handler.batches) == 1
    assert [m.message_id for m in handler.batches[0].messages] == ["r1", "r2", "r3"]


async def test_handler_failure_calls_on_error_and_buffer_keeps_working(redis):
    handler = Collector(fail_first=True)
    errors = []

    async def on_error(key, exc, batch):
        errors.append((key, type(exc).__name__, batch))

    buf = make_buffer(redis, handler, on_error=on_error)
    await push(buf, msg("oi", "e1"))
    await buf.drain(timeout=3)
    assert len(errors) == 1
    key, exc_name, batch = errors[0]
    assert exc_name == "RuntimeError" and batch is not None
    assert key.contact_phone == PHONE

    await push(buf, msg("ainda aí?", "e2"))
    await buf.drain(timeout=3)
    assert len(handler.batches) == 2  # trava foi liberada após a falha


async def test_flush_now_and_discard(redis):
    buf = make_buffer(redis, Collector())
    await push(buf, msg("a", "f1"))
    await push(buf, msg("b", "f2"))
    batch = await buf.flush_now(TENANT, SESSION, PHONE)
    assert batch is not None and batch.flush_reason is FlushReason.MANUAL
    assert batch.message_ids == ["f1", "f2"]
    assert await buf.flush_now(TENANT, SESSION, PHONE) is None

    await push(buf, msg("c", "f3"))
    assert await buf.discard(TENANT, SESSION, PHONE) == 1
    await buf.drain(timeout=2)


async def test_per_tenant_window_override(redis):
    handler = Collector()
    buf = make_buffer(redis, handler, silence_seconds=2.0, max_window_seconds=5.0)
    start = time.monotonic()
    m = msg("oi", "o1")
    r = await buf.push_message(TENANT, SESSION, PHONE, m, silence_seconds=0.2, max_window_seconds=0.5)
    assert (r.flush_deadline - r.first_received_at).total_seconds() == pytest.approx(0.2, abs=0.01)
    await buf.drain(timeout=3)
    assert time.monotonic() - start < 1.0
    with pytest.raises(ValueError):
        await buf.push_message(TENANT, SESSION, PHONE, msg("x", "o2"), silence_seconds=5, max_window_seconds=3)


async def test_processing_lock_is_exclusive_and_times_out(redis):
    buf = make_buffer(redis)
    async with buf.processing_lock(TENANT, SESSION, PHONE):
        with pytest.raises(MessageBufferError, match="timeout"):
            async with buf.processing_lock(TENANT, SESSION, PHONE, wait_timeout=0.2):
                pass
    async with buf.processing_lock(TENANT, SESSION, PHONE, wait_timeout=0.2):
        pass  # liberada corretamente


async def test_lock_is_renewed_during_long_processing(redis):
    buf = make_buffer(redis, lock_ttl_seconds=0.3)
    async with buf.processing_lock(TENANT, SESSION, PHONE):
        await asyncio.sleep(0.7)  # > TTL: sem renovação a trava expiraria
        assert await redis.exists(f"lobo:buf:{{{TENANT}:{SESSION}:{PHONE}}}:lock") == 1


async def test_redis_failure_raises_message_buffer_error():
    class BrokenRedis(fakeredis.FakeAsyncRedis):
        async def evalsha(self, *a, **kw):
            raise RedisConnectionError("down")

        async def eval(self, *a, **kw):
            raise RedisConnectionError("down")

    buf = make_buffer(BrokenRedis(decode_responses=True), Collector())
    with pytest.raises(MessageBufferError) as exc_info:
        await push(buf, msg("oi", "b1"))
    assert exc_info.value.handoff_recommended
    assert PHONE not in str(exc_info.value.to_dict())  # contexto mascarado


async def test_aclose_cancels_pending_workers(redis):
    handler = Collector()
    buf = make_buffer(redis, handler, silence_seconds=5, max_window_seconds=9)
    await push(buf, msg("oi", "z1"))
    assert buf.pending_workers == 1
    await buf.aclose()
    assert buf.pending_workers == 0
    assert handler.batches == []
    assert await buf.pending_count(TENANT, SESSION, PHONE) == 1  # msg preservada no Redis


def test_invalid_windows_rejected(redis):
    with pytest.raises(ValueError):
        MessageBuffer(redis, silence_seconds=5, max_window_seconds=5)


# --------------------------------------------------------------------------- #
# Fábrica do cliente Redis
# --------------------------------------------------------------------------- #
UNREACHABLE = "redis://127.0.0.1:1/0"


async def test_redis_factory_fake_backend():
    client = await create_redis_client(Settings(_env_file=None, redis_backend="fake"))
    assert await client.ping()


async def test_redis_factory_auto_falls_back_to_fake_in_development():
    s = Settings(_env_file=None, redis_backend="auto", redis_url=UNREACHABLE)
    client = await create_redis_client(s, connect_timeout=0.5)
    assert isinstance(client, fakeredis.FakeAsyncRedis)


@pytest.mark.parametrize(
    "overrides",
    [
        {"redis_backend": "redis"},
        {"redis_backend": "auto", "environment": "production", "google_api_key": "k"},
    ],
)
async def test_redis_factory_requires_real_redis(overrides):
    s = Settings(_env_file=None, redis_url="redis://user:pw@127.0.0.1:1/0", **overrides)
    with pytest.raises(MessageBufferError) as exc_info:
        await create_redis_client(s, connect_timeout=0.5)
    assert "pw" not in str(exc_info.value.to_dict())  # credencial redigida

