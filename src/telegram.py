"""Notificatore_Telegram: rendering recap + azioni inline e invio via Bot API.

Centralino AI Studio Medico / Idelia (Flusso A) - Task 9.

Questo modulo contiene la logica PURA e testabile del Notificatore_Telegram
(Requirement 6, criteri 1 e 2):

  - build_recap_text(appointment): costruisce il testo del recap con nome del
    paziente, data e ora dell'appuntamento e dottoressa assegnata. Il campo
    "problema" NON e mai incluso nel recap (regola di privacy, design.md -
    Sicurezza: il campo problema non viene inviato al gruppo Telegram).
  - build_inline_keyboard(appointment): costruisce ESATTAMENTE due bottoni inline
    mutuamente esclusivi ("Conferma" / "Segnala blocker"), ciascuno con un
    callback_data distinto che codifica appointment_id + azione, cosi la Lambda
    telegram-callback (Task 10) puo distinguere le due azioni.
  - TelegramNotifier: servizio che assembla il payload sendMessage (chat_id del
    gruppo dottoresse, text=recap, reply_markup=tastiera inline) e lo invia
    tramite un sender INIETTATO (Telegram Bot API). Il token del bot e ottenuto
    da un secret_loader INIETTATO (Secrets Manager a runtime) e non viene mai
    loggato ne incluso nel recap/tastiera.

Secret safety (regola di workspace aws-agent-rules):
  - Il token del bot vive in AWS Secrets Manager e viene letto A RUNTIME tramite
    il callable secret_loader iniettato. Nei test si inietta un loader fittizio:
    nessuna chiamata secretsmanager get-secret-value nei percorsi coperti dai
    test.
  - Il valore del token NON e mai hardcoded, loggato o incluso nel recap, nella
    tastiera o in qualunque dato/report di test.
  - La skill aws-secrets-manager non e installata in locale: i principi di secret
    safety sono applicati manualmente (segreto per nome/ARN via configurazione,
    valore confinato al callable iniettato, mai in contesto).

Costo zero: modulo puro Python. Nei test si iniettano sender fake e
secret_loader fittizio, quindi nessuna chiamata reale Telegram/HTTP/Secrets/rete.
Il percorso reale (boto3 Secrets Manager + HTTP verso Bot API) e marcato
# pragma: no cover e vive nel thin handler Lambda (lambdas/notificatore_telegram.py).

Riferimento: design.md sezione "Notificatore_Telegram", Correctness Property 17,
Requirement 6 (criteri 1 e 2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from src.models import Appointment, ApptStatus

if TYPE_CHECKING:  # pragma: no cover - solo per type hints, nessun import a runtime
    from src.repository import AnagraficaRepository

logger = logging.getLogger(__name__)

# Etichette UI dei due bottoni (testo mostrato alla dottoressa). Sono UI text, non
# segreti: nessuna emoji per rispettare la convenzione di progetto.
LABEL_CONFERMA = "Conferma"
LABEL_BLOCKER = "Segnala blocker"

# Identificatori stabili delle due azioni mutuamente esclusive. La Lambda
# telegram-callback (Task 10) li usa per distinguere l'azione ricevuta.
ACTION_CONFERMA = "conferma"
ACTION_BLOCKER = "blocker"

# Insieme canonico delle azioni ammesse (esattamente due, mutuamente esclusive).
ACTIONS = (ACTION_CONFERMA, ACTION_BLOCKER)

# Separatore usato per codificare (azione, appointment_id) dentro callback_data.
# Telegram limita callback_data a 64 byte: azione + id restano ampiamente sotto.
_CALLBACK_SEP = ":"


def build_callback_data(action: str, appointment_id: str) -> str:
    """Codifica azione e appointment_id in un unico callback_data.

    Il formato "<action>:<appointment_id>" permette alla Lambda telegram-callback
    (Task 10) di ricavare sia l'azione sia l'appuntamento da un singolo campo.

    Args:
        action: Identificatore dell'azione (ACTION_CONFERMA o ACTION_BLOCKER).
        appointment_id: Identificatore dell'appuntamento a cui l'azione si applica.

    Returns:
        Stringa callback_data nel formato "<action>:<appointment_id>".

    Raises:
        ValueError: se action non e una delle due azioni ammesse.
    """
    if action not in ACTIONS:
        raise ValueError(
            f"azione non ammessa: {action!r} (ammesse: {ACTIONS})"
        )
    return f"{action}{_CALLBACK_SEP}{appointment_id}"


def parse_callback_data(callback_data: str) -> tuple[str, str]:
    """Decodifica un callback_data in (action, appointment_id).

    Inverso di build_callback_data. Utile alla Lambda telegram-callback (Task 10).

    Args:
        callback_data: Stringa nel formato "<action>:<appointment_id>".

    Returns:
        Tupla (action, appointment_id).

    Raises:
        ValueError: se il formato non e valido o l'azione non e ammessa.
    """
    action, sep, appointment_id = callback_data.partition(_CALLBACK_SEP)
    if not sep or action not in ACTIONS or not appointment_id:
        raise ValueError(f"callback_data non valido: {callback_data!r}")
    return action, appointment_id


def _format_start(start_iso: str) -> str:
    """Formatta lo start ISO 8601 in una data/ora leggibile (dd/mm/YYYY HH:MM).

    Se il parsing fallisce (formato non ISO), restituisce la stringa originale
    cosi la data/ora resta comunque presente nel recap.

    Args:
        start_iso: Istante di inizio in formato ISO 8601 (es. 2026-01-20T09:00:00+01:00).

    Returns:
        Data/ora formattata, oppure la stringa originale se non parsabile.
    """
    try:
        parsed = datetime.fromisoformat(start_iso)
    except ValueError:
        return start_iso
    return parsed.strftime("%d/%m/%Y %H:%M")


def build_recap_text(appointment: Appointment) -> str:
    """Costruisce il testo del recap per il gruppo dottoresse (Requirement 6.1).

    Il recap contiene il nome del paziente (nome e cognome, dal campo
    nome_paziente), la data e l'ora dell'appuntamento e la dottoressa assegnata.
    Il campo "problema" NON e incluso: e testo potenzialmente sensibile e non
    viene inviato al gruppo (design.md - Sicurezza; Requirement 6.1 elenca solo
    nome, data/ora, dottoressa).

    Args:
        appointment: Appuntamento in Stato_Provvisorio da riepilogare.

    Returns:
        Testo del recap pronto per il campo text di sendMessage.
    """
    quando = _format_start(appointment.start)
    righe = [
        "Nuovo appuntamento da confermare",
        f"Paziente: {appointment.nome_paziente}",
        f"Quando: {quando}",
        f"Dottoressa: {appointment.doctor}",
    ]
    return "\n".join(righe)


def build_inline_keyboard(appointment: Appointment) -> dict[str, Any]:
    """Costruisce la tastiera inline con le due azioni mutuamente esclusive.

    Restituisce ESATTAMENTE due bottoni ("Conferma" e "Segnala blocker"), ciascuno
    con un callback_data distinto che codifica appointment_id + azione
    (Requirement 6.2, Property 17). La tastiera e nella forma reply_markup attesa
    dalla Telegram Bot API (inline_keyboard = lista di righe di bottoni).

    Args:
        appointment: Appuntamento a cui le azioni si riferiscono.

    Returns:
        Dict reply_markup con i due bottoni inline mutuamente esclusivi.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": LABEL_CONFERMA,
                    "callback_data": build_callback_data(
                        ACTION_CONFERMA, appointment.appointment_id
                    ),
                },
                {
                    "text": LABEL_BLOCKER,
                    "callback_data": build_callback_data(
                        ACTION_BLOCKER, appointment.appointment_id
                    ),
                },
            ]
        ]
    }


