"""Dados de negócio por tenant (catálogo, entrega, agenda, CRM) e seus repositórios.

Fonte atual: arquivos JSON em `data/tenants/<tenant_id>.json` (validados por Pydantic).
A interface `TenantDataStore` permite trocar por PostgreSQL sem tocar nas ferramentas.

Segurança:
- Isolamento: cada tenant tem seu próprio arquivo/objeto; o `tenant_id` do arquivo
  precisa bater com o nome do arquivo.
- Dados de DEMONSTRAÇÃO (`demo_data: true`) são bloqueados em produção — preços
  inventados jamais podem chegar a um cliente real.
- Segredos de CRM NÃO ficam nos arquivos: o tenant referencia o NOME do campo em
  `Settings` (ex.: `ghl_vizzu_api_token`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.exceptions import ConfigurationError, TenantNotFoundError
from src.core.schemas import CRMProvider, TenantConfig

logger = logging.getLogger("lobo.tenant_data")

BusinessInfoType = Literal["hours", "location", "warranty", "payment_methods"]
_HOURS_RANGE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-4]):([0-5]\d)$")


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"

    @classmethod
    def from_date(cls, d: date) -> Weekday:
        return list(cls)[d.weekday()]


# --------------------------------------------------------------------------- #
# Catálogo
# --------------------------------------------------------------------------- #
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class PriceInfo(_Strict):
    """Preço autorizado pelo cliente. `amount=None` = "sob consulta" (exige humano)."""

    amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    cash_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)  # à vista (desconto autorizado)
    max_installments: int = Field(default=1, ge=1, le=24)
    interest_free: bool = True
    updated_at: datetime | None = None
    note: str | None = None  # ex.: "instalação não inclusa"

    @model_validator(mode="after")
    def _cash_not_above_list(self) -> PriceInfo:
        if self.cash_amount is not None and self.amount is not None and self.cash_amount > self.amount:
            raise ValueError("cash_amount não pode ser maior que amount")
        return self


class ItemVariant(_Strict):
    code: str = Field(min_length=1)
    name: str
    price: PriceInfo
    in_stock: bool = True


class CatalogItem(_Strict):
    item_id: str = Field(min_length=1)
    name: str
    category: str
    description: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    price: PriceInfo
    variants: list[ItemVariant] = Field(default_factory=list)
    in_stock: bool = True
    active: bool = True

    def variant(self, code: str) -> ItemVariant | None:
        wanted = code.strip().lower()
        return next((v for v in self.variants if v.code.lower() == wanted), None)


class CatalogConfig(_Strict):
    currency: Literal["BRL"] = "BRL"
    price_max_age_days: int | None = Field(default=None, ge=1)  # None = sem checagem de validade
    synonyms: dict[str, list[str]] = Field(default_factory=dict)  # termo canônico → sinônimos
    items: list[CatalogItem] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def _unique_ids(cls, items: list[CatalogItem]) -> list[CatalogItem]:
        ids = [i.item_id.lower() for i in items]
        if len(ids) != len(set(ids)):
            raise ValueError("item_id duplicado no catálogo")
        return items

    def get(self, item_id: str) -> CatalogItem | None:
        wanted = item_id.strip().lower()
        return next((i for i in self.items if i.item_id.lower() == wanted and i.active), None)


# --------------------------------------------------------------------------- #
# Entrega
# --------------------------------------------------------------------------- #
class DeliveryZone(_Strict):
    cities: list[str] = Field(min_length=1)
    fee: Decimal = Field(ge=0, decimal_places=2)
    lead_time_days_min: int = Field(ge=0)
    lead_time_days_max: int = Field(ge=0)
    business_days: bool = True
    notes: str | None = None

    @model_validator(mode="after")
    def _range(self) -> DeliveryZone:
        if self.lead_time_days_min > self.lead_time_days_max:
            raise ValueError("lead_time_days_min > lead_time_days_max")
        return self


class DeliveryConfig(_Strict):
    zones: list[DeliveryZone] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agenda
# --------------------------------------------------------------------------- #
class ServiceConfig(_Strict):
    service_id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    name: str
    duration_minutes: int = Field(ge=5, le=480)
    capacity: int = Field(default=1, ge=1, le=50)  # atendimentos simultâneos (consultores/salas)
    description: str = ""


class CalendarConfig(_Strict):
    services: list[ServiceConfig] = Field(min_length=1)
    working_hours: dict[Weekday, list[str]]
    slot_step_minutes: int = Field(default=30, ge=5, le=240)
    min_notice_minutes: int = Field(default=120, ge=0)
    horizon_days: int = Field(default=14, ge=1, le=120)
    blackout_dates: list[date] = Field(default_factory=list)

    @field_validator("working_hours")
    @classmethod
    def _validate_hours(cls, wh: dict[Weekday, list[str]]) -> dict[Weekday, list[str]]:
        for day, ranges in wh.items():
            for r in ranges:
                m = _HOURS_RANGE.match(r)
                if not m or (int(m[1]), int(m[2])) >= (int(m[3]), int(m[4])):
                    raise ValueError(f"faixa de horário inválida em {day}: {r!r} (use HH:MM-HH:MM)")
        return wh

    def windows(self, day: date) -> list[tuple[time, time]]:
        out = []
        for r in self.working_hours.get(Weekday.from_date(day), []):
            start, end = r.split("-")
            sh, sm = map(int, start.split(":"))
            eh, em = map(int, end.split(":"))
            out.append((time(sh, sm), time(23, 59, 59) if eh == 24 else time(eh, em)))
        return out

    def service(self, service_id: str | None) -> ServiceConfig | None:
        if service_id is None:
            return self.services[0] if len(self.services) == 1 else None
        return next((s for s in self.services if s.service_id == service_id), None)


# --------------------------------------------------------------------------- #
# CRM (sem segredos)
# --------------------------------------------------------------------------- #
class ChatwootTenantConfig(_Strict):
    account_id: int = Field(ge=1)
    inbox_id: int | None = Field(default=None, ge=1)
    handoff_team_id: int | None = Field(default=None, ge=1)
    handoff_assignee_id: int | None = Field(default=None, ge=1)
    bot_agent_ids: list[int] = Field(default_factory=list)  # atribuições que NÃO contam como humano
    # True: conversa atribuída a um agente humano => IA pausa. Use False se o inbox tiver
    # auto-atribuição (senão a IA nunca responde); a tag `atendimento_humano` sempre pausa.
    assignee_means_human: bool = True
    create_missing: bool = True


class GHLTenantConfig(_Strict):
    token_setting: str = Field(pattern=r"^[a-z0-9_]+$")  # nome do campo em Settings
    location_setting: str = Field(default="ghl_vizzu_location_id", pattern=r"^[a-z0-9_]+$")
    location_id: str | None = None
    handoff_user_id: str | None = None


class CRMSettings(_Strict):
    chatwoot: ChatwootTenantConfig | None = None
    ghl: GHLTenantConfig | None = None


# --------------------------------------------------------------------------- #
# Agregado do tenant
# --------------------------------------------------------------------------- #
class TenantData(_Strict):
    demo_data: bool = False
    tenant: TenantConfig
    business_info: dict[BusinessInfoType, str] = Field(default_factory=dict)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    calendar: CalendarConfig | None = None
    crm: CRMSettings = Field(default_factory=CRMSettings)

    @model_validator(mode="after")
    def _crm_consistency(self) -> TenantData:
        provider = self.tenant.crm_provider
        if provider is CRMProvider.CHATWOOT and self.crm.chatwoot is None:
            raise ValueError("tenant usa Chatwoot mas `crm.chatwoot` não foi configurado")
        if provider is CRMProvider.GHL and self.crm.ghl is None:
            raise ValueError("tenant usa GHL mas `crm.ghl` não foi configurado")
        return self

    @property
    def tenant_id(self) -> str:
        return self.tenant.tenant_id


# --------------------------------------------------------------------------- #
# Repositórios
# --------------------------------------------------------------------------- #
class TenantDataStore(Protocol):
    def get(self, tenant_id: str) -> TenantData: ...

    def tenant_ids(self) -> list[str]: ...


class InMemoryTenantDataStore:
    """Repositório em memória (testes e composição programática)."""

    def __init__(self, tenants: list[TenantData] | None = None, *, allow_demo: bool = True) -> None:
        self._allow_demo = allow_demo
        self._data: dict[str, TenantData] = {}
        for t in tenants or []:
            self.put(t)

    def put(self, data: TenantData) -> None:
        if data.demo_data and not self._allow_demo:
            raise ConfigurationError("dados de demonstração bloqueados neste ambiente", tenant_id=data.tenant_id)
        self._data[data.tenant_id] = data

    def get(self, tenant_id: str) -> TenantData:
        try:
            data = self._data[tenant_id]
        except KeyError:
            raise TenantNotFoundError(tenant_id=tenant_id) from None
        if not data.tenant.active:
            raise TenantNotFoundError("tenant inativo", tenant_id=tenant_id)
        return data

    def tenant_ids(self) -> list[str]:
        return sorted(self._data)

    def get_any(self, tenant_id: str) -> TenantData:
        """Como `get`, mas inclui tenants inativos (uso administrativo/edição)."""
        try:
            return self._data[tenant_id]
        except KeyError:
            raise TenantNotFoundError(tenant_id=tenant_id) from None

    def all(self) -> list[TenantData]:
        return [self._data[t] for t in sorted(self._data)]

    @property
    def allow_demo(self) -> bool:
        return self._allow_demo


def stamp_missing_price_dates(data: TenantData, now: datetime) -> TenantData:
    """Preço novo sem `updated_at` ganha a data atual (senão `price_max_age_days` o esconderia)."""
    raw = data.model_dump(mode="json")
    stamped = False
    for item in raw.get("catalog", {}).get("items", []):
        prices = [item.get("price") or {}, *[(v.get("price") or {}) for v in item.get("variants") or []]]
        for price in prices:
            if price.get("amount") is not None and not price.get("updated_at"):
                price["updated_at"] = now.isoformat()
                stamped = True
    return TenantData.model_validate(raw) if stamped else data


class JsonTenantDataStore(InMemoryTenantDataStore):
    """Carrega `<diretório>/<tenant_id>.json`. Use `reload()` após editar os arquivos."""

    def __init__(self, directory: Path, *, allow_demo: bool = True) -> None:
        super().__init__(allow_demo=allow_demo)
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self.reload()

    def path_for(self, tenant_id: str) -> Path:
        return self.directory / f"{tenant_id}.json"

    def save(self, data: TenantData, *, create: bool = False) -> TenantData:
        """Grava `<tenant_id>.json` de forma atômica e recarrega o repositório na hora.

        - `create=True` exige que o tenant ainda não exista (evita sobrescrever outro cliente).
        - Dados de DEMONSTRAÇÃO continuam proibidos onde `allow_demo=False` (produção).
        """
        tenant_id = data.tenant_id
        path = self.path_for(tenant_id)
        if data.demo_data and not self._allow_demo:
            raise ConfigurationError("dados de DEMONSTRAÇÃO não podem ser salvos em produção", tenant_id=tenant_id)
        if create and (tenant_id in self._data or path.exists()):
            raise ConfigurationError("já existe um cliente com este tenant_id", tenant_id=tenant_id)
        if not create and tenant_id not in self._data:
            raise TenantNotFoundError(tenant_id=tenant_id)

        payload = json.dumps(data.model_dump(mode="json", exclude_none=True), ensure_ascii=False, indent=2)
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(payload + "\n", encoding="utf-8")
            os.replace(tmp, path)
        self.reload()
        logger.info("Tenant %s salvo e recarregado (%s)", tenant_id, "criado" if create else "atualizado")
        return self._data[tenant_id]

    def reload(self) -> None:
        if not self.directory.is_dir():
            raise ConfigurationError(f"diretório de tenants não encontrado: {self.directory}")
        loaded: dict[str, TenantData] = {}
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = TenantData.model_validate_json(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ConfigurationError(f"arquivo de tenant inválido: {path.name}: {exc}") from exc
            if data.tenant_id != path.stem:
                raise ConfigurationError(
                    f"{path.name}: tenant_id '{data.tenant_id}' difere do nome do arquivo (isolamento)"
                )
            if data.demo_data and not self._allow_demo:
                raise ConfigurationError(
                    f"{path.name}: dados de DEMONSTRAÇÃO não podem ser usados em produção", tenant_id=data.tenant_id
                )
            if data.demo_data:
                logger.warning("Tenant %s usa dados de DEMONSTRAÇÃO (preços fictícios)", data.tenant_id)
            loaded[data.tenant_id] = data
        with self._lock:
            self._data = loaded
        logger.info("Tenants carregados: %s", ", ".join(sorted(loaded)) or "(nenhum)")
