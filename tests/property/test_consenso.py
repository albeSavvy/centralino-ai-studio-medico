"""Property test - Gestore_Consenso non declassa mai lo stato (Task 12.1).

Mappatura 1:1 con la proprieta del design (design.md - Correctness Properties):
  - Property 22: per qualunque appuntamento confermato, in tutti i casi in cui
    l'email NON viene inviata (indirizzo assente, indirizzo non conforme al
    formato, o invio fallito dopo 3 tentativi) l'appuntamento rimane in stato
    confermato e viene registrato l'evento corrispondente.

Libreria: Hypothesis, >= 100 iterazioni per proprieta (qui 200). Dipendenze
INIETTATE come fake: pdf_loader fittizio, ses_sender fake, event_sink=lista, e un
InMemoryRepository per verificare che lo stato dell'appuntamento non venga MAI
toccato dal Gestore_Consenso. Nessuna chiamata AWS/S3/SES/rete, costo zero.

Il "non declassamento" e' verificato in due modi complementari:
  1. Lo stato dell'appuntamento nel repository (CONFERMATO o CONFERMATO_AUTO)
     resta identico dopo process() in tutti i rami di mancato invio.
  2. In ogni ramo viene registrato ESATTAMENTE un evento con l'esito atteso.

I commenti usano ASCII.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.consenso import (
    ConsensoConfig,
    ConsensoEvent,
    ConsensoOutcome,
    GestoreConsenso,
)
from src.models import Appointment, ApptStatus
from src.repository import InMemoryRepository

SENDER = "segreteria@studio.example.com"

# Stati "confermati" da cui il consenso non deve mai far regredire l'appuntamento.
CONFIRMED_STATES = [ApptStatus.CONFERMATO, ApptStatus.CONFERMATO_AUTO]


def _make_gestore(
    events: list[ConsensoEvent],
    *,
    always_fail: bool = False,
    max_attempts: int = 3,
) -> GestoreConsenso:
    """Costruisce un GestoreConsenso con dipendenze fake (nessuna rete)."""

    def _pdf_loader() -> bytes:
        # PDF fittizio: byte non sensibili, nessuna lettura S3.
        return b"%PDF-1.4 fake consent form"

    def _ses_sender(sender: str, recipient: str, raw: bytes) -> dict:
        if always_fail:
            raise RuntimeError("invio SES simulato fallito")
        return {"MessageId": "fake-message-id"}

    return GestoreConsenso(
        config=ConsensoConfig(sender_email=SENDER),
        pdf_loader=_pdf_loader,
        ses_sender=_ses_sender,
        event_sink=events.append,
        max_attempts=max_attempts,
    )


def _seed_confirmed_appointment(
    repo: InMemoryRepository, appointment_id: str, status: ApptStatus
) -> None:
    """Inserisce un appuntamento gia' in stato confermato nel repository."""
    appt = Appointment(
        appointment_id=appointment_id,
        client_no="000001",
        nome_paziente="Mario Rossi",
        doctor="chiara",
        sede="Meda",
        start="2026-01-20T09:00:00+01:00",
        duration_min=30,
        status=status,
    )
    repo.save_appointment(appt)


# Generatori del dominio.
_appointment_id = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")
_confirmed_status = st.sampled_from(CONFIRMED_STATES)

# Email assente: None oppure stringa vuota/solo-spazi.
_absent_email = st.one_of(
    st.none(),
    st.text(alphabet=" \t", min_size=0, max_size=5),
)

# Email dal formato non valido: stringhe che NON matchano il formato atteso.
# Escludiamo per costruzione i casi che potrebbero risultare validi.
# NB: una stringa fatta solo di spazi/caratteri di controllo (strip() -> "") e'
# EMAIL_ASSENTE (Req 8.2), non EMAIL_NON_VALIDA (Req 8.3); quindi il primo ramo
# richiede almeno un carattere non-whitespace oltre all'assenza della "@".
_invalid_email = st.one_of(
    st.text(min_size=1, max_size=20).filter(
        lambda s: "@" not in s and s.strip() != ""
    ),  # nessuna @, ma non solo-whitespace
    st.just("mario@senza-tld"),  # dominio senza punto/TLD
    st.just("@example.com"),  # parte locale vuota
    st.just("mario@"),  # dominio vuoto
    st.just("mario rossi@example.com"),  # spazio interno
    st.just("mario@@example.com"),  # doppia chiocciola
)

