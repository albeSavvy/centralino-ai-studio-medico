"""Contract test - sostituibilita CalendarProvider (Task 8.1).

La STESSA suite gira su [FakeCalendarProvider, GoogleCalendarProvider(mock rete)]
e verifica lo stesso comportamento osservabile del contratto (design.md -
Interfacce chiave), dimostrando la sostituibilita richiesta dal Requirement 7,
criterio 4: create_event restituisce un id ed e idempotente su appointment_id;
list_free_slots restituisce una lista di FreeBusySlot.

Costo zero: il GoogleCalendarProvider e costruito con un service Google MOCK
in-memory (nessuna rete), un secret_loader fittizio (nessuna chiamata a Secrets
Manager) e uno sleep spy iniettato (nessuna attesa reale). Nessun valore di
segreto compare nei dati di test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.calendar_provider import (
    CalendarEvent,
    CalendarProvider,
    FakeCalendarProvider,
    FreeBusySlot,
)
from src.google_calendar_provider import GoogleCalendarProvider, RetryConfig
from tests.support.fake_google_service import FakeGoogleCalendarService

# Timezone-aware, offset Europe/Rome.
_TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 1, 20, 9, 0, tzinfo=_TZ)

DOCTORS = ["Chiara", "Francesca"]
CALENDAR_IDS = {"Chiara": "cal-chiara@example.com", "Francesca": "cal-francesca@example.com"}


def _fake_secret_loader() -> str:
    """Loader fittizio: nessun segreto reale, valore non sensibile e non loggato."""
    return "TEST-SERVICE-ACCOUNT-KEY-PLACEHOLDER"


def _make_google_provider() -> tuple[GoogleCalendarProvider, list[float], FakeGoogleCalendarService]:
    """GoogleCalendarProvider con service mock, secret fake e sleep spy iniettato."""
    sleep_calls: list[float] = []
    service = FakeGoogleCalendarService(calendar_ids=CALENDAR_IDS)
    provider = GoogleCalendarProvider(
        service=service,
        secret_loader=_fake_secret_loader,
        calendar_ids=CALENDAR_IDS,
        retry=RetryConfig(max_attempts=3, min_interval_sec=2.0),
        sleep=sleep_calls.append,
    )
    return provider, sleep_calls, service


def _make_fake_provider() -> FakeCalendarProvider:
    return FakeCalendarProvider()


# Ogni caso e (label, factory-che-ritorna-il-provider). Per il Fake precarichiamo
# gli slot liberi; per Google li carichiamo nel service mock.
def _fake_case() -> CalendarProvider:
    provider = _make_fake_provider()
    provider.set_free_slots(
        [
            FreeBusySlot(doctor="Chiara", start=NOW, end=NOW + timedelta(minutes=30)),
            FreeBusySlot(
                doctor="Francesca",
                start=NOW + timedelta(hours=1),
                end=NOW + timedelta(hours=2),
            ),
        ]
    )
    return provider


def _google_case() -> CalendarProvider:
    provider, _, service = _make_google_provider()
    service.add_free_period("Chiara", NOW, NOW + timedelta(minutes=30))
    service.add_free_period("Francesca", NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    return provider


PROVIDER_CASES = [
    pytest.param(_fake_case, id="fake"),
    pytest.param(_google_case, id="google_mock"),
]


@pytest.mark.parametrize("provider_factory", PROVIDER_CASES)
def test_create_event_returns_id(provider_factory) -> None:
    provider = provider_factory()
    event = CalendarEvent(
        appointment_id="a1b2c3",
        doctor="Chiara",
        patient_name="Mario Rossi",
        start=NOW,
        duration_min=30,
    )
    event_id = provider.create_event(event)
    assert isinstance(event_id, str)
    assert event_id


@pytest.mark.parametrize("provider_factory", PROVIDER_CASES)
def test_create_event_idempotent_on_appointment_id(provider_factory) -> None:
    provider = provider_factory()
    event = CalendarEvent(
        appointment_id="dup-001",
        doctor="Francesca",
        patient_name="Anna Bianchi",
        start=NOW,
        duration_min=30,
    )
    first = provider.create_event(event)
    second = provider.create_event(event)
    third = provider.create_event(event)
    # Stesso id su chiamate ripetute: nessun duplicato osservabile.
    assert first == second == third


@pytest.mark.parametrize("provider_factory", PROVIDER_CASES)
def test_list_free_slots_returns_freebusy_slots(provider_factory) -> None:
    provider = provider_factory()
    slots = provider.list_free_slots(
        doctors=DOCTORS, location="Meda", window_days=14, slot_min=30
    )
    assert isinstance(slots, list)
    assert all(isinstance(s, FreeBusySlot) for s in slots)
    assert all(s.doctor in DOCTORS for s in slots)
