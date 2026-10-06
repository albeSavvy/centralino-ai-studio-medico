"""Motore_Conversazionale: orchestrazione del dialogo e tool-use (logica pura).

Centralino AI Studio Medico / Idelia (Flusso A) - Task 14.

Questo modulo contiene la logica TESTABILE del Motore_Conversazionale (Requirement
2 criteri 1, 2, 3, 6, 8 e Requirement 3 criteri 3, 4, 7). Il Motore conduce il
dialogo in italiano tramite un LLM su Amazon Bedrock, ma qui il client Bedrock e'
INIETTATO dietro un'interfaccia (BedrockClient), cosi l'intero motore e' testabile
a costo zero con un fake, senza rete e senza chiamare Bedrock reale.

Contratto testo-in/testo-out (design.md - Decisione 1, Opzione B): ogni turno il
Motore riceve il testo dell'utente e restituisce il testo da sintetizzare/mostrare.
Lo stesso metodo `handle_turn` e' usato sia dal simulatore canale testo (Task 15)
sia dallo strato voce Connect/Lex (Task 16-17). Lo stato del dialogo (dati raccolti
finora, contatori domande/tentativi) e' persistito su una entita' SESSION in
DynamoDB tramite il repository (design.md - Motore_Conversazionale), cosi ogni
turno resta stateless a livello Lambda.

Perche' gli invarianti (Properties 3, 5, 7) sono STRUTTURALMENTE testabili:

  - Il BedrockClient e' un'interfaccia iniettabile. Nei test si iniettano fake che
    (a) sollevano un'eccezione o (b) simulano un timeout (tempo > 10s), cosi il
    ramo di fallimento tool-use e' esercitabile in modo deterministico senza rete.
  - La chiamata tool-use e' incapsulata in _invoke_tool_use, che applica il
    timeout di 10s (Requirement 2.2) e, su fallimento/timeout, NON persiste alcun
    dato parziale e preserva lo stato del dialogo antecedente all'invocazione
    (Requirement 2.3, Property 3). Lo stato e' letto/salvato in modo atomico:
    il salvataggio della SESSION avviene SOLO su un turno andato a buon fine.
  - Il contatore clarifying_questions e' vincolato a <= MAX_CLARIFYING_QUESTIONS
    (3) prima di poter porre una nuova domanda di chiarimento (Requirement 2.6,
    Property 5).
  - Il contatore per-dato field_attempts limita i tentativi di richiesta di un
    dato obbligatorio a MAX_FIELD_ATTEMPTS (3); esaurititi, il dato e' registrato
    come "non fornito" e il dialogo prosegue (Requirement 3.3/3.4, Property 7).

Timeout tool-use: il tempo di esecuzione dell'invocazione e' misurato tramite una
funzione `clock` iniettabile (default time.monotonic). Nei test si inietta un
clock che avanza oltre la soglia per simulare il superamento del timeout, senza
attese reali. Un'eventuale eccezione del client e' trattata come fallimento.

Costo zero: modulo puro Python. Il percorso reale (bedrock-runtime InvokeModel /
Converse) vive nel thin handler Lambda (lambdas/motore_conversazionale.py), marcato
# pragma: no cover. Nessuna chiamata AWS/Bedrock/rete qui.

Registrazione contatto (Requirement 3.7): quando il tool-use `classifica_esito` va
a buon fine con esito RICHIAMERA o SOLO_INFO, il Motore registra un CONTACT su
Anagrafica_Store (repository.save_contact) per un eventuale richiamo futuro; su
esito APPUNTAMENTO non registra alcun contatto (quel flusso passa dall'Assegnatore).
L'esito e' risolto dal Classificatore_Esito, cosi il default RICHIAMERA su
incertezza resta coerente. Il follow-up automatico e' Out of Scope.

Riferimento: design.md sezioni "Motore_Conversazionale", "Tool-use schema per
Bedrock", "Gestione errori e resilienza"; Requirement 2 (1,2,3,6,8), Requirement 3
(3,4,7); Correctness Properties 3, 5, 7. Il Classificatore_Esito (Task 13) resta in
src/classificatore.py e viene alimentato dal tool-use `classifica_esito`.
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from src.classificatore import classify
from src.models import Contact, Esito, Session

logger = logging.getLogger(__name__)

# Timeout dell'invocazione tool-use in secondi (Requirement 2.2, 2.3).
TOOL_USE_TIMEOUT_S = 10.0

# Numero massimo di domande di chiarimento per chiamata (Requirement 2.6).
MAX_CLARIFYING_QUESTIONS = 3

# Numero massimo di tentativi di richiesta per singolo dato obbligatorio
# (Requirement 3.3); esaurititi, il dato e' registrato come "non fornito" (3.4).
MAX_FIELD_ATTEMPTS = 3

# Marcatore registrato quando un dato obbligatorio non e' stato fornito dopo i
# tentativi ammessi (Requirement 3.4).
NON_FORNITO = "non fornito"

# Dati anagrafici OBBLIGATORI dal modulo cartaceo (Requirement 3.1).
REQUIRED_FIELDS: tuple[str, ...] = (
    "nome",
    "eta",
    "telefono",
    "problema",
    "sede",
    "dataChiamata",
)

# Messaggio comunicato al paziente quando il tool-use e' indisponibile
# (Requirement 2.3): indica indisponibilita' temporanea dell'operazione.
TOOL_UNAVAILABLE_MESSAGE = (
    "Mi scusi, in questo momento non riesco a completare l'operazione. "
    "Riproviamo tra poco."
)

# System prompt (italiano, frasi brevi, no diagnosi, max 3 chiarimenti). Vincola il
# modello secondo design.md - Tool-use schema. Tenuto come costante cosi il thin
# handler puo' passarlo al client Bedrock reale invariato.
SYSTEM_PROMPT = (
    "Sei l'assistente telefonico di una segreteria di uno studio di psicologia "
    "(sede di Meda). Sei al TELEFONO: rispondi in italiano con frasi MOLTO brevi "
    "e naturali, come al telefono. Una o due frasi per volta, mai elenchi.\n"
    "\n"
    "STILE DI DIALOGO (importante, al telefono):\n"
    "- Chiedi UN SOLO dato per volta. Mai elencare piu dati insieme: l'utente al "
    "telefono si dimentica. Fai una domanda, aspetta la risposta, poi la "
    "successiva.\n"
    "- NON ripetere ogni volta tutti i dati gia raccolti. Prendi nota in silenzio "
    "e passa alla domanda dopo. Riepiloga i dati UNA SOLA VOLTA alla fine, per "
    "conferma, subito prima di fissare l'appuntamento.\n"
    "- Niente frasi ridondanti tipo 'ho registrato X, ora mi serve...'. Vai "
    "diretta alla domanda successiva in modo naturale.\n"
    "\n"
    "USO OBBLIGATORIO DEGLI STRUMENTI (tool). Non limitarti a dire a parole che "
    "hai fatto qualcosa: DEVI usare gli strumenti per farlo davvero.\n"
    "- Appena l'utente fornisce uno o piu dati, chiama in silenzio lo strumento "
    "`salva_dati_paziente` con i dati raccolti (senza annunciarlo all'utente).\n"
    "- APPENA hai TUTTI i dati obbligatori (nome, eta, telefono, problema, sede), "
    "fai UN breve riepilogo per conferma, e poi chiama SUBITO `assegna_colloquio` "
    "per fissare il colloquio. NON continuare a chiedere o riconfermare: una volta "
    "che l'utente conferma, fissa l'appuntamento e CHIUDI la conversazione.\n"
    "- Per sapere se un paziente esiste gia, usa `cerca_paziente`.\n"
    "- Per registrare l'esito della chiamata usa `classifica_esito`.\n"
    "\n"
    "Dati obbligatori da raccogliere, UNO ALLA VOLTA: nome, eta, telefono, "
    "problema, sede. La DATA DI OGGI ti viene fornita dal sistema nel contesto: "
    "NON chiederla mai all'utente, usala cosi com'e. Chiedi solo i dati che "
    "ancora mancano (non richiedere quelli gia forniti). Lo studio si occupa SOLO "
    "di psicologia: se chiedono altri servizi (es. logopedia), spiega gentilmente "
    "che non sono disponibili. Non fornire mai diagnosi cliniche. In caso di "
    "incertezza sull'esito, il paziente richiamera'."
)


class ToolName(str, Enum):
    """Tool esposti al modello via tool-use (design.md - Tool-use schema)."""

    SALVA_DATI_PAZIENTE = "salva_dati_paziente"
    CLASSIFICA_ESITO = "classifica_esito"
    ASSEGNA_COLLOQUIO = "assegna_colloquio"
    CERCA_PAZIENTE = "cerca_paziente"


# Schema tool-use passato al modello (design.md - Tool-use schema per Bedrock).
# Definito qui una sola volta cosi il thin handler lo inoltra invariato a Bedrock.
TOOL_SCHEMA: list[dict[str, Any]] = [
    {
        "name": ToolName.SALVA_DATI_PAZIENTE.value,
        "description": "Persiste i dati anagrafici raccolti del paziente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nome": {"type": "string"},
                "eta": {"type": "integer"},
                "telefono": {"type": "string"},
                "problema": {"type": "string"},
                "sede": {"type": "string"},
                "dataChiamata": {"type": "string"},
                "parentName": {"type": "string"},
                "email": {"type": "string"},
            },
            "required": list(REQUIRED_FIELDS),
        },
    },
    {
        "name": ToolName.CLASSIFICA_ESITO.value,
        "description": "Assegna uno dei tre esiti alla chiamata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "esito": {
                    "type": "string",
                    "enum": ["RICHIAMERA", "SOLO_INFO", "APPUNTAMENTO"],
                }
            },
            "required": ["esito"],
        },
    },
    {
        "name": ToolName.ASSEGNA_COLLOQUIO.value,
        "description": "Richiede l'assegnazione del primo slot libero a Meda.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": ToolName.CERCA_PAZIENTE.value,
        "description": "Cerca pazienti esistenti per nome.",
        "input_schema": {
            "type": "object",
            "properties": {"nome": {"type": "string"}},
            "required": ["nome"],
        },
    },
]


# ---------------------------------------------------------------------------
# Contratto del modello (tool-use) - decisione del turno
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TurnDecision:
    """Decisione del modello per un turno, gia' interpretata dal client Bedrock.

    Il BedrockClient traduce la risposta grezza di Bedrock in questa struttura,
    cosi il Motore ragiona su un contratto stabile e testabile:

      reply: testo (italiano, breve) da restituire al paziente per questo turno.
      tool: se il turno richiede un'azione concreta, il tool da invocare
            (ToolName); None se il turno e' solo conversazionale.
      tool_input: argomenti del tool (dict); ignorato se tool e' None.
      is_clarifying: True se `reply` e' una domanda di chiarimento (conta verso il
            limite di 3, Requirement 2.6).
      asks_field: se il turno richiede esplicitamente un dato obbligatorio ancora
            mancante, il nome del campo (uno di REQUIRED_FIELDS); None altrimenti.
            Serve al conteggio per-dato dei tentativi (Requirement 3.3/3.4).
    """

    reply: str
    tool: ToolName | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    is_clarifying: bool = False
    asks_field: str | None = None


class BedrockClient(Protocol):
    """Interfaccia del client LLM (Bedrock) iniettata nel Motore.

    Astrae l'invocazione del modello in tool-use. L'implementazione reale
    (bedrock-runtime) vive nel thin handler; nei test si inietta un fake che
    restituisce TurnDecision deterministici (o solleva per simulare fallimenti).
    """

    def decide_turn(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        collected_data: dict[str, Any],
        user_text: str,
    ) -> TurnDecision:
        """Decide la risposta del turno dato lo stato corrente e il testo utente."""
        ...


class ToolExecutor(Protocol):
    """Esegue un tool richiesto dal modello (salva dati, assegna, cerca, ...).

    Astrazione iniettata cosi il Motore non dipende direttamente da repository,
    Assegnatore o Classificatore concreti: nei test si inietta un fake che puo'
    registrare le chiamate o sollevare per simulare un fallimento del tool.
    """

    def execute(self, tool: ToolName, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Esegue il tool e restituisce il risultato (dict). Puo' sollevare."""
        ...


