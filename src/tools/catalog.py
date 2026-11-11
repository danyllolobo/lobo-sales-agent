"""Ferramentas determinísticas de catálogo, preço, entrega e informações do negócio.

Regra inviolável: o LLM NUNCA calcula nem "lembra" preço. Todo valor exibido ao
cliente sai daqui, já formatado (`R$ 2.899,00`) e com parcelamento calculado em
`Decimal`. Sem preço válido → `requires_human=True` + instrução de não informar valor.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.core.schemas import TenantId, utcnow
from src.tools.results import (
    CENT,
    NO_PRICE_INSTRUCTION,
    ToolResult,
    ToolStatus,
    format_brl,
    normalize_text,
    tool_guard,
)
from src.tools.tenant_data import (
    BusinessInfoType,
    CatalogConfig,
    CatalogItem,
    PriceInfo,
    TenantDataStore,
)

# Palavras sem valor de busca em PT-BR (após normalização/singularização)
_STOPWORDS = frozenset(
    "a o e de da do das dos em no na nos nas um uma uns umas com sem para pra por que qual quais "
    "quanto quanta custa custo preco valor tem ter queria quero gostaria ver saber voce voces "
    "me meu minha seu sua ola oi bom boa dia tarde noite favor modelo tipo algum alguma "
    "mais menos muito entao ai la aqui isso esse essa este esta vcs vc tb tambem".split()
)

_UFS = frozenset(
    "ac al ap am ba ce df es go ma mt ms mg pa pb pr pe pi rj rn rs ro rr sc sp se to".split()
)

PRICE_SHOW_INSTRUCTION = (
    "Use EXATAMENTE os valores formatados retornados (campos *_formatted). "
    "Não arredonde, não some, não ofereça descontos além do retornado."
)


# --------------------------------------------------------------------------- #
# Contratos de entrada (ARCHITECTURE.md §5). `tenant_id` é injetado pelo
# orquestrador a partir do contexto da conversa — nunca escolhido pelo LLM.
# --------------------------------------------------------------------------- #
class BusinessInfoInput(BaseModel):
    tenant_id: TenantId
    info_type: BusinessInfoType


class CatalogSearchInput(BaseModel):
    tenant_id: TenantId
    category: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)


class PriceQueryInput(BaseModel):
    tenant_id: TenantId
    item_id: str = Field(min_length=1)
    variant: Optional[str] = None


class DeliveryQueryInput(BaseModel):
    tenant_id: TenantId
    city: str = Field(min_length=2)


# --------------------------------------------------------------------------- #
# Busca textual determinística
# --------------------------------------------------------------------------- #
def tokenize(text: str) -> list[str]:
    """Normaliza, singulariza plurais simples e remove stopwords."""
    tokens = []
    for tok in normalize_text(text).split():
        if tok in _STOPWORDS:
            continue
        if len(tok) > 3 and tok.endswith("s"):
            tok = tok[:-1]
        if tok not in _STOPWORDS:
            tokens.append(tok)
    return tokens


def _synonym_map(catalog: CatalogConfig) -> dict[str, str]:
    """sinônimo (tokenizado) → termo canônico (tokenizado)."""
    mapping: dict[str, str] = {}
    for canonical, aliases in catalog.synonyms.items():
        canon = " ".join(tokenize(canonical))
        for alias in [canonical, *aliases]:
            key = " ".join(tokenize(alias))
            if key:
                mapping[key] = canon
    return mapping


def _expand(tokens: list[str], synonyms: dict[str, str]) -> set[str]:
    """Adiciona os termos canônicos de sinônimos (inclusive de 2 palavras: "molas pocket")."""
    expanded = set(tokens)
    grams = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
    for g in grams:
        if g in synonyms:
            expanded.update(synonyms[g].split())
    return expanded


def _item_fields(item: CatalogItem) -> dict[str, set[str]]:
    strong = tokenize(" ".join([item.name, *item.tags, *item.attributes.values()]))
    return {
        "strong": set(strong),
        "category": set(tokenize(item.category)),
        "weak": set(tokenize(item.description)),
    }


def score_item(item: CatalogItem, query_terms: set[str], synonyms: dict[str, str]) -> int:
    fields = _item_fields(item)
    strong = _expand(sorted(fields["strong"]), synonyms)
    score = 0
    for term in query_terms:
        if term in strong:
            score += 3
        elif term in fields["category"]:
            score += 2
        elif term in fields["weak"]:
            score += 1
    return score


# --------------------------------------------------------------------------- #
# Serviço
# --------------------------------------------------------------------------- #
class CatalogService:
    def __init__(self, store: TenantDataStore, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._store = store
        self._clock = clock

    # -------------------------------------------------------------- busca
    @tool_guard("search_catalog")
    async def search_catalog(
        self,
        tenant_id: str,
        query: str | None = None,
        *,
        category: str | None = None,
        keywords: list[str] | None = None,
        limit: int = 5,
    ) -> ToolResult:
        tool = "search_catalog"
        data = self._store.get(tenant_id)
        catalog = data.catalog
        text = " ".join(filter(None, [query, *(keywords or [])]))
        tokens = tokenize(text)
        if not tokens and not category:
            return ToolResult.invalid(tool, tenant_id, "informe uma busca (query/keywords) ou categoria")

        synonyms = _synonym_map(catalog)
        terms = _expand(tokens, synonyms)
        wanted_category = normalize_text(category) if category else None

        ranked: list[tuple[int, CatalogItem]] = []
        for item in catalog.items:
            if not item.active:
                continue
            if wanted_category and normalize_text(item.category) != wanted_category:
                continue
            score = score_item(item, terms, synonyms) if terms else 1
            if score > 0:
                ranked.append((score, item))
        # Ordem determinística: relevância ↓, em estoque primeiro, item_id ↑
        ranked.sort(key=lambda p: (-p[0], not p[1].in_stock, p[1].item_id))

        if not ranked:
            return ToolResult.needs_human(
                tool,
                tenant_id,
                ToolStatus.NOT_FOUND,
                "no_catalog_match",
                instruction=(
                    "Nenhum produto/serviço do catálogo corresponde à busca. NÃO invente opções nem preços: "
                    "faça uma pergunta para refinar a necessidade ou acione o handoff humano."
                ),
            )
        results = [self._item_summary(catalog, item, score) for score, item in ranked[:limit]]
        return ToolResult.success(tool, tenant_id, {"results": results, "total": len(ranked)}, instruction=PRICE_SHOW_INSTRUCTION)

    # -------------------------------------------------------------- preço
    @tool_guard("get_realtime_price")
    async def get_realtime_price(self, tenant_id: str, item_code: str, variant: str | None = None) -> ToolResult:
        tool = "get_realtime_price"
        catalog = self._store.get(tenant_id).catalog
        item = catalog.get(item_code)
        if item is None:
            return ToolResult.needs_human(
                tool, tenant_id, ToolStatus.NOT_FOUND, "item_not_found", instruction=NO_PRICE_INSTRUCTION
            )
        price, in_stock, variant_name = item.price, item.in_stock, None
        if variant:
            v = item.variant(variant)
            if v is None:
                return ToolResult.needs_human(
                    tool,
                    tenant_id,
                    ToolStatus.NOT_FOUND,
                    "variant_not_found",
                    instruction=NO_PRICE_INSTRUCTION,
                    data={"available_variants": [x.code for x in item.variants]},
                )
            price, in_stock, variant_name = v.price, v.in_stock, v.name

        problem = self._price_problem(catalog, price)
        if problem:
            return ToolResult.needs_human(
                tool,
                tenant_id,
                ToolStatus.UNAVAILABLE,
                problem,
                instruction=NO_PRICE_INSTRUCTION,
                data={"item_id": item.item_id, "name": item.name},
            )
        payload: dict[str, Any] = {
            "item_id": item.item_id,
            "name": item.name,
            "variant": variant_name,
            "in_stock": in_stock,
            **self._price_payload(price),
        }
        instruction = PRICE_SHOW_INSTRUCTION
        if not in_stock:
            instruction += " O item está SEM ESTOQUE: informe isso e ofereça alternativas do catálogo."
        return ToolResult.success(tool, tenant_id, payload, instruction=instruction)

    # -------------------------------------------------------------- entrega
    @tool_guard("get_delivery_info")
    async def get_delivery_info(self, tenant_id: str, city: str) -> ToolResult:
        tool = "get_delivery_info"
        delivery = self._store.get(tenant_id).delivery
        wanted = normalize_text(city)
        # aceita "Belém", "belem pa", "Belém - PA", "Ananindeua/PA"
        parts = wanted.split()
        wanted_city = " ".join(parts[:-1]) if len(parts) > 1 and parts[-1] in _UFS else wanted
        for zone in delivery.zones:
            if any(normalize_text(c) in (wanted, wanted_city) for c in zone.cities):
                unit = "dias úteis" if zone.business_days else "dias"
                lead = (
                    f"{zone.lead_time_days_min} {unit}"
                    if zone.lead_time_days_min == zone.lead_time_days_max
                    else f"{zone.lead_time_days_min} a {zone.lead_time_days_max} {unit}"
                )
                return ToolResult.success(
                    tool,
                    tenant_id,
                    {
                        "city": city.strip(),
                        "delivers": True,
                        "fee": zone.fee,
                        "fee_formatted": "Grátis" if zone.fee == 0 else format_brl(zone.fee),
                        "lead_time_days_min": zone.lead_time_days_min,
                        "lead_time_days_max": zone.lead_time_days_max,
                        "lead_time_formatted": lead,
                        "notes": zone.notes,
                    },
                    instruction="Informe a taxa e o prazo EXATAMENTE como retornados.",
                )
        return ToolResult.needs_human(
            tool,
            tenant_id,
            ToolStatus.NOT_FOUND,
            "delivery_zone_not_found",
            instruction=(
                "Esta cidade não consta na área de entrega cadastrada. NÃO prometa entrega, prazo ou frete: "
                "diga que vai verificar com a equipe e acione o handoff humano."
            ),
            data={"city": city.strip()},
        )

    # -------------------------------------------------------------- negócio
    @tool_guard("get_business_info")
    async def get_business_info(self, tenant_id: str, info_type: BusinessInfoType) -> ToolResult:
        tool = "get_business_info"
        info = self._store.get(tenant_id).business_info.get(info_type)
        if not info:
            return ToolResult.needs_human(tool, tenant_id, ToolStatus.NOT_FOUND, f"business_info_missing:{info_type}")
        return ToolResult.success(tool, tenant_id, {"info_type": info_type, "content": info})

    # -------------------------------------------------------------- helpers
    def _price_problem(self, catalog: CatalogConfig, price: PriceInfo) -> str | None:
        if price.amount is None:
            return "price_on_request"
        if catalog.price_max_age_days is not None:
            if price.updated_at is None:
                return "price_without_update_date"
            if self._clock() - price.updated_at > timedelta(days=catalog.price_max_age_days):
                return "price_outdated"
        return None

    @staticmethod
    def _price_payload(price: PriceInfo) -> dict[str, Any]:
        assert price.amount is not None
        n = price.max_installments
        installment = (price.amount / n).quantize(CENT, rounding=ROUND_HALF_UP)
        payload: dict[str, Any] = {
            "price": price.amount,
            "price_formatted": format_brl(price.amount),
            "max_installments": n,
            "installment_value": installment,
            "installments_formatted": (
                f"{n}x de {format_brl(installment)}{' sem juros' if price.interest_free else ''}"
                if n > 1
                else None
            ),
            "cash_price": price.cash_amount,
            "cash_price_formatted": format_brl(price.cash_amount) if price.cash_amount is not None else None,
            "price_note": price.note,
            "price_updated_at": price.updated_at,
        }
        return {k: v for k, v in payload.items() if v is not None}

    def _item_summary(self, catalog: CatalogConfig, item: CatalogItem, score: int) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "item_id": item.item_id,
            "name": item.name,
            "category": item.category,
            "attributes": item.attributes,
            "in_stock": item.in_stock,
            "relevance": score,
            "variants": [{"code": v.code, "name": v.name, "in_stock": v.in_stock} for v in item.variants] or None,
        }
        problem = self._price_problem(catalog, item.price)
        if problem:
            summary["price_available"] = False
            summary["price_unavailable_reason"] = problem
        else:
            summary["price_available"] = True
            summary.update(self._price_payload(item.price))
        return {k: v for k, v in summary.items() if v is not None}


__all__ = [
    "BusinessInfoInput",
    "CatalogSearchInput",
    "CatalogService",
    "DeliveryQueryInput",
    "PriceQueryInput",
    "tokenize",
]
