"""Handler Lambda scrivi-calendario (thin) - Task 11.

Centralino AI Studio Medico / Idelia (Flusso A).

Handler SOTTILE invocato dalla state machine Step Functions (confirmation-workflow,
design.md - Decisione 4) sullo stato ScritturaCalendario, dopo che l'appuntamento
e' stato confermato (azione "Conferma" entro 8h) oppure confermato automaticamente
(timeout 8h). Costruisce il GoogleCalendarProvider con le dipendenze reali (service
Google, secret_loader su Secrets Manager, calendar_ids) e delega la scrittura
idempotente con retry a src/google_calendar_provider.py. Nessuna logica di
idempotenza/retry qui: vive tutta nel provider (Requirement 7, criteri 5-7).

Semantica verso Step Functions:
  - Successo -> restituisce {eventId, appointmentId}. La state machine prosegue
    verso InviaConsenso -> Completato.
  - Fallimento persistente dopo 3 retry -> il provider solleva CalendarWriteError.
    La state machine, tramite Retry/Catch sul task, instrada verso lo stato
    CalendarioFallito che mappa ApptStatus.SCRITTURA_CALENDARIO_FALLITA
    (Requirement 7, criterio 7). Qui l'errore si propaga: la gestione dello stato
    e' dichiarativa nella state machine, non nell'handler.

Il percorso reale (boto3 Secrets Manager + client Google) e' marcato
# pragma: no cover: non viene esercitato nei test (costo zero, nessuna rete). La
logica testabile (idempotenza + retry) e' interamente in
src/google_calendar_provider.py, coperta dai test di Task 8.1.

Riferimento: design.md sezione "Decisione 4" (ScritturaCalendario), Requirement 7.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from src.calendar_provider import CalendarEvent

logger = logging.getLogger(__name__)

# Variabili d'ambiente di configurazione (nessun segreto in chiaro: qui solo il
# NOME/ARN del segreto con la chiave del Service Account e il mapping calendari).
ENV_SECRET_NAME = "GOOGLE_SA_SECRET_NAME"
# Mapping dottoressa -> calendarId Google, serializzato JSON nell'ambiente.
ENV_CALENDAR_IDS = "GOOGLE_CALENDAR_IDS"


def _load_sa_key(secret_name: str) -> str:  # pragma: no cover - percorso reale
    """Legge la chiave JSON del Service Account da Secrets Manager a runtime.

    Il valore NON viene loggato ne restituito altrove: resta confinato al
    provider che lo usa solo per autenticare il client Google.

    Args:
        secret_name: Nome o ARN del segreto con la chiave del Service Account.

    Returns:
        La chiave JSON del Service Account come stringa.
    """
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return response["SecretString"]


def _build_provider():  # pragma: no cover - percorso reale (Google + Secrets)
    """Costruisce il GoogleCalendarProvider con le dipendenze reali."""
    import json

    from src.google_calendar_provider import GoogleCalendarProvider

    secret_name = os.environ[ENV_SECRET_NAME]
    calendar_ids = json.loads(os.environ.get(ENV_CALENDAR_IDS, "{}"))

    # Autenticazione: costruiamo le credenziali dal JSON del Service Account
    # (letto da Secrets Manager) e le passiamo esplicitamente al client Google.
    # Senza questo, googleapiclient cerca le Application Default Credentials
    # dell'ambiente (assenti in Lambda) e solleva DefaultCredentialsError.
    from google.oauth2 import service_account  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    sa_info = json.loads(_load_sa_key(secret_name))
    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    service = build(
        "calendar", "v3", credentials=credentials, cache_discovery=False
    )
    return GoogleCalendarProvider(
        service=service,
        # Il secret e' gia' stato letto per costruire le credenziali: il provider
        # non deve rileggerlo, quindi il loader restituisce la chiave gia' nota.
        secret_loader=lambda: _load_sa_key(secret_name),
        calendar_ids=calendar_ids,
    )


def _event_from_payload(event: dict[str, Any]) -> CalendarEvent:
    """Ricostruisce un CalendarEvent dal payload di invocazione.

    Accetta le chiavi sia in snake_case sia in camelCase (item DynamoDB / output
    Step Functions) per robustezza rispetto al chiamante.

    Args:
        event: Payload con i dati dell'appuntamento confermato.

    Returns:
        CalendarEvent pronto per create_event (idempotente sull'appointment_id).
    """
    appt = event.get("appointment", event)
    appointment_id = appt.get("appointment_id") or appt["appointmentId"]
    start_raw = appt["start"]
    start = start_raw if isinstance(start_raw, datetime) else datetime.fromisoformat(start_raw)
    return CalendarEvent(
        appointment_id=appointment_id,
        doctor=appt["doctor"],
        patient_name=appt.get("nome_paziente") or appt.get("nomePaziente", ""),
        start=start,
        duration_min=int(appt.get("duration_min") or appt.get("durationMin", 30)),
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover
    """Entrypoint Lambda: scrive l'evento definitivo su Google Calendar.

    Handler sottile: costruisce il provider con dipendenze reali e delega la
    scrittura idempotente con retry a src/google_calendar_provider.py. In caso di
    fallimento persistente l'eccezione si propaga: la state machine la cattura e
    instrada verso lo stato CalendarioFallito (Requirement 7, criterio 7).

    Args:
        event: Payload con l'appuntamento confermato.
        context: Contesto di runtime Lambda (non usato).

    Returns:
        Dict con eventId e appointmentId in caso di successo.
    """
    provider = _build_provider()
    calendar_event = _event_from_payload(event)
    event_id = provider.create_event(calendar_event)
    logger.info(
        "evento calendario creato per appointment_id=%s",
        calendar_event.appointment_id,
    )
    return {
        "appointmentId": calendar_event.appointment_id,
        "eventId": event_id,
    }
