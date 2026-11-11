"""Ponto de entrada FastAPI do Lobo AI Sales Agent.

Execução local:
    uvicorn main:app --reload

Fluxo completo (ETAPA 4):
    POST /webhook/{tenant_id}/{provider}
        → valida assinatura HMAC → normaliza (WAHA/AstraCalls)
        → se `from_me=True` (atendente humano respondeu no celular), pausa a IA imediatamente
        → caso contrário, acumula no buffer de debounce (Redis, 4-8s silêncio / 10s máx)
        → ao fechar o lote, aciona `SalesAgentRunner.handle_batch` (Transcrição + LangGraph + Ferramentas + Gateway)
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.language_models.chat_models import BaseChatModel

from src.agent.graph import LeadConversationStore, SalesAgentRunner
from src.agent.transcription import AudioTranscriber
from src.buffer.message_queue import FlushHandler, MessageBuffer
from src.buffer.redis_client import create_redis_client
from src.config import Settings, get_settings
from src.core.exceptions import (
    ConfigurationError,
    LoboError,
    MessageBufferError,
    TenantNotFoundError,
)
from src.core.schemas import MessageBatch, Provider, TenantId
from src.crm import CRMConnector, create_crm
from src.gateways import WhatsAppGateway, create_gateway
from src.tools.calendar import CalendarService
from src.tools.catalog import CatalogService
from src.tools.handoff import AIPauseRegistry, HandoffService
from src.tools.tenant_data import JsonTenantDataStore, TenantData

logger = logging.getLogger("lobo")


async def log_batch_handler(batch: MessageBatch) -> None:
    """Handler auxiliar de log (sem conteúdo sensível)."""
    logger.info("Lote pronto para o motor de IA %s", batch.log_context())


def create_app(
    settings: Settings | None = None,
    *,
    on_flush: FlushHandler | None = None,
    llm: BaseChatModel | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    def get_gateway_for(app_instance: FastAPI, provider: Provider) -> WhatsAppGateway:
        gateways: dict[Provider, WhatsAppGateway] = app_instance.state.gateways
        if provider not in gateways:
            gateways[provider] = create_gateway(provider, settings)
        return gateways[provider]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Iniciando %s | %s", settings.project_name, settings.safe_summary())
        # Dados de demonstração (preços fictícios) são proibidos em produção.
        store = JsonTenantDataStore(settings.tenant_data_dir, allow_demo=not settings.is_production)
        redis = await create_redis_client(settings)
        crm_cache: dict[str, CRMConnector | None] = {}

        def crm_resolver(tenant: TenantData) -> CRMConnector | None:
            if tenant.tenant_id not in crm_cache:
                crm_cache[tenant.tenant_id] = create_crm(tenant, settings)
            return crm_cache[tenant.tenant_id]

        pause_registry = AIPauseRegistry(redis)
        catalog = CatalogService(store)
        calendar = CalendarService(store)
        handoff = HandoffService(
            store,
            pause_registry,
            crm_resolver,
            pause_hours=settings.ai_pause_hours,
        )
        conv_store = LeadConversationStore(redis)
        transcriber = AudioTranscriber(settings)

        app.state.redis = redis
        app.state.tenants = store
        app.state.pause_registry = pause_registry
        app.state.catalog = catalog
        app.state.calendar = calendar
        app.state.handoff = handoff
        app.state.conversation_store = conv_store
        app.state.gateways = {}

        async def default_agent_flush(batch: MessageBatch) -> None:
            await app.state.agent.handle_batch(batch)

        flush_handler = on_flush or default_agent_flush
        buffer = MessageBuffer(
            redis,
            silence_seconds=settings.debounce_silence_seconds,
            max_window_seconds=settings.debounce_max_window_seconds,
            on_flush=flush_handler,
        )
        app.state.buffer = buffer
        app.state.agent = SalesAgentRunner(
            store=store,
            catalog=catalog,
            calendar=calendar,
            handoff=handoff,
            pause_registry=pause_registry,
            conversation_store=conv_store,
            transcriber=transcriber,
            gateway_resolver=lambda p: get_gateway_for(app, p),
            crm_resolver=crm_resolver,
            buffer=buffer,
            llm=llm,
            settings=settings,
        )

        try:
            yield
        finally:
            await app.state.buffer.aclose()
            for gw in app.state.gateways.values():
                await gw.aclose()
            for crm in crm_cache.values():
                if crm is not None:
                    await crm.aclose()
            await redis.aclose()
            logger.info("Encerrando %s", settings.project_name)

    app = FastAPI(title=settings.project_name, version="0.4.0", lifespan=lifespan)

    @app.get("/health", tags=["infra"])
    async def health() -> dict:
        return {"status": "ok", **settings.safe_summary()}

    @app.post("/webhook/{tenant_id}/{provider}", tags=["webhooks"])
    async def webhook(tenant_id: TenantId, provider: Provider, request: Request) -> JSONResponse:
        try:
            tenant = app.state.tenants.get(tenant_id)
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail="tenant desconhecido") from exc
        try:
            gateway = get_gateway_for(app, provider)
        except ConfigurationError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc

        body = await request.body()
        if not gateway.verify_signature(body, request.headers):
            logger.warning("Assinatura de webhook inválida tenant=%s provider=%s", tenant_id, provider.value)
            raise HTTPException(status_code=401, detail="assinatura inválida")
        try:
            payload: Any = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="JSON inválido") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload deve ser um objeto JSON")

        parsed = gateway.parse_webhook(tenant_id, payload)
        buffer: MessageBuffer = app.state.buffer
        pause_registry: AIPauseRegistry = app.state.pause_registry
        summary = {"accepted": 0, "duplicates": 0, "from_me": 0, "ignored": [i.reason for i in parsed.ignored]}
        try:
            for message in parsed.messages:
                if message.from_me:
                    summary["from_me"] += 1
                    # Ignora se for apenas eco da mensagem recém-enviada pela própria IA
                    if not await pause_registry.is_bot_outbound(
                        message.tenant_id,
                        message.contact_phone,
                        message_id=message.message_id,
                        text=message.text,
                    ):
                        try:
                            await pause_registry.pause(
                                message.tenant_id,
                                message.contact_phone,
                                reason="human_from_me",
                                hours=settings.ai_pause_hours,
                            )
                            await buffer.discard(
                                message.tenant_id,
                                message.session_id,
                                message.contact_phone,
                            )
                        except LoboError as exc:
                            logger.warning("Não foi possível pausar IA em from_me: %s", exc.message)
                    continue
                result = await buffer.push_message(
                    message.tenant_id,
                    message.session_id,
                    message.contact_phone,
                    message,
                    silence_seconds=tenant.tenant.debounce_silence_seconds,
                    max_window_seconds=tenant.tenant.debounce_max_window_seconds,
                )
                summary["accepted" if result.accepted else "duplicates"] += 1
        except MessageBufferError as exc:
            # 503 => o provedor reenvia (AstraCalls/WAHA têm retry), sem perder a mensagem.
            logger.error("Buffer indisponível: %s", exc.message)
            return JSONResponse(status_code=503, content={"error": "buffer_unavailable", **summary})
        return JSONResponse(content=summary)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _s = get_settings()
    uvicorn.run("main:app", host=_s.host, port=_s.port, reload=not _s.is_production)
