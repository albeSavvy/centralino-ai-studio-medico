"""Handler Lambda telegram-callback (thin) - Task 10 / 11.

Centralino AI Studio Medico / Idelia (Flusso A).

Handler SOTTILE dietro API Gateway `/telegram/webhook`. Riceve il callback dei
bottoni inline (Conferma / Segnala blocker) dalla Telegram Bot API e:

  1. Valida l'autenticita' della richiesta tramite il secret token header
     Telegram (X-Telegram-Bot-Api-Secret-Token) confrontato col valore letto da
     Secrets Manager a runtime (Requirement 6.6 - solo callback autentici).
  2. Decodifica (azione, appointment_id) dal callback_data (src/telegram.py).
  3. Applica la transizione di stato con MUTUA ESCLUSIONE via conditional write
     su DynamoDB (src.telegram.apply_callback_action): la PRIMA azione in ordine
     cronologico vince, le successive falliscono la condizione e sono ignorate
     (Requirement 6.6).
  4. Se la transizione e' avvenuta (questa azione ha vinto), invia SendTaskSuccess
     a Step Functions con il taskToken consegnato dentro il callback_data/messaggio,
     sbloccando la state machine (design.md - Decisione 4). Solo la prima chiamata
     sblocca la macchina; le successive non hanno un token valido/attivo.

La logica di dominio (mappatura azione->stato, conditional write) vive in
src/telegram.py; qui c'e' solo il wiring con Secrets Manager, DynamoDB repository
e Step Functions. Il percorso reale (boto3) e' marcato # pragma: no cover: non e'
esercitato nei test (costo zero, nessuna rete). La transizione di stato e la mutua
esclusione sono coperte dai property test di Task 10.1.

Secret safety (regola di workspace aws-agent-rules): il secret token del webhook
vive in Secrets Manager, letto a runtime, mai loggato ne incluso nella risposta.

Riferimento: design.md sezione "Decisione 4" (mutua esclusione + SendTaskSuccess),
Requirement 6 (criteri 3, 5, 6).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.telegram import apply_callback_action, parse_callback_data

logger = logging.getLogger(__name__)

# NOME/ARN del segreto con il secret token del webhook Telegram (non il valore).
ENV_WEBHOOK_SECRET_NAME = "TELEGRAM_WEBHOOK_SECRET_NAME"
# Nome della tabella single-table (Anagrafica_Store) per il repository Dynamo.
ENV_TABLE_NAME = "CENTRALINO_TABLE"
# Header Telegram che trasporta il secret token di autenticita' del webhook.
_SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"


def _load_webhook_secret(secret_name: str) -> str:  # pragma: no cover - reale
    """Legge il secret token del webhook da Secrets Manager a runtime."""
    import boto3

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return response["SecretString"]


def _build_repo():  # pragma: no cover - percorso reale (DynamoDB)
    """Costruisce il DynamoRepository sulla tabella single-table."""
    import boto3

    from src.repository import DynamoRepository

    table = boto3.resource("dynamodb").Table(os.environ[ENV_TABLE_NAME])
    return DynamoRepository(table)


def _headers_lower(event: dict[str, Any]) -> dict[str, str]:
    """Ritorna gli header della richiesta con chiavi in minuscolo (case-insensitive)."""
    headers = event.get("headers") or {}
    return {str(k).lower(): v for k, v in headers.items()}


def _is_authentic(event: dict[str, Any], expected_secret: str) -> bool:
    """Verifica il secret token header Telegram (Requirement 6.6)."""
    received = _headers_lower(event).get(_SECRET_TOKEN_HEADER)
    return bool(received) and received == expected_secret


def _extract_callback(event: dict[str, Any]) -> tuple[str, str | None]:
    """Estrae callback_data e taskToken dal corpo dell'update Telegram.

    Il taskToken di Step Functions e' consegnato nel messaggio (accodato al
    callback_data dopo un separatore, oppure in un campo dedicato del payload).

    Args:
        event: Evento API Gateway (proxy) con il corpo dell'update Telegram.

    Returns:
        Tupla (callback_data, task_token). task_token puo' essere None.
    """
    body = event.get("body")
    if isinstance(body, str):
        body = json.loads(body)
    body = body or {}
    callback_query = body.get("callback_query", {})
    callback_data = callback_query.get("data", "")
    # NOTA: il taskToken NON arriva da Telegram (il callback_data e' limitato a 64
    # byte e non lo contiene). Viene recuperato da DynamoDB per appointment_id nel
    # handler (salvato dal notificatore all'invio del recap). Qui ritorniamo solo
    # il callback_data; il secondo elemento resta per compatibilita' di firma.
    task_token = body.get("taskToken") or callback_query.get("taskToken")
    return callback_data, task_token


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover
    """Entrypoint Lambda: applica l'azione del callback e sblocca Step Functions.

    Handler sottile: valida l'autenticita', delega la transizione con mutua
    esclusione a src/telegram.py, poi (solo se questa azione ha vinto) invia
    SendTaskSuccess a Step Functions. Nessuna logica di stato qui.

    Args:
        event: Evento API Gateway proxy con l'update Telegram.
        context: Contesto di runtime Lambda (non usato).

    Returns:
        Risposta HTTP (statusCode + body) per API Gateway.
    """
    import boto3

    expected_secret = _load_webhook_secret(os.environ[ENV_WEBHOOK_SECRET_NAME])
    if not _is_authentic(event, expected_secret):
        logger.warning("callback Telegram rifiutato: secret token non valido")
        return {"statusCode": 401, "body": json.dumps({"ok": False})}

    callback_data, task_token = _extract_callback(event)
    action, appointment_id = parse_callback_data(callback_data)

    repo = _build_repo()

    # Il taskToken di Step Functions non arriva da Telegram: lo recuperiamo da
    # DynamoDB per appointment_id (salvato dal notificatore all'invio del recap).
    if not task_token:
        task_token = repo.get_task_token(appointment_id)

    won = apply_callback_action(repo, appointment_id, action)

    # Solo la PRIMA azione (che ha vinto la conditional write) sblocca la state
    # machine con SendTaskSuccess. Le successive sono ignorate (mutua esclusione).
    if won and task_token:
        sfn = boto3.client("stepfunctions")
        sfn.send_task_success(
            taskToken=task_token,
            output=json.dumps({"action": action, "appointmentId": appointment_id}),
        )

    return {"statusCode": 200, "body": json.dumps({"ok": True, "applied": won})}