def build_send_message_payload(
    appointment: Appointment, chat_id: str
) -> dict[str, Any]:
    """Assembla il payload sendMessage per la Telegram Bot API.

    Il payload contiene chat_id del gruppo dottoresse, text=recap e
    reply_markup=tastiera inline. Il token del bot NON compare nel payload: viene
    passato separatamente nell'URL/headers dal sender (secret safety).

    Args:
        appointment: Appuntamento da notificare.
        chat_id: Identificatore del gruppo Telegram delle dottoresse.

    Returns:
        Dict del corpo della richiesta sendMessage.
    """
    return {
        "chat_id": chat_id,
        "text": build_recap_text(appointment),
        "reply_markup": build_inline_keyboard(appointment),
    }


@dataclass(frozen=True)
class TelegramConfig:
    """Configurazione del Notificatore_Telegram.

    chat_id: identificatore del gruppo dottoresse a cui inviare il recap.
    secret_name: nome/ARN del segreto Secrets Manager che contiene il token bot.
        Referenziato SOLO per nome (il valore resta nel secret_loader iniettato).
    """

    chat_id: str
    secret_name: str


# Tipo del sender iniettato: riceve (token_bot, payload_sendMessage) e invia.
# Restituisce la risposta dell'API come dict. Non deve loggare il token.
TelegramSender = Callable[[str, dict[str, Any]], dict[str, Any]]