# Email valida (per il ramo "invio fallito").
_valid_email = st.builds(
    lambda local, domain, tld: f"{local}@{domain}.{tld}",
    st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=12),
    st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=12),
    st.sampled_from(["com", "it", "org", "net"]),
)


# ---------------------------------------------------------------------------
# Property 22: nessun declassamento in tutti i rami di mancato invio
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 22: per qualunque appuntamento confermato, in tutti i casi in cui l'email non viene inviata (indirizzo assente, indirizzo non conforme al formato, o invio fallito dopo 3 tentativi) l'appuntamento rimane in stato confermato e viene registrato l'evento corrispondente
@settings(max_examples=200)
@given(
    appointment_id=_appointment_id,
    status=_confirmed_status,
    email=_absent_email,
)
def test_property22_absent_email_keeps_confirmed_and_records_event(
    appointment_id: str,
    status: ApptStatus,
    email,
) -> None:
    repo = InMemoryRepository()
    _seed_confirmed_appointment(repo, appointment_id, status)
    events: list[ConsensoEvent] = []
    gestore = _make_gestore(events)

    outcome = gestore.process(appointment_id, email)

    # Email assente -> esito EMAIL_ASSENTE, nessun invio.
    assert outcome is ConsensoOutcome.EMAIL_ASSENTE
    # Stato confermato invariato (nessun declassamento).
    assert repo.get_appointment(appointment_id).status is status
    # Esattamente un evento registrato, con l'esito atteso e l'appuntamento giusto.
    assert len(events) == 1
    assert events[0].outcome is ConsensoOutcome.EMAIL_ASSENTE
    assert events[0].appointment_id == appointment_id


# Feature: centralino-ai-studio-medico, Property 22: per qualunque appuntamento confermato, in tutti i casi in cui l'email non viene inviata (indirizzo assente, indirizzo non conforme al formato, o invio fallito dopo 3 tentativi) l'appuntamento rimane in stato confermato e viene registrato l'evento corrispondente
@settings(max_examples=200)
@given(
    appointment_id=_appointment_id,
    status=_confirmed_status,
    email=_invalid_email,
)
def test_property22_invalid_email_keeps_confirmed_and_records_event(
    appointment_id: str,
    status: ApptStatus,
    email: str,
) -> None:
    repo = InMemoryRepository()
    _seed_confirmed_appointment(repo, appointment_id, status)
    events: list[ConsensoEvent] = []
    gestore = _make_gestore(events)

    outcome = gestore.process(appointment_id, email)

    # Email non valida -> esito EMAIL_NON_VALIDA, nessun invio.
    assert outcome is ConsensoOutcome.EMAIL_NON_VALIDA
    # Stato confermato invariato.
    assert repo.get_appointment(appointment_id).status is status
    # Esattamente un evento registrato con l'esito atteso.
    assert len(events) == 1
    assert events[0].outcome is ConsensoOutcome.EMAIL_NON_VALIDA
    assert events[0].appointment_id == appointment_id


# Feature: centralino-ai-studio-medico, Property 22: per qualunque appuntamento confermato, in tutti i casi in cui l'email non viene inviata (indirizzo assente, indirizzo non conforme al formato, o invio fallito dopo 3 tentativi) l'appuntamento rimane in stato confermato e viene registrato l'evento corrispondente
@settings(max_examples=200)
@given(
    appointment_id=_appointment_id,
    status=_confirmed_status,
    email=_valid_email,
    max_attempts=st.integers(min_value=1, max_value=3),
)
def test_property22_send_failure_keeps_confirmed_and_records_event(
    appointment_id: str,
    status: ApptStatus,
    email: str,
    max_attempts: int,
) -> None:
    repo = InMemoryRepository()
    _seed_confirmed_appointment(repo, appointment_id, status)
    events: list[ConsensoEvent] = []
    # ses_sender che fallisce sempre: l'invio non riesce dopo max_attempts.
    gestore = _make_gestore(events, always_fail=True, max_attempts=max_attempts)

    outcome = gestore.process(appointment_id, email)

    # Invio fallito dopo i tentativi -> esito INVIO_FALLITO, nessun invio riuscito.
    assert outcome is ConsensoOutcome.INVIO_FALLITO
    # Stato confermato invariato (Requirement 8.4: nessun declassamento).
    assert repo.get_appointment(appointment_id).status is status
    # Esattamente un evento registrato con l'esito atteso.
    assert len(events) == 1
    assert events[0].outcome is ConsensoOutcome.INVIO_FALLITO
    assert events[0].appointment_id == appointment_id
