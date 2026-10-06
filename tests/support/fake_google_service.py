"""Fake del service Google Calendar per i test (nessuna rete).

Emula la forma dell'API usata da GoogleCalendarProvider:
  service.freebusy().query(body=...).execute() -> dict
  service.events().get(calendarId=..., eventId=...).execute() -> dict
  service.events().insert(calendarId=..., body=...).execute() -> dict

Il fake registra ogni insert (per verificare l'assenza di duplicati - Property
20) e supporta due modalita di errore per esercitare i retry (Property 21):
  - transient_failures: numero di primi insert che falliscono in modo transitorio
    prima di riuscire (retry poi successo).
  - always_fail: ogni insert fallisce in modo transitorio (fallimento persistente).

Nessuna dipendenza AWS/Google/rete: puro in-memory, costo zero.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.google_calendar_provider import EventNotFoundError, TransientCalendarError


class FakeGoogleCalendarService:
    """Service Google Calendar-like in-memory con controllo dei fallimenti.

    Args:
        calendar_ids: mapping dottoressa -> calendarId (per il freebusy).
        transient_failures: numero di insert iniziali che falliscono (transitori).
        always_fail: se True ogni insert fallisce in modo transitorio.
    """

    def __init__(
        self,
        calendar_ids: dict[str, str] | None = None,
        transient_failures: int = 0,
        always_fail: bool = False,
    ) -> None:
        self._calendar_ids = dict(calendar_ids or {})
        self._transient_remaining = transient_failures
        self._always_fail = always_fail
        # Store degli eventi: (calendarId, eventId) -> body. Registra i duplicati.
        self._events: dict[tuple[str, str], dict[str, Any]] = {}
        self.insert_attempts = 0
        self.successful_inserts: list[dict[str, Any]] = []
        self._free_periods: dict[str, list[tuple[datetime, datetime]]] = {}

    # --- Setup per i test freebusy ----------------------------------------

    def add_free_period(self, doctor: str, start: datetime, end: datetime) -> None:
        """Precarica un intervallo libero per una dottoressa."""
        cal_id = self._calendar_ids[doctor]
        self._free_periods.setdefault(cal_id, []).append((start, end))

    @property
    def stored_event_count(self) -> int:
        """Numero di eventi effettivamente memorizzati (chiavi distinte)."""
        return len(self._events)

    # --- Superficie API emulata -------------------------------------------

    def freebusy(self) -> "_FreeBusyEndpoint":
        return _FreeBusyEndpoint(self)

    def events(self) -> "_EventsEndpoint":
        return _EventsEndpoint(self)

    # --- Logica interna ---------------------------------------------------

    def _query_freebusy(self, body: dict[str, Any]) -> dict[str, Any]:
        calendars: dict[str, Any] = {}
        for item in body.get("items", []):
            cal_id = item["id"]
            periods = self._free_periods.get(cal_id, [])
            calendars[cal_id] = {
                "free": [
                    {"start": s.isoformat(), "end": e.isoformat()} for s, e in periods
                ]
            }
        return {"calendars": calendars}

    def _get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        key = (calendar_id, event_id)
        if key not in self._events:
            raise EventNotFoundError(event_id)
        return self._events[key]

    def _insert_event(self, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.insert_attempts += 1
        if self._always_fail:
            raise TransientCalendarError("insert fallito (always_fail)")
        if self._transient_remaining > 0:
            self._transient_remaining -= 1
            raise TransientCalendarError("insert fallito (transitorio)")
        event_id = body["id"]
        key = (calendar_id, event_id)
        self._events[key] = dict(body)
        self.successful_inserts.append(dict(body))
        return {"id": event_id}


class _FreeBusyEndpoint:
    def __init__(self, service: FakeGoogleCalendarService) -> None:
        self._service = service

    def query(self, body: dict[str, Any]) -> "_Executable":
        return _Executable(lambda: self._service._query_freebusy(body))


class _EventsEndpoint:
    def __init__(self, service: FakeGoogleCalendarService) -> None:
        self._service = service

    def get(self, calendarId: str, eventId: str) -> "_Executable":  # noqa: N803 - API Google
        return _Executable(lambda: self._service._get_event(calendarId, eventId))

    def insert(self, calendarId: str, body: dict[str, Any]) -> "_Executable":  # noqa: N803
        return _Executable(lambda: self._service._insert_event(calendarId, body))


class _Executable:
    """Wrapper che imita il pattern .execute() del client Google."""

    def __init__(self, action) -> None:
        self._action = action

    def execute(self) -> Any:
        return self._action()
