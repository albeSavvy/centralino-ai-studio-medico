"""Unit test - Gestore_Consenso (Task 12.1).

Verifica gli esempi concreti ed edge case del Gestore_Consenso (Requirement 8):
  - con email VALIDA, ses:SendRawEmail e' invocato UNA volta con il PDF del
    consenso in ALLEGATO (Requirement 8.1). Il sender fake registra il messaggio
    grezzo, che viene riparsato per confermare la presenza dell'allegato PDF.
  - email assente -> EMAIL_ASSENTE, nessun invio (Requirement 8.2).
  - email non valida -> EMAIL_NON_VALIDA, nessun invio (Requirement 8.3).
  - invio fallito -> retry <= 3 poi INVIO_FALLITO (Requirement 8.4).
  - validazione di formato is_valid_email.

Dipendenze fake iniettate: pdf_loader fittizio, ses_sender fake, event_sink=lista.
Nessuna chiamata AWS/S3/SES/rete, costo zero. Il PDF e' byte fittizi non sensibili.
"""

from __future__ import annotations

from email import message_from_bytes

import pytest

from src.consenso import (
    ConsensoConfig,
    ConsensoEvent,
    ConsensoOutcome,
    DEFAULT_PDF_FILENAME,
    GestoreConsenso,
    build_raw_email,
    is_valid_email,
)

SENDER = "segreteria@studio.example.com"
RECIPIENT = "mario.rossi@example.com"
FAKE_PDF = b"%PDF-1.4 fake consent form bytes"


class _RecordingSes:
    """Sender SES fake che registra ogni chiamata (Source, Destination, RawMessage)."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._fail_times = fail_times

    def __call__(self, sender: str, recipient: str, raw: bytes) -> dict:
        self.calls.append((sender, recipient, raw))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("invio SES simulato fallito")
        return {"MessageId": "fake-message-id"}


def _make_gestore(
    events: list[ConsensoEvent],
    ses_sender,
    *,
    max_attempts: int = 3,
) -> GestoreConsenso:
    return GestoreConsenso(
        config=ConsensoConfig(sender_email=SENDER),
        pdf_loader=lambda: FAKE_PDF,
        ses_sender=ses_sender,
        event_sink=events.append,
        max_attempts=max_attempts,
    )


def _find_pdf_attachment(raw_message: bytes):
    """Ritorna la parte MIME dell'allegato PDF, o None se assente."""
    message = message_from_bytes(raw_message)
    for part in message.walk():
        if part.get_content_type() == "application/pdf":
            return part
    return None


# ---------------------------------------------------------------------------
# Requirement 8.1: email valida -> SendRawEmail con PDF in allegato
# ---------------------------------------------------------------------------

def test_valid_email_invokes_send_raw_email_with_pdf_attachment():
    events: list[ConsensoEvent] = []
    ses = _RecordingSes()
    gestore = _make_gestore(events, ses)

    outcome = gestore.process("a1b2c3", RECIPIENT)

    assert outcome is ConsensoOutcome.EMAIL_INVIATA
    # ses:SendRawEmail invocato esattamente una volta.
    assert len(ses.calls) == 1
    sender, recipient, raw = ses.calls[0]
    assert sender == SENDER
    assert recipient == RECIPIENT

    # Il messaggio grezzo contiene il PDF in allegato.
    attachment = _find_pdf_attachment(raw)
    assert attachment is not None
    assert attachment.get_filename() == DEFAULT_PDF_FILENAME
    assert attachment.get_payload(decode=True) == FAKE_PDF
    # Disposizione: allegato.
    assert "attachment" in attachment.get("Content-Disposition", "")

    # Un solo evento, esito inviata.
    assert len(events) == 1
    assert events[0].outcome is ConsensoOutcome.EMAIL_INVIATA


def test_recipient_email_is_trimmed_before_send():
    events: list[ConsensoEvent] = []
    ses = _RecordingSes()
    gestore = _make_gestore(events, ses)

    gestore.process("a1b2c3", f"  {RECIPIENT}  ")

    _, recipient, _ = ses.calls[0]
    assert recipient == RECIPIENT


