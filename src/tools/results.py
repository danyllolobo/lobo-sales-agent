"""Envelope padrão de retorno das ferramentas determinísticas + utilitários.

Toda ferramenta devolve um `ToolResult` — nunca levanta exceção para o LLM.
Quando o dado não existe/é inválido, o resultado traz `requires_human=True` e uma
`agent_instruction` determinística (ex.: "NÃO informe preço"), cumprindo a regra
PROIBIDO INVENTAR PREÇOS e o FALLBACK SEGURO.
"""

from __future__ import annotations

import functools
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any, ParamSpec

from pydantic import BaseModel, ConfigDict

from src.core.exceptions import LoboError, TenantNotFoundError

logger = logging.getLogger("lobo.tools")

P = ParamSpec("P")

# Instruções padronizadas (texto estável = comportamento previsível do agente)
NO_PRICE_INSTRUCTION = (
    "NÃO informe nenhum valor, faixa de preço ou desconto. Diga ao cliente, com cordialidade, "
    "que vai confirmar o valor com a equipe e acione o handoff humano registrando o motivo."
)
NO_DATA_INSTRUCTION = (
    "NÃO invente essa informação. Diga que vai confirmar com a equipe e acione o handoff humano."
)
TOOL_FAILURE_INSTRUCTION = (
    "Houve uma falha técnica ao consultar o sistema. NÃO improvise a resposta: "
    "avise o cliente com cordialidade e acione o handoff humano (motivo: falha técnica)."
)


class ToolStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INVALID_INPUT = "invalid_input"
    ERROR = "error"


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    tenant_id: str
    status: ToolStatus
    data: Any = None
    requires_human: bool = False
    reason: str | None = None
    agent_instruction: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.OK

    def to_llm_payload(self) -> dict[str, Any]:
        """Representação JSON enviada ao LLM (Decimal → string, sem campos nulos)."""
        return self.model_dump(mode="json", exclude_none=True)

    # Construtores ------------------------------------------------------------
    @classmethod
    def success(cls, tool: str, tenant_id: str, data: Any, *, instruction: str | None = None) -> ToolResult:
        return cls(tool=tool, tenant_id=tenant_id, status=ToolStatus.OK, data=data, agent_instruction=instruction)

    @classmethod
    def needs_human(
        cls,
        tool: str,
        tenant_id: str,
        status: ToolStatus,
        reason: str,
        *,
        instruction: str = NO_DATA_INSTRUCTION,
        data: Any = None,
    ) -> ToolResult:
        return cls(
            tool=tool,
            tenant_id=tenant_id,
            status=status,
            data=data,
            requires_human=True,
            reason=reason,
            agent_instruction=instruction,
        )

    @classmethod
    def invalid(cls, tool: str, tenant_id: str, reason: str) -> ToolResult:
        return cls(
            tool=tool,
            tenant_id=tenant_id,
            status=ToolStatus.INVALID_INPUT,
            reason=reason,
            agent_instruction="Parâmetros inválidos. Corrija a chamada ou pergunte ao cliente o dado que falta.",
        )


def tool_guard(tool: str) -> Callable[[Callable[P, Awaitable[ToolResult]]], Callable[P, Awaitable[ToolResult]]]:
    """Converte QUALQUER exceção da ferramenta em `ToolResult` de erro (fallback seguro).

    Espera que o 1º argumento posicional após `self` (ou `tenant_id=`) seja o tenant.
    """

    def decorator(fn: Callable[P, Awaitable[ToolResult]]) -> Callable[P, Awaitable[ToolResult]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
            tenant_id = str(kwargs.get("tenant_id") or (args[1] if len(args) > 1 else "desconhecido"))
            try:
                return await fn(*args, **kwargs)
            except TenantNotFoundError:
                logger.error("Ferramenta %s chamada para tenant inexistente", tool)
                return ToolResult.needs_human(
                    tool, tenant_id, ToolStatus.ERROR, "tenant_not_found", instruction=TOOL_FAILURE_INSTRUCTION
                )
            except (LoboError, ValueError, KeyError, TypeError) as exc:
                logger.error("Falha na ferramenta %s (%s) tenant=%s", tool, type(exc).__name__, tenant_id)
                return ToolResult.needs_human(
                    tool, tenant_id, ToolStatus.ERROR, f"tool_failure:{type(exc).__name__}",
                    instruction=TOOL_FAILURE_INSTRUCTION,
                )
            except Exception as exc:  # noqa: BLE001 - nunca derrubar o grafo
                logger.exception("Erro inesperado na ferramenta %s tenant=%s", tool, tenant_id)
                return ToolResult.needs_human(
                    tool, tenant_id, ToolStatus.ERROR, f"unexpected:{type(exc).__name__}",
                    instruction=TOOL_FAILURE_INSTRUCTION,
                )

        return wrapper

    return decorator


# --------------------------------------------------------------------------- #
# Texto e dinheiro
# --------------------------------------------------------------------------- #
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
CENT = Decimal("0.01")


def normalize_text(text: str) -> str:
    """Minúsculas, sem acentos e sem pontuação: `Belém-PA` → `belem pa`."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", no_accents.lower()).strip()


def money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def format_brl(value: Decimal) -> str:
    """`Decimal("2899.9")` → `R$ 2.899,90` (formatação determinística, sem locale)."""
    q = money(value)
    sign = "-" if q < 0 else ""
    integer, _, cents = f"{abs(q):.2f}".partition(".")
    groups = []
    while integer:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    return f"{sign}R$ {'.'.join(groups)},{cents}"

