"""Property test - invarianti del dialogo (Motore_Conversazionale) - Task 14.1.

Mappatura 1:1 con le proprieta del design (design.md - Correctness Properties):
  - Property 3: per qualunque invocazione tool-use che fallisce o supera il timeout
                di 10s, il Motore_Conversazionale non persiste alcun dato parziale e
                lo stato del dialogo resta invariato (identico a prima
                dell'invocazione), comunicando al paziente l'indisponibilita'
                temporanea (Requirement 2.3).
  - Property 5: per qualunque sequenza di turni, il numero di domande di
                chiarimento poste dal Motore non supera 3 per chiamata
                (Requirement 2.6).
  - Property 7: per qualunque dato anagrafico obbligatorio, il Motore lo richiede al
                massimo 3 volte; esauriti i tentativi lo registra "non fornito" e
                prosegue con i dati rimanenti (Requirement 3.3, 3.4).

Libreria: Hypothesis, >= 100 iterazioni per proprieta (qui 200). Il Motore e'
testato con un BedrockClient fake e un ToolExecutor fake (nessuna rete, nessun
Bedrock reale, costo zero) e un InMemoryRepository per lo stato SESSION. Il timeout
tool-use e' esercitato tramite un clock iniettato che avanza oltre la soglia, senza
attese reali. I commenti usano ASCII.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from src.models import Session
from src.motore import (
    MAX_CLARIFYING_QUESTIONS,
    MAX_FIELD_ATTEMPTS,
    NON_FORNITO,
    REQUIRED_FIELDS,
    TOOL_UNAVAILABLE_MESSAGE,
    TOOL_USE_TIMEOUT_S,
    BedrockClient,
    MotoreConversazionale,
    ToolName,
    TurnDecision,
    TurnResult,
)


# ---------------------------------------------------------------------------
# Fake iniettabili (nessuna rete, nessun Bedrock reale)
# ---------------------------------------------------------------------------

class SessionRepo:
    """Repository minimale della SESSION in-memory (get/save_session).

    Rispecchia la semantica che il DynamoRepository esporra' per l'entita' SESSION.
    Tiene traccia dei salvataggi per verificare che NON avvengano su fallimento.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self.save_calls = 0

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def save_session(self, session: Session) -> None:
        self.save_calls += 1
        # Copia difensiva: lo store persistito non deve mutare per riferimento.
        self._sessions[session.session_id] = Session(
            session_id=session.session_id,
            collected_data=dict(session.collected_data),
            clarifying_questions=session.clarifying_questions,
        )


class ScriptedClient:
    """BedrockClient fake: restituisce una decisione pre-programmata per turno."""

    def __init__(self, decisions: list[TurnDecision]) -> None:
        self._decisions = decisions
        self._i = 0

    def decide_turn(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        collected_data: dict[str, Any],
        user_text: str,
    ) -> TurnDecision:
        decision = self._decisions[min(self._i, len(self._decisions) - 1)]
        self._i += 1
        return decision


class OkExecutor:
    """ToolExecutor fake che ha sempre successo (tool eseguito, risultato ok)."""

    def __init__(self) -> None:
        self.calls: list[tuple[ToolName, dict[str, Any]]] = []

    def execute(self, tool: ToolName, tool_input: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(tool_input)))
        return {"ok": True}


