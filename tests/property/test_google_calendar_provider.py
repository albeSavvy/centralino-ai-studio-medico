"""Property test - scrittura calendario GoogleCalendarProvider (Task 8.1).

Mappatura 1:1 con le proprieta del design (design.md - Correctness Properties):
  - Property 19: l'evento creato contiene nome/cognome, dottoressa, start, durata.
  - Property 20: N chiamate con lo stesso appointment_id -> UN SOLO evento e lo
                 stesso eventId restituito.
  - Property 21: retry <= 3; su fallimento persistente lo stato mappa a
                 SCRITTURA_CALENDARIO_FALLITA; intervallo di retry >= 2s.

Libreria: Hypothesis, >= 100 iterazioni per proprieta (qui 200). Backend Google
MOCK in-memory + secret_loader fittizio + sleep spy iniettato: nessuna chiamata
AWS/Google/rete, nessuna attesa reale, costo zero. Nessun valore di segreto
compare nei dati di test. I commenti usano ASCII.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.calendar_provider import CalendarEvent
from src.google_calendar_provider import (
    DEFAULT_MIN_INTERVAL_SEC,
    CalendarWriteError,
    GoogleCalendarProvider,
    RetryConfig,
    derive_event_id,
)
from src.models import ApptStatus
from tests.support.fake_google_service import FakeGoogleCalendarService

_TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 1, 20, 9, 0, tzinfo=_TZ)

DOCTORS = ["Chiara", "Francesca"]
CALENDAR_IDS = {
    "Chiara": "cal-chiara@example.com",
    "Francesca": "cal-francesca@example.com",
}


def _fake_secret_loader() -> str:
    """Loader fittizio: valore non sensibile, mai loggato ne incluso negli eventi."""
    return "TEST-SERVICE-ACCOUNT-KEY-PLACEHOLDER"


def _make_provider(
    service: FakeGoogleCalendarService,
    sleep_calls: list[float],
    max_attempts: int = 3,
) -> GoogleCalendarProvider:
    return GoogleCalendarProvider(
        service=service,
        secret_loader=_fake_secret_loader,
        calendar_ids=CALENDAR_IDS,
        retry=RetryConfig(max_attempts=max_attempts, min_interval_sec=DEFAULT_MIN_INTERVAL_SEC),
        sleep=sleep_calls.append,
    )


# Generatori del dominio: nomi paziente non vuoti, dottoressa ammessa, durata,
# appointment_id non vuoto.
_patient_name = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=122),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")
_doctor = st.sampled_from(DOCTORS)
_duration = st.integers(min_value=15, max_value=120)
_appointment_id = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")
_offset_min = st.integers(min_value=0, max_value=14 * 24 * 60)


# ---------------------------------------------------------------------------
# Property 19: il corpo dell'evento contiene i dati richiesti
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 19: per qualunque appuntamento confermato, l'evento creato sul calendario contiene nome e cognome del paziente, la dottoressa, l'orario di inizio e la durata
@settings(max_examples=200)
@given(
    appointment_id=_appointment_id,
    doctor=_doctor,
    patient_name=_patient_name,
    offset=_offset_min,
    duration=_duration,
)
def test_property19_event_body_contains_required_fields(
    appointment_id: str,
    doctor: str,
    patient_name: str,
    offset: int,
    duration: int,
) -> None:
    service = FakeGoogleCalendarService(calendar_ids=CALENDAR_IDS)
    provider = _make_provider(service, sleep_calls=[])
    start = NOW + timedelta(minutes=offset)
    event = CalendarEvent(
        appointment_id=appointment_id,
        doctor=doctor,
        patient_name=patient_name,
        start=start,
        duration_min=duration,
    )

    event_id = provider.create_event(event)

    # Recupera il corpo effettivamente inserito nel backend mock.
    assert len(service.successful_inserts) == 1
    body = service.successful_inserts[0]

    # Nome/cognome del paziente presenti (summary + extendedProperties).
    assert patient_name in body["summary"]
    assert body["extendedProperties"]["private"]["patientName"] == patient_name
    # Dottoressa presente.
    assert body["extendedProperties"]["private"]["doctor"] == doctor
    # Orario di inizio presente e coerente.
    assert body["start"]["dateTime"] == start.isoformat()
    # Durata presente e coerente (start + duration == end).
    expected_end = start + timedelta(minutes=duration)
    assert body["end"]["dateTime"] == expected_end.isoformat()
    assert body["extendedProperties"]["private"]["durationMin"] == str(duration)
    # eventId deterministico dall'appointment_id.
    assert event_id == derive_event_id(appointment_id)


# ---------------------------------------------------------------------------
# Property 20: idempotenza sull'appointment_id (N chiamate -> 1 evento)
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 20: per qualunque numero N >= 1 di chiamate di scrittura con lo stesso identificatore di idempotenza (appointment_id), viene creato al piu un evento sul calendario e tutte le chiamate restituiscono lo stesso eventId
@settings(max_examples=200)
@given(
    appointment_id=_appointment_id,
    doctor=_doctor,
    patient_name=_patient_name,
    offset=_offset_min,
    duration=_duration,
    n_calls=st.integers(min_value=1, max_value=6),
)
def test_property20_idempotent_single_event(
    appointment_id: str,
    doctor: str,
    patient_name: str,
    offset: int,
    duration: int,
    n_calls: int,
) -> None:
    service = FakeGoogleCalendarService(calendar_ids=CALENDAR_IDS)
    provider = _make_provider(service, sleep_calls=[])
    event = CalendarEvent(
        appointment_id=appointment_id,
        doctor=doctor,
        patient_name=patient_name,
        start=NOW + timedelta(minutes=offset),
        duration_min=duration,
    )

    returned_ids = {provider.create_event(event) for _ in range(n_calls)}

    # Tutte le chiamate restituiscono lo stesso eventId.
    assert len(returned_ids) == 1
    # Al piu un evento memorizzato nel backend (esattamente uno qui, N >= 1).
    assert service.stored_event_count == 1
    # Un solo insert effettivo e andato a buon fine (nessun duplicato).
    assert len(service.successful_inserts) == 1


# ---------------------------------------------------------------------------
# Property 21: retry <= 3 e fallimento -> SCRITTURA_CALENDARIO_FALLITA
# ---------------------------------------------------------------------------

def _map_error_to_status(provider, event) -> ApptStatus:
    """Esegue create_event; su CalendarWriteError mappa lo stato di fallimento.

    Rappresenta la mappatura che il chiamante (Step Functions, Task 11) applica:
    errore persistente di scrittura -> ApptStatus.SCRITTURA_CALENDARIO_FALLITA.
    """
    try:
        provider.create_event(event)
        return ApptStatus.CONFERMATO
    except CalendarWriteError:
        return ApptStatus.SCRITTURA_CALENDARIO_FALLITA


# Feature: centralino-ai-studio-medico, Property 21: per qualunque scrittura calendario che fallisce, il numero di tentativi non supera 3 con intervallo minimo di 2s tra i tentativi; al fallimento persistente lo stato appuntamento diventa "scrittura calendario fallita"
@settings(max_examples=200)
@given(
    appointment_id=_appointment_id,
    doctor=_doctor,
    patient_name=_patient_name,
    offset=_offset_min,
    duration=_duration,
    transient=st.integers(min_value=0, max_value=5),
)
def test_property21_retry_bounded_and_failure_maps_state(
    appointment_id: str,
    doctor: str,
    patient_name: str,
    offset: int,
    duration: int,
    transient: int,
) -> None:
    max_attempts = 3
    sleep_calls: list[float] = []
    service = FakeGoogleCalendarService(
        calendar_ids=CALENDAR_IDS, transient_failures=transient
    )
    provider = _make_provider(service, sleep_calls=sleep_calls, max_attempts=max_attempts)
    event = CalendarEvent(
        appointment_id=appointment_id,
        doctor=doctor,
        patient_name=patient_name,
        start=NOW + timedelta(minutes=offset),
        duration_min=duration,
    )

    status = _map_error_to_status(provider, event)

    # Tentativi di insert sempre <= 3 (mai piu del massimo consentito).
    assert service.insert_attempts <= max_attempts
    # Ogni sleep di retry richiesto e >= 2s (Requirement 7, criterio 6).
    assert all(interval >= DEFAULT_MIN_INTERVAL_SEC for interval in sleep_calls)
    # Il numero di attese e pari ai tentativi oltre il primo.
    assert len(sleep_calls) == max(service.insert_attempts - 1, 0)

    if transient < max_attempts:
        # I fallimenti transitori si esauriscono entro il budget: successo.
        assert status is ApptStatus.CONFERMATO
        assert service.stored_event_count == 1
    else:
        # Fallimento persistente: stato mappato a SCRITTURA_CALENDARIO_FALLITA.
        assert status is ApptStatus.SCRITTURA_CALENDARIO_FALLITA
        assert service.insert_attempts == max_attempts
        assert service.stored_event_count == 0


# ---------------------------------------------------------------------------
# Caso limite esplicito: fallimento persistente (always_fail) -> errore dopo 3
# ---------------------------------------------------------------------------

def test_persistent_failure_raises_after_three_attempts() -> None:
    sleep_calls: list[float] = []
    service = FakeGoogleCalendarService(calendar_ids=CALENDAR_IDS, always_fail=True)
    provider = _make_provider(service, sleep_calls=sleep_calls, max_attempts=3)
    event = CalendarEvent(
        appointment_id="fail-001",
        doctor="Chiara",
        patient_name="Mario Rossi",
        start=NOW,
        duration_min=30,
    )

    with pytest.raises(CalendarWriteError):
        provider.create_event(event)

    assert service.insert_attempts == 3
    assert sleep_calls == [DEFAULT_MIN_INTERVAL_SEC, DEFAULT_MIN_INTERVAL_SEC]
    assert service.stored_event_count == 0
