"""Modelli dati e chiavi single-table per Anagrafica_Store.

Centralino AI Studio Medico / Idelia (Flusso A).

Questo modulo contiene SOLO logica pura: dataclass delle entita del design
(PATIENT, DOCTOR, APPT, CONTACT, SESSION) e funzioni di costruzione delle chiavi
single-table DynamoDB (PK/SK, GSI1PK/SK per nome normalizzato, GSI2PK/SK per
stato+start). Nessuna chiamata AWS/boto3 qui: la tabella DynamoDB (CDK) e il
repository (boto3) sono task separate. Costo zero.

Riferimento: design.md sezione "Data Models" (single-table design).

Tabella: centralino-medico
  PK  = identita entita (PATIENT#<clientNo>, DOCTOR#<id>, APPT#<apptId>,
        CONTACT#<id>, SESSION#<id>)
  SK  = discriminante entita/relazione (PROFILE, META, ...)
  GSI1 (ricerca paziente per nome): GSI1PK = PNAME#<nome_normalizzato>,
        GSI1SK = PATIENT#<clientNo>
  GSI2 (appuntamenti per stato): GSI2PK = APPTSTATUS#<stato>,
        GSI2SK = <startIso>
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Eta di maggiore eta: sotto questa soglia il paziente e minore (Requirement 3.2).
MINOR_AGE_THRESHOLD = 18


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Esito(str, Enum):
    """Esiti di chiamata assegnati dal Classificatore_Esito (Requirement 2.4)."""

    RICHIAMERA = "RICHIAMERA"
    SOLO_INFO = "SOLO_INFO"
    APPUNTAMENTO = "APPUNTAMENTO"


class ApptStatus(str, Enum):
    """Stati di un appuntamento (design.md - Stati appuntamento)."""

    PROVVISORIO = "PROVVISORIO"
    CONFERMATO = "CONFERMATO"
    CONFERMATO_AUTO = "CONFERMATO_AUTO"
    DA_RIVEDERE = "DA_RIVEDERE"
    SCRITTURA_CALENDARIO_FALLITA = "SCRITTURA_CALENDARIO_FALLITA"
    COMPLETATO = "COMPLETATO"


# ---------------------------------------------------------------------------
# Funzioni di costruzione chiavi single-table
# ---------------------------------------------------------------------------

def normalize_name(nome: str) -> str:
    """Normalizza un nome per la ricerca (lowercase + trim + collasso spazi).

    Usata per GSI1 (ricerca per nome robusta, design.md - Chiavi e indici).
    """
    return " ".join(nome.strip().lower().split())


def patient_pk(client_no: str) -> str:
    return f"PATIENT#{client_no}"


def doctor_pk(doctor_id: str) -> str:
    return f"DOCTOR#{doctor_id}"


def appt_pk(appointment_id: str) -> str:
    return f"APPT#{appointment_id}"


def contact_pk(contact_id: str) -> str:
    return f"CONTACT#{contact_id}"


def session_pk(session_id: str) -> str:
    return f"SESSION#{session_id}"


def gsi1_pk(nome: str) -> str:
    """GSI1PK per la ricerca paziente per nome (nome normalizzato)."""
    return f"PNAME#{normalize_name(nome)}"


def gsi1_sk(client_no: str) -> str:
    return f"PATIENT#{client_no}"


def gsi2_pk(status: "ApptStatus | str") -> str:
    """GSI2PK per la ricerca appuntamenti per stato."""
    value = status.value if isinstance(status, ApptStatus) else str(status)
    return f"APPTSTATUS#{value}"


def gsi2_sk(start_iso: str) -> str:
    return start_iso


# ---------------------------------------------------------------------------
# Dataclass entita
# ---------------------------------------------------------------------------

@dataclass
class Patient:
    """Entita PATIENT (SK = PROFILE).

    Dati obbligatori dal modulo cartaceo (Requirement 3.1): nome, eta, telefono,
    problema, sede, dataChiamata. Facoltativi: parentName, indirizzo, email,
    dataNascita, luogoNascita.

    isMinor e derivato dall'eta (<18, Requirement 3.2); parentName e obbligatorio
    se il paziente e minore.
    """

    client_no: str
    nome: str
    eta: int
    telefono: str
    problema: str
    sede: str
    data_chiamata: str
    parent_name: str | None = None
    indirizzo: str | None = None
    email: str | None = None
    data_nascita: str | None = None
    luogo_nascita: str | None = None
    is_minor: bool = field(init=False)

    def __post_init__(self) -> None:
        # Derivazione isMinor dall'eta (Requirement 3.2, Property 6).
        self.is_minor = self.eta < MINOR_AGE_THRESHOLD
        # Referente genitore obbligatorio per i minori (Requirement 3.2).
        if self.is_minor and not (self.parent_name and self.parent_name.strip()):
            raise ValueError(
                "parent_name e obbligatorio per un paziente minore (eta < 18)"
            )

    def to_item(self) -> dict:
        """Serializza in un item DynamoDB single-table."""
        item = {
            "PK": patient_pk(self.client_no),
            "SK": "PROFILE",
            "entity": "PATIENT",
            "clientNo": self.client_no,
            "nome": self.nome,
            "eta": self.eta,
            "isMinor": self.is_minor,
            "telefono": self.telefono,
            "problema": self.problema,
            "sede": self.sede,
            "dataChiamata": self.data_chiamata,
            "GSI1PK": gsi1_pk(self.nome),
            "GSI1SK": gsi1_sk(self.client_no),
        }
        if self.parent_name:
            item["parentName"] = self.parent_name
        if self.indirizzo:
            item["indirizzo"] = self.indirizzo
        if self.email:
            item["email"] = self.email
        if self.data_nascita:
            item["dataNascita"] = self.data_nascita
        if self.luogo_nascita:
            item["luogoNascita"] = self.luogo_nascita
        return item


@dataclass
class Doctor:
    """Entita DOCTOR (SK = PROFILE)."""

    doctor_id: str
    nome: str
    sede: str
    calendar_id: str

    def to_item(self) -> dict:
        return {
            "PK": doctor_pk(self.doctor_id),
            "SK": "PROFILE",
            "entity": "DOCTOR",
            "nome": self.nome,
            "sede": self.sede,
            "calendarId": self.calendar_id,
        }


@dataclass
class Appointment:
    """Entita APPT (SK = META).

    Registrato inizialmente in stato PROVVISORIO (Requirement 5.5).
    idempotency_key = appointment_id (design.md - Interfacce chiave / Idempotenza).
    """

    appointment_id: str
    client_no: str
    nome_paziente: str
    doctor: str
    sede: str
    start: str  # ISO 8601 con offset, es. 2026-01-20T09:00:00+01:00
    duration_min: int = 30
    status: ApptStatus = ApptStatus.PROVVISORIO

    def to_item(self) -> dict:
        return {
            "PK": appt_pk(self.appointment_id),
            "SK": "META",
            "entity": "APPT",
            "appointmentId": self.appointment_id,
            "clientNo": self.client_no,
            "nomePaziente": self.nome_paziente,
            "doctor": self.doctor,
            "sede": self.sede,
            "start": self.start,
            "durationMin": self.duration_min,
            "status": self.status.value,
            "idempotencyKey": f"appt-{self.appointment_id}",
            "GSI2PK": gsi2_pk(self.status),
            "GSI2SK": gsi2_sk(self.start),
        }


@dataclass
class Contact:
    """Entita CONTACT (SK = META).

    Registrata per esiti RICHIAMERA / SOLO_INFO (Requirement 3.7). Il follow-up
    automatico e Out of Scope.
    """

    contact_id: str
    nome: str
    telefono: str
    esito: Esito
    data_chiamata: str

    def to_item(self) -> dict:
        return {
            "PK": contact_pk(self.contact_id),
            "SK": "META",
            "entity": "CONTACT",
            "nome": self.nome,
            "telefono": self.telefono,
            "esito": self.esito.value,
            "dataChiamata": self.data_chiamata,
        }


@dataclass
class Session:
    """Entita SESSION (SK = META).

    Stato del dialogo (dati raccolti finora, contatori domande/tentativi) cosi
    ogni turno resta stateless a livello Lambda (design.md - Motore_Conversazionale).
    """

    session_id: str
    collected_data: dict = field(default_factory=dict)
    clarifying_questions: int = 0

    def to_item(self) -> dict:
        return {
            "PK": session_pk(self.session_id),
            "SK": "META",
            "entity": "SESSION",
            "sessionId": self.session_id,
            "collectedData": self.collected_data,
            "clarifyingQuestions": self.clarifying_questions,
        }
