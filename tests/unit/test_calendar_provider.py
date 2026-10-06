"""Unit test - astrazione Calendar_Provider e FakeCalendarProvider (Task 6).

Verifica che:
  - FakeCalendarProvider soddisfa l'ABC CalendarProvider (istanziabile, nessun
    metodo astratto residuo);
  - list_free_slots restituisce gli slot precaricati filtrati per dottoressa e
    durata minima;
  - create_event registra un evento e restituisce un eventId;
  - create_event e idempotente sull'appointment_id (nessun duplicato, stesso id).

Nessuna chiamata di rete/AWS/Google: tutto in-memory, costo zero. I contract e
property test (sostituibilita Fake vs Google, Property 19-21) sono in Task 8.1.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.calendar_provider import (
    CalendarEvent,
    CalendarProvider,
    FakeCalendarProvider,
    FreeBusySlot,
)

# Offset Europe/Rome (CET) usato nei test per start/end con timezone.
CET = timezone(timedelta(hours=1))


def _slot(doctor: str, hour: int, duration_min: int = 30) -> FreeBusySlot:
    start = datetime(2026, 1, 20, hour, 0, tzinfo=CET)
    return FreeBusySlot(
        doctor=doctor,
        start=start,
        end=start + timedelta(minutes=duration_min),
    )


def _event(appointment_id: str, doctor: str = "Chiara") -> CalendarEvent:
    return CalendarEvent(
        appointment_id=appointment_id,
        doctor=doctor,
        patient_name="Mario Rossi",
        start=datetime(2026, 1, 20, 9, 0, tzinfo=CET),
        duration_min=30,
    )


# ---------------------------------------------------------------------------
# ABC / sostituibilita
# ---------------------------------------------------------------------------

def test_fake_is_a_calendar_provider() -> None:
    provider = FakeCalendarProvider()
    assert isinstance(provider, CalendarProvider)


# ---------------------------------------------------------------------------
# list_free_slots
# ---------------------------------------------------------------------------

def test_list_free_slots_returns_preloaded_slots() -> None:
    provider = FakeCalendarProvider()
    chiara = _slot("Chiara", hour=9)
    francesca = _slot("Francesca", hour=10)
    provider.set_free_slots([chiara, francesca])

    result = provider.list_free_slots(
        doctors=["Chiara", "Francesca"],
        location="Meda",
        window_days=14,
        slot_min=30,
    )

    assert result == [chiara, francesca]


def test_list_free_slots_filters_by_doctor() -> None:
    provider = FakeCalendarProvider()
    chiara = _slot("Chiara", hour=9)
    provider.set_free_slots([chiara, _slot("Francesca", hour=10)])

    result = provider.list_free_slots(
        doctors=["Chiara"],
        location="Meda",
        window_days=14,
        slot_min=30,
    )

    assert result == [chiara]


def test_list_free_slots_filters_out_too_short_slots() -> None:
    provider = FakeCalendarProvider()
    provider.add_free_slot(_slot("Chiara", hour=9, duration_min=15))

    result = provider.list_free_slots(
        doctors=["Chiara"],
        location="Meda",
        window_days=14,
        slot_min=30,
    )

    assert result == []


def test_list_free_slots_empty_when_no_availability() -> None:
    provider = FakeCalendarProvider()

    result = provider.list_free_slots(
        doctors=["Chiara"],
        location="Meda",
        window_days=14,
        slot_min=30,
    )

    assert result == []


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------

def test_create_event_records_event_and_returns_id() -> None:
    provider = FakeCalendarProvider()
    event = _event("appt-1")

    event_id = provider.create_event(event)

    assert event_id
    assert provider.created_events == [event]


def test_create_event_is_idempotent_on_appointment_id() -> None:
    provider = FakeCalendarProvider()
    event = _event("appt-1")

    first_id = provider.create_event(event)
    second_id = provider.create_event(event)

    assert first_id == second_id
    assert provider.created_events == [event]


def test_create_event_distinct_appointments_create_distinct_events() -> None:
    provider = FakeCalendarProvider()

    id_a = provider.create_event(_event("appt-1"))
    id_b = provider.create_event(_event("appt-2"))

    assert id_a != id_b
    assert len(provider.created_events) == 2
