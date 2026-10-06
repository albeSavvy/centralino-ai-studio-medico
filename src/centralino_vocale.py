"""Centralino_Vocale: logica PURA e testabile dello strato voce (Task 17).

Centralino AI Studio Medico / Idelia (Flusso A).

Questo modulo isola la LOGICA di controllo del Centralino_Vocale (Requirement 1)
in funzioni pure e deterministiche, cosi' da renderla testabile SENZA Amazon
Connect ne' servizi vocali reali (nessun costo, nessuna rete). Amazon Connect e i
prompt vocali/ASR/TTS sono definiti in IaC (CDK) e girano solo quando i componenti
a pagamento sono attivati; qui modelliamo solo le REGOLE che il contact flow deve
rispettare, in modo che i property test (Task 17.1) le verifichino a costo zero.

Cosa copre (design.md - Centralino_Vocale, Correctness Property 1 e 2):

  - Ri-prompt limitato (Requirement 1.4, Property 1): se il paziente resta in
    silenzio dopo il benvenuto o un prompt, il Centralino riproduce un nuovo
    prompt fino a un MASSIMO di 3 tentativi; raggiunta la soglia, termina la
    chiamata con un messaggio di chiusura. `plan_after_silence` decide l'azione a
    ogni silenzio; `run_silence_sequence` simula una sequenza di N silenzi e
    ritorna il numero di ri-prompt emessi e se la chiamata e' stata chiusa.

  - Bassa confidenza (Requirement 1.5, Property 2): se la trascrizione ha una
    confidenza SOTTO la soglia minima accettabile (o non e' riconosciuta),
    l'azione e' "invita a ripetere" mantenendo ATTIVA la chiamata; a confidenza
    >= soglia il flusso PROSEGUE. `evaluate_confidence` implementa questa regola.

  - Soglie temporali (Requirement 1.1 e 1.3): il benvenuto va riprodotto entro 3s
    dall'instaurazione della chiamata; la risposta sintetica entro 5s dal termine
    del parlato. `within_welcome_threshold` / `within_response_threshold`
    verificano una latenza (misurata, o simulata con mock) contro la soglia. Nei
    test la latenza e' iniettata da un clock/mock: nessuna attesa reale.

Costo zero: modulo puro Python, nessuna chiamata AWS/Connect/rete. Commenti ASCII.

Riferimento: design.md - "Centralino_Vocale", Correctness Property 1 e 2,
Requirement 1 (criteri 1, 3, 4, 5). tasks.md Task 17 / 17.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# --- Soglie di controllo (Requirement 1) ---

# Numero massimo di ri-prompt su silenzio prima di chiudere (Requirement 1.4).
MAX_REPROMPTS = 3

# Soglia minima di confidenza accettabile per la trascrizione (Requirement 1.5).
# Allineata alla nlu_confidence_threshold del bot Lex (design.md / stack Lex).
# Sotto questa soglia: "invita a ripetere", chiamata attiva. >= soglia: prosegue.
MIN_CONFIDENCE_THRESHOLD = 0.4

# Soglia temporale del messaggio di benvenuto in secondi (Requirement 1.1).
WELCOME_MAX_SECONDS = 3.0

# Soglia temporale della risposta sintetica in secondi (Requirement 1.3).
RESPONSE_MAX_SECONDS = 5.0


class VoiceAction(str, Enum):
    """Azioni che il Centralino_Vocale puo' intraprendere a un turno.

    - REPROMPT      -> riproduci un nuovo prompt vocale (silenzio, sotto soglia).
    - CLOSE_CALL    -> termina la chiamata con messaggio di chiusura (silenzi
                       esauriti, Requirement 1.4).
    - INVITE_REPEAT -> invita a ripetere mantenendo la chiamata attiva (bassa
                       confidenza, Requirement 1.5).
    - PROCEED       -> prosegui il flusso (trascrizione accettata, confidenza ok).
    """

    REPROMPT = "REPROMPT"
    CLOSE_CALL = "CLOSE_CALL"
    INVITE_REPEAT = "INVITE_REPEAT"
    PROCEED = "PROCEED"


@dataclass(frozen=True)
class ConfidenceDecision:
    """Esito della valutazione di confidenza (Requirement 1.5, Property 2)."""

    action: VoiceAction
    call_active: bool


@dataclass(frozen=True)
class SilenceOutcome:
    """Esito di una sequenza di silenzi (Requirement 1.4, Property 1).

    Attributi:
        reprompts: numero di ri-prompt effettivamente emessi (<= MAX_REPROMPTS).
        call_closed: True se la chiamata e' stata chiusa dopo l'ultimo tentativo.
    """

    reprompts: int
    call_closed: bool


def plan_after_silence(
    reprompts_so_far: int,
    *,
    max_reprompts: int = MAX_REPROMPTS,
) -> VoiceAction:
    """Decide l'azione dopo un SILENZIO del paziente (Requirement 1.4).

    Regola: finche' non sono stati esauriti i tentativi ammessi si riproduce un
    nuovo prompt (REPROMPT); raggiunta la soglia si chiude la chiamata con il
    messaggio di chiusura (CLOSE_CALL).

    Args:
        reprompts_so_far: quanti ri-prompt sono gia' stati emessi in questa
            chiamata (>= 0; valori negativi trattati come 0).
        max_reprompts: soglia massima di ri-prompt (default 3).

    Returns:
        VoiceAction.REPROMPT se restano tentativi, altrimenti
        VoiceAction.CLOSE_CALL.
    """
    already = max(0, reprompts_so_far)
    if already < max_reprompts:
        return VoiceAction.REPROMPT
    return VoiceAction.CLOSE_CALL


def run_silence_sequence(
    num_silences: int,
    *,
    max_reprompts: int = MAX_REPROMPTS,
) -> SilenceOutcome:
    """Simula una sequenza di N silenzi consecutivi (Requirement 1.4, Property 1).

    Modella il ciclo del contact flow: a ogni silenzio si consulta
    `plan_after_silence`. Se l'azione e' REPROMPT si incrementa il contatore e si
    resta in attesa; se e' CLOSE_CALL la chiamata termina e il ciclo si ferma
    (silenzi successivi non producono ulteriori prompt).

    Args:
        num_silences: numero di silenzi consecutivi del paziente (>= 0).
        max_reprompts: soglia massima di ri-prompt (default 3).

    Returns:
        SilenceOutcome con il numero di ri-prompt emessi (mai oltre
        max_reprompts) e se la chiamata e' stata chiusa.
    """
    reprompts = 0
    call_closed = False
    for _ in range(max(0, num_silences)):
        action = plan_after_silence(reprompts, max_reprompts=max_reprompts)
        if action is VoiceAction.REPROMPT:
            reprompts += 1
            continue
        # CLOSE_CALL: soglia raggiunta, si chiude e si esce dal ciclo.
        call_closed = True
        break
    return SilenceOutcome(reprompts=reprompts, call_closed=call_closed)


def evaluate_confidence(
    confidence: float | None,
    *,
    threshold: float = MIN_CONFIDENCE_THRESHOLD,
) -> ConfidenceDecision:
    """Valuta la confidenza della trascrizione (Requirement 1.5, Property 2).

    Regola: se la confidenza e' assente (None: trascrizione non riconosciuta) o
    STRETTAMENTE inferiore alla soglia minima accettabile, l'azione e'
    INVITE_REPEAT (segnala il mancato riconoscimento e invita a ripetere)
    mantenendo la chiamata ATTIVA. Se la confidenza e' >= soglia, il flusso
    PROSEGUE (PROCEED).

    Args:
        confidence: livello di confidenza della trascrizione in [0.0, 1.0], oppure
            None se la trascrizione non e' stata riconosciuta.
        threshold: soglia minima accettabile (default MIN_CONFIDENCE_THRESHOLD).

    Returns:
        ConfidenceDecision: action e stato della chiamata (call_active). Con bassa
        confidenza call_active resta True (Requirement 1.5).
    """
    if confidence is None or confidence < threshold:
        # Mancato riconoscimento o sotto soglia: invita a ripetere, chiamata viva.
        return ConfidenceDecision(action=VoiceAction.INVITE_REPEAT, call_active=True)
    # Confidenza sufficiente: prosegui il flusso.
    return ConfidenceDecision(action=VoiceAction.PROCEED, call_active=True)


def within_welcome_threshold(
    latency_seconds: float,
    *,
    max_seconds: float = WELCOME_MAX_SECONDS,
) -> bool:
    """True se il benvenuto e' riprodotto entro la soglia (Requirement 1.1).

    latency_seconds e' la latenza misurata (o simulata con un mock nei test) tra
    l'instaurazione della chiamata e l'inizio del messaggio di benvenuto. Ritorna
    True se e' <= max_seconds (default 3s).
    """
    return latency_seconds <= max_seconds


def within_response_threshold(
    latency_seconds: float,
    *,
    max_seconds: float = RESPONSE_MAX_SECONDS,
) -> bool:
    """True se la risposta sintetica e' entro la soglia (Requirement 1.3).

    latency_seconds e' la latenza misurata (o simulata con un mock nei test) tra
    il termine del parlato del paziente e l'inizio della risposta sintetica.
    Ritorna True se e' <= max_seconds (default 5s).
    """
    return latency_seconds <= max_seconds