# ---------------------------------------------------------------------------
# Requirement 8.2: email assente -> nessun invio
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("email", [None, "", "   ", "\t"])
def test_absent_email_omits_send_and_records_event(email):
    events: list[ConsensoEvent] = []
    ses = _RecordingSes()
    gestore = _make_gestore(events, ses)

    outcome = gestore.process("a1b2c3", email)

    assert outcome is ConsensoOutcome.EMAIL_ASSENTE
    assert ses.calls == []  # nessun invio
    assert len(events) == 1
    assert events[0].outcome is ConsensoOutcome.EMAIL_ASSENTE


# ---------------------------------------------------------------------------
# Requirement 8.3: email non valida -> nessun invio
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "email",
    [
        "not-an-email",
        "mario@senza-tld",
        "@example.com",
        "mario@",
        "mario rossi@example.com",
        "mario@@example.com",
    ],
)
def test_invalid_email_omits_send_and_records_event(email):
    events: list[ConsensoEvent] = []
    ses = _RecordingSes()
    gestore = _make_gestore(events, ses)

    outcome = gestore.process("a1b2c3", email)

    assert outcome is ConsensoOutcome.EMAIL_NON_VALIDA
    assert ses.calls == []  # nessun invio
    assert len(events) == 1
    assert events[0].outcome is ConsensoOutcome.EMAIL_NON_VALIDA


# ---------------------------------------------------------------------------
# Requirement 8.4: invio fallito -> retry <= 3 poi INVIO_FALLITO
# ---------------------------------------------------------------------------

def test_send_failure_retries_up_to_three_times_then_records_failure():
    events: list[ConsensoEvent] = []
    ses = _RecordingSes(fail_times=99)  # fallisce sempre
    gestore = _make_gestore(events, ses, max_attempts=3)

    outcome = gestore.process("a1b2c3", RECIPIENT)

    assert outcome is ConsensoOutcome.INVIO_FALLITO
    # Esattamente 3 tentativi di invio (non di piu).
    assert len(ses.calls) == 3
    assert len(events) == 1
    assert events[0].outcome is ConsensoOutcome.INVIO_FALLITO


def test_send_succeeds_after_transient_failures():
    events: list[ConsensoEvent] = []
    ses = _RecordingSes(fail_times=2)  # 2 fallimenti poi successo al 3o
    gestore = _make_gestore(events, ses, max_attempts=3)

    outcome = gestore.process("a1b2c3", RECIPIENT)

    assert outcome is ConsensoOutcome.EMAIL_INVIATA
    assert len(ses.calls) == 3
    assert events[0].outcome is ConsensoOutcome.EMAIL_INVIATA


def test_pdf_loaded_once_across_attempts():
    events: list[ConsensoEvent] = []
    load_count = {"n": 0}

    def _counting_loader() -> bytes:
        load_count["n"] += 1
        return FAKE_PDF

    ses = _RecordingSes(fail_times=2)
    gestore = GestoreConsenso(
        config=ConsensoConfig(sender_email=SENDER),
        pdf_loader=_counting_loader,
        ses_sender=ses,
        event_sink=events.append,
        max_attempts=3,
    )
    gestore.process("a1b2c3", RECIPIENT)

    # Il PDF e' caricato una sola volta (lazy + cache), anche con retry.
    assert load_count["n"] == 1


# ---------------------------------------------------------------------------
# is_valid_email / build_raw_email
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "email,expected",
    [
        ("mario.rossi@example.com", True),
        ("a@b.co", True),
        ("nome+tag@dominio.it", True),
        (None, False),
        ("", False),
        ("   ", False),
        ("no-at-sign", False),
        ("mario@nodot", False),
        ("mario@dominio.c", False),  # TLD troppo corto
        ("spazio interno@x.com", False),
    ],
)
def test_is_valid_email(email, expected):
    assert is_valid_email(email) is expected


def test_build_raw_email_structure():
    raw = build_raw_email(SENDER, RECIPIENT, FAKE_PDF)
    message = message_from_bytes(raw)
    assert message["From"] == SENDER
    assert message["To"] == RECIPIENT
    assert message.is_multipart()
    # Presenza di una parte testuale e di una parte PDF.
    content_types = {part.get_content_type() for part in message.walk()}
    assert "text/plain" in content_types
    assert "application/pdf" in content_types


def test_gestore_rejects_invalid_max_attempts():
    with pytest.raises(ValueError):
        GestoreConsenso(
            config=ConsensoConfig(sender_email=SENDER),
            pdf_loader=lambda: FAKE_PDF,
            ses_sender=_RecordingSes(),
            event_sink=[].append,
            max_attempts=0,
        )
