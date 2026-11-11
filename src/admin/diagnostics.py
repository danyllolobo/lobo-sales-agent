"""Diagnóstico ao vivo das 6 integrações externas (Aba 1 do painel).

Cada teste faz UMA chamada leve e somente-leitura à API do provedor e devolve
`ok/erro + latência + detalhe curto`. Nunca devolve segredos nem payload bruto
(ex.: a listagem de sessões do AstraCalls traz credenciais SIP — só extraímos nome/estado).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from src.config import Settings
from src.crm.ghl import GHL_API_VERSION, GHL_BASE_URL
from src.tools.tenant_data import TenantDataStore

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_API = "https://api.openai.com/v1"
TIMEOUT = 8.0

INTEGRATIONS: dict[str, str] = {
    "gemini": "Google Gemini",
    "openai": "OpenAI Whisper",
    "waha": "WAHA",
    "astracalls": "AstraCalls",
    "chatwoot": "ChatCRM (Chatwoot)",
    "ghl": "GoHighLevel (Vizzu)",
}


class DiagnosticError(Exception):
    """Falha esperada de diagnóstico (mensagem já segura para exibir)."""


def _host(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.hostname or None


def _ghl_location(settings: Settings, store: TenantDataStore | None) -> str | None:
    if settings.ghl_vizzu_location_id:
        return settings.ghl_vizzu_location_id
    if store is not None and hasattr(store, "all"):
        for tenant in store.all():  # type: ignore[attr-defined]
            if tenant.crm.ghl and tenant.crm.ghl.location_id:
                return tenant.crm.ghl.location_id
    return None


def integration_overview(settings: Settings, store: TenantDataStore | None = None) -> list[dict[str, Any]]:
    """Estado de configuração (sem rede)."""
    info = {
        "gemini": (settings.google_api_key is not None, settings.gemini_model),
        "openai": (settings.openai_api_key is not None, "whisper-1"),
        "waha": (settings.waha_enabled, _host(settings.waha_base_url)),
        "astracalls": (
            settings.astracalls_enabled and bool(settings.astracalls_session_id),
            _host(settings.astracalls_base_url),
        ),
        "chatwoot": (settings.chatwoot_enabled, _host(settings.chatwoot_base_url)),
        "ghl": (
            settings.ghl_vizzu_api_token is not None and bool(_ghl_location(settings, store)),
            _ghl_location(settings, store),
        ),
    }
    return [
        {"name": name, "label": label, "configured": info[name][0], "target": info[name][1]}
        for name, label in INTEGRATIONS.items()
    ]


def _redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "***")
    return text


def _http_error(resp: httpx.Response) -> str:
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error") if body.get("error") is not None else body.get("message")
            if isinstance(err, dict):
                err = err.get("message") or err.get("status")
            detail = str(err or "")
    except ValueError:
        detail = resp.text[:120]
    detail = " ".join(detail.split())[:140]
    return f"HTTP {resp.status_code}" + (f" — {detail}" if detail else "")


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    resp = await client.get(url, timeout=TIMEOUT, **kwargs)
    if resp.status_code >= 400:
        raise DiagnosticError(_http_error(resp))
    return resp


# --------------------------------------------------------------------------- #
# Testes individuais
# --------------------------------------------------------------------------- #
async def _test_gemini(settings: Settings, client: httpx.AsyncClient, _store: Any) -> str:
    assert settings.google_api_key is not None
    model = settings.gemini_model
    resp = await _get(
        client,
        f"{GEMINI_API}/models/{quote(model, safe='')}",
        headers={"x-goog-api-key": settings.google_api_key.get_secret_value()},
    )
    body = resp.json() if resp.content else {}
    name = body.get("displayName") or body.get("name") or model
    return f"Modelo {model} disponível ({name})"


async def _test_openai(settings: Settings, client: httpx.AsyncClient, _store: Any) -> str:
    assert settings.openai_api_key is not None
    await _get(
        client,
        f"{OPENAI_API}/models/whisper-1",
        headers={"Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}"},
    )
    return "Whisper-1 disponível para fallback de transcrição"


async def _test_waha(settings: Settings, client: httpx.AsyncClient, _store: Any) -> str:
    assert settings.waha_api_key is not None
    resp = await _get(
        client,
        f"{settings.waha_base_url}/api/sessions",
        params={"all": "true"},
        headers={"X-Api-Key": settings.waha_api_key.get_secret_value()},
    )
    sessions = resp.json() if resp.content else []
    if not isinstance(sessions, list):
        return "API respondeu (formato de sessões inesperado)"
    parts = [f"{s.get('name', '?')} ({s.get('status', '?')})" for s in sessions[:6] if isinstance(s, dict)]
    return "Sessões: " + (", ".join(parts) if parts else "nenhuma sessão criada")


async def _test_astracalls(settings: Settings, client: httpx.AsyncClient, _store: Any) -> str:
    assert settings.astracalls_api_key is not None
    headers = {"X-API-Key": settings.astracalls_api_key.get_secret_value()}
    if settings.astracalls_client_id:
        headers["X-Client-Id"] = settings.astracalls_client_id
    resp = await _get(client, f"{settings.astracalls_base_url}/api/sessions", headers=headers)
    body = resp.json() if resp.content else {}
    sessions = body.get("sessions", []) if isinstance(body, dict) else []
    target = settings.astracalls_session_id
    mine = next((s for s in sessions if isinstance(s, dict) and s.get("id") == target), None)
    if mine is None:
        raise DiagnosticError(f"API OK, mas a sessão configurada não foi encontrada ({len(sessions)} sessão(ões) na conta)")
    state = mine.get("state", "?")
    label = mine.get("name") or "sessão"
    if state != "open":
        raise DiagnosticError(f"Sessão '{label}' encontrada, porém desconectada (estado: {state})")
    return f"Sessão '{label}' conectada (estado: open)"


async def _test_chatwoot(settings: Settings, client: httpx.AsyncClient, _store: Any) -> str:
    assert settings.chatwoot_api_token is not None
    resp = await _get(
        client,
        f"{settings.chatwoot_base_url}/api/v1/profile",
        headers={"api_access_token": settings.chatwoot_api_token.get_secret_value()},
    )
    body = resp.json() if resp.content else {}
    accounts = [str(a.get("id")) for a in body.get("accounts", []) if isinstance(a, dict)]
    who = body.get("name") or body.get("email") or "usuário"
    return f"Autenticado como {who}" + (f" · contas: {', '.join(accounts)}" if accounts else "")


async def _test_ghl(settings: Settings, client: httpx.AsyncClient, store: Any) -> str:
    assert settings.ghl_vizzu_api_token is not None
    location = _ghl_location(settings, store)
    resp = await _get(
        client,
        f"{GHL_BASE_URL}/locations/{quote(str(location), safe='')}",
        headers={
            "Authorization": f"Bearer {settings.ghl_vizzu_api_token.get_secret_value()}",
            "Version": GHL_API_VERSION,
            "Accept": "application/json",
        },
    )
    body = resp.json() if resp.content else {}
    loc = body.get("location", body) if isinstance(body, dict) else {}
    return f"Location '{loc.get('name') or location}' acessível"


_TESTS: dict[str, Callable[[Settings, httpx.AsyncClient, Any], Awaitable[str]]] = {
    "gemini": _test_gemini,
    "openai": _test_openai,
    "waha": _test_waha,
    "astracalls": _test_astracalls,
    "chatwoot": _test_chatwoot,
    "ghl": _test_ghl,
}

_MISSING: dict[str, str] = {
    "gemini": "Defina GOOGLE_API_KEY",
    "openai": "Defina OPENAI_API_KEY",
    "waha": "Defina WAHA_BASE_URL e WAHA_API_KEY",
    "astracalls": "Defina ASTRACALLS_BASE_URL, ASTRACALLS_API_KEY e ASTRACALLS_SESSION_ID",
    "chatwoot": "Defina CHATWOOT_BASE_URL e CHATWOOT_API_TOKEN",
    "ghl": "Defina GHL_VIZZU_API_TOKEN e o location_id",
}


def _secrets(settings: Settings) -> list[str]:
    values = [
        settings.google_api_key, settings.openai_api_key, settings.waha_api_key,
        settings.astracalls_api_key, settings.chatwoot_api_token, settings.ghl_vizzu_api_token,
    ]
    return [v.get_secret_value() for v in values if v is not None]


async def run_check(
    name: str,
    settings: Settings,
    client: httpx.AsyncClient,
    store: TenantDataStore | None = None,
) -> dict[str, Any]:
    if name not in INTEGRATIONS:
        raise KeyError(name)
    overview = next(i for i in integration_overview(settings, store) if i["name"] == name)
    result: dict[str, Any] = {**overview, "ok": False, "status": "not_configured", "latency_ms": 0.0, "detail": _MISSING[name]}
    if not overview["configured"]:
        return result
    t0 = time.perf_counter()
    try:
        detail = await _TESTS[name](settings, client, store)
        result.update(ok=True, status="ok", detail=detail)
    except DiagnosticError as exc:
        result.update(status="error", detail=str(exc))
    except httpx.TimeoutException:
        result.update(status="error", detail=f"Tempo esgotado ({TIMEOUT:.0f}s) sem resposta")
    except httpx.HTTPError as exc:
        result.update(status="error", detail=f"Falha de conexão ({type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001 - diagnóstico nunca derruba o painel
        result.update(status="error", detail=f"Erro inesperado ({type(exc).__name__})")
    result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    result["detail"] = _redact(str(result["detail"]), _secrets(settings))
    return result


async def run_all_checks(settings: Settings, client: httpx.AsyncClient, store: TenantDataStore | None = None) -> list[dict[str, Any]]:
    return list(await asyncio.gather(*(run_check(n, settings, client, store) for n in INTEGRATIONS)))


__all__ = ["INTEGRATIONS", "integration_overview", "run_all_checks", "run_check"]
