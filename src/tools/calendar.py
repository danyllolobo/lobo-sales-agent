"""Ferramentas determinísticas de agenda: disponibilidade e criação de agendamentos.

- Horários calculados a partir do expediente do tenant (fuso do tenant), respeitando
  antecedência mínima, horizonte máximo, datas bloqueadas e capacidade por serviço.
- `create_booking` revalida o horário e grava de forma atômica (sem overbooking).
- Sempre devolve 2 sugestões concretas (regra do nó de agendamento).

O repositório padrão é em memória; integrações reais (Google Calendar, Cal.com,
calendários do GHL) implementam o mesmo protocolo `BookingRepository`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.core.exceptions import LoboError
from src.core.schemas import TenantId, mask_phone, normalize_phone, utcnow
from src.tools.results import ToolResult, ToolStatus, tool_guard
from src.tools.tenant_data import CalendarConfig, ServiceConfig, TenantData, TenantDataStore

MAX_RANGE_DAYS = 14
_WEEKDAYS_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


# --------------------------------------------------------------------------- #
# Contratos (ARCHITECTURE.md §5) — tenant_id/lead_phone injetados pelo orquestrador
# --------------------------------------------------------------------------- #
class AvailabilityInput(BaseModel):
    tenant_id: TenantId
    service_id: str
    start_date: str
    end_date: str


class BookingInput(BaseModel):
    tenant_id: TenantId
    lead_phone: str
    lead_name: str
    service_id: str
    slot_datetime: str


# --------------------------------------------------------------------------- #
# Domínio
# --------------------------------------------------------------------------- #
class SlotUnavailableError(LoboError):
    """Horário ocupado/inválido — fluxo normal de negócio (não exige humano)."""

    default_message = "Horário indisponível"


class Booking(BaseModel):
    model_config = ConfigDict(frozen=True)

    booking_id: str
    tenant_id: TenantId
    service_id: str
    lead_phone: str = Field(repr=False)
    lead_name: str | None = None
    start: datetime
    end: datetime
    created_at: datetime = Field(default_factory=utcnow)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end


@dataclass(frozen=True, slots=True)
class TimeSlot:
    start: datetime
    end: datetime

    def to_payload(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": slot_label(self.start)}


def slot_label(dt: datetime) -> str:
    """`terça-feira, 11/11 às 14:00` (sem depender de locale do SO)."""
    return f"{_WEEKDAYS_PT[dt.weekday()]}, {dt:%d/%m} às {dt:%H:%M}"


class BookingRepository(Protocol):
    async def list_bookings(self, tenant_id: str, start: datetime, end: datetime) -> list[Booking]: ...

    async def add_booking(self, booking: Booking, capacity: int) -> Booking:
        """Grava atomicamente; levanta `SlotUnavailableError` se a capacidade estourar."""
        ...


class InMemoryBookingRepository:
    """Repositório em memória com trava por tenant (dev/testes; não persiste entre reinícios)."""

    def __init__(self) -> None:
        self._bookings: dict[str, list[Booking]] = defaultdict(list)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def list_bookings(self, tenant_id: str, start: datetime, end: datetime) -> list[Booking]:
        return [b for b in self._bookings[tenant_id] if b.overlaps(start, end)]

    async def add_booking(self, booking: Booking, capacity: int) -> Booking:
        async with self._locks[booking.tenant_id]:
            taken = sum(
                1
                for b in self._bookings[booking.tenant_id]
                if b.service_id == booking.service_id and b.overlaps(booking.start, booking.end)
            )
            if taken >= capacity:
                raise SlotUnavailableError(tenant_id=booking.tenant_id)
            self._bookings[booking.tenant_id].append(booking)
            return booking


# --------------------------------------------------------------------------- #
# Serviço
# --------------------------------------------------------------------------- #
def _parse_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value.strip()[:10])


class CalendarService:
    def __init__(
        self,
        store: TenantDataStore,
        bookings: BookingRepository | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._bookings = bookings or InMemoryBookingRepository()
        self._clock = clock

    # -------------------------------------------------------------- consulta
    @tool_guard("check_available_slots")
    async def check_available_slots(
        self,
        tenant_id: str,
        date: date | str,
        *,
        service_id: str | None = None,
        end_date: date | str | None = None,
        limit: int = 12,
    ) -> ToolResult:
        tool = "check_available_slots"
        data = self._store.get(tenant_id)
        resolved = self._resolve(tool, data, service_id)
        if isinstance(resolved, ToolResult):
            return resolved
        calendar, service = resolved

        try:
            start_day = _parse_date(date)
            end_day = _parse_date(end_date) if end_date else start_day
        except ValueError:
            return ToolResult.invalid(tool, tenant_id, "datas devem estar no formato AAAA-MM-DD")
        if end_day < start_day:
            return ToolResult.invalid(tool, tenant_id, "end_date anterior a start_date")
        end_day = min(end_day, start_day + timedelta(days=MAX_RANGE_DAYS - 1))

        slots = await self._compute(data, calendar, service, start_day, end_day)
        searched_forward = False
        if not slots:  # procura os próximos dias livres dentro do horizonte
            searched_forward = True
            nxt = end_day + timedelta(days=1)
            slots = await self._compute(data, calendar, service, nxt, nxt + timedelta(days=MAX_RANGE_DAYS - 1))

        if not slots:
            return ToolResult.needs_human(
                tool,
                tenant_id,
                ToolStatus.NOT_FOUND,
                "no_slots_in_horizon",
                instruction="Não há horários livres na agenda. NÃO invente horários: acione o handoff humano.",
            )
        suggestions = pick_two(slots)
        return ToolResult.success(
            tool,
            tenant_id,
            {
                "service": {"service_id": service.service_id, "name": service.name, "duration_minutes": service.duration_minutes},
                "timezone": data.tenant.timezone,
                "requested_range": {"start": start_day.isoformat(), "end": end_day.isoformat()},
                "no_slots_in_requested_range": searched_forward,
                "suggestions": [s.to_payload() for s in suggestions],
                "slots": [s.to_payload() for s in slots[:limit]],
            },
            instruction=(
                "Ofereça ao cliente exatamente as 2 opções de 'suggestions' (use o 'label'). "
                "Só confirme o agendamento depois que create_booking retornar status ok."
                + (" Não havia horários na data pedida: explique e ofereça estas próximas opções." if searched_forward else "")
            ),
        )

    # -------------------------------------------------------------- agendamento
    @tool_guard("create_booking")
    async def create_booking(
        self,
        tenant_id: str,
        phone: str,
        slot: datetime | str,
        *,
        service_id: str | None = None,
        lead_name: str | None = None,
    ) -> ToolResult:
        tool = "create_booking"
        data = self._store.get(tenant_id)
        resolved = self._resolve(tool, data, service_id)
        if isinstance(resolved, ToolResult):
            return resolved
        calendar, service = resolved

        try:
            lead_phone = normalize_phone(phone)
            start = datetime.fromisoformat(slot.strip()) if isinstance(slot, str) else slot
        except ValueError as exc:
            return ToolResult.invalid(tool, tenant_id, f"dados inválidos: {exc}")
        tz = data.tenant.tz
        start = start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)

        day_slots = await self._compute(data, calendar, service, start.date(), start.date())
        if not any(s.start == start for s in day_slots):
            return self._unavailable(tool, tenant_id, day_slots)

        booking = Booking(
            booking_id=f"bk_{uuid.uuid4().hex[:12]}",
            tenant_id=data.tenant_id,
            service_id=service.service_id,
            lead_phone=lead_phone,
            lead_name=(lead_name or "").strip() or None,
            start=start,
            end=start + timedelta(minutes=service.duration_minutes),
        )
        try:
            await self._bookings.add_booking(booking, service.capacity)
        except SlotUnavailableError:
            day_slots = await self._compute(data, calendar, service, start.date(), start.date())
            return self._unavailable(tool, tenant_id, day_slots)

        return ToolResult.success(
            tool,
            tenant_id,
            {
                "booking_id": booking.booking_id,
                "service": service.name,
                "start": booking.start.isoformat(),
                "end": booking.end.isoformat(),
                "label": slot_label(booking.start),
                "lead": mask_phone(lead_phone),
            },
            instruction="Confirme ao cliente o agendamento usando EXATAMENTE o 'label' e o nome do serviço.",
        )

    # -------------------------------------------------------------- helpers
    def _resolve(self, tool: str, data: TenantData, service_id: str | None) -> tuple[CalendarConfig, ServiceConfig] | ToolResult:
        calendar = data.calendar
        if calendar is None:
            return ToolResult.needs_human(tool, data.tenant_id, ToolStatus.NOT_FOUND, "calendar_not_configured")
        service = calendar.service(service_id)
        if service is None:
            return ToolResult(
                tool=tool,
                tenant_id=data.tenant_id,
                status=ToolStatus.INVALID_INPUT,
                reason="service_id ausente ou desconhecido",
                data={"available_services": [{"service_id": s.service_id, "name": s.name} for s in calendar.services]},
                agent_instruction="Escolha um service_id de 'available_services' (pergunte ao cliente se necessário).",
            )
        return calendar, service

    def _unavailable(self, tool: str, tenant_id: str, day_slots: list[TimeSlot]) -> ToolResult:
        return ToolResult(
            tool=tool,
            tenant_id=tenant_id,
            status=ToolStatus.UNAVAILABLE,
            reason="slot_unavailable",
            data={"alternatives": [s.to_payload() for s in pick_two(day_slots)]},
            agent_instruction=(
                "Esse horário não está disponível. Informe o cliente e ofereça as alternativas "
                "(ou consulte check_available_slots para outra data)."
            ),
        )

    async def _compute(
        self, data: TenantData, calendar: CalendarConfig, service: ServiceConfig, start_day: date, end_day: date
    ) -> list[TimeSlot]:
        tz = data.tenant.tz
        now = self._clock().astimezone(tz)
        earliest = now + timedelta(minutes=calendar.min_notice_minutes)
        last_day = now.date() + timedelta(days=calendar.horizon_days)
        duration = timedelta(minutes=service.duration_minutes)
        step = timedelta(minutes=calendar.slot_step_minutes)

        range_start = datetime.combine(start_day, datetime.min.time(), tz)
        range_end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tz)
        existing = [
            b
            for b in await self._bookings.list_bookings(data.tenant_id, range_start, range_end)
            if b.service_id == service.service_id
        ]

        slots: list[TimeSlot] = []
        day = max(start_day, now.date())
        while day <= min(end_day, last_day):
            if day not in calendar.blackout_dates:
                for win_start, win_end in calendar.windows(day):
                    cur = datetime.combine(day, win_start, tz)
                    limit = datetime.combine(day, win_end, tz)
                    while cur + duration <= limit:
                        end = cur + duration
                        if cur >= earliest and sum(b.overlaps(cur, end) for b in existing) < service.capacity:
                            slots.append(TimeSlot(cur, end))
                        cur += step
            day += timedelta(days=1)
        return slots


def pick_two(slots: list[TimeSlot]) -> list[TimeSlot]:
    """1º horário livre + uma alternativa "diferente" (outro dia, ou ≥3h depois)."""
    if len(slots) <= 1:
        return list(slots)
    first = slots[0]
    for candidate in slots[1:]:
        if candidate.start.date() != first.start.date():
            return [first, candidate]
    for candidate in slots[1:]:
        if candidate.start - first.start >= timedelta(hours=3):
            return [first, candidate]
    return [first, slots[1]]


__all__: list[Any] = [
    "AvailabilityInput",
    "Booking",
    "BookingInput",
    "BookingRepository",
    "CalendarService",
    "InMemoryBookingRepository",
    "SlotUnavailableError",
    "TimeSlot",
    "pick_two",
    "slot_label",
]

