"""Property test - transizione di stato del telegram-callback (Task 10.1).

Mappatura 1:1 con la proprieta del design (design.md - Correctness Properties):
  - Property 18: per qualunque appuntamento in Stato_Provvisorio e qualunque
    sequenza di azioni ricevute entro la finestra di 8 ore, lo stato finale e'
    determinato ESCLUSIVAMENTE dalla prima azione in ordine cronologico
    (Conferma -> confermato, Segnala blocker -> da rivedere) e le azioni
    successive NON modificano lo stato; se nessuna azione e' ricevuta entro 8 ore
    lo stato finale e' confermato automaticamente (CONFERMATO_AUTO).

Cio' valida la logica del telegram-callback (mutua esclusione via conditional
write, Requirement 6.3/6.4/6.5/6.6) esercitata su src.telegram.apply_callback_action
+ resolve_confirmation_timeout sopra il repository in-memory (stessa semantica di
conditional write del backend Dynamo).

Libreria: Hypothesis, >= 100 iterazioni (qui 200). Backend in-memory: nessuna
chiamata AWS/rete, costo zero. I commenti usano ASCII.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.models import Appointment, ApptStatus
from src.repository import InMemoryRepository
from src.telegram import (
    ACTION_BLOCKER,
    ACTION_CONFERMA,
    ACTIONS,
    action_to_status,
    apply_callback_action,
    resolve_confirmation_timeout,
)

APPOINTMENT_ID = "appt-0001"

# Sequenza di azioni ricevute entro le 8 ore, in ORDINE CRONOLOGICO. Puo' essere
# vuota (nessuna azione -> deve scattare la conferma automatica). Le azioni sono
# solo le due ammesse, in qualunque ordine e con ripetizioni (i callback Telegram
# possono ripetersi: la mutua esclusione deve renderli innocui).
_actions_sequence = st.lists(st.sampled_from(ACTIONS), min_size=0, max_size=8)

# Se scade il timeout della finestra prima di ricevere (altre) azioni.
_timeout_fires = st.booleans()


def _fresh_provvisorio_repo() -> InMemoryRepository:
    """Repository con un singolo appuntamento in Stato_Provvisorio."""
    repo = InMemoryRepository()
    repo.save_appointment(
        Appointment(
            appointment_id=APPOINTMENT_ID,
            client_no="000001",
            nome_paziente="Mario Rossi",
            doctor="Chiara",
            sede="Meda",
            start="2026-01-20T09:00:00+01:00",
            duration_min=30,
            status=ApptStatus.PROVVISORIO,
        )
    )
    return repo


# ---------------------------------------------------------------------------
# Property 18: la prima azione cronologica determina lo stato finale;
# le successive sono ignorate; nessuna azione entro 8h -> confermato automatico.
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 18: per qualunque appuntamento in Stato_Provvisorio e qualunque sequenza di azioni entro 8 ore, lo stato finale e' determinato solo dalla prima azione cronologica (Conferma -> confermato, Segnala blocker -> da rivedere) e le azioni successive non lo modificano; se nessuna azione arriva entro 8 ore lo stato finale e' confermato automaticamente
@settings(max_examples=200)
@given(actions=_actions_sequence, timeout_fires=_timeout_fires)
def test_property18_first_action_determines_final_state(
    actions: list[str],
    timeout_fires: bool,
) -> None:
    repo = _fresh_provvisorio_repo()

    # Stato atteso in base alla PRIMA azione cronologica (se esiste).
    first_action = actions[0] if actions else None

    # Applica le azioni nell'ordine cronologico ricevuto. Solo la prima deve
    # vincere (transizionare lo stato); tutte le successive devono essere ignorate.
    won_indices = [
        i
        for i, action in enumerate(actions)
        if apply_callback_action(repo, APPOINTMENT_ID, action)
    ]
    if first_action is None:
        assert won_indices == []  # nessuna azione, nessuna transizione
    else:
        # Esattamente una azione vince, ed e' la prima in ordine cronologico.
        assert won_indices == [0]

    # Il timeout della finestra puo' scattare: marca CONFERMATO_AUTO solo se lo
    # stato e' ancora PROVVISORIO (nessuna azione ha determinato l'esito).
    timeout_applied = False
    if timeout_fires:
        timeout_applied = resolve_confirmation_timeout(repo, APPOINTMENT_ID)

    final = repo.get_appointment(APPOINTMENT_ID)
    assert final is not None

    if first_action is not None:
        # Lo stato finale dipende SOLO dalla prima azione, mai dalle successive
        # ne' dal timeout (la conferma esplicita disattiva la conferma automatica).
        assert final.status == action_to_status(first_action)
        assert timeout_applied is False
    elif timeout_fires:
        # Nessuna azione entro le 8h + timeout scaduto -> confermato automatico.
        assert final.status == ApptStatus.CONFERMATO_AUTO
        assert timeout_applied is True
    else:
        # Nessuna azione e finestra non ancora scaduta: resta provvisorio.
        assert final.status == ApptStatus.PROVVISORIO


# Feature: centralino-ai-studio-medico, Property 18: per qualunque appuntamento in Stato_Provvisorio e qualunque sequenza di azioni entro 8 ore, lo stato finale e' determinato solo dalla prima azione cronologica (Conferma -> confermato, Segnala blocker -> da rivedere) e le azioni successive non lo modificano; se nessuna azione arriva entro 8 ore lo stato finale e' confermato automaticamente
@settings(max_examples=200)
@given(
    first=st.sampled_from(ACTIONS),
    later=st.lists(st.sampled_from(ACTIONS), min_size=1, max_size=8),
)
def test_property18_conflicting_actions_first_wins(
    first: str,
    later: list[str],
) -> None:
    """Conflitto esplicito: dopo una prima azione, qualsiasi mix successivo
    (inclusa l'azione opposta) non cambia l'esito fissato dalla prima."""
    repo = _fresh_provvisorio_repo()

    assert apply_callback_action(repo, APPOINTMENT_ID, first) is True
    expected = action_to_status(first)

    for action in later:
        # Ogni azione successiva e' ignorata: la condizione fallisce.
        assert apply_callback_action(repo, APPOINTMENT_ID, action) is False
        assert repo.get_appointment(APPOINTMENT_ID).status == expected

    # Anche un eventuale timeout dopo un'azione non declassa/altera l'esito.
    assert resolve_confirmation_timeout(repo, APPOINTMENT_ID) is False
    assert repo.get_appointment(APPOINTMENT_ID).status == expected


def test_action_mapping_is_exhaustive_and_exclusive() -> None:
    """Le due azioni mappano su stati distinti e mutuamente esclusivi."""
    assert action_to_status(ACTION_CONFERMA) == ApptStatus.CONFERMATO
    assert action_to_status(ACTION_BLOCKER) == ApptStatus.DA_RIVEDERE
    assert action_to_status(ACTION_CONFERMA) != action_to_status(ACTION_BLOCKER)
