"""Property test - Centralino_Vocale: ri-prompt, bassa confidenza, latenza (Task 17.1).

Mappatura 1:1 con le proprieta del design (design.md - Correctness Properties):

  - Property 1 (Requirement 1.4): per qualunque sequenza di silenzi del paziente,
    il numero di ri-prompt emessi dal Centralino_Vocale non supera 3 e, raggiunta
    la soglia, la chiamata termina con il messaggio di chiusura.

  - Property 2 (Requirement 1.5): per qualunque valore di confidenza della
    trascrizione inferiore alla soglia minima, la decisione del Centralino_Vocale
    e' "invita a ripetere" e la chiamata resta attiva; per valori pari o superiori
    alla soglia il flusso prosegue.

In aggiunta (Requirement 1.1, 1.3): verifica delle SOGLIE TEMPORALI (benvenuto
<= 3s, risposta <= 5s) con latenze SIMULATE da un mock (nessun Connect/voce reale,
nessuna attesa reale): la latenza e' iniettata come valore, cosi' il test resta
puro e a costo zero.

Libreria: Hypothesis, >= 100 iterazioni per proprieta (qui 200). Il
Centralino_Vocale testato qui e' LOGICA PURA (src/centralino_vocale.py): nessuna
dipendenza esterna, nessuna chiamata AWS/Connect/rete, costo zero. Amazon Connect
non e' eseguibile nei test (a pagamento): modelliamo le sue REGOLE come funzioni
pure e le verifichiamo con generatori Hypothesis.

Commenti in ASCII.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.centralino_vocale import (
    MAX_REPROMPTS,
    MIN_CONFIDENCE_THRESHOLD,
    RESPONSE_MAX_SECONDS,
    WELCOME_MAX_SECONDS,
    VoiceAction,
    evaluate_confidence,
    plan_after_silence,
    run_silence_sequence,
    within_response_threshold,
    within_welcome_threshold,
)

# Numero di silenzi consecutivi del paziente: 0, sotto, esatti e OLTRE la soglia
# (fino a 10 > MAX 3), inclusi valori negativi (trattati come 0).
_silences = st.integers(min_value=-2, max_value=10)

# Confidenze SOTTO soglia (bassa confidenza): [0.0, soglia). include None
# (trascrizione non riconosciuta), che va trattato come bassa confidenza.
_low_confidence = st.one_of(
    st.none(),
    st.floats(
        min_value=0.0,
        max_value=MIN_CONFIDENCE_THRESHOLD,
        exclude_max=True,
        allow_nan=False,
        allow_infinity=False,
    ),
)

# Confidenze >= soglia (flusso prosegue): [soglia, 1.0].
_ok_confidence = st.floats(
    min_value=MIN_CONFIDENCE_THRESHOLD,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)

# Confidenza QUALSIASI in [0,1] o None (copre l'intero spazio degli input).
_any_confidence = st.one_of(
    st.none(),
    st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)

# Latenze non negative (secondi), simulate da mock: da 0 a 15s (copre entro e
# oltre le soglie di 3s e 5s).
_latency = st.floats(
    min_value=0.0, max_value=15.0, allow_nan=False, allow_infinity=False
)


# ---------------------------------------------------------------------------
# Property 1: ri-prompt <= 3 poi chiusura (Requirement 1.4)
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 1: per qualunque sequenza di silenzi del paziente, il numero di ri-prompt emessi dal Centralino_Vocale non supera 3 e, raggiunta la soglia, la chiamata termina con il messaggio di chiusura
@settings(max_examples=200)
@given(num_silences=_silences)
def test_property1_reprompt_limited_then_close(num_silences: int) -> None:
    outcome = run_silence_sequence(num_silences)

    # Il numero di ri-prompt non supera mai la soglia (Requirement 1.4).
    assert outcome.reprompts <= MAX_REPROMPTS
    assert outcome.reprompts >= 0

    normalized = max(0, num_silences)
    if normalized <= MAX_REPROMPTS:
        # Fino a 3 silenzi: un ri-prompt per silenzio, chiamata ancora aperta.
        assert outcome.reprompts == normalized
        assert outcome.call_closed is False
    else:
        # Oltre la soglia: emessi esattamente 3 ri-prompt, poi chiusura.
        assert outcome.reprompts == MAX_REPROMPTS
        assert outcome.call_closed is True


# Feature: centralino-ai-studio-medico, Property 1: per qualunque sequenza di silenzi del paziente, il numero di ri-prompt emessi dal Centralino_Vocale non supera 3 e, raggiunta la soglia, la chiamata termina con il messaggio di chiusura
@settings(max_examples=200)
@given(reprompts_so_far=st.integers(min_value=-2, max_value=10))
def test_property1_plan_reprompts_until_threshold_then_close(
    reprompts_so_far: int,
) -> None:
    action = plan_after_silence(reprompts_so_far)
    already = max(0, reprompts_so_far)
    if already < MAX_REPROMPTS:
        # Restano tentativi: si riproduce un nuovo prompt.
        assert action is VoiceAction.REPROMPT
    else:
        # Soglia raggiunta o superata: si chiude con il messaggio di chiusura.
        assert action is VoiceAction.CLOSE_CALL


# ---------------------------------------------------------------------------
# Property 2: bassa confidenza -> invita a ripetere, chiamata attiva (Req 1.5)
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 2: per qualunque valore di confidenza della trascrizione inferiore alla soglia minima, la decisione del Centralino_Vocale e' "invita a ripetere" e la chiamata resta attiva; per valori pari o superiori alla soglia il flusso prosegue
@settings(max_examples=200)
@given(confidence=_low_confidence)
def test_property2_low_confidence_invites_repeat_call_active(confidence) -> None:
    decision = evaluate_confidence(confidence)

    # Sotto soglia (o non riconosciuto): invita a ripetere...
    assert decision.action is VoiceAction.INVITE_REPEAT
    # ...e la chiamata resta ATTIVA (Requirement 1.5).
    assert decision.call_active is True


# Feature: centralino-ai-studio-medico, Property 2: per qualunque valore di confidenza della trascrizione inferiore alla soglia minima, la decisione del Centralino_Vocale e' "invita a ripetere" e la chiamata resta attiva; per valori pari o superiori alla soglia il flusso prosegue
@settings(max_examples=200)
@given(confidence=_ok_confidence)
def test_property2_sufficient_confidence_proceeds(confidence: float) -> None:
    decision = evaluate_confidence(confidence)

    # Confidenza >= soglia: il flusso prosegue, chiamata attiva.
    assert decision.action is VoiceAction.PROCEED
    assert decision.call_active is True


# Feature: centralino-ai-studio-medico, Property 2: per qualunque valore di confidenza della trascrizione inferiore alla soglia minima, la decisione del Centralino_Vocale e' "invita a ripetere" e la chiamata resta attiva; per valori pari o superiori alla soglia il flusso prosegue
@settings(max_examples=200)
@given(confidence=_any_confidence)
def test_property2_totality_call_never_dropped_on_confidence(confidence) -> None:
    # Per QUALUNQUE confidenza la decisione e' esattamente una delle due azioni
    # ammesse e la chiamata non viene mai chiusa per bassa confidenza (1.5).
    decision = evaluate_confidence(confidence)
    assert decision.action in (VoiceAction.INVITE_REPEAT, VoiceAction.PROCEED)
    assert decision.call_active is True

    below = confidence is None or confidence < MIN_CONFIDENCE_THRESHOLD
    if below:
        assert decision.action is VoiceAction.INVITE_REPEAT
    else:
        assert decision.action is VoiceAction.PROCEED


# ---------------------------------------------------------------------------
# Soglie temporali con latenza SIMULATA (Requirement 1.1, 1.3) - mock latency
# ---------------------------------------------------------------------------

@settings(max_examples=200)
@given(latency=_latency)
def test_welcome_threshold_3s_with_mock_latency(latency: float) -> None:
    # Latenza del benvenuto simulata da mock: dentro soglia sse <= 3s (Req 1.1).
    assert within_welcome_threshold(latency) == (latency <= WELCOME_MAX_SECONDS)


@settings(max_examples=200)
@given(latency=_latency)
def test_response_threshold_5s_with_mock_latency(latency: float) -> None:
    # Latenza della risposta simulata da mock: dentro soglia se <= 5s (Req 1.3).
    assert within_response_threshold(latency) == (latency <= RESPONSE_MAX_SECONDS)


def test_welcome_and_response_boundaries_exact() -> None:
    # Esempi ai bordi (mock di latenza): la soglia e' inclusiva.
    assert within_welcome_threshold(3.0) is True
    assert within_welcome_threshold(3.01) is False
    assert within_response_threshold(5.0) is True
    assert within_response_threshold(5.01) is False


def test_response_latency_simulated_via_clock_mock() -> None:
    """Verifica la soglia risposta misurando una latenza da un CLOCK mockato.

    Nessuna attesa reale: il clock e' una lista di timestamp che il mock
    restituisce a ogni chiamata, cosi' la latenza (end - start) e' deterministica.
    Simula "termine parlato" -> "inizio risposta sintetica" a 4.2s (entro 5s).
    """
    timestamps = iter([100.0, 104.2])  # start, end -> latenza 4.2s
    mock_clock = lambda: next(timestamps)

    start = mock_clock()
    end = mock_clock()
    measured = end - start

    assert measured == pytest.approx(4.2)
    assert within_response_threshold(measured) is True