class TelegramNotifier:
    """Servizio di invio recap al gruppo dottoresse (Requirement 6.1, 6.2).

    Dipendenze iniettate (composition, testabile a costo zero):
      - config: TelegramConfig (chat_id del gruppo + nome del segreto token).
      - sender: TelegramSender iniettato che effettua l'invio via Bot API. Nei
        test si inietta un fake che registra le chiamate senza rete.
      - secret_loader: Callable[[], str] che restituisce il token del bot letto
        da Secrets Manager a runtime. Iniettato come fake nei test.

    Il token e caricato pigramente (lazy) alla prima invio e memorizzato, cosi non
    viene riletto ad ogni chiamata; il valore resta confinato a questa istanza e
    non viene mai loggato ne incluso nel payload.
    """

    def __init__(
        self,
        config: TelegramConfig,
        sender: TelegramSender,
        secret_loader: Callable[[], str],
    ) -> None:
        self._config = config
        self._sender = sender
        self._secret_loader = secret_loader
        # Cache del token: caricato pigramente, mai loggato.
        self._bot_token: str | None = None

    def send_recap(self, appointment: Appointment) -> dict[str, Any]:
        """Invia il recap con le due azioni inline al gruppo dottoresse.

        Assembla il payload sendMessage (recap senza "problema" + tastiera inline
        con le due azioni mutuamente esclusive) e lo invia tramite il sender
        iniettato, passando il token del bot ottenuto dal secret_loader.

        Args:
            appointment: Appuntamento in Stato_Provvisorio da notificare.

        Returns:
            Risposta dell'invio restituita dal sender (dict).
        """
        token = self._get_bot_token()
        payload = build_send_message_payload(appointment, self._config.chat_id)
        logger.info(
            "invio recap Telegram per appointment_id=%s al gruppo dottoresse",
            appointment.appointment_id,
        )
        # Il token e passato al sender ma non loggato ne incluso nel payload.
        return self._sender(token, payload)

    def _get_bot_token(self) -> str:
        """Carica il token del bot una sola volta (lazy), mai loggato.

        Il valore e ottenuto dal secret_loader iniettato (nei test un fake, in
        produzione un loader che legge da Secrets Manager per nome/ARN). Non viene
        mai loggato ne incluso in alcun payload.
        """
        if self._bot_token is None:
            self._bot_token = self._secret_loader()
        return self._bot_token


# ---------------------------------------------------------------------------
# Transizione di stato della Finestra_Conferma (logica del telegram-callback)
# ---------------------------------------------------------------------------
#
# Questa e' la logica PURA e testabile che la Lambda telegram-callback (Task 10)
# delega al dominio: mappa un'azione ricevuta (Conferma / Segnala blocker) sullo
# stato bersaglio e applica la transizione tramite la conditional write del
# repository (transizione da PROVVISORIO solo se ancora provvisorio). La mutua
# esclusione della Finestra_Conferma (Requirement 6, criterio 6) e' garantita a
# livello di dato: la PRIMA azione in ordine cronologico vince, le successive
# falliscono la condizione e non modificano lo stato. Se nessuna azione arriva
# entro 8 ore, il timeout marca l'appuntamento come confermato automaticamente
# (Requirement 6.4).
#
# La Lambda reale (thin handler, Task 10) si limita a: validare il secret token
# Telegram, chiamare parse_callback_data + apply_callback_action, poi
# SendTaskSuccess a Step Functions. Nessuna logica di stato vive nell'handler.

