"""Astrazione Calendar_Provider + FakeCalendarProvider in-memory.

Centralino AI Studio Medico / Idelia (Flusso A) - Task 6.

Questo modulo definisce l'accesso al calendario dietro un'unica interfaccia
astratta (ABC CalendarProvider) cosi il sink e sostituibile senza toccare il
Motore_Conversazionale ne l'Assegnatore_Colloquio (Requirement 7, criteri 2 e 4).

  - CalendarProvider: contratto astratto (list_free_slots, create_event).
  - FakeCalendarProvider: backend puro in-memory per i test (nessuna rete,
    nessun costo). Consente di precaricare gli slot liberi e registra gli eventi
    creati, cosi Assegnatore (Task 7) e i contract/property test (Task 8.1)
    possono pilotarlo in modo deterministico.

La concreta GoogleCalendarProvider (idempotenza via appointment_id, retry) e una
task separata (Task 8) e NON e implementata qui.

Semantica del contratto (design.md - Interfacce chiave):
  - list_free_slots(doctors, location, window_days, slot_min) -> lista di
    FreeBusySlot per le dottoresse indicate entro la finestra.
  - create_event(event) -> eventId (str). Idempotente rispetto ad appointment_id:
    ripetute chiamate con lo stesso appointment_id NON creano duplicati e
    restituiscono lo stesso eventId (Requirement 7, criterio 5). Il fake rispetta
    questa proprieta gia in-memory, cosi i contract test (Task 8.1) valgono su
    entrambe le implementazioni.

Costo zero: modulo puro Python, nessuna chiamata AWS/Google/rete.

Riferimento: design.md sezione "Interfacce chiave" (Calendar_Provider astratto +
GoogleCalendarProvider). Le firme e i campi delle dataclass sono allineati 1:1 al
design.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


# ---------------------------------------------------------------------------
# Dataclass del contratto (design.md - Interfacce chiave)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FreeBusySlot:
    """Slot libero di una dottoressa (finestra freebusy).

    Immutabile (frozen) cosi puo entrare in set/dict e restare confrontabile in
    modo deterministico nei test. start/end sono datetime con timezone (offset
    Europe/Rome nel dominio reale).
    """

    doctor: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class CalendarEvent:
    """Evento di calendario da creare per un appuntamento confermato.

    appointment_id e la chiave di idempotenza (Requirement 7, criterio 5).
    patient_name contiene nome e cognome del paziente (Property 19).
    """

    appointment_id: str
    doctor: str
    patient_name: str
    start: datetime
    duration_min: int


# ---------------------------------------------------------------------------
# Interfaccia astratta
# ---------------------------------------------------------------------------

class CalendarProvider(ABC):
    """Contratto astratto per l'accesso al calendario.

    Qualunque backend (Fake in-memory, Google) implementa queste due operazioni
    con la stessa semantica, garantendo la sostituibilita richiesta dal
    Requirement 7 (criteri 2, 3 e 4). Il codice a valle (Assegnatore, Step
    Functions) dipende solo da questa interfaccia.
    """

    @abstractmethod
    def list_free_slots(
        self,
        doctors: list[str],
        location: str,
        window_days: int,
        slot_min: int,
    ) -> list[FreeBusySlot]:
        """Restituisce gli slot liberi per le dottoresse indicate.

        Args:
            doctors: Nomi delle dottoresse di cui leggere la disponibilita.
            location: Sede richiesta (es. "Meda").
            window_days: Ampiezza della finestra di ricerca in giorni.
            slot_min: Durata minima di uno slot utile in minuti.

        Returns:
            Lista di FreeBusySlot liberi entro la finestra. Vuota se nessuna
            disponibilita.
        """

    @abstractmethod
    def create_event(self, event: CalendarEvent) -> str:
        """Crea l'evento di calendario in modo idempotente.

        Ripetute chiamate con lo stesso event.appointment_id NON creano duplicati
        e restituiscono lo stesso eventId (Requirement 7, criterio 5).

        Args:
            event: Dati dell'evento da creare.

        Returns:
            eventId dell'evento creato (o esistente, in caso di retry idempotente).
        """


# ---------------------------------------------------------------------------
# Backend in-memory (test) - nessuna rete, nessun costo
# ---------------------------------------------------------------------------

class FakeCalendarProvider(CalendarProvider):
    """Backend puro in-memory. Nessuna dipendenza esterna, adatto ai test.

    Consente di precaricare gli slot liberi (add_free_slot / set_free_slots) e
    registra gli eventi creati (created_events), cosi i test possono pilotare
    Assegnatore (Task 7) e i contract/property test (Task 8.1) in modo
    deterministico.

    Idempotenza: create_event deriva l'eventId in modo deterministico da
    appointment_id e mantiene una mappa appointment_id -> eventId, cosi ripetute
    chiamate con lo stesso appointment_id restituiscono lo stesso eventId senza
    creare duplicati (Requirement 7, criterio 5 - stessa proprieta che
    GoogleCalendarProvider dovra garantire).
    """

    def __init__(self) -> None:
        self._free_slots: list[FreeBusySlot] = []
        self._events_by_appointment: dict[str, CalendarEvent] = {}
        self._event_id_by_appointment: dict[str, str] = {}

    # --- Setup della disponibilita (solo per i test) ----------------------

    def add_free_slot(self, slot: FreeBusySlot) -> None:
        """Precarica un singolo slot libero."""
        self._free_slots.append(slot)

    def set_free_slots(self, slots: list[FreeBusySlot]) -> None:
        """Sostituisce interamente la disponibilita precaricata."""
        self._free_slots = list(slots)

    @property
    def created_events(self) -> list[CalendarEvent]:
        """Eventi creati finora (uno per appointment_id distinto)."""
        return list(self._events_by_appointment.values())

    # --- Contratto CalendarProvider ---------------------------------------

    def list_free_slots(
        self,
        doctors: list[str],
        location: str,
        window_days: int,
        slot_min: int,
    ) -> list[FreeBusySlot]:
        """Filtra gli slot precaricati per dottoressa e durata minima.

        La selezione dello slot (primo libero, tie-break, sede) e responsabilita
        dell'Assegnatore_Colloquio (Task 7): qui si applica solo il filtro
        freebusy grezzo (dottoressa richiesta e durata sufficiente).
        """
        wanted = set(doctors)
        return [
            slot
            for slot in self._free_slots
            if slot.doctor in wanted
            and self._duration_min(slot) >= slot_min
        ]

    def create_event(self, event: CalendarEvent) -> str:
        """Registra l'evento in-memory in modo idempotente sull'appointment_id."""
        existing_id = self._event_id_by_appointment.get(event.appointment_id)
        if existing_id is not None:
            return existing_id
        event_id = self._derive_event_id(event.appointment_id)
        self._events_by_appointment[event.appointment_id] = event
        self._event_id_by_appointment[event.appointment_id] = event_id
        return event_id

    # --- Helper puri ------------------------------------------------------

    @staticmethod
    def _duration_min(slot: FreeBusySlot) -> float:
        """Durata dello slot in minuti (end - start)."""
        return (slot.end - slot.start).total_seconds() / 60.0

    @staticmethod
    def _derive_event_id(appointment_id: str) -> str:
        """Deriva un eventId deterministico dall'appointment_id.

        Mirror della strategia di idempotenza di GoogleCalendarProvider (eventId
        stabile derivato dall'appointment_id), cosi il contract test (Task 8.1)
        vede lo stesso comportamento su entrambe le implementazioni.
        """
        return f"evt-{appointment_id}"
