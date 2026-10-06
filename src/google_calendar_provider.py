"""GoogleCalendarProvider: implementazione concreta con idempotenza e retry.

Centralino AI Studio Medico / Idelia (Flusso A) - Task 8.

Implementa l'interfaccia astratta CalendarProvider (src/calendar_provider.py) su
Google Calendar. Rispetta lo STESSO contratto della FakeCalendarProvider cosi la
stessa suite di contract test passa su entrambe (Requirement 7, criterio 4 -
sostituibilita senza modifiche al codice a valle).

Responsabilita (design.md - Interfacce chiave / Decisione 3):
  - list_free_slots: legge la disponibilita grezza (freebusy) e la mappa in
    FreeBusySlot. La selezione dello slot resta all'Assegnatore_Colloquio (Task 7).
  - create_event: crea l'evento in modo IDEMPOTENTE. L'eventId Google e derivato
    in modo deterministico dall'appointment_id (Requirement 7, criterio 5), cosi
    N chiamate con lo stesso appointment_id producono UN SOLO evento e lo stesso
    eventId (Property 20). Il corpo dell'evento contiene nome/cognome del paziente,
    dottoressa, start e durata (Property 19).
  - Retry: fino a MAX_ATTEMPTS (default 3) tentativi con intervallo >= MIN_INTERVAL
    secondi (default 2s) sui fallimenti transitori. Al fallimento persistente
    solleva CalendarWriteError, che il chiamante (Step Functions, Task 11) mappa
    allo stato ApptStatus.SCRITTURA_CALENDARIO_FALLITA (Requirement 7, criteri 6 e
    7 / Property 21).

Autenticazione (Decisione 3). La chiave JSON del Service Account Google vive in
AWS Secrets Manager e viene letta A RUNTIME. In questo modulo il recupero e
INIETTATO come callable secret_loader: i test iniettano un loader fittizio senza
alcuna chiamata reale a Secrets Manager. Il percorso reale (boto3 +
google-auth) e un fallback marcato # pragma: no cover, mai esercitato nei test.

Secret safety (regola di workspace):
  - Nessun valore di segreto e hardcoded, loggato o incluso nei dati di test.
  - Il segreto e referenziato SOLO per nome/ARN via configurazione; il valore
    resta nel callable iniettato e non entra mai nei log ne nel corpo evento.
  - Nessuna chiamata secretsmanager get-secret-value nei percorsi coperti dai
    test.

Costo zero: nei test si usano service mock + secret_loader/sleep iniettati.
Nessuna chiamata reale AWS/Google/rete e nessuna attesa reale.

Riferimento: design.md sezione "Interfacce chiave" (Calendar_Provider astratto +
GoogleCalendarProvider), "Gestione errori e resilienza" (idempotenza + retry).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from src.calendar_provider import CalendarEvent, CalendarProvider, FreeBusySlot

logger = logging.getLogger(__name__)

# Numero massimo di tentativi di scrittura calendario (Requirement 7, criterio 6).
DEFAULT_MAX_ATTEMPTS = 3

# Intervallo minimo tra i tentativi in secondi (Requirement 7, criterio 6).
DEFAULT_MIN_INTERVAL_SEC = 2.0

# Charset ammesso per un eventId Google Calendar: base32hex (a-v, 0-9), lunghezza
# tra 5 e 1024 caratteri. Derivo un id deterministico dall'appointment_id
# rispettando questo charset (Requirement 7, criterio 5).
_BASE32HEX_ALPHABET = "0123456789abcdefghijklmnopqrstuv"

# Prefisso dell'eventId derivato (mantiene la lunghezza minima e la leggibilita).
_EVENT_ID_PREFIX = "appt"


class CalendarWriteError(Exception):
    """Scrittura calendario fallita in modo persistente (Requirement 7, criterio 7).

    Sollevata dopo l'esaurimento dei retry. Il chiamante (Step Functions) mappa
    questo errore allo stato ApptStatus.SCRITTURA_CALENDARIO_FALLITA. L'eccezione
    NON contiene valori di segreto.
    """


class TransientCalendarError(Exception):
    """Errore transitorio (ritentabile) durante l'accesso a Google Calendar.

    Un backend puo sollevarla per segnalare che il tentativo va ripetuto. Errori
    non marcati come transitori sono trattati come permanenti e non ritentati.
    """


@dataclass(frozen=True)
class RetryConfig:
    """Configurazione dei retry di scrittura calendario.

    max_attempts: numero massimo di tentativi complessivi (>= 1).
    min_interval_sec: intervallo minimo tra due tentativi in secondi (>= 2s per
        rispettare il Requirement 7, criterio 6).
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts deve essere >= 1")
        if self.min_interval_sec < DEFAULT_MIN_INTERVAL_SEC:
            raise ValueError(
                "min_interval_sec deve essere >= "
                f"{DEFAULT_MIN_INTERVAL_SEC} secondi (Requirement 7, criterio 6)"
            )


