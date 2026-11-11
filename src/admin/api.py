"""API do painel administrativo (`/api/admin/*`).

Todas as rotas passam por `require_admin` (token opcional; obrigatório em produção).
Erros retornam `{"detail": "..."}` com status HTTP adequado — o painel transforma isso
em toast vermelho; sucesso vira toast verde.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.admin.auth import require_admin
from src.admin.diagnostics import INTEGRATIONS, integration_overview, run_all_checks, run_check
from src.admin.playground import PlaygroundUnavailable
from src.core.exceptions import ConfigurationError, GatewayError, LoboError, TenantNotFoundError
from src.core.sandbox import SandboxConfig
from src.core.schemas import PLAN_SPECS, OutgoingMessage, Provider, TenantId, mask_phone, normalize_phone, utcnow
from src.tools.tenant_data import TenantData, stamp_missing_price_dates

logger = logging.getLogger("lobo.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# --------------------------------------------------------------------------- #
# Modelos de entrada
# --------------------------------------------------------------------------- #
def _phone(v: str) -> str:
    try:
        return normalize_phone(v)
    except (ValueError, TypeError) as exc:
        raise ValueError("telefone inválido: use DDI+DDD+número, ex.: 5591988887777") from exc


class PhoneIn(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str) -> str:
        return _phone(v)


class SandboxUpdate(BaseModel):
    enabled: bool | None = None
    allowed_phones: list[str] | None = None
    trigger_keywords: list[str] | None = None
    stop_keywords: list[str] | None = None
    session_ttl_minutes: int | None = None
    debounce_silence_seconds: float | None = None
    debounce_max_window_seconds: float | None = None
    split_messages: bool | None = None
    llm_model: str | None = None


class SimulateIn(BaseModel):
    tenant_id: TenantId
    engine: Literal["deterministic", "gemini"] = "deterministic"
    phone: str = "5591900000001"
    name: str | None = Field(default="Lead Playground", max_length=80)
    messages: list[str] = Field(min_length=1, max_length=20)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str) -> str:
        return _phone(v)

    @field_validator("messages")
    @classmethod
    def _messages(cls, v: list[str]) -> list[str]:
        cleaned = [m.strip()[:2000] for m in v if m and m.strip()]
        if not cleaned:
            raise ValueError("envie ao menos uma mensagem com texto")
        return cleaned


class PauseIn(BaseModel):
    hours: float | None = Field(default=None, gt=0, le=24 * 30)


class SendTestIn(BaseModel):
    tenant_id: TenantId
    provider: Literal["waha", "astracalls"]
    phone: str
    text: str = Field(min_length=1, max_length=1000)
    session_id: str | None = Field(default=None, max_length=120)

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str) -> str:
        return _phone(v)


class WebhookConfigUpdate(BaseModel):
    whatsapp_provider: Literal["waha", "astracalls"] | None = None
    whatsapp_session_id: str | None = Field(default=None, max_length=120)
    webhook_secret: str | None = Field(default=None, max_length=256)


class FollowUpRunIn(BaseModel):
    tenant_id: str | None = None
    dry_run: bool = False
    force: bool = False


def _mask_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 4:
        return "••••••••"
    return f"••••••••{secret[-4:]}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _state(request: Request) -> Any:
    return request.app.state


def _tenant_or_404(request: Request, tenant_id: str, *, include_inactive: bool = True) -> TenantData:
    store = _state(request).tenants
    try:
        return store.get_any(tenant_id) if include_inactive else store.get(tenant_id)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Cliente '{tenant_id}' não encontrado.") from exc


def _validation_response(exc: ValidationError) -> JSONResponse:
    errors = [
        {"loc": ".".join(str(p) for p in err.get("loc", ())), "msg": err.get("msg", "inválido")}
        for err in exc.errors()
    ]
    first = errors[0] if errors else {"loc": "", "msg": "dados inválidos"}
    detail = f"Validação falhou em '{first['loc']}': {first['msg']}" if first["loc"] else first["msg"]
    if len(errors) > 1:
        detail += f" (+{len(errors) - 1} erro(s))"
    return JSONResponse(status_code=422, content={"detail": detail, "errors": errors})


def _tenant_summary(t: TenantData) -> dict[str, Any]:
    plan_spec = PLAN_SPECS.get(t.tenant.plan_tier) or PLAN_SPECS["pro"]
    return {
        "tenant_id": t.tenant_id,
        "display_name": t.tenant.display_name,
        "segment": t.tenant.segment.value,
        "niche_name": t.tenant.niche_name,
        "custom_instructions": t.tenant.custom_instructions,
        "plan_tier": t.tenant.plan_tier,
        "plan_spec": plan_spec,
        "monthly_turn_limit": t.tenant.monthly_turn_limit or plan_spec["monthly_turn_limit"],
        "followup_enabled": t.tenant.followup_enabled,
        "followup_delay_minutes": t.tenant.followup_delay_minutes,
        "followup_max_attempts": t.tenant.followup_max_attempts,
        "followup_steps": [s.model_dump(mode="json") for s in t.tenant.followup_steps],
        "external_webhook_url": t.tenant.external_webhook_url,
        "active": t.tenant.active,
        "demo_data": t.demo_data,
        "whatsapp_provider": t.tenant.whatsapp_provider.value,
        "whatsapp_session_id": t.tenant.whatsapp_session_id,
        "webhook_secret_configured": bool(t.tenant.webhook_secret),
        "webhook_secret_masked": _mask_secret(t.tenant.webhook_secret),
        "crm_provider": t.tenant.crm_provider.value,
        "timezone": t.tenant.timezone,
        "debounce_silence_seconds": t.tenant.debounce_silence_seconds,
        "debounce_max_window_seconds": t.tenant.debounce_max_window_seconds,
        "split_messages": t.tenant.split_messages,
        "llm_model": t.tenant.llm_model,
        "items": len(t.catalog.items),
        "delivery_zones": len(t.delivery.zones),
        "services": len(t.calendar.services) if t.calendar else 0,
    }


async def _invalidate_crm(request: Request, tenant_id: str) -> None:
    crm = _state(request).crm_cache.pop(tenant_id, None)
    if crm is not None:
        try:
            await crm.aclose()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Visão geral & integrações
# --------------------------------------------------------------------------- #
@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    st = _state(request)
    settings = st.settings
    sandbox = await st.sandbox.status()
    return {
        "project_name": settings.project_name,
        "version": request.app.version,
        "environment": settings.environment,
        "gemini_model": settings.gemini_model,
        "redis_backend": settings.redis_backend,
        "debounce": {
            "silence_seconds": st.buffer.silence_seconds,
            "max_window_seconds": st.buffer.max_window_seconds,
            "split_messages": settings.split_messages,
        },
        "auth_required": settings.admin_auth_required,
        "sandbox_active": sandbox["active"],
        "sandbox_forced": sandbox["forced_by_environment"],
        "tenants": [_tenant_summary(t) for t in st.tenants.all()],
        "plans": PLAN_SPECS,
        "integrations": integration_overview(settings, st.tenants),
        "playground_gemini_available": st.playground.gemini_available,
        "server_time": utcnow().isoformat(),
    }


@router.get("/integrations")
async def integrations(request: Request) -> list[dict[str, Any]]:
    return integration_overview(_state(request).settings, _state(request).tenants)


@router.post("/integrations/test-all")
async def integrations_test_all(request: Request) -> list[dict[str, Any]]:
    st = _state(request)
    return await run_all_checks(st.settings, st.http_client, st.tenants)


@router.post("/integrations/{name}/test")
async def integrations_test(name: str, request: Request) -> dict[str, Any]:
    if name not in INTEGRATIONS:
        raise HTTPException(status_code=404, detail=f"Integração desconhecida: {name}")
    st = _state(request)
    return await run_check(name, st.settings, st.http_client, st.tenants)


# --------------------------------------------------------------------------- #
# Sandbox
# --------------------------------------------------------------------------- #
@router.get("/sandbox")
async def sandbox_get(request: Request) -> dict[str, Any]:
    return await _state(request).sandbox.status()


@router.put("/sandbox")
async def sandbox_put(request: Request, body: SandboxUpdate) -> Any:
    st = _state(request)
    sandbox = st.sandbox
    current = await sandbox.get_config()
    updates = body.model_dump(exclude_none=True)
    merged = {**current.model_dump(), **updates}
    if merged.get("debounce_silence_seconds") is not None and merged.get("debounce_max_window_seconds") is None:
        merged["debounce_max_window_seconds"] = max(
            st.buffer.max_window_seconds, float(merged["debounce_silence_seconds"]) + 5.0
        )
    try:
        new_config = SandboxConfig.model_validate(merged)
    except ValidationError as exc:
        return _validation_response(exc)
    await sandbox.save_config(new_config)

    # Aplica imediatamente no buffer e nas configurações em memória
    if new_config.debounce_silence_seconds is not None:
        st.buffer.silence_seconds = new_config.debounce_silence_seconds
    if new_config.debounce_max_window_seconds is not None:
        st.buffer.max_window_seconds = new_config.debounce_max_window_seconds
    st.settings.split_messages = new_config.split_messages
    if new_config.llm_model:
        st.settings.gemini_model = new_config.llm_model
    return await sandbox.status()


@router.post("/sandbox/phones")
async def sandbox_add_phone(request: Request, body: PhoneIn) -> dict[str, Any]:
    sandbox = _state(request).sandbox
    config = await sandbox.get_config()
    if body.phone not in config.allowed_phones:
        await sandbox.save_config(config.model_copy(update={"allowed_phones": [*config.allowed_phones, body.phone]}))
    return await sandbox.status()


@router.delete("/sandbox/phones/{phone}")
async def sandbox_remove_phone(request: Request, phone: str) -> dict[str, Any]:
    sandbox = _state(request).sandbox
    try:
        target = normalize_phone(phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="telefone inválido") from exc
    config = await sandbox.get_config()
    if target not in config.allowed_phones:
        detail = "Número vem de SANDBOX_ALLOWED_PHONES (.env) e não pode ser removido pelo painel." \
            if target in sandbox.seed_phones else "Número não está na whitelist."
        raise HTTPException(status_code=404, detail=detail)
    await sandbox.save_config(
        config.model_copy(update={"allowed_phones": [p for p in config.allowed_phones if p != target]})
    )
    return await sandbox.status()


@router.delete("/sandbox/sessions/{tenant_id}/{phone}")
async def sandbox_end_session(request: Request, tenant_id: TenantId, phone: str) -> dict[str, Any]:
    try:
        result = await _state(request).control.reset(tenant_id, phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="telefone inválido") from exc
    return {"reset": result, **(await _state(request).sandbox.status())}


# --------------------------------------------------------------------------- #
# Playground
# --------------------------------------------------------------------------- #
@router.post("/playground/simulate")
async def playground_simulate(request: Request, body: SimulateIn) -> dict[str, Any]:
    try:
        return await _state(request).playground.simulate(
            tenant_id=body.tenant_id, engine=body.engine, phone=body.phone, name=body.name, messages=body.messages
        )
    except PlaygroundUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Cliente '{body.tenant_id}' não encontrado ou inativo.") from exc


@router.delete("/playground/conversations/{tenant_id}/{phone}")
async def playground_reset(request: Request, tenant_id: TenantId, phone: str) -> dict[str, Any]:
    try:
        return await _state(request).playground.reset(tenant_id, phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="telefone inválido") from exc


# --------------------------------------------------------------------------- #
# Tenants (clientes): leitura, criação e edição com recarga imediata
# --------------------------------------------------------------------------- #
@router.get("/tenants")
async def tenants_list(request: Request) -> list[dict[str, Any]]:
    return [_tenant_summary(t) for t in _state(request).tenants.all()]


@router.get("/tenants/{tenant_id}")
async def tenants_get(request: Request, tenant_id: TenantId) -> dict[str, Any]:
    return _tenant_or_404(request, tenant_id).model_dump(mode="json")


async def _save_tenant(request: Request, payload: dict[str, Any], *, create: bool, path_id: str | None) -> Any:
    st = _state(request)
    # Normaliza debounce_max_window_seconds quando o usuário move apenas o slider de silêncio na UI
    t_raw = payload.get("tenant")
    if isinstance(t_raw, dict):
        s_val = t_raw.get("debounce_silence_seconds")
        m_val = t_raw.get("debounce_max_window_seconds")
        if s_val is not None and m_val is not None:
            try:
                if float(s_val) >= float(m_val):
                    t_raw["debounce_max_window_seconds"] = min(60.0, float(s_val) + 5.0)
            except (TypeError, ValueError):
                pass
        plan_code = str(t_raw.get("plan_tier") or "pro")
        plan_spec = PLAN_SPECS.get(plan_code) or PLAN_SPECS["pro"]
        if not plan_spec.get("followup_allowed", True):
            t_raw["followup_enabled"] = False
        max_items = plan_spec.get("max_catalog_items")
        cat_items = (payload.get("catalog") or {}).get("items") or []
        if max_items is not None and len(cat_items) > int(max_items):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"O {plan_spec['name']} permite até {max_items} itens no catálogo "
                    f"(atual: {len(cat_items)}). Mude para o Plano Pro Vendas para catálogo ilimitado."
                ),
            )
    try:
        data = TenantData.model_validate(payload)
    except ValidationError as exc:
        return _validation_response(exc)
    if path_id is not None and data.tenant_id != path_id:
        raise HTTPException(status_code=422, detail="O tenant_id do corpo difere do informado na URL.")
    data = stamp_missing_price_dates(data, utcnow())
    try:
        saved = st.tenants.save(data, create=create)
    except TenantNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Cliente '{data.tenant_id}' não encontrado.") from exc
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Falha ao gravar o arquivo do cliente ({type(exc).__name__}).") from exc
    await _invalidate_crm(request, saved.tenant_id)
    return {"tenant": saved.model_dump(mode="json"), "summary": _tenant_summary(saved)}


@router.post("/tenants", status_code=201)
async def tenants_create(request: Request, payload: dict[str, Any] = Body(...)) -> Any:
    return await _save_tenant(request, payload, create=True, path_id=None)


@router.put("/tenants/{tenant_id}")
async def tenants_update(request: Request, tenant_id: TenantId, payload: dict[str, Any] = Body(...)) -> Any:
    _tenant_or_404(request, tenant_id)
    return await _save_tenant(request, payload, create=False, path_id=tenant_id)


# --------------------------------------------------------------------------- #
# Conversas reais & handoff
# --------------------------------------------------------------------------- #
@router.get("/conversations")
async def conversations_list(request: Request, tenant_id: str | None = None) -> list[dict[str, Any]]:
    st = _state(request)
    items = await st.conversation_store.list_conversations(tenant_id=tenant_id or None)
    out: list[dict[str, Any]] = []
    for item in items:
        lead = item["lead"]
        try:
            pause_info = await st.pause_registry.get(lead.tenant_id, lead.phone)
        except LoboError:
            pause_info = None
        out.append(
            {
                "tenant_id": lead.tenant_id,
                "phone": lead.phone,
                "phone_masked": mask_phone(lead.phone),
                "name": lead.name,
                "status": lead.status.value,
                "tags": lead.tags,
                "attributes": lead.attributes,
                "source": lead.source,
                "last_interaction_at": lead.last_interaction_at.isoformat(),
                "message_count": item["message_count"],
                "last_message": item["last_message"],
                "last_role": item["last_role"],
                "ai_paused": bool(pause_info) or lead.ai_paused,
                "pause": pause_info,
            }
        )
    return out


@router.post("/conversations/{tenant_id}/{phone}/resume")
async def conversations_resume(request: Request, tenant_id: TenantId, phone: str) -> dict[str, Any]:
    _tenant_or_404(request, tenant_id)
    try:
        return await _state(request).control.resume_ai(tenant_id, phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="telefone inválido") from exc


@router.post("/conversations/{tenant_id}/{phone}/pause")
async def conversations_pause(request: Request, tenant_id: TenantId, phone: str, body: PauseIn | None = None) -> dict[str, Any]:
    _tenant_or_404(request, tenant_id)
    try:
        return await _state(request).control.pause_ai(tenant_id, phone, hours=body.hours if body else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="telefone inválido") from exc
    except LoboError as exc:
        raise HTTPException(status_code=503, detail=exc.message) from exc


@router.delete("/conversations/{tenant_id}/{phone}")
async def conversations_reset(request: Request, tenant_id: TenantId, phone: str) -> dict[str, Any]:
    try:
        return await _state(request).control.reset(tenant_id, phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="telefone inválido") from exc


# --------------------------------------------------------------------------- #
# Webhooks & disparo de teste real
# --------------------------------------------------------------------------- #
@router.get("/webhooks")
async def webhooks(request: Request) -> dict[str, Any]:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    st = _state(request)
    return {
        "base_url": f"{proto}://{host}",
        "trigger_keywords": (await st.sandbox.get_config()).trigger_keywords,
        "tenants": [
            {
                **_tenant_summary(t),
                "paths": {p: f"/webhook/{t.tenant_id}/{p}" for p in ("waha", "astracalls")},
            }
            for t in st.tenants.all()
        ],
    }


@router.put("/webhooks/{tenant_id}")
async def webhooks_update(request: Request, tenant_id: TenantId, body: WebhookConfigUpdate) -> dict[str, Any]:
    st = _state(request)
    existing = _tenant_or_404(request, tenant_id)
    payload = existing.model_dump(mode="json")
    t_dict = payload.setdefault("tenant", {})
    if body.whatsapp_provider is not None:
        t_dict["whatsapp_provider"] = body.whatsapp_provider
    if "whatsapp_session_id" in body.model_fields_set:
        t_dict["whatsapp_session_id"] = (body.whatsapp_session_id or "").strip() or None
    if "webhook_secret" in body.model_fields_set:
        raw_secret = (body.webhook_secret or "").strip()
        if not raw_secret:
            t_dict["webhook_secret"] = None
        elif raw_secret.startswith("••••"):
            t_dict["webhook_secret"] = existing.tenant.webhook_secret
        else:
            t_dict["webhook_secret"] = raw_secret
    try:
        validated = TenantData.model_validate(payload)
    except ValidationError as exc:
        return _validation_response(exc)  # type: ignore[return-value]
    saved = st.tenants.save(validated)
    return {
        **_tenant_summary(saved),
        "paths": {p: f"/webhook/{saved.tenant_id}/{p}" for p in ("waha", "astracalls")},
    }


@router.post("/followup/run")
async def followup_run(request: Request, body: FollowUpRunIn | None = None) -> dict[str, Any]:
    st = _state(request)
    opts = body or FollowUpRunIn()
    return await st.followup.run_sweep(
        tenant_id=opts.tenant_id or None,
        dry_run=opts.dry_run,
        force=opts.force,
    )


@router.post("/gateway/test-send")
async def gateway_test_send(request: Request, body: SendTestIn) -> dict[str, Any]:
    st = _state(request)
    tenant = _tenant_or_404(request, body.tenant_id)
    provider = Provider(body.provider)
    try:
        gateway = st.get_gateway(provider)
    except ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    session = body.session_id or tenant.tenant.whatsapp_session_id or gateway.default_session
    outgoing = OutgoingMessage(
        tenant_id=tenant.tenant_id,
        provider=provider,
        session_id=session,
        contact_phone=body.phone,
        text=body.text,
        simulate_typing=False,
    )
    # Registra o eco ANTES do envio: o webhook `from_me` desta mensagem não pausa a IA.
    await st.pause_registry.register_bot_outbound(tenant.tenant_id, body.phone, text=body.text)
    try:
        result = await gateway.send_message(outgoing)
    except GatewayError as exc:
        raise HTTPException(status_code=502, detail=f"{provider.value.upper()} recusou o envio: {exc.message}") from exc
    if result.message_id:
        await st.pause_registry.register_bot_outbound(tenant.tenant_id, body.phone, message_id=result.message_id)
    logger.info("Disparo de teste via %s sessão=%s contato=%s", provider.value, session, mask_phone(body.phone))
    return {"provider": provider.value, "session_id": session, "message_id": result.message_id, "to": body.phone}


__all__ = ["router"]
