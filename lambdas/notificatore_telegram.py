"""Handler Lambda notificatore-telegram (thin) - Task 9.

Centralino AI Studio Medico / Idelia (Flusso A).

Handler SOTTILE: costruisce il TelegramNotifier con le dipendenze reali
(secret_loader su AWS Secrets Manager, sender HTTP verso la Telegram Bot API) e
delega tutta la logica a src/telegram.py. Nessuna logica di rendering qui.

Il token del bot vive in AWS Secrets Manager e viene letto A RUNTIME dal
secret_loader (mai in variabili d'ambiente in chiaro, mai nel codice, mai
loggato). L'invocazione riceve nel payload l'appuntamento in Stato_Provvisorio da
riepilogare; l'handler lo trasforma in Appointment e invoca send_recap.

Il percorso reale (boto3 Secrets Manager + HTTP verso Bot API) e marcato
# pragma: no cover: non viene esercitato nei test (costo zero, nessuna rete). La
logica testabile e interamente in src/telegram.py, coperta dai test.

Riferimento: design.md sezione "Notificatore_Telegram", Requirement 6 (criteri 1
e 2).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from src.models import Appointment
from src.telegram import TelegramConfig, TelegramNotifier

logger = logging.getLogger(__name__)

# Variabili d'ambiente di configurazione (nessun segreto in chiaro: qui solo il
# NOME/ARN del segreto e il chat_id del gruppo, non il token).
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_SECRET_NAME = "TELEGRAM_BOT_TOKEN_SECRET_NAME"
# Nome della tabella single-table (per salvare il taskToken sull'item APPT).
ENV_TABLE_NAME = "CENTRALINO_TABLE"

_TELEGRAM_API_BASE = "https://api.telegram.org"


def _load_bot_token(secret_name: str) -> str:  # pragma: no cover - percorso reale
    """Legge il token del bot da AWS Secrets Manager a runtime.

    Il valore NON viene loggato ne restituito altrove: resta confinato al
    TelegramNotifier che lo usa solo per costruire l'URL della Bot API.

    Args:
        secret_name: Nome o ARN del segreto che contiene il token del bot.

    Returns:
        Il token del bot come stringa.
    """
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return response["SecretString"]


def _http_sender(  # pragma: no cover - percorso reale (rete)
    token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Invia il payload sendMessage alla Telegram Bot API via HTTPS.

    Il token e usato solo per comporre l'URL del metodo Bot API e non viene
    loggato. Il payload (chat_id, text, reply_markup) e serializzato in JSON.

    Args:
        token: Token del bot Telegram.
        payload: Corpo della richiesta sendMessage.

    Returns:
        La risposta JSON dell'API come dict.
    """
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _appointment_from_event(event: dict[str, Any]) -> Appointment:
    """Ricostruisce un Appointment dal payload di invocazione.

    Accetta sia le chiavi in snake_case (Python) sia quelle dell'item DynamoDB
    (camelCase) per robustezza rispetto al chiamante (Step Functions, Task 11).

    Args:
        event: Payload di invocazione con i campi dell'appuntamento.

    Returns:
        Istanza Appointment costruita dai campi ricevuti.
    """
    appt = event.get("appointment", event)
    return Appointment(
        appointment_id=appt.get("appointment_id") or appt["appointmentId"],
        client_no=appt.get("client_no") or appt.get("clientNo", ""),
        nome_paziente=appt.get("nome_paziente") or appt.get("nomePaziente", ""),
        doctor=appt["doctor"],
        sede=appt.get("sede", ""),
        start=appt["start"],
        duration_min=int(appt.get("duration_min") or appt.get("durationMin", 30)),
    )


def _build_notifier(config: TelegramConfig) -> TelegramNotifier:  # pragma: no cover
    """Costruisce il TelegramNotifier con le dipendenze reali (Secrets + HTTP)."""
    return TelegramNotifier(
        config=config,
        sender=_http_sender,
        secret_loader=lambda: _load_bot_token(config.secret_name),
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover
    """Entrypoint Lambda: invia il recap Telegram per l'appuntamento ricevuto.

    Handler sottile: costruisce il notifier con dipendenze reali e delega a
    src/telegram.py. Nessuna logica di rendering qui.

    Args:
        event: Payload di invocazione con l'appuntamento in Stato_Provvisorio.
        context: Contesto di runtime Lambda (non usato).

    Returns:
        Dict con lo stato dell'invio e l'appointment_id notificato.
    """
    config = TelegramConfig(
        chat_id=os.environ[ENV_CHAT_ID],
        secret_name=os.environ[ENV_SECRET_NAME],
    )
    notifier = _build_notifier(config)
    appointment = _appointment_from_event(event)

    # Persisti il taskToken di Step Functions (waitForTaskToken) associato
    # all'appuntamento PRIMA di inviare il recap. Il callback lo recuperera' per
    # appointment_id (il taskToken e' troppo lungo per il callback_data Telegram,
    # limite 64 byte). Se manca il token (invocazione diretta/test), si salta.
    task_token = event.get("taskToken")
    if task_token:
        _save_task_token(appointment.appointment_id, task_token)

    result = notifier.send_recap(appointment)
    return {"sent": True, "appointmentId": appointment.appointment_id, "response": result}


def _save_task_token(appointment_id: str, task_token: str) -> None:  # pragma: no cover
    """Salva il taskToken sull'item APPT via DynamoRepository (percorso reale)."""
    import boto3

    from src.repository import DynamoRepository

    table = boto3.resource("dynamodb").Table(os.environ[ENV_TABLE_NAME])
    DynamoRepository(table).save_task_token(appointment_id, task_token)