def derive_event_id(appointment_id: str) -> str:
    """Deriva un eventId Google deterministico dall'appointment_id.

    L'id e stabile (stesso appointment_id -> stesso eventId), cosi un retry o una
    chiamata duplicata riconosce/aggiorna lo stesso evento invece di crearne uno
    nuovo (Requirement 7, criterio 5 / Property 20). L'output rispetta il charset
    ammesso da Google Calendar (base32hex, minuscole) ed e lungo abbastanza da
    soddisfare il minimo di 5 caratteri.

    Args:
        appointment_id: Chiave di idempotenza dell'appuntamento.

    Returns:
        eventId deterministico valido per Google Calendar.
    """
    digest = hashlib.sha256(appointment_id.encode("utf-8")).digest()
    encoded = _to_base32hex(digest)
    # Prefisso costante + hash troncato: deterministico, entro i limiti di lunghezza.
    return f"{_EVENT_ID_PREFIX}{encoded[:52]}"


def _to_base32hex(data: bytes) -> str:
    """Codifica dei byte nel charset base32hex minuscolo (0-9a-v)."""
    bits = 0
    value = 0
    out: list[str] = []
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_BASE32HEX_ALPHABET[(value >> bits) & 0x1F])
    if bits > 0:
        out.append(_BASE32HEX_ALPHABET[(value << (5 - bits)) & 0x1F])
    return "".join(out)


