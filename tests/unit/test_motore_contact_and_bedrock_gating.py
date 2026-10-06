"""Unit test - registrazione contatto e Bedrock reale (gated) - Task 14.2.

Copre due concern del Motore_Conversazionale, entrambi a COSTO ZERO (nessuna rete,
nessun Bedrock reale, fake in-memory):

  1. Registrazione CONTACT (Requirement 3.7). Quando il tool-use `classifica_esito`
     va a buon fine con esito RICHIAMERA o SOLO_INFO, il Motore registra un CONTACT
     su Anagrafica_Store (repository.save_contact) per un eventuale richiamo futuro.
     Su esito APPUNTAMENTO (e su altri tool) NON viene registrato alcun contatto.
     L'esito e' risolto dal Classificatore_Esito, quindi un segnale incerto/None
     ricade nel default RICHIAMERA e registra comunque il contatto (Requirement
     2.5). Il follow-up automatico e' Out of Scope.

  2. Gating Bedrock (Requirement 10.2) e limite di 10 invocazioni per sessione di
     test a pagamento (Requirement 10.3). Di DEFAULT il thin handler
     lambdas/motore_conversazionale.py costruisce un client MOCK che non chiama
     Bedrock (costo zero); il percorso col modello REALE e' costruito solo se
     PAID_COMPONENTS_ENABLED == "true". Anche quando i componenti a pagamento sono
     attivi, una sessione di test non deve superare 10 invocazioni del modello:
     un guard iniettabile (PaidInvocationLimiter) rifiuta l'11a invocazione.

Nessun dato reale: nomi/telefoni sono finti. Nessuna chiamata AWS/Bedrock/rete.
I commenti usano ASCII.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.models import Contact, Esito, Session, contact_pk
from src.motore import (
    MAX_CLARIFYING_QUESTIONS,
    MotoreConversazionale,
    ToolName,
    TurnDecision,
)
from src.repository import InMemoryRepository


# ---------------------------------------------------------------------------
# Fake iniettabili
# ---------------------------------------------------------------------------

class RepoWithSession(InMemoryRepository):
    """InMemoryRepository esteso con get/save della SESSION.

    Rispecchia la semantica che il DynamoRepository esporra' per l'entita' SESSION,
    riusando lo store single-table dell'InMemoryRepository per i CONTACT (cosi il
    test puo' rileggerli). Aggiunge solo il minimo necessario al Motore.
    """

    def __init__(self) -> None:
        super().__init__()
        self._sessions: dict[str, Session] = {}

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def save_session(self, session: Session) -> None:
        self._sessions[session.session_id] = Session(
            session_id=session.session_id,
            collected_data=dict(session.collected_data),
            clarifying_questions=session.clarifying_questions,
        )

    def contacts(self) -> list[Contact]:
        """Rilegge i CONTACT persistiti nello store single-table."""
        out: list[Contact] = []
        for (pk, sk), item in self._items.items():
            if item.get("entity") == "CONTACT":
                out.append(
                    Contact(
                        contact_id=pk.split("#", 1)[1],
                        nome=item["nome"],
                        telefono=item["telefono"],
                        esito=Esito(item["esito"]),
                        data_chiamata=item["dataChiamata"],
                    )
                )
        return out


class OneShotClient:
    """BedrockClient fake: restituisce una singola TurnDecision (poi la ripete)."""

    def __init__(self, decision: TurnDecision) -> None:
        self._decision = decision

    def decide_turn(
        self,
        system_prompt: str,
        tools: list[dict[str, Any]],
        collected_data: dict[str, Any],
        user_text: str,
    ) -> TurnDecision:
        return self._decision


class OkExecutor:
    """ToolExecutor fake che ha sempre successo."""

    def execute(self, tool: ToolName, tool_input: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


def _make_motore(client, repo, contact_ids=None):
    """Costruisce il Motore con fake e clock fermo (nessun timeout)."""
    factory = None
    if contact_ids is not None:
        seq = iter(contact_ids)
        factory = lambda: next(seq)  # noqa: E731 - id deterministici nei test
    return MotoreConversazionale(
        client=client,
        tool_executor=OkExecutor(),
        repository=repo,
        clock=lambda: 0.0,
        contact_id_factory=factory,
    )


def _preload_patient_data(repo: RepoWithSession, session_id: str) -> None:
    """Pre-carica una SESSION con i dati anagrafici di un paziente (dati finti)."""
    repo.save_session(
        Session(
            session_id=session_id,
            collected_data={
                "nome": "Giulia Verdi",
                "telefono": "+39 333 2222222",
                "dataChiamata": "2026-01-20",
            },
        )
    )


# ---------------------------------------------------------------------------
# Requirement 3.7 - registrazione CONTACT su RICHIAMERA / SOLO_INFO
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("esito", [Esito.RICHIAMERA, Esito.SOLO_INFO])
def test_classifica_esito_richiamera_o_solo_info_registra_contact(esito: Esito) -> None:
    # Requirement 3.7: esito (a) richiamera' o (b) solo informazioni -> CONTACT.
    session_id = "s-contact"
    repo = RepoWithSession()
    _preload_patient_data(repo, session_id)

    decision = TurnDecision(
        reply="Va bene, la ricontattiamo.",
        tool=ToolName.CLASSIFICA_ESITO,
        tool_input={"esito": esito.value},
    )
    motore = _make_motore(OneShotClient(decision), repo, contact_ids=["c-1"])

    result = motore.handle_turn(session_id, "vorrei solo un'informazione")

    assert result.tool_failed is False
    assert result.tool_invoked is ToolName.CLASSIFICA_ESITO
    # Un CONTACT e' stato persistito su Anagrafica_Store.
    contatti = repo.contacts()
    assert len(contatti) == 1
    contatto = contatti[0]
    assert contatto.esito is esito
    assert contatto.nome == "Giulia Verdi"
    assert contatto.telefono == "+39 333 2222222"
    assert contatto.data_chiamata == "2026-01-20"
    # E' presente nello store con la chiave CONTACT attesa.
    assert (contact_pk("c-1"), "META") in repo._items


def test_classifica_esito_appuntamento_non_registra_contact() -> None:
    # Su esito APPUNTAMENTO NON si registra un contatto: quel flusso passa
    # dall'Assegnatore_Colloquio, non dalla registrazione contatto (Req 3.7).
    session_id = "s-appt"
    repo = RepoWithSession()
    _preload_patient_data(repo, session_id)

    decision = TurnDecision(
        reply="Le fisso un colloquio.",
        tool=ToolName.CLASSIFICA_ESITO,
        tool_input={"esito": Esito.APPUNTAMENTO.value},
    )
    motore = _make_motore(OneShotClient(decision), repo)

    motore.handle_turn(session_id, "vorrei un appuntamento")

    assert repo.contacts() == []


def test_esito_incerto_ricade_su_richiamera_e_registra_contact() -> None:
    # Requirement 2.5: segnale incerto/non riconosciuto -> default RICHIAMERA,
    # quindi si registra comunque un CONTACT (Requirement 3.7).
    session_id = "s-incerto"
    repo = RepoWithSession()
    _preload_patient_data(repo, session_id)

    decision = TurnDecision(
        reply="Ho capito.",
        tool=ToolName.CLASSIFICA_ESITO,
        tool_input={"esito": "boh-non-riconosciuto"},
    )
    motore = _make_motore(OneShotClient(decision), repo, contact_ids=["c-x"])

    motore.handle_turn(session_id, "testo ambiguo")

    contatti = repo.contacts()
    assert len(contatti) == 1
    assert contatti[0].esito is Esito.RICHIAMERA


def test_salva_dati_paziente_non_registra_contact() -> None:
    # Un tool diverso da classifica_esito non deve registrare contatti.
    session_id = "s-salva"
    repo = RepoWithSession()

    decision = TurnDecision(
        reply="Dati salvati.",
        tool=ToolName.SALVA_DATI_PAZIENTE,
        tool_input={"nome": "Mario Rossi", "telefono": "+39 333 1111111"},
    )
    motore = _make_motore(OneShotClient(decision), repo)

    motore.handle_turn(session_id, "mi chiamo Mario Rossi")

    assert repo.contacts() == []


def test_contact_saltato_se_repository_non_espone_save_contact() -> None:
    # Se il repository (fake della sola SESSION) non espone save_contact, la
    # registrazione e' silenziosamente saltata: la logica resta backend-agnostic.
    session_id = "s-nosave"

    class SessionOnlyRepo:
        def __init__(self) -> None:
            self._sessions: dict[str, Session] = {}

        def get_session(self, session_id: str) -> Session | None:
            return self._sessions.get(session_id)

        def save_session(self, session: Session) -> None:
            self._sessions[session.session_id] = session

    repo = SessionOnlyRepo()
    decision = TurnDecision(
        reply="Va bene.",
        tool=ToolName.CLASSIFICA_ESITO,
        tool_input={"esito": Esito.RICHIAMERA.value},
    )
    motore = MotoreConversazionale(
        client=OneShotClient(decision),
        tool_executor=OkExecutor(),
        repository=repo,
        clock=lambda: 0.0,
    )

    # Non deve sollevare: il turno va comunque a buon fine.
    result = motore.handle_turn(session_id, "richiamero'")
    assert result.tool_failed is False


# ---------------------------------------------------------------------------
# Requirement 10.2 - Bedrock reale gated; mock di default (costo zero)
# ---------------------------------------------------------------------------

def _import_handler_module():
    """Importa il thin handler lambdas/motore_conversazionale.py.

    E' fuori dal package src; lo si carica via importlib dal path del progetto.
    """
    import importlib.util
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    module_path = root / "lambdas" / "motore_conversazionale.py"
    spec = importlib.util.spec_from_file_location(
        "lambda_motore_conversazionale", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_bedrock_usa_mock_per_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Requirement 10.2: senza PAID_COMPONENTS_ENABLED il client e' il mock, che
    # non chiama Bedrock (costo zero) e risponde in modo conversazionale.
    handler_mod = _import_handler_module()
    monkeypatch.delenv(handler_mod.ENV_PAID_COMPONENTS, raising=False)

    client = handler_mod._build_bedrock_client()
    assert type(client).__name__ == "_MockBedrockClient"

    # Il mock decide un turno senza rete: reply testuale, nessun tool.
    decision = client.decide_turn(
        system_prompt="p", tools=[], collected_data={}, user_text="ciao"
    )
    assert decision.tool is None
    assert isinstance(decision.reply, str) and decision.reply


def test_bedrock_mock_anche_se_flag_non_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # Qualunque valore diverso da "true" (case-insensitive) mantiene il mock.
    handler_mod = _import_handler_module()
    for value in ["false", "False", "0", "yes", ""]:
        monkeypatch.setenv(handler_mod.ENV_PAID_COMPONENTS, value)
        client = handler_mod._build_bedrock_client()
        assert type(client).__name__ == "_MockBedrockClient", value


# ---------------------------------------------------------------------------
# Requirement 10.3 - max 10 invocazioni per sessione di test a pagamento
# ---------------------------------------------------------------------------

MAX_PAID_INVOCATIONS_PER_SESSION = 10


class PaidInvocationLimitExceeded(Exception):
    """Superato il limite di invocazioni a pagamento per sessione (Req 10.3)."""


class PaidInvocationLimiter:
    """Guard che avvolge un BedrockClient e conta le invocazioni del modello.

    Requirement 10.3: quando un componente a pagamento e' attivo, una sessione di
    test non deve superare MAX_PAID_INVOCATIONS_PER_SESSION invocazioni del modello.
    Al superamento del limite la (N+1)-esima invocazione e' rifiutata senza
    chiamare il client sottostante (nessun costo oltre soglia).
    """

    def __init__(
        self, inner, limit: int = MAX_PAID_INVOCATIONS_PER_SESSION
    ) -> None:
        self._inner = inner
        self._limit = limit
        self.invocations = 0

    def decide_turn(self, *args: Any, **kwargs: Any) -> TurnDecision:
        if self.invocations >= self._limit:
            raise PaidInvocationLimitExceeded(
                f"limite di {self._limit} invocazioni a pagamento per sessione "
                "raggiunto"
            )
        self.invocations += 1
        return self._inner.decide_turn(*args, **kwargs)


def test_paid_session_permette_fino_a_10_invocazioni() -> None:
    # Le prime 10 invocazioni passano; l'11a e' rifiutata (Requirement 10.3).
    inner = OneShotClient(TurnDecision(reply="ok", tool=None))
    limiter = PaidInvocationLimiter(inner)

    for _ in range(MAX_PAID_INVOCATIONS_PER_SESSION):
        limiter.decide_turn(
            system_prompt="p", tools=[], collected_data={}, user_text="x"
        )
    assert limiter.invocations == MAX_PAID_INVOCATIONS_PER_SESSION

    with pytest.raises(PaidInvocationLimitExceeded):
        limiter.decide_turn(
            system_prompt="p", tools=[], collected_data={}, user_text="x"
        )
    # Il contatore non supera il limite: l'11a non ha invocato il client.
    assert limiter.invocations == MAX_PAID_INVOCATIONS_PER_SESSION


def test_paid_limiter_gira_su_una_sessione_reale_del_motore() -> None:
    # Il limiter e' un BedrockClient a tutti gli effetti: il Motore ci gira sopra
    # e una sessione di test non supera 10 invocazioni del modello.
    session_id = "s-paid"
    repo = RepoWithSession()
    inner = OneShotClient(TurnDecision(reply="ok", tool=None))
    limiter = PaidInvocationLimiter(inner)
    motore = MotoreConversazionale(
        client=limiter,
        tool_executor=OkExecutor(),
        repository=repo,
        clock=lambda: 0.0,
    )

    for _ in range(MAX_PAID_INVOCATIONS_PER_SESSION):
        motore.handle_turn(session_id, "testo")

    assert limiter.invocations == MAX_PAID_INVOCATIONS_PER_SESSION
    with pytest.raises(PaidInvocationLimitExceeded):
        motore.handle_turn(session_id, "oltre soglia")
