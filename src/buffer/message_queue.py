"""Buffer inteligente de mensagens (debounce) com Redis e trava atômica.

Fluxo (ver ARCHITECTURE.md §3):

    webhook ─► push_message() ──► Redis: RPUSH no lote + renova janela de silêncio
                    │
                    └─► agenda um "worker" de debounce (asyncio) para aquele `seq`
                              │ dorme até  min(last_at + silêncio, first_at + teto)
                              │ se chegou mensagem mais nova (seq mudou) → sai; o worker
                              │   mais novo assume (e herda o MESMO teto via first_at)
                              ▼
                        adquire trava de processamento (SET NX PX + token)
                              ▼
                        flush atômico (Lua: confere seq, LRANGE, DEL)
                              ▼
                        on_flush(MessageBatch)  ← motor de IA (1 único turno)
                              ▼
                        libera a trava (Lua compare-and-delete)

Garantias:
- Atomicidade: push e flush são scripts Lua (sem condição de corrida entre réplicas).
- Teto máximo: o lote fecha em no máximo `max_window_seconds` desde a 1ª mensagem.
- Turnos serializados por contato: enquanto a IA processa um lote, as novas
  mensagens continuam se acumulando no Redis e formam o próximo lote.
- Idempotência: `message_id` repetido (retry de webhook) é descartado.
- Isolamento multi-tenant: todas as chaves contêm `tenant_id`.
- LGPD: logs só com telefone mascarado e sem conteúdo.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from pydantic import TypeAdapter, ValidationError
from redis.exceptions import RedisError

from src.core.exceptions import MessageBufferError, TenantIsolationError
from src.core.schemas import (
    FlushReason,
    IncomingMessage,
    MessageBatch,
    TenantId,
    mask_phone,
    normalize_phone,
)

logger = logging.getLogger("lobo.buffer")

_TENANT_ADAPTER: TypeAdapter[str] = TypeAdapter(TenantId)
_REDIS_ERRORS = (RedisError, OSError)

FlushHandler = Callable[[MessageBatch], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Scripts Lua (executados atomicamente no Redis)
# --------------------------------------------------------------------------- #
# KEYS: 1=msgs 2=meta 3=seen | ARGV: 1=payload 2=now_ms 3=key_ttl_ms 4=seen_ttl_ms
#                                    5=silence_ms 6=max_ms
_PUSH_LUA = """
if not redis.call('SET', KEYS[3], '1', 'NX', 'PX', ARGV[4]) then
  return {0, 0, '0'}
