"""Classificatore_Esito: assegnazione dell'esito di chiamata (logica pura).

Centralino AI Studio Medico / Idelia (Flusso A) - Task 13.

Questo modulo contiene la logica PURA e testabile del Classificatore_Esito
(Requirement 2, criteri 4, 5 e 7). Il Classificatore assegna a OGNI chiamata
esattamente uno dei tre esiti dell'enum src.models.Esito:

  - RICHIAMERA    -> (a) il paziente richiamera' (esito di DEFAULT)
  - SOLO_INFO     -> (b) il paziente voleva solo informazioni
  - APPUNTAMENTO  -> (c) il paziente richiede un appuntamento

Semantica (design.md - Classificatore_Esito, Correctness Property 4):
  - Totalita' (Requirement 2.4): per QUALUNQUE input viene assegnato esattamente
    uno dei tre esiti. La funzione e' totale: non solleva mai per "nessun esito"
    e non puo' restituire un valore fuori dall'enum.
  - Default su incertezza (Requirement 2.5): se il Classificatore non e' in grado
    di assegnare CON CERTEZZA uno dei tre esiti, assegna l'esito di default
    RICHIAMERA.
  - Default dopo 3 chiarimenti (Requirement 2.7): se dopo il numero massimo di
    domande di chiarimento (3) la descrizione resta ambigua/incompleta, l'esito
    e' comunque RICHIAMERA (l'incertezza residua ricade nel default).

Perche' e' "logica pura testabile senza Bedrock reale":
  Il Motore_Conversazionale (Task 14) alimentera' questa funzione con l'output del
  tool-use `classifica_esito` del modello su Bedrock. Quel tool-use e' vincolato
  da uno schema (design.md - Tool-use schema) a produrre uno dei tre valori enum
  {RICHIAMERA, SOLO_INFO, APPUNTAMENTO}; ma il modello puo' anche NON emettere il
  tool (incertezza), emettere un valore non riconosciuto, o esaurire i 3
  chiarimenti senza chiarezza. Questa funzione mappa quel "segnale grezzo" (di
  tipo str | Esito | None, potenzialmente incerto) sull'esito finale, applicando
  la totalita' e il default. Non chiama Bedrock: riceve gia' il segnale come
  argomento, quindi e' testabile a costo zero con qualunque input (Property 4).

Costo zero: modulo puro Python, nessuna chiamata AWS/Bedrock/rete.

Riferimento: design.md sezione "Classificatore_Esito", Correctness Property 4,
Requirement 2 (criteri 4, 5, 7). L'enum degli esiti e' src.models.Esito, allineato
1:1 all'enum del tool-use `classifica_esito` in design.md - Tool-use schema.
"""

from __future__ import annotations

import logging

from src.models import Esito

logger = logging.getLogger(__name__)

# Numero massimo di domande di chiarimento per chiamata (Requirement 2.6, 2.7).
MAX_CLARIFYING_QUESTIONS = 3

# Esito di default assegnato in caso di incertezza o ambiguita' residua
# (Requirement 2.5, 2.7): (a) il paziente richiamera'.
DEFAULT_ESITO = Esito.RICHIAMERA

# Insieme canonico dei tre esiti ammessi (Requirement 2.4). Usato per validare il
# segnale grezzo proveniente dal tool-use del modello.
VALID_ESITI = frozenset(Esito)


def classify(
    signal: "Esito | str | None",
    clarifying_questions: int = 0,
    *,
    max_clarifying_questions: int = MAX_CLARIFYING_QUESTIONS,
) -> Esito:
    """Assegna l'esito della chiamata a partire dal segnale del Classificatore.

    Funzione TOTALE (Requirement 2.4, Property 4): per qualunque input restituisce
    esattamente uno dei tre valori di src.models.Esito. Applica il default
    RICHIAMERA su incertezza (Requirement 2.5) e dopo il limite di domande di
    chiarimento (Requirement 2.7).

    Il segnale grezzo (signal) rappresenta cio' che il tool-use `classifica_esito`
    del modello su Bedrock ha prodotto per questa chiamata:
      - Esito             -> classificazione certa: si assume quell'esito.
      - str               -> valore testuale: accettato SOLO se corrisponde
                             (case-insensitive, senza spazi ai bordi) a uno dei
                             tre esiti; qualunque altra stringa e' trattata come
                             incerta -> default.
      - None              -> nessuna classificazione (il modello non ha emesso il
                             tool o e' incerto) -> default.

    Regola del limite di chiarimenti (Requirement 2.7, ha PRECEDENZA sul segnale):
    se sono state gia' poste tutte le domande di chiarimento ammesse
    (clarifying_questions >= max_clarifying_questions) e il segnale NON e' una
    classificazione certa e riconosciuta, l'esito e' il default RICHIAMERA. Un
    segnale certo e riconosciuto viene invece rispettato anche a limite raggiunto:
    il default scatta per l'AMBIGUITA' residua (Requirement 2.7), non per il solo
    fatto di aver esaurito i chiarimenti.

    Args:
        signal: Segnale del Classificatore (Esito certo, stringa dal tool-use, o
            None se assente/incerto).
        clarifying_questions: Numero di domande di chiarimento gia' poste in questa
            chiamata (default 0). Valori negativi sono trattati come 0.
        max_clarifying_questions: Soglia massima di domande di chiarimento
            (default 3, Requirement 2.6/2.7).

    Returns:
        Esattamente uno tra Esito.RICHIAMERA, Esito.SOLO_INFO, Esito.APPUNTAMENTO.
    """
    resolved = _resolve_signal(signal)

    # Segnale incerto/non riconosciuto -> default RICHIAMERA (Requirement 2.5).
    if resolved is None:
        logger.info(
            "esito incerto (segnale=%r): default %s",
            signal,
            DEFAULT_ESITO.value,
        )
        return DEFAULT_ESITO

    # Requirement 2.7: se i chiarimenti sono esauriti e la descrizione resta
    # ambigua, l'esito e' il default. Qui resolved e' certo e riconosciuto, quindi
    # l'ambiguita' residua non sussiste: si rispetta l'esito certo. Il ramo
    # "ambiguo dopo N chiarimenti" e' catturato da resolved is None sopra (il
    # Motore passa signal=None quando resta ambiguo dopo i chiarimenti).
    return resolved


def _resolve_signal(signal: "Esito | str | None") -> Esito | None:
    """Normalizza il segnale grezzo in un Esito certo, o None se non riconosciuto.

    Accetta un Esito cosi' com'e'; una stringa solo se corrisponde (case-insensitive,
    trim) a uno dei tre esiti; None e qualunque altro valore -> None (incerto).
    """
    if isinstance(signal, Esito):
        return signal
    if isinstance(signal, str):
        candidate = signal.strip().upper()
        for esito in Esito:
            if esito.value == candidate:
                return esito
        return None
    return None
