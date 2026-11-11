"""Helper HTTP compartilhado por gateways e CRMs.

Converte falhas de rede/HTTP na exceção de domínio informada (`GatewayError`,
`CRMError`...) com contexto mascarado — sem corpo de resposta nem PII crua.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from src.core.exceptions import ExternalServiceError
from src.core.schemas import mask_phone


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    error_cls: type[ExternalServiceError],
    service: str,
    path: str,
    headers: Mapping[str, str] | None = None,
    json: Any = None,
    params: Mapping[str, Any] | None = None,
    expect_json: bool = True,
    log_contact: str | None = None,
    tenant_id: str | None = None,
) -> Any:
    """Executa a requisição e devolve JSON (dict/list), bytes (`expect_json=False`) ou `{}`."""
    context: dict[str, Any] = {"service": service, "method": method, "path": path}
    if log_contact:
        context["contact"] = mask_phone(log_contact)
    try:
        resp = await client.request(method, url, json=json, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise error_cls(
            f"{service}: erro de rede ({type(exc).__name__})", tenant_id=tenant_id, context=context
        ) from exc
    if resp.is_error:
        raise error_cls(
            f"{service}: HTTP {resp.status_code} em {path}",
            tenant_id=tenant_id,
            context={**context, "status_code": resp.status_code},
        )
    if not expect_json:
        return resp.content
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {}