end
local n = redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('HSETNX', KEYS[2], 'first_at', ARGV[2])
redis.call('HSET', KEYS[2], 'last_at', ARGV[2], 'silence_ms', ARGV[5], 'max_ms', ARGV[6])
local seq = redis.call('HINCRBY', KEYS[2], 'seq', 1)
redis.call('PEXPIRE', KEYS[1], ARGV[3])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
return {seq, n, redis.call('HGET', KEYS[2], 'first_at')}
"""

# KEYS: 1=msgs 2=meta | ARGV: 1=expected_seq ('*' = forçar)
_FLUSH_LUA = """
local seq = redis.call('HGET', KEYS[2], 'seq')
if not seq then return false end
if ARGV[1] ~= '*' and seq ~= ARGV[1] then return false end
local first = redis.call('HGET', KEYS[2], 'first_at')
local items = redis.call('LRANGE', KEYS[1], 0, -1)
redis.call('DEL', KEYS[1], KEYS[2])
return {first, items}
"""

# KEYS: 1=lock | ARGV: 1=token
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# KEYS: 1=lock | ARGV: 1=token 2=ttl_ms
_EXTEND_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


# --------------------------------------------------------------------------- #
# Tipos auxiliares
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BufferKey:
    """Identidade de um buffer: (tenant, sessão, contato)."""

    tenant_id: str
    session_id: str
    contact_phone: str

    def redis_base(self, prefix: str) -> str:
        # `{...}` = hash tag: todas as chaves do contato caem no mesmo slot (Redis Cluster).
        return f"{prefix}:{{{self.tenant_id}:{self.session_id}:{self.contact_phone}}}"

    def log_context(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "contact": mask_phone(self.contact_phone),
        }


@dataclass(frozen=True, slots=True)
class PushResult:
    accepted: bool
    reason: str | None = None  # "duplicate" | "from_me" quando não aceita
    seq: int = 0
    batch_size: int = 0
    first_received_at: datetime | None = None
    flush_deadline: datetime | None = None


ErrorHandler = Callable[[BufferKey, BaseException, MessageBatch | None], Awaitable[None]]


def _s(value: Any) -> str:
    """Normaliza respostas do Redis (bytes ou str)."""
    return value.decode() if isinstance(value, bytes) else str(value)


def _dt(ms: int | float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Buffer
# --------------------------------------------------------------------------- #
class MessageBuffer:
    """Buffer de debounce por contato, seguro para múltiplas réplicas."""

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        silence_seconds: float = 4.0,
        max_window_seconds: float = 10.0,
        on_flush: FlushHandler | None = None,
        on_error: ErrorHandler | None = None,
        key_prefix: str = "lobo:buf",
        lock_ttl_seconds: float = 90.0,
        lock_poll_seconds: float = 0.25,
        dedup_ttl_seconds: float = 6 * 3600,
        buffer_ttl_seconds: float = 600.0,
    ) -> None:
        self._validate_windows(silence_seconds, max_window_seconds)
        self._redis = redis
        self.silence_seconds = silence_seconds
        self.max_window_seconds = max_window_seconds
        self._on_flush = on_flush
        self._on_error = on_error
        self._prefix = key_prefix
        self._lock_ttl_ms = int(lock_ttl_seconds * 1000)
        self._lock_poll = lock_poll_seconds
        self._dedup_ttl_ms = int(dedup_ttl_seconds * 1000)
        self._buffer_ttl_ms = int(max(buffer_ttl_seconds, max_window_seconds * 3) * 1000)

        self._push_script = redis.register_script(_PUSH_LUA)
        self._flush_script = redis.register_script(_FLUSH_LUA)
        self._release_script = redis.register_script(_RELEASE_LUA)
        self._extend_script = redis.register_script(_EXTEND_LUA)

        self._tasks: set[asyncio.Task[None]] = set()
        self._waiters: dict[BufferKey, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    async def push_message(
        self,
        tenant_id: str,
        session_id: str,
        contact_phone: str,
        message: IncomingMessage,
        *,
        silence_seconds: float | None = None,
        max_window_seconds: float | None = None,
        schedule: bool = True,
    ) -> PushResult:
        """Adiciona a mensagem ao lote do contato e renova a janela de silêncio.

        `silence_seconds`/`max_window_seconds` permitem override por tenant.
        Com `schedule=True` e `on_flush` definido, o flush é agendado automaticamente.
        """
        key = self.make_key(tenant_id, session_id, contact_phone)
        if (
            message.tenant_id != key.tenant_id
            or message.contact_phone != key.contact_phone
            or message.session_id != key.session_id
        ):
            raise TenantIsolationError(
                "mensagem não pertence ao buffer informado",
                tenant_id=key.tenant_id,
                context=key.log_context(),
            )
        if message.from_me:
            return PushResult(accepted=False, reason="from_me")

        silence = silence_seconds if silence_seconds is not None else self.silence_seconds
        if max_window_seconds is not None:
            max_window = max_window_seconds
        else:
            max_window = max(self.max_window_seconds, silence + 5.0) if silence_seconds is not None else self.max_window_seconds
        self._validate_windows(silence, max_window)

        base = key.redis_base(self._prefix)
        now = _now_ms()
        try:
            seq, size, first = await self._push_script(
                keys=[f"{base}:msgs", f"{base}:meta", f"{base}:seen:{message.message_id}"],
                args=[
                    message.model_dump_json(),
                    now,
                    self._buffer_ttl_ms,
                    self._dedup_ttl_ms,
                    int(silence * 1000),
                    int(max_window * 1000),
                ],
            )
        except _REDIS_ERRORS as exc:
            raise MessageBufferError(
                "falha ao gravar mensagem no buffer",
                tenant_id=key.tenant_id,
                context={**key.log_context(), "error": type(exc).__name__},
            ) from exc

        seq, size, first = int(seq), int(size), int(_s(first))
        if seq == 0:
            logger.info("Mensagem duplicada ignorada %s", key.log_context())
            return PushResult(accepted=False, reason="duplicate")

        deadline = min(now + int(silence * 1000), first + int(max_window * 1000))
        logger.debug("Buffer push seq=%s size=%s %s", seq, size, key.log_context())

        if schedule and self._on_flush is not None:
            self._spawn_worker(key, seq)

        return PushResult(
            accepted=True,
            seq=seq,
            batch_size=size,
            first_received_at=_dt(first),
            flush_deadline=_dt(deadline),
        )

    async def flush_now(self, tenant_id: str, session_id: str, contact_phone: str) -> MessageBatch | None:
        """Esvazia o lote imediatamente (sem esperar a janela) e o devolve, sem chamar `on_flush`."""
        key = self.make_key(tenant_id, session_id, contact_phone)
        self._cancel_waiter(key)
        return await self._flush(key, expected_seq="*", reason=FlushReason.MANUAL)

    async def discard(self, tenant_id: str, session_id: str, contact_phone: str) -> int:
        """Descarta o lote pendente (ex.: humano assumiu a conversa). Retorna quantas msgs havia."""
        batch = await self.flush_now(tenant_id, session_id, contact_phone)
        return len(batch.messages) if batch else 0

    async def pending_count(self, tenant_id: str, session_id: str, contact_phone: str) -> int:
        base = self.make_key(tenant_id, session_id, contact_phone).redis_base(self._prefix)
        try:
            return int(await self._redis.llen(f"{base}:msgs"))
        except _REDIS_ERRORS as exc:
            raise MessageBufferError("falha ao consultar buffer", tenant_id=tenant_id) from exc

    @contextlib.asynccontextmanager
    async def processing_lock(
        self,
        tenant_id: str,
        session_id: str,
        contact_phone: str,
        *,
        wait_timeout: float | None = None,
    ) -> AsyncIterator[str]:
        """Trava distribuída de processamento por contato (renovada automaticamente).

        Levanta `MessageBufferError` se não conseguir a trava dentro de `wait_timeout`.
        """
        key = self.make_key(tenant_id, session_id, contact_phone)
        loop = asyncio.get_running_loop()
        give_up_at = None if wait_timeout is None else loop.time() + wait_timeout
        while (token := await self._try_acquire_lock(key)) is None:
            if give_up_at is not None and loop.time() >= give_up_at:
                raise MessageBufferError(
                    "timeout aguardando trava de processamento",
                    tenant_id=key.tenant_id,
                    context=key.log_context(),
                )
            await asyncio.sleep(self._lock_poll)
        async with self._held_lock(key, token):
            yield token

    async def drain(self, timeout: float | None = None) -> None:
        """Aguarda todos os workers pendentes (útil em testes e shutdown gracioso)."""
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while self._tasks:
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("buffer não drenou dentro do timeout")
            await asyncio.wait(set(self._tasks), timeout=remaining)

    async def aclose(self) -> None:
        """Cancela workers pendentes. As mensagens continuam no Redis (TTL de segurança)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._waiters.clear()

    @property
    def pending_workers(self) -> int:
        return len(self._tasks)

    @staticmethod
    def make_key(tenant_id: str, session_id: str, contact_phone: str) -> BufferKey:
        if not session_id or not session_id.strip():
            raise ValueError("session_id é obrigatório")
        return BufferKey(
            tenant_id=_TENANT_ADAPTER.validate_python(tenant_id),
            session_id=session_id.strip(),
            contact_phone=normalize_phone(contact_phone),
        )

    # ------------------------------------------------------------------ #
    # Worker de debounce
    # ------------------------------------------------------------------ #
    def _spawn_worker(self, key: BufferKey, seq: int) -> None:
        # Otimização local: o worker anterior (ainda dormindo) deste contato fica obsoleto.
        self._cancel_waiter(key)
        task = asyncio.create_task(self._debounce_worker(key, seq), name=f"debounce:{key.tenant_id}:{seq}")
        self._tasks.add(task)
        self._waiters[key] = task
        task.add_done_callback(self._tasks.discard)

    def _cancel_waiter(self, key: BufferKey) -> None:
        old = self._waiters.pop(key, None)
        if old is not None and not old.done():
            old.cancel()

    async def _debounce_worker(self, key: BufferKey, seq: int) -> None:
        base = key.redis_base(self._prefix)
        expected = str(seq)
        try:
            while True:
                meta = {_s(k): _s(v) for k, v in (await self._redis.hgetall(f"{base}:meta")).items()}
                if meta.get("seq") != expected:
                    return  # lote já processado ou uma mensagem mais nova assumiu

                deadline_ms, reason = self._deadline(meta)
                delay = deadline_ms / 1000 - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue

                # A partir daqui o worker não deve ser cancelado pela otimização local
                # (evita perder a resposta do SET NX e "vazar" a trava até o TTL).
                if self._waiters.get(key) is asyncio.current_task():
                    del self._waiters[key]

                token = await self._try_acquire_lock(key)
                if token is None:
                    await asyncio.sleep(self._lock_poll)  # outro turno em andamento
                    continue

                async with self._held_lock(key, token):
                    batch = await self._flush(key, expected_seq=expected, reason=reason)
                    if batch is not None:
                        await self._dispatch(key, batch)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fallback seguro: nunca derruba o servidor
            await self._report_error(key, exc, None)

    def _deadline(self, meta: dict[str, str]) -> tuple[int, FlushReason]:
        first, last = int(meta["first_at"]), int(meta["last_at"])
        silence_deadline = last + int(meta.get("silence_ms", self.silence_seconds * 1000))
        max_deadline = first + int(meta.get("max_ms", self.max_window_seconds * 1000))
        if max_deadline < silence_deadline:
            return max_deadline, FlushReason.MAX_WINDOW
        return silence_deadline, FlushReason.SILENCE

    async def _flush(self, key: BufferKey, *, expected_seq: str, reason: FlushReason) -> MessageBatch | None:
        base = key.redis_base(self._prefix)
        try:
            result = await self._flush_script(keys=[f"{base}:msgs", f"{base}:meta"], args=[expected_seq])
        except _REDIS_ERRORS as exc:
            raise MessageBufferError(
                "falha no flush do buffer",
                tenant_id=key.tenant_id,
                context={**key.log_context(), "error": type(exc).__name__},
            ) from exc
        if not result:
            return None

        first_ms, items = int(_s(result[0])), result[1]
        messages: list[IncomingMessage] = []
        for raw in items:
            try:
                messages.append(IncomingMessage.model_validate_json(_s(raw)))
            except ValidationError:
                logger.error("Item corrompido descartado do buffer %s", key.log_context())
        if not messages:
            return None

        batch = MessageBatch(
            tenant_id=key.tenant_id,
            session_id=key.session_id,
            contact_phone=key.contact_phone,
            messages=messages,
            first_received_at=_dt(first_ms),
            flush_reason=reason,
        )
        logger.info("Lote fechado %s", batch.log_context())
        return batch

    async def _dispatch(self, key: BufferKey, batch: MessageBatch) -> None:
        if self._on_flush is None:
            return
        try:
            await self._on_flush(batch)
        except Exception as exc:  # noqa: BLE001
            await self._report_error(key, exc, batch)

    async def _report_error(self, key: BufferKey, exc: BaseException, batch: MessageBatch | None) -> None:
        # LGPD: sem traceback/mensagem crua no nível ERROR (podem conter PII).
        logger.error(
            "Falha no processamento do buffer (%s) %s",
            type(exc).__name__,
            key.log_context(),
            exc_info=logger.isEnabledFor(logging.DEBUG),
        )
        if self._on_error is not None:
            try:
                await self._on_error(key, exc, batch)
            except Exception:  # noqa: BLE001
                logger.error("on_error também falhou %s", key.log_context())

    # ------------------------------------------------------------------ #
    # Trava de processamento
    # ------------------------------------------------------------------ #
    async def _try_acquire_lock(self, key: BufferKey) -> str | None:
        token = uuid.uuid4().hex
        lock_key = f"{key.redis_base(self._prefix)}:lock"
        try:
            ok = await self._redis.set(lock_key, token, nx=True, px=self._lock_ttl_ms)
        except _REDIS_ERRORS as exc:
            raise MessageBufferError(
                "falha ao adquirir trava",
                tenant_id=key.tenant_id,
                context={**key.log_context(), "error": type(exc).__name__},
            ) from exc
        return token if ok else None

    @contextlib.asynccontextmanager
    async def _held_lock(self, key: BufferKey, token: str) -> AsyncIterator[None]:
        lock_key = f"{key.redis_base(self._prefix)}:lock"
        watchdog = asyncio.create_task(self._renew_lock(lock_key, token))
        try:
            yield
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            try:
                await self._release_script(keys=[lock_key], args=[token])
            except _REDIS_ERRORS:
                logger.warning("Falha ao liberar trava (expira pelo TTL) %s", key.log_context())

    async def _renew_lock(self, lock_key: str, token: str) -> None:
        """Renova a trava enquanto o turno é processado (LLM lento não perde a trava)."""
        interval = self._lock_ttl_ms / 3000
        while True:
            await asyncio.sleep(interval)
            try:
                if not await self._extend_script(keys=[lock_key], args=[token, self._lock_ttl_ms]):
                    logger.warning("Trava perdida durante o processamento")
                    return
            except _REDIS_ERRORS:
                logger.warning("Falha ao renovar trava")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_windows(silence: float, max_window: float) -> None:
        if silence <= 0 or max_window <= 0:
            raise ValueError("janelas de debounce devem ser positivas")
        if silence >= max_window:
            raise ValueError("silence_seconds deve ser menor que max_window_seconds")

