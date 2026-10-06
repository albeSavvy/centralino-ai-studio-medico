"""Property test - rendering recap Telegram (Task 9.1).

Mappatura 1:1 con la proprieta del design (design.md - Correctness Properties):
  - Property 17: per qualunque appuntamento in Stato_Provvisorio, il recap
    contiene nome del paziente, data/ora e dottoressa, e include ESATTAMENTE le
    due azioni mutuamente esclusive "Conferma" e "Segnala blocker". Inoltre il
    campo "problema" (potenzialmente sensibile) NON compare mai nel recap.

Libreria: Hypothesis, >= 100 iterazioni (qui 200). Nessuna dipendenza AWS/rete:
il rendering e logica pura. Non viene mai costruito ne loggato alcun token: il
test esercita solo build_recap_text / build_inline_keyboard. I commenti usano
ASCII.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from src.models import ApptStatus, Appointment
from src.telegram import (
    ACTION_BLOCKER,
    ACTION_CONFERMA,
    LABEL_BLOCKER,
    LABEL_CONFERMA,
    build_callback_data,
    build_inline_keyboard,
    build_recap_text,
    parse_callback_data,
)

_TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 1, 20, 9, 0, tzinfo=_TZ)

DOCTORS = ["Chiara", "Francesca"]

# Generatori del dominio.
# Nome paziente: lettere miste (a-z, A-Z) senza underscore, cosi non puo contenere
# per caso il marcatore univoco del "problema" (che include "_").
_name = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll"), max_codepoint=122
    ),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")
_doctor = st.sampled_from(DOCTORS)
_appointment_id = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")
_offset_min = st.integers(min_value=0, max_value=14 * 24 * 60)
# Un "problema" arbitrario, potenzialmente sensibile: deve NON comparire nel recap.
# Usa un marcatore univoco riconoscibile (prefisso PROBLEMA_) che non puo comparire
# per caso negli altri campi (nome, dottoressa, data/ora, etichette fisse): cosi il
# test verifica la NON-inclusione del problema come campo, non una coincidenza di
# sottostringhe con testo legittimo del recap.
_PROBLEMA_MARKER = "PROBLEMA_"
_problema = st.text(
    alphabet=st.characters(min_codepoint=65, max_codepoint=90),  # A-Z
    min_size=1,
    max_size=60,
).map(lambda s: _PROBLEMA_MARKER + s)


def _make_appointment(
    appointment_id: str,
    nome_paziente: str,
    doctor: str,
    offset: int,
    problema: str,
) -> Appointment:
    """Costruisce un appuntamento in Stato_Provvisorio per il rendering del recap."""
    return Appointment(
        appointment_id=appointment_id,
        client_no="000001",
        nome_paziente=nome_paziente,
        doctor=doctor,
        sede="Meda",
        start=(NOW + timedelta(minutes=offset)).isoformat(),
        duration_min=30,
        status=ApptStatus.PROVVISORIO,
    )


# ---------------------------------------------------------------------------
# Property 17: rendering completo del recap + due azioni mutuamente esclusive
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 17: per qualunque appuntamento in Stato_Provvisorio il recap contiene nome, data e ora e dottoressa, e include esattamente le due azioni mutuamente esclusive "Conferma" e "Segnala blocker"
@settings(max_examples=200)
@given(
    appointment_id=_appointment_id,
    nome_paziente=_name,
    doctor=_doctor,
    offset=_offset_min,
    problema=_problema,
)
def test_property17_recap_content_and_exclusive_actions(
    appointment_id: str,
    nome_paziente: str,
    doctor: str,
    offset: int,
    problema: str,
) -> None:
    appointment = _make_appointment(
        appointment_id, nome_paziente, doctor, offset, problema
    )

    recap = build_recap_text(appointment)
    keyboard = build_inline_keyboard(appointment)

    # Il recap contiene il nome del paziente e la dottoressa.
    assert nome_paziente in recap
    assert doctor in recap
    # Il recap contiene la data e l'ora (formato dd/mm/YYYY HH:MM derivato da start).
    quando = datetime.fromisoformat(appointment.start).strftime("%d/%m/%Y %H:%M")
    assert quando in recap

    # Privacy: il campo "problema" NON compare mai nel recap.
    assert problema not in recap

    # La tastiera ha ESATTAMENTE due bottoni (una sola riga con due azioni).
    rows = keyboard["inline_keyboard"]
    buttons = [button for row in rows for button in row]
    assert len(buttons) == 2

    # Le due azioni sono esattamente "Conferma" e "Segnala blocker" (mutuamente
    # esclusive: etichette e callback_data distinti).
    labels = {button["text"] for button in buttons}
    assert labels == {LABEL_CONFERMA, LABEL_BLOCKER}

    callbacks = [button["callback_data"] for button in buttons]
    assert len(set(callbacks)) == 2  # callback_data distinti tra le due azioni

    # Ogni callback_data codifica (azione, appointment_id) ed e ricodificabile.
    decoded_actions = {parse_callback_data(cb)[0] for cb in callbacks}
    assert decoded_actions == {ACTION_CONFERMA, ACTION_BLOCKER}
    for cb in callbacks:
        action, decoded_id = parse_callback_data(cb)
        assert decoded_id == appointment_id
        assert cb == build_callback_data(action, appointment_id)
