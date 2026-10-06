"""Handler Lambda motore-conversazionale (thin) - Task 14.

Centralino AI Studio Medico / Idelia (Flusso A).

Handler SOTTILE: costruisce il MotoreConversazionale con le dipendenze reali
(client Bedrock su bedrock-runtime, tool executor su repository/Assegnatore,
repository su DynamoDB per la SESSION) e delega tutta la logica a src/motore.py.
Nessuna logica di dialogo/limiti/timeout qui.

Contratto testo-in/testo-out (design.md - Decisione 1, Opzione B): l'evento porta
session_id e il testo dell'utente (dal simulatore canale testo - Task 15 - oppure
dallo strato voce Connect/Lex - Task 16/17). L'handler restituisce il testo di
risposta del turno. Lo stesso handler serve entrambi i canali.

Costo-zero e componenti a pagamento (Requirement 10.2): Bedrock e' un componente a
PAGAMENTO, disabilitato di default. Il client reale viene costruito solo se
`PAID_COMPONENTS_ENABLED` == "true"; altrimenti si usa un client mock che non
chiama Bedrock (nessun costo). Questo consente al simulatore (Task 15) e ai test di
girare a costo zero.

Il percorso reale (bedrock-runtime Converse + DynamoDB Get/PutItem sulla SESSION)
e' marcato # pragma: no cover: non e' esercitato nei test (nessuna rete, nessun
costo). La logica testabile e' interamente in src/motore.py, coperta dai test.

Riferimento: design.md sezioni "Motore_Conversazionale", "Tool-use schema per
Bedrock", Requirement 2 (1,2,3,6,8), Requirement 3 (3,4,7), Requirement 10.2.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.motore import (
    BedrockClient,
    MotoreConversazionale,
    ToolExecutor,
    ToolName,
    TurnDecision,
)

logger = logging.getLogger(__name__)

# Flag componenti a pagamento (Requirement 10.2): Bedrock reale solo se "true".
ENV_PAID_COMPONENTS = "PAID_COMPONENTS_ENABLED"
# Nome tabella DynamoDB single-table (SESSION inclusa).
ENV_TABLE_NAME = "CENTRALINO_TABLE"
# Id del modello Bedrock da invocare (solo se componenti a pagamento attivi).
ENV_BEDROCK_MODEL_ID = "BEDROCK_MODEL_ID"


class _MockBedrockClient:  # pragma: no cover - usato quando Bedrock e' disabilitato
    """Client Bedrock finto per il funzionamento a costo zero (Requirement 10.2).

    Non chiama Bedrock: restituisce una decisione conversazionale neutra. Serve al
    simulatore e agli ambienti dev con `PAID_COMPONENTS_ENABLED` non attivo.
    """

    def decide_turn(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        collected_data: dict[str, Any],
        user_text: str,
    ) -> TurnDecision:
        return TurnDecision(
            reply="Grazie, ho preso nota. Puo' dirmi altro?",
            tool=None,
        )


def _build_bedrock_client() -> BedrockClient:  # pragma: no cover - percorso reale
    """Costruisce il client Bedrock reale o il mock a seconda del flag a pagamento.

    Se `PAID_COMPONENTS_ENABLED` != "true", ritorna il mock (nessun costo). Il
    client reale (bedrock-runtime Converse con tool-use) e' costruito solo quando
    l'attivazione a pagamento e' confermata (Requirement 10.2). L'implementazione
    concreta del parsing tool-use -> TurnDecision e' fuori dallo scope della logica
    testabile e vive qui, nel percorso reale non coperto.
    """
    if os.environ.get(ENV_PAID_COMPONENTS, "false").lower() != "true":
        logger.info("Bedrock disabilitato (componenti a pagamento off): uso mock")
        return _MockBedrockClient()

    import boto3

    model_id = os.environ[ENV_BEDROCK_MODEL_ID]
    runtime = boto3.client("bedrock-runtime")

    class _RealBedrockClient:
        def decide_turn(
            self,
            system_prompt: str,
            tools: list[dict[str, Any]],
            collected_data: dict[str, Any],
            user_text: str,
        ) -> TurnDecision:
            # Data di oggi fornita dal SISTEMA (non la chiediamo all'utente).
            # Se non e' ancora tra i dati raccolti, la pre-popoliamo subito cosi
            # `dataChiamata` risulta sempre presente senza dover interrogare il
            # paziente (fuso Europe/Rome).
            oggi = _today_rome()
            if not str(collected_data.get("dataChiamata", "")).strip():
                collected_data = {**collected_data, "dataChiamata": oggi}

            # Converse API con tool-use. Passiamo lo stato raccolto finora nel
            # system prompt cosi il modello sa quali dati mancano, e i tool come
            # toolConfig. La risposta puo' contenere testo e/o un blocco toolUse.
            tool_config = _to_tool_config(tools)
            context = (
                f"{system_prompt}\n\nData di oggi (dataChiamata, gia' nota, NON "
                f"chiederla): {oggi}\n\nDati gia' raccolti (JSON): "
                f"{json.dumps(collected_data, ensure_ascii=False, default=_json_safe)}"
            )
            response = runtime.converse(
                modelId=model_id,
                system=[{"text": context}],
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                toolConfig=tool_config,
            )
            decision = _parse_turn_decision(response)

            # Garantiamo che dataChiamata sia sempre persistita (dal sistema):
            # se il modello non ha gia' scelto un tool, forziamo il salvataggio
            # della data odierna insieme agli altri eventuali dati estratti.

            # --- Persistenza robusta dei dati (fix estrazione strutturata) -----
            # Claude Haiku spesso NON emette il blocco toolUse `salva_dati_paziente`
            # anche quando l'utente fornisce dati anagrafici (dice a parole di aver
            # salvato ma tool=None). Per non dipendere dalla decisione del modello,
            # facciamo una SECONDA chiamata Bedrock che estrae in modo strutturato
            # i dati dal turno corrente. Se emergono campi NUOVI e il modello non ha
            # gia' scelto un tool di azione, forziamo `salva_dati_paziente` cosi il
            # Motore li persiste sempre nella SESSION (contratto: un tool per turno,
            # quindi le azioni finali come assegna_colloquio hanno priorita').
            if decision.tool is None:
                extracted = _extract_patient_fields(
                    runtime, model_id, collected_data, user_text
                )
                # La data odierna va persistita dal sistema: se non e' ancora
                # in sessione, la aggiungiamo ai campi da salvare in questo turno.
                if "dataChiamata" not in collected_data or not str(
                    collected_data.get("dataChiamata", "")
                ).strip():
                    extracted = {**extracted, "dataChiamata": oggi}
                if extracted:
                    logger.info(
                        "estrazione strutturata: salvo campi %s",
                        list(extracted.keys()),
                    )
                    decision = TurnDecision(
                        reply=decision.reply,
                        tool=ToolName.SALVA_DATI_PAZIENTE,
                        tool_input=extracted,
                    )
            return decision

    return _RealBedrockClient()


def _today_rome() -> str:  # pragma: no cover - dipende dall'orologio
    """Data di oggi nel fuso Europe/Rome, formato gg/mm/aaaa.

    Fornita dal sistema come `dataChiamata`, cosi il paziente non deve dirla.
    """
    from datetime import datetime, timezone, timedelta

    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Europe/Rome"))
    except Exception:  # noqa: BLE001 - fallback se tzdata non disponibile
        # Europe/Rome e' UTC+1 (o +2 in ora legale): fallback semplice a UTC+1.
        now = datetime.now(timezone.utc) + timedelta(hours=1)
    return now.strftime("%d/%m/%Y")


# Campi anagrafici estraibili dal dialogo e loro tipo atteso (allineati a
# TOOL_SCHEMA/salva_dati_paziente in src/motore.py).
_EXTRACTABLE_FIELDS: dict[str, str] = {
    "nome": "string",
    "eta": "integer",
    "telefono": "string",
    "problema": "string",
    "sede": "string",
    "dataChiamata": "string",
    "parentName": "string",
}

_EXTRACTION_SYSTEM = (
    "Sei un estrattore di dati. Analizza il messaggio del paziente e restituisci "
    "SOLO i dati anagrafici ESPLICITAMENTE presenti nel messaggio corrente. "
    "Non inventare nulla: se un dato non e' presente, ometti la chiave. "
    "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido, senza testo attorno. "
    "Chiavi ammesse: nome (stringa, nome e cognome), eta (intero), telefono "
    "(stringa di sole cifre), problema (stringa, il motivo della chiamata), sede "
    "(stringa), dataChiamata (stringa), parentName (stringa, nome del genitore se "
    "il paziente e' minorenne). Esempio: {\"nome\": \"Mario Rossi\", \"eta\": 34}."
)


def _extract_patient_fields(  # pragma: no cover - percorso reale
    runtime: Any,
    model_id: str,
    collected_data: dict[str, Any],
    user_text: str,
) -> dict[str, Any]:
    """Seconda chiamata Bedrock: estrae in JSON i dati anagrafici dal turno.

    Ritorna SOLO i campi nuovi/aggiornati (non vuoti e diversi da quanto gia'
    presente in `collected_data`), gia' normalizzati sul tipo atteso. Se
    l'estrazione fallisce o non trova nulla, ritorna un dict vuoto (nessun tool
    forzato, il turno resta come deciso dal modello conversazionale).
    """
    already = (
        f"Dati gia' raccolti (non ri-estrarli se invariati): "
        f"{json.dumps(collected_data, ensure_ascii=False, default=_json_safe)}"
    )
    try:
        response = runtime.converse(
            modelId=model_id,
            system=[{"text": f"{_EXTRACTION_SYSTEM}\n\n{already}"}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 256},
        )
    except Exception as exc:  # noqa: BLE001 - estrazione best-effort, mai bloccante
        logger.warning("DIAG estrazione fallita: %s", exc)
        return {}

    raw = _extract_text(response)
    parsed = _parse_json_object(raw)
    if not isinstance(parsed, dict):
        return {}

    new_fields: dict[str, Any] = {}
    for key, kind in _EXTRACTABLE_FIELDS.items():
        if key not in parsed:
            continue
        value = _coerce_field(parsed[key], kind)
        if value in (None, ""):
            continue
        # Solo se cambia rispetto a quanto gia' salvato (evita salvataggi inutili).
        if str(collected_data.get(key, "")).strip() == str(value).strip():
            continue
        new_fields[key] = value
    return new_fields


def _parse_json_object(text: str) -> Any:  # pragma: no cover - percorso reale
    """Estrae il primo oggetto JSON dal testo (robusto a code-fence/testo extra)."""
    if not text:
        return None
    stripped = text.strip()
    # Rimuove eventuali code-fence ```json ... ```
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fallback: prende dalla prima graffa aperta al primo oggetto valido.
    start = stripped.find("{")
    if start == -1:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(stripped[start:])
        return obj
    except json.JSONDecodeError:
        return None


def _coerce_field(value: Any, kind: str) -> Any:  # pragma: no cover - percorso reale
    """Normalizza un valore estratto sul tipo atteso dal TOOL_SCHEMA."""
    if value is None:
        return None
    if kind == "integer":
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None
    text = str(value).strip()
    return text


def _to_tool_config(tools: list[dict[str, Any]]) -> dict[str, Any]:  # pragma: no cover
    """Mappa il TOOL_SCHEMA (formato Anthropic) nel formato toolConfig della
    Converse API: {"tools": [{"toolSpec": {"name","description","inputSchema":{"json":...}}}]}.
    """
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "inputSchema": {"json": t["input_schema"]},
                }
            }
            for t in tools
        ]
    }


def _parse_turn_decision(response: dict[str, Any]) -> TurnDecision:  # pragma: no cover
    """Traduce la risposta Converse in TurnDecision.

    Estrae il testo (se presente) e il PRIMO blocco toolUse (se presente). Il
    contratto del Motore e' one-shot: un tool per turno. Il nome del tool e'
    mappato su ToolName; se non riconosciuto, il turno resta conversazionale.
    """
    reply_text = ""
    tool: ToolName | None = None
    tool_input: dict[str, Any] = {}
    try:
        content = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        content = []
    for block in content:
        if "text" in block and not reply_text:
            reply_text = block["text"]
        if "toolUse" in block and tool is None:
            tu = block["toolUse"]
            try:
                tool = ToolName(tu.get("name", ""))
                tool_input = tu.get("input", {}) or {}
            except ValueError:
                tool = None  # nome tool non riconosciuto: resta conversazionale
    if not reply_text:
        reply_text = "Va bene." if tool is not None else "Mi scusi, puo' ripetere?"
    return TurnDecision(reply=reply_text, tool=tool, tool_input=tool_input)


def _parse_sa_key(sa_key: Any) -> dict[str, Any]:  # pragma: no cover - percorso reale
    """Ritorna il dict della chiave Service Account, robusto al formato.

    Il valore del secret puo' arrivare come dict (gia' parsato), come stringa JSON
    pura, o come stringa con dati extra in coda (che manda in errore json.loads con
    'Extra data'). In quest'ultimo caso usiamo raw_decode per prendere solo il
    primo oggetto JSON valido.
    """
    if isinstance(sa_key, dict):
        return sa_key
    text = str(sa_key).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 'Extra data': decodifica solo il primo oggetto JSON e ignora la coda.
        obj, _end = json.JSONDecoder().raw_decode(text)
        return obj


def _json_safe(obj: Any):  # pragma: no cover - percorso reale
    """Rende serializzabili i tipi non-JSON che arrivano da DynamoDB.

    DynamoDB restituisce i numeri come Decimal: li convertiamo in int (se interi)
    o float. Serve per json.dumps del contesto passato al modello.
    """
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return str(obj)


def _extract_text(response: dict[str, Any]) -> str:  # pragma: no cover - percorso reale
    """Estrae il testo di risposta dalla risposta Converse di Bedrock."""
    try:
        content = response["output"]["message"]["content"]
        for block in content:
            if "text" in block:
                return block["text"]
    except (KeyError, IndexError, TypeError):
        pass
    return "Mi scusi, puo' ripetere?"


def _build_tool_executor(table: Any, session_id: str) -> ToolExecutor:  # pragma: no cover
    """Costruisce l'esecutore dei tool con AZIONI REALI su DynamoDB + Step Functions.

    - salva_dati_paziente: accumula i dati raccolti (per la registrazione).
    - cerca_paziente: ricerca per nome nel repository.
    - assegna_colloquio: registra il paziente (se non gia' fatto), assegna il
      primo slot libero via AssegnatoreColloquio (che salva l'APPT PROVVISORIO),
      poi avvia la state machine confirmation-workflow con {"appointment": {...}}.
    - classifica_esito: la registrazione del CONTACT e' gestita dal Motore
      (_apply_tool_success); qui ritorniamo solo l'eco dell'esito.

    IMPORTANTE: la Lambda e' stateless tra invocazioni (ogni turno = nuova
    invocazione). I dati raccolti nei turni precedenti vivono nella SESSION su
    DynamoDB (salvata dal Motore). Quindi per assegna_colloquio i dati si leggono
    dalla SESSION (via session_id), uniti a quelli eventualmente arrivati nel
    turno corrente.
    """
    import boto3

    from datetime import datetime, timezone

    from src.assegnatore import AssegnatoreColloquio
    from src.google_calendar_provider import GoogleCalendarProvider
    from src.models import Patient, session_pk

    from src.repository import DynamoRepository

    repository = DynamoRepository(table=table)
    collected: dict[str, Any] = {}
    state: dict[str, Any] = {"client_no": None}

    def _load_collected_from_session() -> dict[str, Any]:
        """Legge collected_data dalla SESSION su DynamoDB (turni precedenti)."""
        resp = table.get_item(Key={"PK": session_pk(session_id), "SK": "META"})
        item = resp.get("Item") or {}
        data = dict(item.get("collectedData", {}))
        # I dati del turno corrente (se presenti) hanno la precedenza.
        data.update(collected)
        return data

    def _as_int(value: Any) -> int:
        """Converte robustamente l'eta' (str/Decimal/int) in int."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _build_calendar_provider():
        """Costruisce il GoogleCalendarProvider reale (per la disponibilita' slot)."""
        secret_name = os.environ["GOOGLE_SA_SECRET_NAME"]
        calendar_ids = json.loads(os.environ.get("GOOGLE_CALENDAR_IDS", "{}"))
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_key = boto3.client("secretsmanager").get_secret_value(
            SecretId=secret_name
        )["SecretString"]
        sa_info = _parse_sa_key(sa_key)
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return GoogleCalendarProvider(
            service=service,
            secret_loader=lambda: sa_key,
            calendar_ids=calendar_ids,
        )

    def _start_state_machine(appointment) -> None:
        """Avvia la confirmation-workflow con l'appuntamento provvisorio."""
        sm_arn = os.environ["CONFIRMATION_STATE_MACHINE_ARN"]
        boto3.client("stepfunctions").start_execution(
            stateMachineArn=sm_arn,
            input=json.dumps({"appointment": appointment.to_item()}),
        )

    class _RepoToolExecutor:
        def execute(
            self, tool: ToolName, tool_input: dict[str, Any]
        ) -> dict[str, Any]:
            import traceback

            try:
                return self._run(tool, tool_input)
            except Exception:
                logger.error(
                    "azione tool %s fallita:\n%s",
                    getattr(tool, "value", tool),
                    traceback.format_exc(),
                )
                raise

        def _run(
            self, tool: ToolName, tool_input: dict[str, Any]
        ) -> dict[str, Any]:
            if tool is ToolName.SALVA_DATI_PAZIENTE:
                collected.update(tool_input)
                return {"saved": True}

            if tool is ToolName.CERCA_PAZIENTE:
                found = repository.search_by_name(tool_input.get("nome", ""))
                return {"count": len(found)}

            if tool is ToolName.ASSEGNA_COLLOQUIO:
                # Dati raccolti: dalla SESSION (turni precedenti) + turno corrente.
                data = _load_collected_from_session()
                # 1. Registra il paziente (una volta) con i dati raccolti.
                if state["client_no"] is None:
                    patient = Patient(
                        client_no="",  # assegnato da register_patient
                        nome=data.get("nome", ""),
                        eta=_as_int(data.get("eta", 0)),
                        telefono=data.get("telefono", ""),
                        problema=data.get("problema", ""),
                        sede=data.get("sede", "Meda"),
                        data_chiamata=data.get("dataChiamata", ""),
                        parent_name=data.get("parentName"),
                        email=data.get("email"),
                    )
                    persisted = repository.register_patient(patient)
                    state["client_no"] = persisted.client_no
                # 2. Assegna il primo slot libero (salva APPT PROVVISORIO).
                assegnatore = AssegnatoreColloquio(
                    repository=repository,
                    calendar=_build_calendar_provider(),
                    now=datetime.now(timezone.utc),
                )
                result = assegnatore.assign(
                    client_no=state["client_no"],
                    patient_name=data.get("nome", ""),
                )
                if not result.available or result.appointment is None:
                    return {"assigned": False, "reason": "nessuno slot disponibile"}
                # 3. Avvia la state machine (Telegram -> conferma -> calendario).
                _start_state_machine(result.appointment)
                return {
                    "assigned": True,
                    "appointmentId": result.appointment.appointment_id,
                    "doctor": result.appointment.doctor,
                    "start": result.appointment.start,
                }

            # classifica_esito e altri: eco, il Motore gestisce il CONTACT.
            return {"ok": True, "esito": tool_input.get("esito")}

    return _RepoToolExecutor()


def _build_session_repository(table: Any):  # pragma: no cover - percorso reale
    """Repository della SESSION su DynamoDB (Get/PutItem sull'entita' SESSION)."""
    from src.models import Session, session_pk

    class _SessionRepo:
        def get_session(self, session_id: str) -> Session | None:
            resp = table.get_item(Key={"PK": session_pk(session_id), "SK": "META"})
            item = resp.get("Item")
            if item is None:
                return None
            return Session(
                session_id=session_id,
                collected_data=dict(item.get("collectedData", {})),
                clarifying_questions=int(item.get("clarifyingQuestions", 0)),
            )

        def save_session(self, session: Session) -> None:
            table.put_item(Item=session.to_item())

    return _SessionRepo()


def parse_request(event: dict[str, Any]) -> tuple[str, str, bool]:
    """Estrae (session_id, user_text, channel) dall'evento, pura e testabile.

    Supporta TRE forme di evento con lo stesso contratto testo-in, distinte dal
    `channel` restituito ("direct" | "http" | "lex"):
      - "direct": invocazione diretta (test / futuro), campi `sessionId`/`text`
        al top level.
      - "http": proxy HTTP API v2 (simulatore canale testo, Task 15), payload JSON
        nel campo `body` (eventualmente base64).
      - "lex": evento Amazon Lex V2 (strato voce, Task 16/17): il testo e' in
        `inputTranscript`, la sessione in `sessionState.sessionId` o `sessionId`.

    Returns:
        (session_id, user_text, channel).
    """
    # Riconoscimento canale Lex V2: l'evento ha sessionState + inputTranscript
    # (e tipicamente bot/interpretations). E' il formato che Lex invia al code
    # hook di fulfillment.
    if "sessionState" in event and (
        "inputTranscript" in event or "bot" in event
    ):
        session_id = (
            event.get("sessionId")
            or event.get("sessionState", {}).get("sessionId")
            or ""
        )
        user_text = event.get("inputTranscript") or ""
        return session_id, user_text, "lex"

    is_http = "body" in event and (
        event.get("version") == "2.0"
        or "requestContext" in event
        or "routeKey" in event
    )
    payload: dict[str, Any] = event
    if is_http:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            raw = base64.b64decode(raw).decode("utf-8")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            payload = {}

    session_id = payload.get("sessionId") or payload.get("session_id") or ""
    user_text = payload.get("text") or payload.get("userText") or ""
    return session_id, user_text, ("http" if is_http else "direct")


# Nome dell'intent di fulfillment atteso da Lex nella risposta (il catch-all
# FallbackIntent definito nello stack). Deve combaciare con _LEX_FALLBACK_INTENT.
_LEX_FULFILLMENT_INTENT = "CatchAllFallbackIntent"


def build_response(
    session_id: str, result: Any, channel: str, intent_name: str | None = None
) -> dict[str, Any]:
    """Costruisce la risposta nel formato del canale ("direct" | "http" | "lex").

    - direct: dict diretto col contratto testo-out.
    - http: risposta proxy HTTP API (statusCode 200 + body JSON).
    - lex: formato Lex V2 fulfillment: sessionState con dialogAction Close +
      intent Fulfilled, e `messages` col testo da pronunciare (Polly/TTS). L'intent
      name DEVE combaciare con quello inviato da Lex nella richiesta (arriva in
      sessionState.intent.name): lo rimandiamo tale e quale. Fallback a
      FallbackIntent (nome standard sempre presente in un bot Lex V2).
    """
    if channel == "lex":
        # Lex NON legge "reply": il testo pronunciato va in `messages`.
        #
        # MULTI-TURNO: la sessione Lex resta aperta finche' la conversazione non e'
        # conclusa. Usiamo dialogAction:
        #   - ElicitIntent  -> Lex resta in ascolto per un altro turno (continua)
        #   - Close         -> conversazione finita (Lex chiude la sessione)
        # Il turno e' CONCLUSIVO quando la prenotazione e' stata completata con
        # successo (tool assegna_colloquio andato a buon fine) oppure quando il
        # motore segnala esplicitamente la fine. Altrimenti continuiamo a dialogare.
        tool_done = getattr(result, "tool_invoked", None)
        conversation_ended = (
            tool_done is not None
            and getattr(tool_done, "value", "") == "assegna_colloquio"
            and not getattr(result, "tool_failed", False)
        )
        dialog_type = "Close" if conversation_ended else "ElicitIntent"
        session_state: dict[str, Any] = {"dialogAction": {"type": dialog_type}}
        # L'intent va incluso (state Fulfilled) solo quando chiudiamo; con
        # ElicitIntent Lex rieliciterà l'intent dal prossimo turno.
        if conversation_ended:
            session_state["intent"] = {
                "name": intent_name or "FallbackIntent",
                "state": "Fulfilled",
            }
        return {
            "sessionState": session_state,
            "messages": [
                {"contentType": "PlainText", "content": result.reply}
            ],
        }

    payload = {
        "sessionId": session_id,
        "reply": result.reply,
        "toolInvoked": result.tool_invoked.value if result.tool_invoked else None,
        "toolFailed": result.tool_failed,
    }
    if channel != "http":
        return payload
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pragma: no cover
    """Entrypoint Lambda: elabora un turno di dialogo (testo-in/testo-out).

    Handler sottile: costruisce il Motore con dipendenze reali e delega a
    src/motore.py. Bedrock e' usato solo se i componenti a pagamento sono attivi
    (Requirement 10.2); altrimenti mock a costo zero. Lo stesso handler serve sia
    lo strato voce (invocazione diretta) sia il simulatore canale testo via HTTP
    API `/simulate` (Task 15): la forma dell'evento e della risposta e' risolta da
    parse_request/build_response.

    Args:
        event: payload diretto (`sessionId`/`text`) o proxy HTTP API (JSON in body).
        context: contesto di runtime Lambda (non usato).

    Returns:
        Dict diretto oppure risposta proxy HTTP (statusCode/body) col contratto
        testo-out (sessionId, reply, toolInvoked, toolFailed).
    """
    import boto3

    session_id, user_text, channel = parse_request(event)

    # Per il canale Lex: l'intent name da rimandare nella risposta e' quello
    # inviato da Lex (sessionState.intent.name). Deve combaciare con un intent
    # reale del bot, altrimenti Lex rifiuta la risposta.
    lex_intent_name = None
    if channel == "lex":
        lex_intent_name = (
            event.get("sessionState", {}).get("intent", {}).get("name")
        )

    table = boto3.resource("dynamodb").Table(os.environ[ENV_TABLE_NAME])
    motore = MotoreConversazionale(
        client=_build_bedrock_client(),
        tool_executor=_build_tool_executor(table, session_id),
        repository=_build_session_repository(table),
    )
    result = motore.handle_turn(session_id, user_text)
    return build_response(session_id, result, channel, lex_intent_name)
