"""Ferramentas determinísticas de catálogo, preço, entrega e informações do negócio.

Regra inviolável: o LLM NUNCA calcula nem "lembra" preço. Todo valor exibido ao
cliente sai daqui, já formatado (`R$ 2.899,00`) e com parcelamento calculado em
`Decimal`. Sem preço válido → `requires_human=True` + instrução de não informar valor.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.core.schemas import TenantId, utcnow
from src.tools.geo import (
    GeoLookup,
    GeoResolution,
    extract_cep,
    extract_coordinates,
    resolve_neighborhood,
)
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
    DeliveryConfig,
    DeliveryZone,
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

_KNOWN_SIZES = frozenset({"solteiro", "casal", "queen", "king"})


def _catalog_sizes(catalog: CatalogConfig) -> frozenset[str]:
    """Variantes de tamanho declaradas no PRÓPRIO catálogo (atributo `tamanho`), agnóstico de nicho.

    Ex.: colchões (solteiro/casal/queen/king), roupas (p/m/g), piscinas (6x3/8x4)…
    Mantém `_KNOWN_SIZES` como base para catálogos legados sem o atributo.
    """
    sizes = {
        normalize_text(item.attributes.get("tamanho", ""))
        for item in catalog.items
        if item.attributes.get("tamanho")
    }
    return frozenset(s for s in sizes if s) or _KNOWN_SIZES

PRICE_SHOW_INSTRUCTION = (
    "Use EXATAMENTE os valores formatados retornados (campos *_formatted). "
    "Não arredonde, não some, não ofereça descontos além do retornado. "
    "Apresente apenas os itens retornados que correspondem à variante pedida pelo cliente "
    "(tamanho, modelo, modalidade etc.) e que estão disponíveis. "
    "NUNCA invente preços ou descontos fora da tabela."
)

OUT_OF_STOCK_INSTRUCTION = (
    "AVISO DE DISPONIBILIDADE: o item/variante pesquisado está indisponível para pronta-entrega no momento (in_stock=False). "
    "Conduta de consultor humano: "
    "1. Informe com naturalidade e honestidade que essa opção está temporariamente indisponível para pronta-entrega. "
    "2. Você pode informar o valor oficial formatado normalmente (price_formatted / installments_formatted). "
    "3. NÃO acione o handoff por causa de disponibilidade. "
    "4. Pergunte, em UMA frase, se o cliente prefere que você verifique o prazo de reposição/encomenda "
    "ou se quer conhecer uma alternativa disponível (outra variante ou item parecido do catálogo)."
)

DELIVERY_OK_INSTRUCTION = (
    "Informe a taxa (fee_formatted) e o prazo (lead_time_formatted) EXATAMENTE como retornados. "
    "Se houver `neighborhood`, cite o bairro do cliente com naturalidade."
)

LOCATION_UNRESOLVED_INSTRUCTION = (
    "Não foi possível identificar a cidade a partir do que o cliente disse. NÃO acione o handoff e NÃO prometa entrega. "
    "Pergunte de forma leve, em UMA frase, a cidade/município (ou o CEP, ou que ele envie a localização pelo WhatsApp) "
    "e, se ajudar, cite as cidades atendidas listadas em `served_cities`."
)

OUTSIDE_AREA_INSTRUCTION = (
    "A cidade do cliente foi identificada, mas NÃO consta na área de entrega cadastrada. NÃO invente frete nem prazo "
    "e NÃO acione o handoff automaticamente. Seja honesto e acolhedor: diga que a entrega padrão atende as cidades em "
    "`served_cities`; se `pickup_available` for true, ofereça a retirada na loja (`store_location`). Pergunte se ele "
    "quer seguir assim ou se prefere que você verifique uma entrega especial. Só acione `trigger_human_handoff` "
    "(reason='customer_request') se o cliente pedir essa cotação especial."
)

BUSINESS_INFO_MISSING_INSTRUCTION = (
    "Essa informação não está cadastrada. NÃO invente e NÃO acione o handoff por isso. Responda com naturalidade que "
    "vai confirmar esse detalhe, siga conduzindo a conversa e só transfira se o cliente insistir ou se for decisivo "
    "para fechar."
)

FEATURED_FALLBACK_INSTRUCTION = (
    "Itens mais procurados e populares do catálogo retornados. "
    "Apresente brevemente as opções principais e faça uma pergunta consultiva "
    "para identificar a real necessidade do cliente."
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
    def __init__(
        self,
        store: TenantDataStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        geo: GeoLookup | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._geo = geo

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
        text = " ".join(filter(None, [query, *(keywords or [])])).strip()
        if not text and not category:
            return ToolResult.invalid(tool, tenant_id, "informe uma busca (query/keywords) ou categoria")

        tokens = tokenize(text)
        synonyms = _synonym_map(catalog)
        terms = _expand(tokens, synonyms)
        wanted_category = normalize_text(category) if category else None
        known_sizes = _catalog_sizes(catalog)
        requested_sizes = terms & known_sizes

        ranked: list[tuple[int, CatalogItem]] = []
        if terms or wanted_category:
            for item in catalog.items:
                if not item.active:
                    continue
                if wanted_category and normalize_text(item.category) != wanted_category:
                    continue
                if requested_sizes:
                    item_size = normalize_text(item.attributes.get("tamanho", ""))
                    if item_size in known_sizes and item_size not in requested_sizes:
                        continue
                score = score_item(item, terms, synonyms) if terms else 1
                if score > 0:
                    ranked.append((score, item))
            # Se o cliente pediu um tamanho específico e existem itens em estoque desse tamanho,
            # remove itens sem estoque para o agente jamais enviar produto esgotado contraditório.
            if requested_sizes and any(it.in_stock for _, it in ranked):
                ranked = [(sc, it) for sc, it in ranked if it.in_stock]
            # Ordem determinística: relevância ↓, em estoque primeiro, item_id ↑
            ranked.sort(key=lambda p: (-p[0], not p[1].in_stock, p[1].item_id))

        if not ranked:
            # REGRA INVIOLÁVEL: search_catalog NUNCA retorna requires_human=True por falta de match exato.
            # Faz fallback automático para os 3 carros-chefes (featured=True / em estoque com maior margem/procura).
            fallback_items = self._select_featured_items(catalog, wanted_category, limit=min(3, limit))
            if not fallback_items:
                return ToolResult(
                    tool=tool,
                    tenant_id=tenant_id,
                    status=ToolStatus.NOT_FOUND,
                    requires_human=False,
                    reason="empty_catalog",
                    data={"results": [], "total": 0},
                    agent_instruction=FEATURED_FALLBACK_INSTRUCTION,
                )
            results = [self._item_summary(catalog, item, 1) for item in fallback_items]
            return ToolResult.success(
                tool,
                tenant_id,
                {"results": results, "total": len(results), "featured_fallback": True},
                instruction=FEATURED_FALLBACK_INSTRUCTION,
            )

        has_in_stock = any(item.in_stock for _, item in ranked)
        instruction = PRICE_SHOW_INSTRUCTION if has_in_stock else OUT_OF_STOCK_INSTRUCTION
        results = [self._item_summary(catalog, item, score) for score, item in ranked[:limit]]
        return ToolResult.success(
            tool,
            tenant_id,
            {"results": results, "total": len(ranked), "has_in_stock": has_in_stock},
            instruction=instruction,
        )

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
    @staticmethod
    def _zone_for_city(delivery: DeliveryConfig, city: str) -> tuple[DeliveryZone, str] | None:
        """Zona cuja cidade cadastrada coincide (normalizada) com `city`."""
        wanted = normalize_text(city)
        parts = wanted.split()
        if len(parts) > 1 and parts[-1] in _UFS:
            wanted = " ".join(parts[:-1])
        for zone in delivery.zones:
            for c in zone.cities:
                if normalize_text(c) == wanted:
                    return zone, c
        return None

    @staticmethod
    def _zone_city_in_text(delivery: DeliveryConfig, text: str) -> tuple[DeliveryZone, str] | None:
        """Cidade cadastrada citada em qualquer parte do texto ("bairro X, Marituba"), a mais longa primeiro."""
        norm = f" {normalize_text(text)} "
        candidates = [(normalize_text(c), c, zone) for zone in delivery.zones for c in zone.cities]
        for key, original, zone in sorted(candidates, key=lambda x: len(x[0]), reverse=True):
            if key and f" {key} " in norm:
                return zone, original
        return None

    @staticmethod
    def _served_cities(delivery: DeliveryConfig) -> list[str]:
        seen: list[str] = []
        for zone in delivery.zones:
            for c in zone.cities:
                if c not in seen:
                    seen.append(c)
        return seen

    async def _resolve_location(self, delivery: DeliveryConfig, text: str) -> GeoResolution | None:
        """Cadeia genérica: cidade no texto -> gazetteer do tenant -> CEP -> coordenadas -> geocodificação."""
        nb = resolve_neighborhood(text, delivery.neighborhoods)
        direct = self._zone_city_in_text(delivery, text)
        if direct is not None:
            return GeoResolution(city=direct[1], neighborhood=nb[0] if nb else None, source="zone_text")
        if nb is not None:
            return GeoResolution(city=nb[1], neighborhood=nb[0], source="tenant_gazetteer")
        if self._geo is None:
            return None
        cep = extract_cep(text)
        if cep:
            res = await self._geo.lookup_cep(cep)
            if res is not None:
                return res
        coords = extract_coordinates(text)
        if coords:
            res = await self._geo.reverse(*coords)
            if res is not None:
                return res
        return await self._geo.lookup_text(text, region_hint=delivery.region_hint)

    @tool_guard("get_delivery_info")
    async def get_delivery_info(self, tenant_id: str, city: str) -> ToolResult:
        """Frete/prazo para o local citado (cidade, bairro, ponto de referência, CEP ou coordenadas).

        REGRA: local desconhecido ou fora da área NUNCA gera `requires_human=True`; o agente pergunta
        a cidade/CEP ou oferece alternativas (retirada), mantendo a conversa com o cliente.
        """
        tool = "get_delivery_info"
        tenant_data = self._store.get(tenant_id)
        delivery = tenant_data.delivery
        raw = (city or "").strip()
        served = self._served_cities(delivery)
        store_location = tenant_data.business_info.get("location")

        resolution: GeoResolution | None = None
        match = self._zone_for_city(delivery, raw)
        if match is not None:
            resolution = GeoResolution(city=match[1], source="zone_exact")
        else:
            resolution = await self._resolve_location(delivery, raw)
            if resolution is not None:
                match = self._zone_for_city(delivery, resolution.city)

        if match is not None and resolution is not None:
            zone, zone_city = match
            unit = "dias úteis" if zone.business_days else "dias"
            lead = (
                f"{zone.lead_time_days_min} {unit}"
                if zone.lead_time_days_min == zone.lead_time_days_max
                else f"{zone.lead_time_days_min} a {zone.lead_time_days_max} {unit}"
            )
            payload: dict[str, Any] = {
                "city": resolution.neighborhood or zone_city,
                "delivers": True,
                "fee": zone.fee,
                "fee_formatted": "Grátis" if zone.fee == 0 else format_brl(zone.fee),
                "lead_time_days_min": zone.lead_time_days_min,
                "lead_time_days_max": zone.lead_time_days_max,
                "lead_time_formatted": lead,
                "notes": zone.notes,
                "resolution_source": resolution.source,
            }
            if resolution.neighborhood:
                payload["neighborhood"] = resolution.neighborhood
                payload["parent_city"] = zone_city
                if store_location:
                    payload["store_reference"] = store_location
            return ToolResult.success(tool, tenant_id, payload, instruction=DELIVERY_OK_INSTRUCTION)

        base: dict[str, Any] = {
            "query": raw,
            "delivers": False,
            "served_cities": served,
            "pickup_available": delivery.pickup_available,
        }
        if store_location:
            base["store_location"] = store_location

        if resolution is not None:
            return ToolResult(
                tool=tool,
                tenant_id=tenant_id,
                status=ToolStatus.NOT_FOUND,
                requires_human=False,
                reason="outside_delivery_area",
                data={**base, **resolution.as_dict()},
                agent_instruction=OUTSIDE_AREA_INSTRUCTION,
            )
        return ToolResult(
            tool=tool,
            tenant_id=tenant_id,
            status=ToolStatus.NOT_FOUND,
            requires_human=False,
            reason="location_unresolved",
            data=base,
            agent_instruction=LOCATION_UNRESOLVED_INSTRUCTION,
        )

    # -------------------------------------------------------------- negócio
    @tool_guard("get_business_info")
    async def get_business_info(self, tenant_id: str, info_type: BusinessInfoType) -> ToolResult:
        tool = "get_business_info"
        info = self._store.get(tenant_id).business_info.get(info_type)
        if not info:
            return ToolResult(
                tool=tool,
                tenant_id=tenant_id,
                status=ToolStatus.NOT_FOUND,
                requires_human=False,
                reason=f"business_info_missing:{info_type}",
                agent_instruction=BUSINESS_INFO_MISSING_INSTRUCTION,
            )
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

    def _select_featured_items(
        self,
        catalog: CatalogConfig,
        wanted_category: str | None,
        *,
        limit: int = 3,
    ) -> list[CatalogItem]:
        """Seleciona os carros-chefes (featured=True ou em estoque com maior margem/procura)."""
        candidates = [
            item
            for item in catalog.items
            if item.active and (not wanted_category or normalize_text(item.category) == wanted_category)
        ]
        if not candidates and wanted_category:
            candidates = [item for item in catalog.items if item.active]
        candidates.sort(
            key=lambda it: (
                not it.featured,
                not it.in_stock,
                self._price_problem(catalog, it.price) is not None,
                -(it.price.amount or Decimal(0)),
                it.item_id,
            )
        )
        return candidates[: max(1, limit)]

    def _item_summary(self, catalog: CatalogConfig, item: CatalogItem, score: int) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "item_id": item.item_id,
            "name": item.name,
            "category": item.category,
            "description": item.description or None,
            "featured": item.featured,
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
    "FEATURED_FALLBACK_INSTRUCTION",
    "PriceQueryInput",
    "resolve_neighborhood",
    "tokenize",
]
