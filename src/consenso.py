"""Gestore_Consenso: invio email con PDF del modulo di consenso via SES.

Centralino AI Studio Medico / Idelia (Flusso A) - Task 12.

Questo modulo contiene la logica PURA e testabile del Gestore_Consenso
(Requirement 8, criteri 1-4):

  - is_valid_email(email): validazione di formato dell'indirizzo email del
    paziente (Requirement 8.3). Nessuna rete: solo controllo sintattico.
  - build_raw_email(...): costruisce il messaggio MIME (multipart) con il corpo
    testuale e il PDF del consenso in ALLEGATO, pronto per ses:SendRawEmail
    (Requirement 8.1). Logica pura, nessuna chiamata AWS.
  - ConsensoEvent / ConsensoOutcome: enum degli esiti registrati come evento
    (email inviata, email assente, email non valida, invio fallito). Requirement
    8.2, 8.3, 8.4 richiedono di REGISTRARE un evento e MANTENERE lo stato
    confermato senza declassamento.
  - GestoreConsenso: servizio che orchestra l'invio. Dipendenze INIETTATE
    (composition, testabile a costo zero):
      * pdf_loader: Callable[[], bytes] che legge il PDF del consenso dal bucket
        S3 (encryption on) a runtime. Nei test si inietta un fake che restituisce
        byte fittizi: nessuna chiamata S3.
      * ses_sender: Callable che invoca ses:SendRawEmail. Nei test si inietta un
        fake/mock che registra la chiamata: nessuna chiamata SES, nessun costo.
      * event_sink: Callable[[ConsensoEvent], None] che registra l'evento (es. su
        DynamoDB / EventBridge / log). Nei test si inietta una lista.

Semantica di resilienza (design.md - Gestione errori e resilienza):
  - Email assente          -> ometti invio, registra evento EMAIL_ASSENTE,
                              stato confermato invariato (Requirement 8.2).
  - Email formato invalido -> ometti invio, registra evento EMAIL_NON_VALIDA,
                              stato confermato invariato (Requirement 8.3).
  - Invio fallito          -> retry fino a MAX_SEND_ATTEMPTS (3); se tutti i
                              tentativi falliscono registra evento INVIO_FALLITO
                              e mantiene lo stato confermato (Requirement 8.4).
  - Invio riuscito         -> registra evento EMAIL_INVIATA.
  In NESSUN caso il Gestore_Consenso declassa lo stato dell'appuntamento: la
  gestione del consenso e best-effort e non altera l'esito della conferma.

Secret safety (regola di workspace aws-agent-rules): questo modulo non maneggia
segreti in chiaro; l'accesso a S3/SES avviene tramite il ruolo IAM least-privilege
della Lambda (Task 12 in CDK, gia' previsto in design.md - Sicurezza). Nessun
valore sensibile e' loggato.

Costo zero: modulo puro Python. Nei test si iniettano pdf_loader, ses_sender ed
event_sink fake, quindi nessuna chiamata reale S3/SES/rete. Il percorso reale
(boto3 S3 + SES) vive nel thin handler Lambda (lambdas/gestore_consenso.py),
marcato # pragma: no cover.

Riferimento: design.md sezione "Gestore_Consenso", Correctness Property 22,
Requirement 8 (criteri 1-4) e 9.6 (encryption at rest del bucket PDF).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Numero massimo di tentativi di invio email prima di rinunciare (Requirement 8.4).
MAX_SEND_ATTEMPTS = 3

# Nome di default dell'allegato PDF del modulo di consenso.
DEFAULT_PDF_FILENAME = "modulo-consenso.pdf"

# Oggetto e corpo di default dell'email di consenso (italiano, testo semplice).
DEFAULT_SUBJECT = "Modulo di consenso - primo colloquio"
DEFAULT_BODY_TEXT = (
    "Gentile paziente,\n"
    "in allegato trova il modulo di consenso da compilare e portare al primo "
    "colloquio.\n"
    "Cordiali saluti,\nSegreteria Studio"
)

# Validazione di formato email (Requirement 8.3): controllo sintattico volutamente
# conservativo (una chiocciola, parte locale e dominio non vuoti, dominio con
# almeno un punto e TLD di >= 2 lettere, nessuno spazio). Non e una validazione
# RFC 5322 completa: serve a scartare gli indirizzi palesemente malformati senza
# dipendenze esterne.
_EMAIL_RE = re.compile(
    r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"
)


class ConsensoOutcome(str, Enum):
    """Esiti possibili della gestione consenso, registrati come evento."""

    EMAIL_INVIATA = "EMAIL_INVIATA"
    EMAIL_ASSENTE = "EMAIL_ASSENTE"
    EMAIL_NON_VALIDA = "EMAIL_NON_VALIDA"
    INVIO_FALLITO = "INVIO_FALLITO"


@dataclass(frozen=True)
class ConsensoEvent:
    """Evento registrato dal Gestore_Consenso (Requirement 8.2, 8.3, 8.4).

    outcome: esito della gestione (inviata / assente / non valida / invio fallito).
    appointment_id: appuntamento a cui l'esito si riferisce.
    detail: dettaglio testuale (mai contiene segreti; l'email e' inclusa solo per
        tracciabilita' dell'esito, dato finto in questa fase).
    """

    outcome: ConsensoOutcome
    appointment_id: str
    detail: str = ""


@dataclass(frozen=True)
class ConsensoConfig:
    """Configurazione del Gestore_Consenso.

    sender_email: mittente verificato SES da cui parte l'email di consenso.
    subject: oggetto dell'email.
    body_text: corpo testuale dell'email.
    pdf_filename: nome dell'allegato PDF.
    """

    sender_email: str
    subject: str = DEFAULT_SUBJECT
    body_text: str = DEFAULT_BODY_TEXT
    pdf_filename: str = DEFAULT_PDF_FILENAME


# Tipo del sender iniettato: riceve (sender, recipient, raw_message_bytes) e invia
# via ses:SendRawEmail. Restituisce la risposta dell'API (dict con MessageId).
SesRawSender = Callable[[str, str, bytes], dict[str, Any]]

# Tipo del loader del PDF: nessun argomento, restituisce i byte del PDF (da S3).
PdfLoader = Callable[[], bytes]

# Tipo del sink degli eventi: riceve il ConsensoEvent e lo registra.
EventSink = Callable[[ConsensoEvent], None]


def is_valid_email(email: str | None) -> bool:
    """Valida il formato di un indirizzo email (Requirement 8.3).

    Restituisce False per None, stringa vuota/solo-spazi o formato non conforme.
    Controllo sintattico conservativo, senza rete e senza dipendenze esterne.

    Args:
        email: Indirizzo email da validare (o None se assente).

    Returns:
        True se l'indirizzo ha un formato valido, False altrimenti.
    """
    if email is None:
        return False
    candidate = email.strip()
    if not candidate:
        return False
    return _EMAIL_RE.match(candidate) is not None


def build_raw_email(
    sender_email: str,
    recipient_email: str,
    pdf_bytes: bytes,
    subject: str = DEFAULT_SUBJECT,
    body_text: str = DEFAULT_BODY_TEXT,
    pdf_filename: str = DEFAULT_PDF_FILENAME,
) -> bytes:
    """Costruisce il messaggio MIME con il PDF del consenso in allegato.

    Il messaggio e' un multipart/mixed con una parte testuale (corpo) e una parte
    application/pdf (l'allegato). E' il formato atteso da ses:SendRawEmail
    (Requirement 8.1). Logica pura: nessuna chiamata AWS.

    Args:
        sender_email: Mittente verificato SES.
        recipient_email: Destinatario (email valida del paziente).
        pdf_bytes: Contenuto del PDF del modulo di consenso.
        subject: Oggetto dell'email.
        body_text: Corpo testuale dell'email.
        pdf_filename: Nome dell'allegato PDF.

    Returns:
        Il messaggio MIME serializzato in byte, pronto per SendRawEmail.
    """
    message = MIMEMultipart("mixed")
    message["Subject"] = subject
    message["From"] = sender_email
    message["To"] = recipient_email
    message.attach(MIMEText(body_text, "plain", "utf-8"))

    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header(
        "Content-Disposition", "attachment", filename=pdf_filename
    )
    message.attach(attachment)
    return message.as_bytes()


class GestoreConsenso:
    """Servizio di invio del modulo di consenso (Requirement 8).

    Dipendenze iniettate (composition, testabile a costo zero):
      - config: ConsensoConfig (mittente, oggetto, corpo, nome allegato).
      - pdf_loader: PdfLoader che legge il PDF dal bucket S3 a runtime. Caricato
        pigramente (lazy) e memorizzato, cosi il PDF non e' riletto ad ogni invio.
      - ses_sender: SesRawSender che invoca ses:SendRawEmail. Nei test un fake.
      - event_sink: EventSink che registra l'evento di esito. Nei test una lista.
      - max_attempts: numero massimo di tentativi di invio (default 3).

    Invariante chiave (Property 22): il Gestore_Consenso NON declassa mai lo stato
    dell'appuntamento. Comunichi solo l'esito tramite un evento; lo stato
    CONFERMATO/CONFERMATO_AUTO resta invariato in ogni ramo.
    """

    def __init__(
        self,
        config: ConsensoConfig,
        pdf_loader: PdfLoader,
        ses_sender: SesRawSender,
        event_sink: EventSink,
        max_attempts: int = MAX_SEND_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts deve essere >= 1")
        self._config = config
        self._pdf_loader = pdf_loader
        self._ses_sender = ses_sender
        self._event_sink = event_sink
        self._max_attempts = max_attempts
        # Cache del PDF: caricato pigramente alla prima invio riuscita.
        self._pdf_bytes: bytes | None = None

    def process(
        self, appointment_id: str, recipient_email: str | None
    ) -> ConsensoOutcome:
        """Gestisce l'invio del consenso per un appuntamento confermato.

        Rami (Requirement 8.1-8.4), tutti SENZA declassare lo stato:
          - email assente          -> EMAIL_ASSENTE
          - email formato invalido -> EMAIL_NON_VALIDA
          - invio riuscito         -> EMAIL_INVIATA
          - invio fallito (dopo <= max_attempts) -> INVIO_FALLITO

        Args:
            appointment_id: Appuntamento confermato per cui inviare il consenso.
            recipient_email: Email del paziente (o None se assente).

        Returns:
            L'esito della gestione (ConsensoOutcome). L'evento corrispondente e'
            gia' stato registrato tramite event_sink.
        """
        # Requirement 8.2: email assente -> ometti invio, registra evento.
        if recipient_email is None or not recipient_email.strip():
            return self._record(
                ConsensoOutcome.EMAIL_ASSENTE,
                appointment_id,
                "nessun indirizzo email associato al paziente",
            )

        # Requirement 8.3: email formato invalido -> ometti invio, registra evento.
        if not is_valid_email(recipient_email):
            return self._record(
                ConsensoOutcome.EMAIL_NON_VALIDA,
                appointment_id,
                "indirizzo email non valido (formato)",
            )

        # Requirement 8.1 / 8.4: invio con retry fino a max_attempts.
        return self._send_with_retry(appointment_id, recipient_email.strip())

    def _send_with_retry(
        self, appointment_id: str, recipient_email: str
    ) -> ConsensoOutcome:
        """Invia l'email con retry; su fallimento persistente registra INVIO_FALLITO.

        Il PDF e' caricato pigramente (una sola volta) e allegato al messaggio.
        Nessun tentativo supera max_attempts (Requirement 8.4).
        """
        raw = build_raw_email(
            sender_email=self._config.sender_email,
            recipient_email=recipient_email,
            pdf_bytes=self._get_pdf_bytes(),
            subject=self._config.subject,
            body_text=self._config.body_text,
            pdf_filename=self._config.pdf_filename,
        )
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                self._ses_sender(self._config.sender_email, recipient_email, raw)
                return self._record(
                    ConsensoOutcome.EMAIL_INVIATA,
                    appointment_id,
                    "email di consenso inviata con PDF in allegato",
                )
            except Exception as exc:  # noqa: BLE001 - qualunque errore -> retry
                last_error = exc
                logger.warning(
                    "invio consenso tentativo %d/%d fallito per appointment_id=%s: %s: %s",
                    attempt,
                    self._max_attempts,
                    appointment_id,
                    type(exc).__name__,
                    exc,
                )
        # Requirement 8.4: tutti i tentativi falliti -> evento, stato invariato.
        return self._record(
            ConsensoOutcome.INVIO_FALLITO,
            appointment_id,
            f"invio fallito dopo {self._max_attempts} tentativi: {last_error}",
        )

    def _get_pdf_bytes(self) -> bytes:
        """Carica il PDF del consenso una sola volta (lazy) dal bucket S3."""
        if self._pdf_bytes is None:
            self._pdf_bytes = self._pdf_loader()
        return self._pdf_bytes

    def _record(
        self, outcome: ConsensoOutcome, appointment_id: str, detail: str
    ) -> ConsensoOutcome:
        """Registra l'evento di esito tramite l'event_sink iniettato."""
        event = ConsensoEvent(
            outcome=outcome, appointment_id=appointment_id, detail=detail
        )
        self._event_sink(event)
        logger.info(
            "consenso appointment_id=%s esito=%s", appointment_id, outcome.value
        )
        return outcome
