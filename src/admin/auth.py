"""Proteção opcional das rotas `/api/admin/*`.

- `ADMIN_TOKEN` (ou `ADMIN_PASSWORD`) definido → exige header `X-Admin-Token: <valor>`
  (ou `Authorization: Bearer <valor>`), comparado em tempo constante.
- Nenhum definido em development/staging/test → acesso livre (homologação sem fricção).
- Nenhum definido em PRODUÇÃO → painel bloqueado (fail-closed): um painel aberto
  permitiria editar preços, abrir o Sandbox e disparar mensagens pelo número real.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from src.config import Settings


def _provided_token(request: Request) -> str | None:
    token = request.headers.get("x-admin-token")
    if token:
        return token.strip()
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def check_admin(settings: Settings, request: Request) -> None:
    secrets = [s.get_secret_value() for s in (settings.admin_token, settings.admin_password) if s is not None]
    if not secrets:
        if settings.is_production:
            raise HTTPException(
                status_code=503,
                detail="Painel bloqueado em produção: defina ADMIN_TOKEN nas variáveis de ambiente.",
            )
        return
    provided = _provided_token(request)
    if not provided or not any(hmac.compare_digest(provided.encode(), s.encode()) for s in secrets):
        raise HTTPException(status_code=401, detail="Token de administrador ausente ou inválido.")


async def require_admin(request: Request) -> None:
    """Dependência FastAPI aplicada ao router administrativo."""
    check_admin(request.app.state.settings, request)


__all__ = ["check_admin", "require_admin"]