class RaisingExecutor:
    """ToolExecutor fake che fallisce sempre (simula errore del tool)."""

    def execute(self, tool: ToolName, tool_input: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("tool non disponibile")


def _advancing_clock(delta: float):
    """Clock monotono che avanza di `delta` secondi ad ogni chiamata.

    Con delta > TOOL_USE_TIMEOUT_S il tempo trascorso tra start e fine
    dell'invocazione supera la soglia, cosi il timeout scatta in modo
    deterministico senza attese reali.
    """
    state = {"t": 0.0}

    def _clock() -> float:
        state["t"] += delta
        return state["t"]

    return _clock


def _make_motore(client: BedrockClient, executor, repo=None, clock=None):
    """Costruisce il Motore con i fake dati."""
    return MotoreConversazionale(
        client=client,
        tool_executor=executor,
        repository=repo if repo is not None else SessionRepo(),
        clock=clock if clock is not None else (lambda: 0.0),
    )


# ---------------------------------------------------------------------------
# Property 3: fallimento/timeout tool-use -> stato invariato, nessun dato parziale
# ---------------------------------------------------------------------------

# Strategia: uno stato SESSION iniziale arbitrario (dati gia' raccolti) su cui
# il tool-use fallisce; verifichiamo che lo stato NON cambi.
_field_value = st.text(min_size=1, max_size=20)
_collected = st.dictionaries(
    keys=st.sampled_from(list(REQUIRED_FIELDS)),
    values=_field_value,
    max_size=len(REQUIRED_FIELDS),
)
_tool = st.sampled_from(list(ToolName))
# tool_input "parziale" che, se venisse persistito erroneamente, cambierebbe stato.
_partial_input = st.dictionaries(
    keys=st.sampled_from(["nome", "eta", "telefono", "problema", "sede", "dataChiamata"]),
    values=st.text(min_size=1, max_size=15),
    max_size=4,
)
# Modalita' di fallimento: eccezione del tool oppure superamento del timeout.
_failure_mode = st.sampled_from(["raise", "timeout"])


# Feature: centralino-ai-studio-medico, Property 3: per qualunque invocazione tool-use che fallisce o supera il timeout di 10s, il Motore non persiste alcun dato parziale e lo stato del dialogo resta invariato, comunicando l'indisponibilita' temporanea (Requirement 2.3)
@settings(max_examples=200)
@given(
    initial=_collected,
    clarifying=st.integers(min_value=0, max_value=MAX_CLARIFYING_QUESTIONS),
    tool=_tool,
    tool_input=_partial_input,
    mode=_failure_mode,
)
def test_property3_tool_failure_preserves_state(
    initial: dict[str, str],
    clarifying: int,
    tool: ToolName,
    tool_input: dict[str, str],
    mode: str,
) -> None:
    session_id = "s-prop3"
    repo = SessionRepo()
    # Pre-carica uno stato SESSION persistito arbitrario.
    repo.save_session(
        Session(
            session_id=session_id,
            collected_data=dict(initial),
            clarifying_questions=clarifying,
        )
    )
    saves_before = repo.save_calls
    persisted_before = repo.get_session(session_id)
    data_before = dict(persisted_before.collected_data)
    clar_before = persisted_before.clarifying_questions

    # Il turno richiede un tool che fallira' (eccezione o timeout).
    decision = TurnDecision(reply="ok", tool=tool, tool_input=dict(tool_input))
    if mode == "raise":
        executor: Any = RaisingExecutor()
        clock = lambda: 0.0  # noqa: E731 - tempo fermo: fallira' per eccezione
    else:
        executor = OkExecutor()  # ha successo, ma il clock sfora il timeout
        clock = _advancing_clock(TOOL_USE_TIMEOUT_S + 1.0)

    motore = _make_motore(ScriptedClient([decision]), executor, repo=repo, clock=clock)
    result: TurnResult = motore.handle_turn(session_id, "testo utente")

    # Requirement 2.3: messaggio di indisponibilita', tool marcato fallito.
    assert result.tool_failed is True
    assert result.tool_invoked is None
    assert result.reply == TOOL_UNAVAILABLE_MESSAGE

    # Nessun salvataggio SESSION dopo il pre-caricamento: lo stato durevole resta.
    assert repo.save_calls == saves_before

    # Stato persistito INVARIATO: nessun dato parziale del tool_input e' entrato.
    persisted_after = repo.get_session(session_id)
    assert persisted_after.collected_data == data_before
    assert persisted_after.clarifying_questions == clar_before
    for key, value in tool_input.items():
        # Il dato parziale non deve essere stato scritto (a meno che fosse gia'
        # identico nello stato di partenza).
        if data_before.get(key) != value:
            assert persisted_after.collected_data.get(key) != value


# ---------------------------------------------------------------------------
# Property 5: max 3 domande di chiarimento per chiamata
# ---------------------------------------------------------------------------

# Sequenza di turni: ogni turno e' o una domanda di chiarimento o un turno neutro.
_turn_is_clarifying = st.booleans()
_turn_sequence = st.lists(_turn_is_clarifying, min_size=1, max_size=12)


# Feature: centralino-ai-studio-medico, Property 5: per qualunque sequenza di turni il numero di domande di chiarimento poste dal Motore non supera 3 per chiamata (Requirement 2.6)
@settings(max_examples=200)
@given(sequence=_turn_sequence)
def test_property5_max_three_clarifying_questions(sequence: list[bool]) -> None:
    session_id = "s-prop5"
    repo = SessionRepo()
    # Ogni turno: se clarifying, il client pone una domanda di chiarimento.
    decisions = [
        TurnDecision(
            reply=("Puo' chiarire?" if is_clar else "Ok."),
            tool=None,
            is_clarifying=is_clar,
        )
        for is_clar in sequence
    ]
    motore = _make_motore(ScriptedClient(decisions), OkExecutor(), repo=repo)

    clarifying_asked = 0
    for _ in sequence:
        result = motore.handle_turn(session_id, "testo")
        # Una domanda di chiarimento e' stata effettivamente posta solo se la reply
        # e' la domanda (non il messaggio di "proseguiamo" del limite raggiunto).
        if result.reply == "Puo' chiarire?":
            clarifying_asked += 1

    # Non piu' di 3 domande di chiarimento poste (Requirement 2.6).
    assert clarifying_asked <= MAX_CLARIFYING_QUESTIONS
    # Il contatore persistito riflette lo stesso limite.
    persisted = repo.get_session(session_id)
    assert persisted.clarifying_questions <= MAX_CLARIFYING_QUESTIONS


# ---------------------------------------------------------------------------
# Property 7: max 3 tentativi per dato obbligatorio, poi "non fornito" e prosegue
# ---------------------------------------------------------------------------

_required_field = st.sampled_from(list(REQUIRED_FIELDS))
_num_requests = st.integers(min_value=1, max_value=8)


# Feature: centralino-ai-studio-medico, Property 7: per qualunque dato anagrafico obbligatorio il Motore lo richiede al massimo 3 volte, poi lo registra "non fornito" e prosegue con i dati rimanenti (Requirement 3.3, 3.4)
@settings(max_examples=200)
@given(field_name=_required_field, num_requests=_num_requests)
def test_property7_max_three_attempts_then_non_fornito(
    field_name: str, num_requests: int
) -> None:
    session_id = "s-prop7"
    repo = SessionRepo()
    # Il paziente non fornisce mai il dato: il client continua a richiederlo.
    decision = TurnDecision(
        reply=f"Mi puo' dire {field_name}?",
        tool=None,
        is_clarifying=False,
        asks_field=field_name,
    )
    motore = _make_motore(
        ScriptedClient([decision]), OkExecutor(), repo=repo
    )

    for _ in range(num_requests):
        motore.handle_turn(session_id, "non lo dico")

    persisted = repo.get_session(session_id)
    attempts = persisted.collected_data.get("_fieldAttempts", {})
    # Il dato e' stato richiesto al massimo 3 volte (Requirement 3.3).
    assert attempts.get(field_name, 0) <= MAX_FIELD_ATTEMPTS

    if num_requests > MAX_FIELD_ATTEMPTS:
        # Esauriti i 3 tentativi il dato e' registrato "non fornito" (Req 3.4)...
        assert persisted.collected_data.get(field_name) == NON_FORNITO
        # ...e il conteggio dei tentativi non supera comunque la soglia.
        assert attempts.get(field_name, 0) == MAX_FIELD_ATTEMPTS


# Feature: centralino-ai-studio-medico, Property 7: per qualunque dato anagrafico obbligatorio il Motore lo richiede al massimo 3 volte, poi lo registra "non fornito" e prosegue con i dati rimanenti (Requirement 3.3, 3.4)
@settings(max_examples=200)
@given(field_name=_required_field)
def test_property7_marks_non_fornito_and_proceeds(field_name: str) -> None:
    # Dopo 3 richieste senza risposta, la 4a richiesta segna il dato "non fornito"
    # e prosegue (la reply non e' piu' la domanda del dato).
    session_id = "s-prop7b"
    repo = SessionRepo()
    ask = f"Mi puo' dire {field_name}?"
    decision = TurnDecision(reply=ask, tool=None, asks_field=field_name)
    motore = _make_motore(ScriptedClient([decision]), OkExecutor(), repo=repo)

    replies = [motore.handle_turn(session_id, "non lo dico").reply for _ in range(4)]

    # Le prime 3 richieste pongono la domanda; la 4a non ripropone il dato.
    assert replies[:MAX_FIELD_ATTEMPTS] == [ask] * MAX_FIELD_ATTEMPTS
    assert replies[MAX_FIELD_ATTEMPTS] != ask
    # Prosegue: il dato e' registrato "non fornito".
    persisted = repo.get_session(session_id)
    assert persisted.collected_data.get(field_name) == NON_FORNITO
