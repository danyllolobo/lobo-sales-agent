"""Fábrica do cliente Redis assíncrono.

Estratégia (`REDIS_BACKEND`):
- `redis`: conexão real obrigatória via `REDIS_URL` (VPS / Easypanel).
- `fake` : fakeredis em memória (testes e dev local sem Docker).
- `auto` : tenta `REDIS_URL`; se indisponível e o ambiente NÃO for produção,
           cai para fakeredis com um aviso. Em produção, falha explicitamente.

ATENÇÃO: o fakeredis vive na memória do processo. Ele não é compartilhado entre
workers do uvicorn, então use `--workers 1` enquanto estiver no modo fake.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from src.config import Settings
from src.core.exceptions import MessageBufferError

logger = logging.getLogger("lobo.redis")


def _redact_url(url: str) -> str:
    """Remove credenciais da URL para logs."""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url


def create_fake_redis() -> aioredis.Redis:
    try:
        import fakeredis
    except ImportError as exc:  # pragma: no cover - dependência listada no requirements
        raise MessageBufferError("fakeredis não instalado (pip install 'fakeredis[lua]')") from exc
    return fakeredis.FakeAsyncRedis(decode_responses=True)


async def create_redis_client(
    settings: Settings,
    *,
    connect_timeout: float = 2.0,
) -> aioredis.Redis:
    """Cria e valida (PING) o cliente Redis conforme `settings.redis_backend`."""
    backend = settings.redis_backend

    if backend == "fake":
        logger.warning("Usando fakeredis em memória (REDIS_BACKEND=fake).")
        return create_fake_redis()

    client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=connect_timeout,
        socket_timeout=max(connect_timeout, 5.0),
        health_check_interval=30,
    )
    try:
        await client.ping()
        logger.info("Redis conectado em %s", _redact_url(settings.redis_url))
        return client
    except (RedisError, OSError) as exc:
        await client.aclose()
        if backend == "auto" and (not settings.is_production or settings.allow_demo_data is True):
            logger.warning(
                "Redis indisponível em %s (%s). Caindo para fakeredis em memória (ambiente=%s).",
                _redact_url(settings.redis_url),
                type(exc).__name__,
                settings.environment,
            )
            return create_fake_redis()
        raise MessageBufferError(
            "Redis indisponível",
            context={"url": _redact_url(settings.redis_url), "error": type(exc).__name__},
        ) from exc