class GoogleCalendarProvider(CalendarProvider):
    """Provider Google Calendar idempotente con retry (Requirement 7).

    Dipendenze iniettate (composition, testabile a costo zero):
      - service: client Google Calendar-like. Deve esporre:
          service.freebusy().query(body=...).execute() -> dict
          service.events().get(calendarId=..., eventId=...).execute() -> dict
          service.events().insert(calendarId=..., body=..., eventId?...).execute()
        Nei test si inietta un mock che registra gli insert.
      - secret_loader: Callable[[], str] che restituisce la chiave JSON del
        Service Account (letta da Secrets Manager a runtime). Iniettato come fake
        nei test: il valore NON viene mai loggato ne incluso nell'evento.
      - calendar_ids: mapping dottoressa -> calendarId Google.
      - retry: RetryConfig (default 3 tentativi, intervallo >= 2s).
      - sleep: Callable[[float], None] iniettabile cosi i test non attendono
        davvero (di default time.sleep nel percorso reale).

    Il secret_loader viene invocato pigramente (lazy) alla prima operazione che
    richiede autenticazione e memorizzato, cosi il segreto non viene riletto ad
    ogni chiamata. Il valore resta confinato a questa istanza.
    """

    def __init__(
        self,
        service: Any,
        secret_loader: Callable[[], str],
        calendar_ids: dict[str, str],
        retry: RetryConfig | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._service = service
        self._secret_loader = secret_loader
        # Normalizza le chiavi (nomi dottoresse) a minuscolo per un match
        # case-insensitive: l'Assegnatore usa "Chiara"/"Francesca" (maiuscolo),
        # il mapping puo' arrivare in minuscolo. Cosi' combaciano sempre.
        self._calendar_ids = {str(k).lower(): v for k, v in calendar_ids.items()}
        self._retry = retry or RetryConfig()
        self._sleep = sleep or _default_sleep
        # Cache del segreto: caricato pigramente, mai loggato.
        self._service_account_key: str | None = None

    # --- Contratto CalendarProvider ---------------------------------------

    def list_free_slots(
        self,
        doctors: list[str],
        location: str,
        window_days: int,
        slot_min: int,
    ) -> list[FreeBusySlot]:
        """Legge la disponibilita grezza via freebusy e la mappa in FreeBusySlot.

        Semantica coerente con il Fake: qui si restituisce la disponibilita grezza
        (blocchi liberi >= slot_min per le dottoresse indicate); la SELEZIONE dello
        slot (primo libero, tie-break) resta all'Assegnatore_Colloquio (Task 7).

        Args:
            doctors: Dottoresse di cui leggere la disponibilita.
            location: Sede richiesta (non filtra qui: pass-through semantico).
            window_days: Ampiezza della finestra in giorni.
            slot_min: Durata minima di uno slot utile in minuti.

        Returns:
            Lista di FreeBusySlot liberi (>= slot_min) per le dottoresse indicate.
        """
        self._ensure_authenticated()
        time_min = _now_provider()
        time_max = time_min + timedelta(days=window_days)
        items = [
            {"id": self._calendar_id_for(doctor)}
            for doctor in doctors
            if str(doctor).lower() in self._calendar_ids
        ]
        body = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "items": items,
        }
        response = self._service.freebusy().query(body=body).execute()
        return self._map_freebusy(response, doctors, slot_min, time_min, time_max)

    def create_event(self, event: CalendarEvent) -> str:
        """Crea l'evento in modo idempotente con retry (Requirement 7, criteri 5-7).

        L'eventId e derivato in modo deterministico dall'appointment_id. Se un
        evento con quell'id esiste gia (check-then-create) NON viene creato un
        duplicato e viene restituito lo stesso eventId (Property 20). In caso di
        errori transitori il tentativo e ripetuto fino a max_attempts con
        intervallo >= 2s; al fallimento persistente solleva CalendarWriteError
        (Property 21).

        Args:
            event: Dati dell'evento (contengono nome/cognome, dottoressa, start,
                durata: Property 19).

        Returns:
            eventId dell'evento creato o gia esistente.

        Raises:
            CalendarWriteError: dopo l'esaurimento dei retry su errore persistente.
        """
        self._ensure_authenticated()
        event_id = derive_event_id(event.appointment_id)
        calendar_id = self._calendar_id_for(event.doctor)
        body = self._build_event_body(event, event_id)

        last_error: Exception | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            if attempt > 1:
                # Intervallo minimo tra i tentativi (Requirement 7, criterio 6).
                self._sleep(self._retry.min_interval_sec)
            try:
                return self._insert_idempotent(calendar_id, event_id, body)
            except TransientCalendarError as exc:
                last_error = exc
                logger.warning(
                    "scrittura calendario tentativo %d/%d fallita (transitorio)",
                    attempt,
                    self._retry.max_attempts,
                )
                continue
        # Fallimento persistente dopo i retry: segnalazione d'errore (criterio 7).
        raise CalendarWriteError(
            "scrittura calendario fallita dopo "
            f"{self._retry.max_attempts} tentativi per appointment_id="
            f"{event.appointment_id}"
        ) from last_error

    # --- Idempotenza (check-then-create) ----------------------------------

    def _insert_idempotent(
        self, calendar_id: str, event_id: str, body: dict[str, Any]
    ) -> str:
        """Crea l'evento solo se non esiste gia (check-then-create).

        Prima verifica se un evento con event_id e gia presente: in tal caso
        restituisce il suo id senza creare duplicati (Property 20). Altrimenti lo
        inserisce con l'eventId deterministico. Gli errori transitori si propagano
        come TransientCalendarError cosi il chiamante ritenta.
        """
        existing = self._get_existing_event(calendar_id, event_id)
        if existing is not None:
            return str(existing.get("id", event_id))
        created = (
            self._service.events()
            .insert(calendarId=calendar_id, body=body)
            .execute()
        )
        return str(created.get("id", event_id))

    def _get_existing_event(
        self, calendar_id: str, event_id: str
    ) -> dict[str, Any] | None:
        """Ritorna l'evento esistente per event_id, o None se assente.

        Un backend che non trova l'evento deve restituire None (o sollevare un
        errore non transitorio interpretato come "assente"). Gli errori transitori
        si propagano per attivare il retry.
        """
        try:
            return (
                self._service.events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except TransientCalendarError:
            raise
        except EventNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            # Il client reale Google (googleapiclient) solleva HttpError con
            # status_code 404 quando l'evento NON esiste ancora: e' il caso
            # normale alla prima creazione (check-then-create). Lo interpretiamo
            # come "evento assente" -> None, cosi il flusso procede a crearlo.
            # Altri status (403, 5xx, ...) NON vanno silenziati: si ri-sollevano.
            status = _http_status(exc)
            if status == 404:
                return None
            raise

    # --- Autenticazione (segreto iniettabile) -----------------------------

    def _ensure_authenticated(self) -> None:
        """Carica la chiave del Service Account una sola volta (lazy).

        Il valore e ottenuto dal secret_loader iniettato (nei test un fake, in
        produzione un loader che legge da Secrets Manager). NON viene loggato ne
        incluso in alcun payload. Serve solo a confermare che le credenziali sono
        disponibili prima di usare il service.
        """
        if self._service_account_key is None:
            self._service_account_key = self._secret_loader()

    # --- Helper puri ------------------------------------------------------

    def _calendar_id_for(self, doctor: str) -> str:
        """Risolve il calendarId della dottoressa (case-insensitive)."""
        try:
            return self._calendar_ids[str(doctor).lower()]
        except KeyError as exc:
            raise KeyError(
                f"nessun calendarId configurato per la dottoressa {doctor!r}"
            ) from exc

    @staticmethod
    def _build_event_body(event: CalendarEvent, event_id: str) -> dict[str, Any]:
        """Costruisce il corpo dell'evento Google (Property 19).

        Il corpo contiene nome/cognome del paziente, la dottoressa, lo start e la
        durata (derivando end da start + duration_min). L'eventId deterministico e
        incluso per l'idempotenza.
        """
        end = event.start + timedelta(minutes=event.duration_min)
        return {
            "id": event_id,
            "summary": f"Colloquio {event.patient_name}",
            "description": (
                f"Paziente: {event.patient_name} - Dottoressa: {event.doctor}"
            ),
            "start": {"dateTime": event.start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "extendedProperties": {
                "private": {
                    "appointmentId": event.appointment_id,
                    "patientName": event.patient_name,
                    "doctor": event.doctor,
                    "durationMin": str(event.duration_min),
                }
            },
        }

    def _map_freebusy(
        self,
        response: dict[str, Any],
        doctors: list[str],
        slot_min: int,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
    ) -> list[FreeBusySlot]:
        """Mappa la risposta freebusy in FreeBusySlot liberi (>= slot_min).

        Due modalita', per compatibilita':
        - Se il calendario espone "free" (mock/test): usa direttamente quei blocchi.
        - Altrimenti (Google reale, che espone "busy"): CALCOLA gli slot liberi
          generando candidati da slot_min minuti negli orari di lavoro e
          scartando quelli che si sovrappongono a un periodo busy.
        """
        calendars = response.get("calendars", {})
        id_to_doctor = {
            self._calendar_ids[str(doctor).lower()]: doctor
            for doctor in doctors
            if str(doctor).lower() in self._calendar_ids
        }
        slots: list[FreeBusySlot] = []
        for calendar_id, doctor in id_to_doctor.items():
            entry = calendars.get(calendar_id, {})
            if "free" in entry:
                # Modalita' mock/test: blocchi liberi gia' pronti.
                for period in entry["free"]:
                    start = _parse_dt(period["start"])
                    end = _parse_dt(period["end"])
                    if (end - start).total_seconds() / 60.0 >= slot_min:
                        slots.append(
                            FreeBusySlot(doctor=doctor, start=start, end=end)
                        )
            elif time_min is not None and time_max is not None:
                # Modalita' Google reale: calcola i liberi dai busy.
                busy = [
                    (_parse_dt(p["start"]), _parse_dt(p["end"]))
                    for p in entry.get("busy", [])
                ]
                slots.extend(
                    _generate_free_slots(
                        doctor, time_min, time_max, slot_min, busy
                    )
                )
        return slots


class EventNotFoundError(Exception):
    """Segnala che l'evento richiesto non esiste (usata dai backend/mock)."""


# Orari di lavoro dello studio per la generazione degli slot (Europe/Rome).
_WORK_START_HOUR = 9
_WORK_END_HOUR = 18
# Giorni lavorativi: lun-ven (weekday 0-4).
_WORK_WEEKDAYS = {0, 1, 2, 3, 4}


def _generate_free_slots(  # pragma: no cover - percorso reale (Google busy)
    doctor: str,
    time_min: datetime,
    time_max: datetime,
    slot_min: int,
    busy: list[tuple[datetime, datetime]],
) -> list[FreeBusySlot]:
    """Genera slot liberi da slot_min minuti negli orari di lavoro.

    Scorre i giorni della finestra [time_min, time_max], per ogni giorno
    lavorativo genera slot consecutivi di slot_min minuti tra WORK_START e
    WORK_END, e tiene solo quelli che (a) iniziano nel futuro e (b) non si
    sovrappongono a nessun periodo busy. Il calcolo e' semplice e deterministico.
    """
    slots: list[FreeBusySlot] = []
    step = timedelta(minutes=slot_min)
    # Partiamo dall'inizio del giorno di time_min, alle WORK_START.
    day = time_min.replace(
        hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0
    )
    while day < time_max:
        if day.weekday() in _WORK_WEEKDAYS:
            slot_start = day
            day_end = day.replace(hour=_WORK_END_HOUR, minute=0)
            while slot_start + step <= day_end:
                slot_end = slot_start + step
                # Solo slot nel futuro ed entro la finestra.
                if slot_start >= time_min and slot_end <= time_max:
                    overlaps = any(
                        slot_start < b_end and slot_end > b_start
                        for b_start, b_end in busy
                    )
                    if not overlaps:
                        slots.append(
                            FreeBusySlot(
                                doctor=doctor, start=slot_start, end=slot_end
                            )
                        )
                slot_start = slot_end
        # Giorno successivo alle WORK_START.
        day = (day + timedelta(days=1)).replace(
            hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0
        )
    return slots


def _http_status(exc: Exception) -> int | None:
    """Estrae lo status HTTP da un'eccezione del client Google, se presente.

    Il client reale (googleapiclient) solleva HttpError, che espone lo status in
    `exc.resp.status` (stringa/int) o `exc.status_code`. Qui leggiamo l'attributo
    in modo difensivo senza importare googleapiclient (il provider resta testabile
    senza quella dipendenza). Ritorna None se lo status non e' determinabile.
    """
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str) -> datetime:
    """Parsa un timestamp ISO 8601 in datetime timezone-aware."""
    return datetime.fromisoformat(value)


def _now_provider() -> datetime:  # pragma: no cover - banale, sostituibile nei test
    """Istante corrente (isolato per eventuale override nei test freebusy)."""
    from datetime import timezone

    return datetime.now(timezone.utc)


def _default_sleep(seconds: float) -> None:  # pragma: no cover - mai nei test
    """Attesa reale tra i retry. Nei test si inietta uno sleep spy che non attende."""
    import time

    time.sleep(seconds)
