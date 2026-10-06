"""Property test - Classificatore_Esito: totalita' e default (Task 13.1).

Mappatura 1:1 con la proprieta del design (design.md - Correctness Properties):
  - Property 4: per qualunque input di chiamata, il Classificatore_Esito assegna
    ESATTAMENTE uno dei tre esiti {RICHIAMERA, SOLO_INFO, APPUNTAMENTO}; e per
    qualunque input classificato come incerto (inclusi i casi ancora ambigui dopo
    il limite di domande di chiarimento) l'esito assegnato e' RICHIAMERA.

Libreria: Hypothesis, >= 100 iterazioni per proprieta (qui 200). Il
Classificatore e' logica pura: nessuna dipendenza esterna, nessuna chiamata
AWS/Bedrock/rete, costo zero. Il "segnale grezzo" del tool-use e' generato
direttamente da Hypothesis (Esito certo, stringa qualsiasi, o None).

La proprieta e' verificata in due parti complementari:
  1. Totalita' (Requirement 2.4): per QUALUNQUE input il risultato e' uno dei tre
     valori dell'enum Esito. Essendo sempre un singolo Esito, "esattamente uno" e'
     garantito per costruzione del tipo di ritorno.
  2. Default su incerto (Requirement 2.5, 2.7): per qualunque input incerto
     (None, stringa non riconosciuta) e per qualunque numero di chiarimenti,
     l'esito e' RICHIAMERA.

I commenti usano ASCII.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.classificatore import (
    DEFAULT_ESITO,
    MAX_CLARIFYING_QUESTIONS,
    classify,
)
from src.models import Esito

# I tre esiti ammessi (Requirement 2.4). classify() deve restituire uno di questi.
ALL_ESITI = frozenset(Esito)

# Valori testuali che il tool-use potrebbe restituire e che corrispondono a un
# esito certo (case-insensitive, con spazi ai bordi: devono comunque essere
# riconosciuti dal Classificatore).
_recognized_str = st.sampled_from([e.value for e in Esito]).flatmap(
    lambda v: st.sampled_from([v, v.lower(), v.title(), f"  {v}  "])
)

# Segnale CERTO: un Esito, oppure una stringa che corrisponde a un esito.
_certain_signal = st.one_of(st.sampled_from(list(Esito)), _recognized_str)

# Stringhe NON riconosciute: qualunque testo che non corrisponde a un esito noto.
_ESITO_TOKENS = {e.value for e in Esito}
_unrecognized_str = st.text(max_size=40).filter(
    lambda s: s.strip().upper() not in _ESITO_TOKENS
)

# Segnale INCERTO: None oppure una stringa non riconosciuta.
_uncertain_signal = st.one_of(st.none(), _unrecognized_str)

# Segnale QUALSIASI: certo o incerto (copre l'intero spazio degli input).
_any_signal = st.one_of(_certain_signal, _uncertain_signal)

# Numero di domande di chiarimento: include 0, valori sotto e OLTRE la soglia
# (fino a 6 > MAX 3) e valori negativi (trattati come 0).
_clarifying_count = st.integers(min_value=-2, max_value=6)


# ---------------------------------------------------------------------------
# Property 4 - parte 1: totalita' (esattamente 1 esito su 3 per qualunque input)
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 4: per qualunque input di chiamata, il Classificatore_Esito assegna esattamente uno dei tre esiti {RICHIAMERA, SOLO_INFO, APPUNTAMENTO}; e per qualunque input classificato come incerto (inclusi i casi ancora ambigui dopo il limite di domande di chiarimento) l'esito assegnato e' RICHIAMERA
@settings(max_examples=200)
@given(signal=_any_signal, clarifying=_clarifying_count)
def test_property4_totality_exactly_one_of_three(signal, clarifying: int) -> None:
    esito = classify(signal, clarifying_questions=clarifying)

    # Il risultato e' un membro dell'enum Esito (uno dei tre esiti ammessi).
    assert isinstance(esito, Esito)
    # ...e appartiene esattamente all'insieme dei tre esiti (esattamente uno).
    assert esito in ALL_ESITI


# ---------------------------------------------------------------------------
# Property 4 - parte 2: default RICHIAMERA su incerto (anche oltre i chiarimenti)
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 4: per qualunque input di chiamata, il Classificatore_Esito assegna esattamente uno dei tre esiti {RICHIAMERA, SOLO_INFO, APPUNTAMENTO}; e per qualunque input classificato come incerto (inclusi i casi ancora ambigui dopo il limite di domande di chiarimento) l'esito assegnato e' RICHIAMERA
@settings(max_examples=200)
@given(signal=_uncertain_signal, clarifying=_clarifying_count)
def test_property4_uncertain_defaults_to_richiamera(signal, clarifying: int) -> None:
    esito = classify(signal, clarifying_questions=clarifying)

    # Incertezza (segnale assente o non riconosciuto) -> default RICHIAMERA,
    # indipendentemente dal numero di domande di chiarimento gia' poste
    # (Requirement 2.5 e 2.7, inclusi i casi oltre la soglia MAX).
    assert esito is Esito.RICHIAMERA
    assert esito is DEFAULT_ESITO


# ---------------------------------------------------------------------------
# Property 4 - parte 2 (rafforzata): ambiguita' residua DOPO i 3 chiarimenti
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 4: per qualunque input di chiamata, il Classificatore_Esito assegna esattamente uno dei tre esiti {RICHIAMERA, SOLO_INFO, APPUNTAMENTO}; e per qualunque input classificato come incerto (inclusi i casi ancora ambigui dopo il limite di domande di chiarimento) l'esito assegnato e' RICHIAMERA
@settings(max_examples=200)
@given(
    signal=_uncertain_signal,
    clarifying=st.integers(min_value=MAX_CLARIFYING_QUESTIONS, max_value=10),
)
def test_property4_ambiguous_after_clarifications_defaults(
    signal, clarifying: int
) -> None:
    # Chiarimenti esauriti (>= 3) e descrizione ancora ambigua -> RICHIAMERA
    # (Requirement 2.7).
    esito = classify(signal, clarifying_questions=clarifying)

    assert esito is Esito.RICHIAMERA
