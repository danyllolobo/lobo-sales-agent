"""Hierarquia de exceções do Lobo AI Sales Agent.

Cada exceção carrega `handoff_recommended`: quando `True`, a camada de orquestração
deve aplicar o FALLBACK SEGURO (mensagem cordial ao lead + transferência humana)
em vez de propagar o erro e derrubar o processamento.
"""

from __future__ import annotations

from typing import Any


class LoboError(Exception):
    """Base de todas as exceções do domínio."""

    default_message: str = "Erro interno no Lobo AI Sales Agent"
    handoff_recommended: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        tenant_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.tenant_id = tenant_id
        # `context` NÃO deve conter PII crua (telefones, nomes): use `mask_phone`.
        self.context: dict[str, Any] = context or {}
        super().__init__(self.message)

    def __str__(self) -> str:
        prefix = f"[tenant={self.tenant_id}] " if self.tenant_id else ""
        return f"{prefix}{self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": self.message,
            "tenant_id": self.tenant_id,
            "handoff_recommended": self.handoff_recommended,
            "context": self.context,
        }


# --- Configuração / Multi-tenant -------------------------------------------
class ConfigurationError(LoboError):
    default_message = "Configuração inválida ou ausente"


class TenantNotFoundError(LoboError):
    default_message = "Tenant não encontrado"


class TenantIsolationError(LoboError):
    """Tentativa de acessar dado de um tenant a partir de outro."""

    default_message = "Violação de isolamento multi-tenant"


# --- Integrações externas (FALLBACK SEGURO => handoff) ---------------------
class ExternalServiceError(LoboError):
    default_message = "Falha em serviço externo"
    handoff_recommended = True


class MessageBufferError(ExternalServiceError):
    default_message = "Falha no buffer de mensagens (Redis)"


class StateStoreError(ExternalServiceError):
    """Falha ao ler/gravar estado compartilhado (ex.: pausa da IA no Redis)."""

    default_message = "Falha no armazenamento de estado (Redis)"


class GatewayError(ExternalServiceError):
    default_message = "Falha no gateway de WhatsApp"


class CRMError(ExternalServiceError):
    default_message = "Falha na integração com o CRM"


class LLMError(ExternalServiceError):
    default_message = "Falha no provedor de LLM"


class TranscriptionError(ExternalServiceError):
    default_message = "Falha ao transcrever áudio"


class DatabaseError(ExternalServiceError):
    default_message = "Falha no banco de dados"


# --- Payloads / Ferramentas -------------------------------------------------
class PayloadNormalizationError(LoboError):
    """Payload de webhook não reconhecido ou malformado (descartável, sem handoff)."""

    default_message = "Payload de webhook inválido"


class ToolExecutionError(LoboError):
    default_message = "Falha na execução de ferramenta"
    handoff_recommended = True


class PriceNotAvailableError(ToolExecutionError):
    """Ferramenta não retornou preço: o agente NUNCA deve inventar o valor."""

    default_message = "Preço indisponível; requer confirmação da equipe humana"


class HandoffRequired(LoboError):
    """Sinaliza explicitamente que a conversa deve ir para um humano."""

    default_message = "Transferência para atendimento humano necessária"
    handoff_recommended = True