# Mappa azione -> stato bersaglio della transizione da PROVVISORIO.
_ACTION_TARGET_STATUS: dict[str, ApptStatus] = {
    ACTION_CONFERMA: ApptStatus.CONFERMATO,  # Conferma -> confermato (Req 6.3)
    ACTION_BLOCKER: ApptStatus.DA_RIVEDERE,  # Segnala blocker -> da rivedere (Req 6.5)
}


def action_to_status(action: str) -> ApptStatus:
    """Mappa un'azione del callback sullo stato bersaglio della transizione.

    Args:
        action: Identificatore azione (ACTION_CONFERMA o ACTION_BLOCKER).

    Returns:
        Stato bersaglio: CONFERMATO per la conferma, DA_RIVEDERE per il blocker.

    Raises:
        ValueError: se action non e' una delle due azioni ammesse.
    """
    try:
        return _ACTION_TARGET_STATUS[action]
    except KeyError:
        raise ValueError(
            f"azione non ammessa: {action!r} (ammesse: {ACTIONS})"
        ) from None


def apply_callback_action(
    repo: "AnagraficaRepository",
    appointment_id: str,
    action: str,
) -> bool:
    """Applica un'azione di callback all'appuntamento con mutua esclusione.

    Esegue una conditional write da PROVVISORIO verso lo stato bersaglio
    dell'azione. La PRIMA azione ricevuta (appuntamento ancora PROVVISORIO) vince
    e transiziona lo stato; ogni azione successiva trova lo stato gia' cambiato,
    la condizione fallisce e lo stato resta invariato (Requirement 6.6). Le azioni
    successive sono quindi innocue: e' esattamente cio' che rende sicuri anche i
    callback ripetuti (retry Telegram).

    Args:
        repo: Repository Anagrafica_Store (in-memory o Dynamo, stessa API).
        appointment_id: Identificatore dell'appuntamento su cui agire.
        action: Azione ricevuta (ACTION_CONFERMA o ACTION_BLOCKER).

    Returns:
        True se questa azione ha vinto (ha transizionato lo stato); False se e'
        stata ignorata perche' una azione precedente aveva gia' fissato l'esito.

    Raises:
        ValueError: se action non e' ammessa.
        RepositoryError: se l'appuntamento non esiste.
    """
    # Import locale per evitare un ciclo di import a livello di modulo
    # (repository importa da models, telegram importa da repository solo qui).
    from src.repository import ConditionalUpdateError

    target = action_to_status(action)
    try:
        repo.update_appointment_status(
            appointment_id, target, expected_current=ApptStatus.PROVVISORIO
        )
        return True
    except ConditionalUpdateError:
        # Un'azione precedente ha gia' fissato l'esito: ignora (mutua esclusione).
        logger.info(
            "azione %s su appointment_id=%s ignorata: esito gia' determinato",
            action,
            appointment_id,
        )
        return False


def resolve_confirmation_timeout(
    repo: "AnagraficaRepository",
    appointment_id: str,
) -> bool:
    """Applica il timeout della Finestra_Conferma (8h senza azioni).

    Marca l'appuntamento come confermato automaticamente (CONFERMATO_AUTO) SOLO se
    e' ancora PROVVISORIO, cioe' se nessuna azione lo ha gia' risolto entro le 8
    ore (Requirement 6.4). Se un'azione ha gia' fissato l'esito (Requirement 6.3:
    la conferma esplicita disattiva la conferma automatica), la conditional write
    fallisce e lo stato resta invariato.

    Args:
        repo: Repository Anagrafica_Store.
        appointment_id: Identificatore dell'appuntamento.

    Returns:
        True se il timeout ha applicato CONFERMATO_AUTO; False se l'esito era gia'
        determinato da un'azione precedente.

    Raises:
        RepositoryError: se l'appuntamento non esiste.
    """
    from src.repository import ConditionalUpdateError

    try:
        repo.update_appointment_status(
            appointment_id,
            ApptStatus.CONFERMATO_AUTO,
            expected_current=ApptStatus.PROVVISORIO,
        )
        return True
    except ConditionalUpdateError:
        # Un'azione entro le 8h ha gia' determinato l'esito: nessuna conferma auto.
        logger.info(
            "timeout su appointment_id=%s ignorato: esito gia' determinato",
            appointment_id,
        )
        return False
