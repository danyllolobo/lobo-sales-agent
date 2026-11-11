"""Ponto de entrada FastAPI do Lobo AI Sales Agent.

Execução local:
    uvicorn main:app --reload

Rotas:
    GET  /                  → Painel administrativo & Playground (também em /admin)
    GET  /health            → health-check sem segredos
    /api/admin/*            → API do painel (token opcional; obrigatório em produção)
    POST /webhook/{tenant_id}/{provider}
        → valida assinatura HMAC → normaliza (WAHA/AstraCalls)
        → `from_me=True` (atendente respondeu no celular) → pausa a IA para o contato
        → TRAVA SANDBOX: só segue quem está na whitelist, tem sessão de teste ativa ou
          enviou `/lobo`/`/lobo-sales-agent` (keyword removida); `/parar`/`/reset` resetam
        → buffer de debounce (Redis) → `SalesAgentRunner.handle_batch` (Transcrição + LangGraph + Gateway)
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models.chat_models import BaseChatModel

from src.admin.api import router as admin_router
from src.admin.control import ConversationControl
from src.admin.playground import Playground
from src.agent.graph import LeadConversationStore, SalesAgentRunner
from src.agent.transcription import AudioTranscriber
from src.buffer.message_queue import FlushHandler, MessageBuffer
from src.buffer.redis_client import create_redis_client
from src.config import PROJECT_ROOT, Settings, get_settings
from src.core.exceptions import (
    ConfigurationError,
    LoboError,
    MessageBufferError,
    TenantNotFoundError,
)
from src.core.sandbox import RESET_CONFIRMATION_TEXT, SandboxAction, SandboxGuard
from src.core.schemas import IncomingMessage, MessageBatch, OutgoingMessage, Provider, TenantId, mask_phone
from src.crm import CRMConnector, create_crm
from src.gateways import WhatsAppGateway, create_gateway
from src.tools.calendar import CalendarService
from src.tools.catalog import CatalogService
from src.tools.handoff import AIPauseRegistry, HandoffService
from src.tools.tenant_data import JsonTenantDataStore, TenantData

logger = logging.getLogger("lobo")

APP_VERSION = "0.5.0"
STATIC_DIR = PROJECT_ROOT / "static"
ADMIN_INDEX = STATIC_DIR / "admin" / "index.html"


async def log_batch_handler(batch: MessageBatch) -> None:
    """Handler auxiliar de log (sem conteúdo sensível)."""
    logger.info("Lote pronto para o motor de IA %s", batch.log_context())


async def _send_reset_confirmation(
    gateway: WhatsAppGateway, pause_registry: AIPauseRegistry, message: IncomingMessage
) -> bool:
    """Confirma o `/parar` no WhatsApp. O eco `from_me` é registrado para não pausar a IA."""
    outgoing = OutgoingMessage.text_reply(message, RESET_CONFIRMATION_TEXT, simulate_typing=False)
    try:
        await pause_registry.register_bot_outbound(message.tenant_id, message.contact_phone, text=outgoing.text)
        result = await gateway.send_message(outgoing)
        if result.message_id:
            await pause_registry.register_bot_outbound(
                message.tenant_id, message.contact_phone, message_id=result.message_id
            )
        return True
    except Exception as exc:  # noqa: BLE001 - confirmação é best-effort
        logger.warning(
            "Falha ao enviar confirmação de reset (%s) contato=%s", type(exc).__name__, mask_phone(message.contact_phone)
        )
        return False


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
        sandbox = SandboxGuard(
            redis,
            environment=settings.environment,
            config_path=settings.sandbox_config_path,
            seed_phones=settings.sandbox_allowed_phones,
        )

        app.state.settings = settings
        app.state.redis = redis
        app.state.tenants = store
        app.state.pause_registry = pause_registry
        app.state.catalog = catalog
        app.state.calendar = calendar
        app.state.handoff = handoff
        app.state.conversation_store = conv_store
        app.state.sandbox = sandbox
        app.state.crm_cache = crm_cache
        app.state.gateways = {}
        app.state.get_gateway = lambda p: get_gateway_for(app, p)
        app.state.http_client = httpx.AsyncClient(timeout=10.0)

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
        app.state.control = ConversationControl(
            store=store,
            sandbox=sandbox,
            conversation_store=conv_store,
            pause_registry=pause_registry,
            buffer=buffer,
            crm_resolver=crm_resolver,
            pause_hours=settings.ai_pause_hours,
        )
        app.state.playground = Playground(store=store, settings=settings, redis=redis, injected_llm=llm)

        try:
            yield
        finally:
            await app.state.buffer.aclose()
            for gw in app.state.gateways.values():
                await gw.aclose()
            for crm in crm_cache.values():
                if crm is not None:
                    await crm.aclose()
            await app.state.http_client.aclose()
            await redis.aclose()
            logger.info("Encerrando %s", settings.project_name)

    app = FastAPI(title=settings.project_name, version=APP_VERSION, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    app.include_router(admin_router)

    @app.get("/", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    async def admin_panel() -> FileResponse:
        if not ADMIN_INDEX.is_file():
            raise HTTPException(status_code=404, detail="painel não encontrado (pasta static/admin ausente)")
        return FileResponse(ADMIN_INDEX, media_type="text/html", headers={"Cache-Control": "no-cache"})

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
        sandbox: SandboxGuard = app.state.sandbox
        control: ConversationControl = app.state.control
        summary: dict[str, Any] = {
            "accepted": 0, "duplicates": 0, "from_me": 0, "ignored": [i.reason for i in parsed.ignored]
        }
        blocked = 0
        resets: list[dict[str, Any]] = []
        session_started = False
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

                # --- TRAVA DE SEGURANÇA (Sandbox) — falha fecha a trava ---
                try:
                    decision = await sandbox.evaluate(message)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Falha no SandboxGuard (%s): mensagem ignorada por segurança", type(exc).__name__)
                    blocked += 1
                    continue
                if decision.action is SandboxAction.IGNORE:
                    blocked += 1
                    continue
                if decision.action is SandboxAction.RESET:
                    reset = await control.reset(
                        message.tenant_id, message.contact_phone, session_id=message.session_id
                    )
                    reset["reply_sent"] = await _send_reset_confirmation(gateway, pause_registry, message)
                    resets.append(reset)
                    continue

                session_started = session_started or decision.session_started
                allowed_message = decision.message or message
                result = await buffer.push_message(
                    allowed_message.tenant_id,
                    allowed_message.session_id,
                    allowed_message.contact_phone,
                    allowed_message,
                    silence_seconds=tenant.tenant.debounce_silence_seconds,
                    max_window_seconds=tenant.tenant.debounce_max_window_seconds,
                )
                summary["accepted" if result.accepted else "duplicates"] += 1
        except MessageBufferError as exc:
            # 503 => o provedor reenvia (AstraCalls/WAHA têm retry), sem perder a mensagem.
            logger.error("Buffer indisponível: %s", exc.message)
            return JSONResponse(status_code=503, content={"error": "buffer_unavailable", **summary})

        if resets:
            summary["status"] = "sandbox_reset"
            summary["reply_sent"] = all(r.get("reply_sent") for r in resets)
        elif blocked and not summary["accepted"] and not summary["duplicates"]:
            summary["status"] = "ignored"
            summary["reason"] = "sandbox_locked"
        if blocked:
            summary["sandbox_blocked"] = blocked
        if session_started:
            summary["sandbox_session_started"] = True
        return JSONResponse(content=summary)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _s = get_settings()
    uvicorn.run("main:app", host=_s.host, port=_s.port, reload=not _s.is_production)