class ToolUseError(Exception):
    """Fallimento (o timeout) dell'invocazione tool-use (Requirement 2.3)."""


@dataclass(frozen=True)
class TurnResult:
    """Esito di un turno di dialogo restituito dal Motore.

    reply: testo da restituire al paziente (contratto testo-out).
    tool_invoked: il tool effettivamente eseguito in questo turno (o None).
    tool_result: risultato del tool eseguito (o None).
    tool_failed: True se un'azione tool-use e' fallita o andata in timeout: in tal
        caso reply e' il messaggio di indisponibilita' e lo stato e' preservato
        (Requirement 2.3, Property 3).
    """

    reply: str
    tool_invoked: ToolName | None = None
    tool_result: dict[str, Any] | None = None
    tool_failed: bool = False


class MotoreConversazionale:
    """Orchestratore del dialogo (Requirement 2, 3). Dipendenze iniettate.

    Args:
        client: BedrockClient (fake nei test) che decide il turno.
        tool_executor: ToolExecutor (fake nei test) che esegue i tool.
        repository: repository con save/get della SESSION (InMemoryRepository nei
            test). Deve esporre get_session/save_session (vedi note sotto).
        clock: funzione che restituisce un tempo monotono in secondi; iniettabile
            per simulare il timeout nei test (default time.monotonic).
        tool_timeout_s: soglia di timeout tool-use in secondi (default 10).

    Nota sul repository: il Motore usa due operazioni sulla SESSION,
    get_session(session_id) -> Session | None e save_session(Session). Se il
    repository non le espone, il Motore usa un piccolo store in-memory interno
    (utile nei test che iniettano un repo minimale). In produzione il
    DynamoRepository le implementera' come Get/PutItem sull'entita' SESSION.
    """

    def __init__(
        self,
        client: BedrockClient,
        tool_executor: ToolExecutor,
        repository: Any,
        clock: Callable[[], float] = time.monotonic,
        tool_timeout_s: float = TOOL_USE_TIMEOUT_S,
        contact_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._client = client
        self._tool_executor = tool_executor
        self._repository = repository
        self._clock = clock
        self._tool_timeout_s = tool_timeout_s
        # Factory del contact_id (Requirement 3.7): iniettabile cosi i test possono
        # forzare id deterministici; default a un id casuale univoco (uuid4).
        self._contact_id_factory = contact_id_factory or _default_contact_id

    # --- API pubblica (contratto testo-in/testo-out) ----------------------

    def handle_turn(self, session_id: str, user_text: str) -> TurnResult:
        """Elabora un turno di dialogo e restituisce il testo da comunicare.

        Flusso del turno:
          1. Carica lo stato SESSION (o ne crea uno nuovo).
          2. Chiede al client la decisione del turno (testo + eventuale tool).
          3. Se il turno pone una domanda di chiarimento oltre il limite di 3,
             la domanda NON viene posta (Requirement 2.6, Property 5).
          4. Se il turno richiede un dato obbligatorio: se i tentativi per quel
             dato sono esauriti (>=3) il dato e' registrato "non fornito" e il
             dialogo prosegue senza riproporlo (Requirement 3.3/3.4, Property 7).
          5. Se il turno richiede un tool, lo invoca con timeout 10s. Su
             fallimento/timeout: nessun dato parziale persistito, stato preservato,
             messaggio di indisponibilita' (Requirement 2.3, Property 3).
          6. Persiste lo stato SESSION aggiornato SOLO se il turno e' andato a buon
             fine (nessun fallimento tool-use).

        Args:
            session_id: identificativo della sessione di dialogo.
            user_text: testo dell'utente per questo turno.

        Returns:
            TurnResult con il testo di risposta e i metadati del turno.
        """
        # 1. Stato di partenza (snapshot per il ripristino su fallimento).
        session = self._load_session(session_id)
        state_before = _snapshot(session)

        # 2. Decisione del turno dal modello (nessuna azione ancora eseguita).
        decision = self._client.decide_turn(
            system_prompt=SYSTEM_PROMPT,
            tools=TOOL_SCHEMA,
            collected_data=dict(session.collected_data),
            user_text=user_text,
        )

        # 3-4. Applica i limiti di chiarimenti e di tentativi per-dato PRIMA di
        # eseguire azioni. Puo' sostituire la reply (es. limite chiarimenti
        # raggiunto) o marcare un dato come "non fornito".
        reply = self._apply_dialog_limits(session, decision)

        # 5. Se il turno richiede un tool, invocalo con timeout e fallback.
        if decision.tool is not None:
            try:
                result = self._invoke_tool_use(decision.tool, decision.tool_input)
            except ToolUseError as _tue:
                # Requirement 2.3 / Property 3: nessun dato parziale, stato
                # preservato, messaggio di indisponibilita'. NON salviamo la
                # SESSION: lo stato persistito resta quello antecedente.
                self._restore_session(session, state_before)
                logger.warning(
                    "tool-use fallito/timeout (tool=%s) session=%s: %s | stato preservato",
                    decision.tool.value,
                    session_id,
                    _tue,
                )
                return TurnResult(
                    reply=TOOL_UNAVAILABLE_MESSAGE,
                    tool_invoked=None,
                    tool_result=None,
                    tool_failed=True,
                )
            # Tool riuscito: applica gli effetti sullo stato (es. dati salvati).
            self._apply_tool_success(session, decision.tool, decision.tool_input)
            # 6. Persisti lo stato aggiornato (turno andato a buon fine).
            self._save_session(session)
            return TurnResult(
                reply=reply,
                tool_invoked=decision.tool,
                tool_result=result,
                tool_failed=False,
            )

        # Turno solo conversazionale: persisti lo stato aggiornato.
        self._save_session(session)
        return TurnResult(reply=reply, tool_invoked=None, tool_result=None)

    # --- Limiti di dialogo (Requirement 2.6, 3.3, 3.4) --------------------

    def _apply_dialog_limits(
        self, session: Session, decision: TurnDecision
    ) -> str:
        """Applica limite chiarimenti e tentativi per-dato; ritorna la reply.

        - Chiarimenti (Requirement 2.6, Property 5): se il turno e' una domanda di
          chiarimento e il contatore ha gia' raggiunto MAX_CLARIFYING_QUESTIONS,
          la domanda NON viene posta (reply neutra, contatore invariato). Altrimenti
          il contatore e' incrementato.
        - Tentativi per-dato (Requirement 3.3/3.4, Property 7): se il turno chiede
          un dato obbligatorio, incrementa il contatore per quel campo. Al
          raggiungimento di MAX_FIELD_ATTEMPTS il campo e' registrato "non fornito"
          e non deve piu essere richiesto (reply neutra: si prosegue).
        """
        field_attempts = self._field_attempts(session)

        # Tentativi per singolo dato obbligatorio (Requirement 3.3/3.4).
        if decision.asks_field is not None and decision.asks_field in REQUIRED_FIELDS:
            field_name = decision.asks_field
            # Se il dato risulta gia' presente e valorizzato, non e' un tentativo.
            already_present = bool(
                str(session.collected_data.get(field_name, "")).strip()
            ) and session.collected_data.get(field_name) != NON_FORNITO
            if not already_present:
                attempts = field_attempts.get(field_name, 0)
                if attempts >= MAX_FIELD_ATTEMPTS:
                    # Requirement 3.4: registra "non fornito" e prosegui.
                    session.collected_data[field_name] = NON_FORNITO
                    return (
                        f"Va bene, proseguiamo. Ho registrato "
                        f"'{field_name}' come non fornito."
                    )
                # Requirement 3.3: consuma un tentativo per questo dato.
                field_attempts[field_name] = attempts + 1

        # Domande di chiarimento (Requirement 2.6, Property 5).
        if decision.is_clarifying:
            if session.clarifying_questions >= MAX_CLARIFYING_QUESTIONS:
                # Limite raggiunto: non si pone un'altra domanda di chiarimento.
                return (
                    "Va bene, proseguiamo con le informazioni che ho raccolto."
                )
            session.clarifying_questions += 1

        return decision.reply

    # --- Tool-use con timeout (Requirement 2.2, 2.3) ----------------------

    def _invoke_tool_use(
        self, tool: ToolName, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Esegue il tool applicando il timeout di 10s (Requirement 2.2, 2.3).

        Misura il tempo trascorso con il clock iniettato. Se l'esecuzione solleva
        un'eccezione o il tempo trascorso supera la soglia, solleva ToolUseError:
        il chiamante ripristina lo stato e comunica indisponibilita' (Property 3).

        Nota: qui non si interrompe forzatamente un thread bloccante (nel percorso
        reale la Lambda/il client Bedrock impongono il proprio timeout di rete);
        si applica il contratto di timeout misurandone il superamento, cosi il
        ramo di fallback e' deterministicamente testabile senza attese reali.
        """
        start = self._clock()
        try:
            result = self._tool_executor.execute(tool, tool_input)
        except Exception as exc:  # noqa: BLE001 - qualunque errore -> fallback
            raise ToolUseError(
                f"esecuzione tool {tool.value} fallita: {exc}"
            ) from exc
        elapsed = self._clock() - start
        if elapsed > self._tool_timeout_s:
            raise ToolUseError(
                f"tool {tool.value} oltre il timeout di {self._tool_timeout_s}s "
                f"({elapsed:.1f}s)"
            )
        return result

    def _apply_tool_success(
        self, session: Session, tool: ToolName, tool_input: dict[str, Any]
    ) -> None:
        """Applica allo stato SESSION gli effetti di un tool andato a buon fine.

        In particolare:
          - salva_dati_paziente aggiorna collected_data con i campi forniti (solo
            qui i dati diventano parte dello stato: mai su fallimento, Property 3).
          - classifica_esito, quando l'esito e' RICHIAMERA o SOLO_INFO, registra
            un CONTACT su Anagrafica_Store per un eventuale richiamo futuro
            (Requirement 3.7). Su esito APPUNTAMENTO NON si registra alcun contatto
            (quel flusso passa dall'Assegnatore_Colloquio).
        """
        if tool is ToolName.SALVA_DATI_PAZIENTE:
            for key, value in tool_input.items():
                session.collected_data[key] = value
        elif tool is ToolName.CLASSIFICA_ESITO:
            self._register_contact_if_needed(session, tool_input)

    def _register_contact_if_needed(
        self, session: Session, tool_input: dict[str, Any]
    ) -> None:
        """Registra un CONTACT se l'esito classificato e' RICHIAMERA/SOLO_INFO.

        Requirement 3.7: IF l'esito e' (a) richiamera' oppure (b) solo informazioni,
        THEN l'Anagrafica_Store SHALL registrare il contatto per un eventuale
        richiamo futuro. Il follow-up automatico e' Out of Scope.

        L'esito e' risolto dal Classificatore_Esito (src.classificatore.classify)
        a partire dal segnale del tool-use, cosi la totalita' e il default
        RICHIAMERA restano coerenti (Requirement 2.4/2.5). Il contatto e' costruito
        dai dati anagrafici gia' raccolti (nome, telefono, dataChiamata) e persistito
        tramite repository.save_contact. Se il repository non espone save_contact
        (es. fake minimale della sola SESSION nei property test), la registrazione
        e' silenziosamente saltata: la logica resta backend-agnostic.
        """
        esito = classify(
            tool_input.get("esito"),
            clarifying_questions=session.clarifying_questions,
        )
        # Un CONTACT si registra solo per richiamo/solo-info (Requirement 3.7).
        if esito not in (Esito.RICHIAMERA, Esito.SOLO_INFO):
            return

        saver = getattr(self._repository, "save_contact", None)
        if not callable(saver):
            return

        data = session.collected_data
        contact = Contact(
            contact_id=self._contact_id_factory(),
            nome=str(data.get("nome", "")),
            telefono=str(data.get("telefono", "")),
            esito=esito,
            data_chiamata=str(data.get("dataChiamata", "")),
        )
        saver(contact)
        logger.info(
            "CONTACT registrato (esito=%s) session=%s",
            esito.value,
            session.session_id,
        )

    # --- Persistenza SESSION (repository o store interno) -----------------

    def _load_session(self, session_id: str) -> Session:
        """Carica la SESSION dal repository, o ne crea una nuova vuota."""
        getter = getattr(self._repository, "get_session", None)
        if callable(getter):
            existing = getter(session_id)
            if existing is not None:
                # Copia di lavoro: le mutazioni del turno (contatori, dati) non
                # toccano l'oggetto persistito finche' non si chiama save_session.
                # Cosi' su fallimento tool-use lo stato durevole resta identico
                # (Requirement 2.3, Property 3).
                return Session(
                    session_id=existing.session_id,
                    collected_data=copy.deepcopy(existing.collected_data),
                    clarifying_questions=existing.clarifying_questions,
                )
        else:
            store = self._internal_store()
            if session_id in store:
                return _snapshot_to_session(session_id, store[session_id])
        return Session(session_id=session_id)

    def _save_session(self, session: Session) -> None:
        """Persiste la SESSION nel repository, o nello store interno."""
        saver = getattr(self._repository, "save_session", None)
        if callable(saver):
            saver(session)
        else:
            self._internal_store()[session.session_id] = _snapshot(session)

    def _restore_session(self, session: Session, snapshot: dict[str, Any]) -> None:
        """Ripristina lo stato in-memory della SESSION allo snapshot dato.

        Usato su fallimento tool-use: garantisce che l'oggetto session in memoria
        torni identico a prima dell'invocazione. La SESSION persistita non viene
        toccata (non chiamiamo save), quindi lo stato durevole resta invariato.
        """
        session.collected_data = copy.deepcopy(snapshot["collected_data"])
        session.clarifying_questions = snapshot["clarifying_questions"]
        # Ripristina anche i contatori per-dato annessi allo stato.
        self._field_attempts(session).clear()
        self._field_attempts(session).update(
            copy.deepcopy(snapshot["field_attempts"])
        )

    def _field_attempts(self, session: Session) -> dict[str, int]:
        """Contatori per-dato dei tentativi, annessi al collected_data.

        Conservati sotto una chiave riservata di collected_data cosi viaggiano con
        la SESSION persistita (DynamoDB) senza cambiare lo schema del modello.
        """
        attempts = session.collected_data.get("_fieldAttempts")
        if not isinstance(attempts, dict):
            attempts = {}
            session.collected_data["_fieldAttempts"] = attempts
        return attempts

    def _internal_store(self) -> dict[str, dict[str, Any]]:
        """Store in-memory di fallback per repository senza get/save_session."""
        store = getattr(self, "_sessions_fallback", None)
        if store is None:
            store = {}
            self._sessions_fallback = store
        return store


# ---------------------------------------------------------------------------
# Helper puri per snapshot / ripristino stato
# ---------------------------------------------------------------------------

def _default_contact_id() -> str:
    """Genera un contact_id univoco (usato se non iniettato).

    Delegato a uuid4 per l'uso reale; nei test si inietta una factory
    deterministica tramite il parametro contact_id_factory del Motore.
    """
    import uuid

    return uuid.uuid4().hex


def _snapshot(session: Session) -> dict[str, Any]:
    """Copia profonda dello stato rilevante della SESSION (per il ripristino)."""
    collected = copy.deepcopy(session.collected_data)
    field_attempts = collected.get("_fieldAttempts", {})
    if not isinstance(field_attempts, dict):
        field_attempts = {}
    return {
        "collected_data": collected,
        "clarifying_questions": session.clarifying_questions,
        "field_attempts": copy.deepcopy(field_attempts),
    }


def _snapshot_to_session(session_id: str, snapshot: dict[str, Any]) -> Session:
    """Ricostruisce una Session da uno snapshot dello store interno."""
    return Session(
        session_id=session_id,
        collected_data=copy.deepcopy(snapshot["collected_data"]),
        clarifying_questions=snapshot["clarifying_questions"],
    )
