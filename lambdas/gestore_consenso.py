"""Handler Lambda gestore-consenso (thin) - Task 12.

Centralino AI Studio Medico / Idelia (Flusso A).

Handler SOTTILE: costruisce il GestoreConsenso con le dipendenze reali (pdf_loader
su S3, ses_sender su ses:SendRawEmail, event_sink su log/EventBridge) e delega
tutta la logica a src/consenso.py. Nessuna logica di validazione/invio qui.

Il PDF del modulo di consenso risiede in un bucket S3 con encryption at rest
attiva (Requirement 9.6, definito in CDK - Task 12/18) e viene letto A RUNTIME dal
pdf_loader. L'invio avviene via SES SendRawEmail con il PDF in allegato
(Requirement 8.1). L'invocazione riceve nel payload l'appuntamento confermato e
l'email del paziente; l'handler delega a GestoreConsenso.process.

In nessun caso l'handler declassa lo stato dell'appuntamento: la gestione consenso
e' best-effort (Requirement 8.2, 8.3, 8.4). L'esito e' restituito e registrato
come evento.

Il percorso reale (boto3 S3 get_object + SES send_raw_email) e' marcato
# pragma: no cover: non viene esercitato nei test (costo zero, nessuna rete). La
logica testabile e' interamente in src/consenso.py, coperta dai test.

Riferimento: design.md sezione "Gestore_Consenso", Requirement 8 (criteri 1-4).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.consenso import (
    ConsensoConfig,
    ConsensoEvent,
    GestoreConsenso,
)

logger = logging.getLogger(__name__)

# Variabili d'ambiente di configurazione (nessun segreto: mittente verificato,
# bucket/chiave S3 del PDF del consenso).
ENV_SENDER_EMAIL = "CONSENSO_SENDER_EMAIL"
ENV_PDF_BUCKET = "CONSENSO_PDF_BUCKET"
ENV_PDF_KEY = "CONSENSO_PDF_KEY"


def _s3_pdf_loader(bucket: str, key: str):  # pragma: no cover - percorso reale (S3)
    """Restituisce un loader che legge il PDF del consenso da S3 a runtime.

    Il bucket ha encryption at rest attiva (Requirement 9.6, CDK). La lettura usa
    il ruolo IAM least-privilege della Lambda (s3:GetObject sul solo oggetto).

    Args:
        bucket: Nome del bucket S3 che contiene il PDF.
        key: Chiave dell'oggetto PDF del consenso.

    Returns:
        Callable[[], bytes] che restituisce i byte del PDF.
    """
    import boto3

    def _load() -> bytes:
        client = boto3.client("s3")
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()

    return _load


def _ses_raw_sender(  # pragma: no cover - percorso reale (SES)
    sender: str, recipient: str, raw_message: bytes
) -> dict[str, Any]:
    """Invia il messaggio MIME via ses:SendRawEmail.

    Il messaggio grezzo contiene gia' il PDF in allegato (costruito da
    src/consenso.build_raw_email). Usa il ruolo IAM least-privilege della Lambda
    (ses:SendRawEmail sulla sola identita' verificata).

    Args:
        sender: Mittente verificato SES.
        recipient: Destinatario (email valida del paziente).
        raw_message: Messaggio MIME serializzato.

    Returns:
        La risposta dell'API SES (dict con MessageId).
    """
    import boto3

    client = boto3.client("ses")
    return client.send_raw_email(
        Source=sender,
        Destinations=[recipient],
        RawMessage={"Data": raw_message},
    )


def _log_event_sink(event: ConsensoEvent) -> None:  # pragma: no cover - percorso reale
    """Registra l'evento di esito consenso (qui su log; estendibile a EventBridge)."""
    logger.info(
        "evento consenso appointment_id=%s outcome=%s detail=%s",
        event.appointment_id,
        event.outcome.value,
        event.detail,
    )


def _build_gestore(config: ConsensoConfig) -> GestoreConsenso:  # pragma: no cover
    """Costruisce il GestoreConsenso con le dipendenze reali (S3 + SES + log)."""
    bucket = os.environ[ENV_PDF_BUCKET]
    key = os.environ[ENV_PDF_KEY]
    return GestoreConsenso(
        config=config,
        pdf_loader=_s3_pdf_loader(bucket, key),
        ses_sender=_ses_raw_sender,
        event_sink=_log_event_sink,
    )


def _extract_email(event: dict[str, Any]) -> str | None:
    """Estrae l'email del paziente dal payload di invocazione.

    Accetta l'email a livello top-level oppure annidata nell'appuntamento/paziente,
    in snake_case o camelCase, per robustezza rispetto al chiamante (Step Functions).

    Args:
        event: Payload di invocazione.

    Returns:
        L'email trovata, o None se assente.
    """
    if event.get("email"):
        return event["email"]
    appt = event.get("appointment", {})
    if isinstance(appt, dict) and appt.get("email"):
        return appt["email"]
    patient = event.get("patient", {})
    if isinstance(patient, dict) and patient.get("email"):
        return patient["email"]
    return None


def _extract_appointment_id(event: dict[str, Any]) -> str:
    """Estrae l'appointment_id dal payload (top-level o annidato)."""
    if event.get("appointment_id"):
        return event["appointment_id"]
    if event.get("appointmentId"):
        return event["appointmentId"]
    appt = event.get("appointment", {})
    if isinstance(appt, dict):
        return appt.get("appointment_id") or appt.get("appointmentId", "")
    return ""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover
    """Entrypoint Lambda: invia il consenso per l'appuntamento confermato.

    Handler sottile: costruisce il gestore con dipendenze reali e delega a
    src/consenso.py. Nessuna logica di validazione/invio qui. Non declassa mai lo
    stato dell'appuntamento (Requirement 8.2, 8.3, 8.4).

    Args:
        event: Payload con appuntamento confermato ed email del paziente.
        context: Contesto di runtime Lambda (non usato).

    Returns:
        Dict con appointment_id ed esito della gestione consenso.
    """
    config = ConsensoConfig(sender_email=os.environ[ENV_SENDER_EMAIL])
    gestore = _build_gestore(config)
    appointment_id = _extract_appointment_id(event)
    recipient_email = _extract_email(event)
    outcome = gestore.process(appointment_id, recipient_email)
    return {"appointmentId": appointment_id, "outcome": outcome.value}
