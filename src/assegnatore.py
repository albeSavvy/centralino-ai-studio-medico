"""Assegnatore_Colloquio: selezione slot + tie-break + registrazione provvisoria.

Centralino AI Studio Medico / Idelia (Flusso A) - Task 7.

Questo modulo contiene la logica pura di assegnazione del colloquio (Requirement
5). Su Esito_Appuntamento consulta il Calendar_Provider, individua il primo slot
libero da 30 minuti a Meda con Chiara o Francesca entro 14 giorni, applica le
regole di tie-break e registra l'appuntamento in Stato_Provvisorio; se nessuno
slot e disponibile restituisce un esito di indisponibilita senza effetti
collaterali.

Dipendenze iniettate (composition over inheritance):
  - repository (AnagraficaRepository): per persistere l'APPT in PROVVISORIO.
  - calendar (CalendarProvider): per leggere la disponibilita (freebusy).
  - now (datetime): istante di riferimento della finestra di 14 giorni; iniettato
    per rendere i test deterministici (nessuna chiamata a datetime.now() qui).
  - appointment_id_factory (Callable[[], str]): genera l'id dell'APPT; iniettabile
    cosi i test controllano l'id in modo deterministico.

Semantica (design.md - Assegnatore_Colloquio, Correctness Properties 13-16):
  - Selezione: primo slot valido con orario di inizio piu vicino a now.
    Uno slot e valido se libero, di durata >= 30 min, di Chiara o Francesca (le
    due dottoresse sono intercambiabili, Req 5.2), entro la finestra di 14 giorni.
  - Tie-break (Req 5.4): a parita di orario di inizio, la dottoressa il cui nome
    precede in ordine alfabetico. Deterministico e indipendente dall'ordine di
    input degli slot.
  - Indipendenza dal problema (Req 5.3, Property 14): la selezione NON legge ne
    ramifica sul campo "problema"; il problema entra solo nei dati del paziente
    persistiti (nome/telefono/ecc.), mai nel criterio di scelta.
  - Registrazione (Req 5.5): sullo slot scelto viene salvato un Appointment in
    stato PROVVISORIO (durata 30, sede Meda, dottoressa e start coerenti).
  - Indisponibilita (Req 5.6, Property 16): se nessuno slot valido, restituisce un
    esito unavailable SENZA salvare alcun APPT e senza toccare il calendario.

Costo zero: modulo puro Python, nessuna chiamata AWS/Google/rete. I test usano
InMemoryRepository + FakeCalendarProvider.

Riferimento: design.md sezione "Assegnatore_Colloquio". Le firme del
CalendarProvider e del repository sono riusate 1:1 (nessuna ridefinizione).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from src.calendar_provider import CalendarProvider, FreeBusySlot
from src.models import Appointment, ApptStatus
from src.repository import AnagraficaRepository

# Sede unica degli appuntamenti in questo scope (Requirement 5.1).
SEDE_MEDA = "Meda"

# Le due dottoresse, intercambiabili per specializzazione (Requirement 5.2).
DOCTORS = ["Chiara", "Francesca"]

# Durata fissa del colloquio in minuti (Requirement 5.1).
SLOT_MIN = 30

# Ampiezza della finestra di ricerca in giorni di calendario (Requirement 5.1).
WINDOW_DAYS = 14


@dataclass(frozen=True)
class AssignmentResult:
    """Esito dell'assegnazione del colloquio.

    Se available e True, appointment contiene l'APPT registrato in PROVVISORIO e
    slot lo slot scelto. Se available e False (Requirement 5.6, Property 16),
    appointment e slot sono None e nessun effetto collaterale e stato prodotto.
    """

    available: bool
    appointment: Appointment | None = None
    slot: FreeBusySlot | None = None


def _default_appointment_id_factory() -> str:
    """Genera un appointment_id univoco (usato se non iniettato).

    Delegato a uuid4 per l'uso reale; nei test si inietta una factory
    deterministica.
    """
    import uuid

    return f"appt-{uuid.uuid4().hex}"


class AssegnatoreColloquio:
    """Assegna il primo slot valido e registra l'APPT in PROVVISORIO (Req 5).

    La classe dipende solo dalle interfacce astratte (CalendarProvider,
    AnagraficaRepository), quindi e testabile con i backend in-memory a costo
    zero. now e appointment_id_factory sono iniettati per il determinismo.
    """

    def __init__(
        self,
        repository: AnagraficaRepository,
        calendar: CalendarProvider,
        now: datetime,
        appointment_id_factory: Callable[[], str] = _default_appointment_id_factory,
    ) -> None:
        self._repository = repository
        self._calendar = calendar
        self._now = now
        self._appointment_id_factory = appointment_id_factory

    def assign(self, client_no: str, patient_name: str) -> AssignmentResult:
        """Assegna un colloquio al paziente indicato.

        Nota: la firma NON accetta il campo "problema". La selezione dipende solo
        da client_no e patient_name (dati identificativi persistiti sull'APPT) e
        dalla disponibilita del calendario, mai dal problema (Requirement 5.3,
        Property 14).

        Args:
            client_no: Numero cliente del paziente gia registrato.
            patient_name: Nome e cognome del paziente (per l'APPT e il recap).

        Returns:
            AssignmentResult available=True con l'APPT PROVVISORIO se esiste uno
            slot valido, altrimenti available=False senza effetti collaterali.
        """
        slot = self._select_slot()
        if slot is None:
            # Requirement 5.6 / Property 16: nessun effetto collaterale.
            return AssignmentResult(available=False)

        appointment = Appointment(
            appointment_id=self._appointment_id_factory(),
            client_no=client_no,
            nome_paziente=patient_name,
            doctor=slot.doctor,
            sede=SEDE_MEDA,
            start=slot.start.isoformat(),
            duration_min=SLOT_MIN,
            status=ApptStatus.PROVVISORIO,
        )
        # Requirement 5.5: registrazione in Stato_Provvisorio.
        self._repository.save_appointment(appointment)
        return AssignmentResult(available=True, appointment=appointment, slot=slot)

    def _select_slot(self) -> FreeBusySlot | None:
        """Individua il primo slot valido applicando il tie-break (Req 5.1-5.4).

        Legge la disponibilita grezza dal CalendarProvider, filtra gli slot validi
        (durata >= 30, dottoressa ammessa, entro la finestra di 14 giorni) e
        seleziona quello con l'ordinamento deterministico (start piu vicino, poi
        nome dottoressa alfabetico).
        """
        window_end = self._now + timedelta(days=WINDOW_DAYS)
        raw_slots = self._calendar.list_free_slots(
            doctors=DOCTORS,
            location=SEDE_MEDA,
            window_days=WINDOW_DAYS,
            slot_min=SLOT_MIN,
        )
        candidates = [
            slot
            for slot in raw_slots
            if self._is_valid_slot(slot, window_end)
        ]
        if not candidates:
            return None
        # Tie-break deterministico (Requirement 5.4): ordina per (start, doctor).
        # end.isoformat() e usato solo come discriminatore finale per rendere la
        # scelta totalmente deterministica anche quando due blocchi liberi della
        # stessa dottoressa iniziano allo stesso istante ma hanno durata diversa
        # (caso non esplicitato dal Requirement 5.4: in ogni caso l'APPT risultante
        # e identico - stessa dottoressa, stesso start, 30 min - quindi la scelta
        # e comunque equivalente sul piano funzionale). Cosi il risultato non
        # dipende dall'ordine di input degli slot.
        return min(candidates, key=lambda s: (s.start, s.doctor, s.end))

    def _is_valid_slot(self, slot: FreeBusySlot, window_end: datetime) -> bool:
        """True se lo slot e idoneo (durata >= 30, dottoressa ammessa, in finestra).

        La finestra e [now, now + 14 giorni]: lo slot deve iniziare non prima di
        now e la sua fine non deve superare il termine della finestra.
        """
        if slot.doctor not in DOCTORS:
            return False
        if slot.start < self._now:
            return False
        if slot.end > window_end:
            return False
        duration = (slot.end - slot.start).total_seconds() / 60.0
        return duration >= SLOT_MIN
