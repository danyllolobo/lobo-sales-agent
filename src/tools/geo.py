"""Inteligência geográfica genérica (agnóstica de cliente/região) para entrega e frete.

Cadeia de resolução usada por `CatalogService.get_delivery_info`:

1. Cidade cadastrada nas zonas de entrega do tenant citada no texto ("Cidade Nova, Ananindeua").
2. Gazetteer de bairros do PRÓPRIO tenant (`delivery.neighborhoods`: bairro -> cidade).
3. CEP no texto -> ViaCEP (gratuito, sem chave).
4. Geocodificação aberta (OpenStreetMap/Nominatim, gratuita, sem chave) com viés regional
   do tenant (`delivery.region_hint`, ex.: "Belém, Pará").
5. Localização compartilhada no WhatsApp (lat/lng) -> geocodificação reversa.

Nenhuma etapa é obrigatória: falhas de rede/timeout retornam `None` e o agente apenas
pergunta a cidade/CEP ao cliente (NUNCA transfere para humano por isso).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from src.tools.results import normalize_text

logger = logging.getLogger("lobo.geo")

_CEP_RE = re.compile(r"\b(\d{5})-?(\d{3})\b")
_COORD_RE = re.compile(r"(-?\d{1,2}\.\d{3,})\s*[,; ]\s*(-?\d{1,3}\.\d{3,})")

_PLACE_CATEGORIES = frozenset({"place", "boundary", "landuse"})
_PLACE_TYPES = frozenset({
    "city", "town", "village", "municipality", "hamlet", "suburb", "neighbourhood", "quarter",
    "city_district", "district", "borough", "residential", "locality", "isolated_dwelling",
    "administrative", "county", "state_district", "postcode",
})

_UF_BY_STATE = {
    "acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA", "ceara": "CE",
    "distrito federal": "DF", "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
    "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG", "para": "PA",
    "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
    "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
    "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO",
}


@dataclass(frozen=True, slots=True)
class GeoResolution:
    """Resultado normalizado de uma resolução geográfica."""

    city: str
    state: str | None = None
    neighborhood: str | None = None
    source: str = "unknown"  # zone_text | tenant_gazetteer | cep | geocoder | reverse

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in {
            "resolved_city": self.city,
            "resolved_state": self.state,
            "resolved_neighborhood": self.neighborhood,
            "resolution_source": self.source,
        }.items() if v}


class GeoLookup(Protocol):
    async def lookup_text(self, text: str, *, region_hint: str | None = None) -> GeoResolution | None: ...

    async def lookup_cep(self, cep: str) -> GeoResolution | None: ...

    async def reverse(self, latitude: float, longitude: float) -> GeoResolution | None: ...


def extract_cep(text: str) -> str | None:
    m = _CEP_RE.search(text or "")
    return f"{m.group(1)}{m.group(2)}" if m else None


def extract_coordinates(text: str) -> tuple[float, float] | None:
    m = _COORD_RE.search(text or "")
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lng <= 180:
        return lat, lng
    return None


def resolve_neighborhood(raw_text: str, neighborhoods: dict[str, str] | None) -> tuple[str, str] | None:
    """Procura no texto um bairro do gazetteer do tenant. Retorna `(Bairro, Cidade)`.

    Chaves mais longas primeiro para priorizar nomes compostos ("cidade nova" antes de "nova").
    """
    if not raw_text or not neighborhoods:
        return None
    norm = f" {normalize_text(raw_text)} "
    index = {normalize_text(name): (name, city) for name, city in neighborhoods.items() if name and city}
    for token in sorted(index, key=len, reverse=True):
        if token and f" {token} " in norm:
            return index[token]
    return None


def _uf(state: str | None) -> str | None:
    if not state:
        return None
    norm = normalize_text(state)
    if len(norm) == 2:
        return norm.upper()
    return _UF_BY_STATE.get(norm)


def _city_from_address(address: dict[str, Any]) -> str | None:
    for key in ("city", "town", "municipality", "village", "city_district"):
        value = address.get(key)
        if value:
            return str(value)
    return None


def _neighborhood_from_address(address: dict[str, Any]) -> str | None:
    for key in ("suburb", "neighbourhood", "quarter", "city_district", "hamlet"):
        value = address.get(key)
        if value:
            return str(value)
    return None


class OpenGeoLookup:
    """Geocodificação com serviços abertos (ViaCEP + OpenStreetMap Nominatim), com cache e rate-limit.

    Política de uso do Nominatim: no máximo 1 requisição/segundo e User-Agent identificável.
    """

    NOMINATIM_URL = "https://nominatim.openstreetmap.org"
    VIACEP_URL = "https://viacep.com.br/ws"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: float = 4.0,
        user_agent: str = "LoboSalesAgent/1.0 (+https://salesagent.loboads.top)",
        cache_size: int = 512,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._headers = {"User-Agent": user_agent, "Accept-Language": "pt-BR"}
        self._cache: dict[str, GeoResolution | None] = {}
        self._cache_size = cache_size
        self._lock = asyncio.Lock()
        self._last_nominatim = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _remember(self, key: str, value: GeoResolution | None) -> GeoResolution | None:
        if len(self._cache) >= self._cache_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = value
        return value

    async def _get_json(self, url: str, params: dict[str, Any] | None = None, *, nominatim: bool = False) -> Any:
        if nominatim:
            async with self._lock:
                wait = 1.05 - (time.monotonic() - self._last_nominatim)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_nominatim = time.monotonic()
        resp = await self._http().get(url, params=params, headers=self._headers, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    async def lookup_cep(self, cep: str) -> GeoResolution | None:
        digits = re.sub(r"\D", "", cep or "")
        if len(digits) != 8:
            return None
        key = f"cep:{digits}"
        if key in self._cache:
            return self._cache[key]
        try:
            data = await self._get_json(f"{self.VIACEP_URL}/{digits}/json/")
        except Exception as exc:  # noqa: BLE001 - geocodificação é best-effort
            logger.info("ViaCEP indisponível (%s)", type(exc).__name__)
            return None
        if not isinstance(data, dict) or data.get("erro") or not data.get("localidade"):
            return self._remember(key, None)
        return self._remember(
            key,
            GeoResolution(
                city=str(data["localidade"]),
                state=str(data.get("uf") or "") or None,
                neighborhood=str(data.get("bairro") or "") or None,
                source="cep",
            ),
        )

    async def lookup_text(self, text: str, *, region_hint: str | None = None) -> GeoResolution | None:
        clean = " ".join((text or "").split())[:160]
        if len(clean) < 3:
            return None
        key = f"txt:{normalize_text(clean)}|{normalize_text(region_hint or '')}"
        if key in self._cache:
            return self._cache[key]
        queries = [f"{clean}, {region_hint}"] if region_hint else []
        queries.append(clean)
        for q in queries:
            try:
                data = await self._get_json(
                    f"{self.NOMINATIM_URL}/search",
                    {"q": q, "format": "jsonv2", "addressdetails": 1, "countrycodes": "br", "limit": 3},
                    nominatim=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("Geocodificação indisponível (%s)", type(exc).__name__)
                return None
            for hit in data or []:
                # Só aceita LUGARES (cidade/bairro/distrito/conjunto). Ruas homônimas ("Travessa Abaetetuba"
                # em Belém, category=highway) não podem sequestrar o nome de um município.
                category = str(hit.get("category") or "").lower()
                kind = str(hit.get("addresstype") or hit.get("type") or "").lower()
                if category and category not in _PLACE_CATEGORIES:
                    continue
                if kind and kind not in _PLACE_TYPES:
                    continue
                address = hit.get("address") or {}
                city = _city_from_address(address)
                if city:
                    return self._remember(
                        key,
                        GeoResolution(
                            city=city,
                            state=_uf(address.get("state")),
                            neighborhood=_neighborhood_from_address(address),
                            source="geocoder",
                        ),
                    )
        return self._remember(key, None)

    async def reverse(self, latitude: float, longitude: float) -> GeoResolution | None:
        key = f"rev:{latitude:.4f},{longitude:.4f}"
        if key in self._cache:
            return self._cache[key]
        try:
            data = await self._get_json(
                f"{self.NOMINATIM_URL}/reverse",
                {"lat": latitude, "lon": longitude, "format": "jsonv2", "addressdetails": 1},
                nominatim=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("Geocodificação reversa indisponível (%s)", type(exc).__name__)
            return None
        address = (data or {}).get("address") or {}
        city = _city_from_address(address)
        if not city:
            return self._remember(key, None)
        return self._remember(
            key,
            GeoResolution(
                city=city,
                state=_uf(address.get("state")),
                neighborhood=_neighborhood_from_address(address),
                source="reverse",
            ),
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()


__all__ = [
    "GeoLookup",
    "GeoResolution",
    "OpenGeoLookup",
    "extract_cep",
    "extract_coordinates",
    "resolve_neighborhood",
]

