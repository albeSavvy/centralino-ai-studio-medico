"""Repository Anagrafica_Store: interfaccia + backend in-memory e DynamoDB.

Centralino AI Studio Medico / Idelia (Flusso A) - Task 5.

Questo modulo definisce un'interfaccia repository unica (ABC AnagraficaRepository)
e due backend che espongono la STESSA API, cosi il codice a valle resta
backend-agnostic:

  - InMemoryRepository: backend puro in-memory per i test (nessuna chiamata AWS).
  - DynamoRepository: backend boto3 su tabella single-table centralino-medico.

Operazioni (design.md - Components / Anagrafica_Store):
  - save_patient / get_patient
  - search_by_name (ricerca per nome, cap 50, validazione criterio)
  - save_appointment
  - update_appointment_status (conditional write: transizione da PROVVISORIO solo
    se ancora PROVVISORIO)
  - save_contact
  - generate_client_no (numero cliente univoco con retry fino a 5 tentativi)
  - register_patient (genera numero cliente univoco + persiste in un colpo)

Semantica trasversale (Requirement 3, 4, 5.5 e relative Properties 8-13):
  - Numero cliente: univoco, retry fino a 5 su collisione, poi rifiuto senza
    persistere (ClientNumberExhaustedError).
  - search_by_name: rifiuta nome vuoto/solo-spazi o >100 caratteri SENZA eseguire
    la query (InvalidSearchCriteriaError); risultati sempre <= 50.
  - update_appointment_status: conditional write, la prima transizione da
    PROVVISORIO vince; una transizione su stato non piu PROVVISORIO fallisce la
    condizione (ConditionalUpdateError).

Costo zero: il DynamoRepository non viene mai esercitato su una tabella reale nei
test (solo mock/fake); l'InMemoryRepository e completamente offline.

Riferimenti: design.md (Data Models, Components, Gestione errori e resilienza) e
src/models.py (dataclass + costruzione chiavi single-table). Le chiavi NON sono
ridefinite qui: si riusano i builder di models.py.
"""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from src.models import (
    Appointment,
    ApptStatus,
    Contact,
    MINOR_AGE_THRESHOLD,  # noqa: F401  (riesportato per comodita dei chiamanti)
    Patient,
    appt_pk,
    gsi1_pk,
    gsi2_pk,
    gsi2_sk,
    normalize_name,
    patient_pk,
)

if TYPE_CHECKING:  # pragma: no cover - solo per type hints, nessun import a runtime
    from botocore.exceptions import ClientError

# Numero massimo di tentativi di generazione di un numero cliente univoco
# (Requirement 4.2, 4.3). Al sesto fallimento la registrazione e rifiutata.
MAX_CLIENT_NO_ATTEMPTS = 5

# Cap sui risultati di ricerca per nome (Requirement 4.4).
MAX_SEARCH_RESULTS = 50

# Lunghezza massima del criterio di ricerca (Requirement 4.6).
MAX_SEARCH_NAME_LEN = 100

# Larghezza del numero cliente generato (zero-padded).
CLIENT_NO_WIDTH = 6


# ---------------------------------------------------------------------------
# Eccezioni di dominio
# ---------------------------------------------------------------------------

class RepositoryError(Exception):
    """Errore generico del repository."""


class PersistenceError(RepositoryError):
    """La persistenza sottostante e fallita (Requirement 3.6).

    Il chiamante puo intercettarla per ritentare: i dati raccolti restano nelle
    sue mani (non vengono persi ne alterati dal repository).
    """


class ClientNumberExhaustedError(RepositoryError):
    """Impossibile assegnare un numero cliente univoco dopo i retry (Req 4.3)."""


class InvalidSearchCriteriaError(RepositoryError):
    """Criterio di ricerca per nome non valido (Requirement 4.6)."""


class ConditionalUpdateError(RepositoryError):
    """La conditional write sulla transizione di stato ha fallito la condizione.

    Usata per la mutua esclusione della Finestra_Conferma (Requirement 6.6): una
    transizione da PROVVISORIO tentata quando l'appuntamento non e piu PROVVISORIO
    non modifica lo stato.
    """


# ---------------------------------------------------------------------------
# Interfaccia repository (backend-agnostic)
# ---------------------------------------------------------------------------

class AnagraficaRepository(ABC):
    """Contratto comune ai backend InMemory e Dynamo.

    Tutte le firme sono identiche tra i backend: il codice a valle (Assegnatore,
    Motore, Step Functions) dipende solo da questa interfaccia.
    """

    # --- Pazienti ---------------------------------------------------------

    @abstractmethod
    def save_patient(self, patient: Patient) -> None:
        """Persiste (o sovrascrive) un paziente con numero cliente gia assegnato.

        Solleva PersistenceError se la persistenza sottostante fallisce, senza
        alterare i dati passati (Requirement 3.5, 3.6).
        """

    @abstractmethod
    def get_patient(self, client_no: str) -> Patient | None:
        """Rilegge un paziente per numero cliente, o None se assente."""

    @abstractmethod
    def search_by_name(self, nome: str) -> list[Patient]:
        """Cerca pazienti per nome (match sul nome normalizzato).

        Valida il criterio PRIMA di qualunque query: nome vuoto/solo-spazi o
        >100 caratteri => InvalidSearchCriteriaError (Requirement 4.6). Risultati
        sempre <= 50 (Requirement 4.4); elenco vuoto se nessun match (4.5).
        """

    @abstractmethod
    def client_no_exists(self, client_no: str) -> bool:
        """True se esiste gia un paziente con quel numero cliente."""

    # --- Appuntamenti -----------------------------------------------------

    @abstractmethod
    def save_appointment(self, appointment: Appointment) -> None:
        """Persiste un appuntamento (tipicamente in PROVVISORIO, Req 5.5)."""

    @abstractmethod
    def get_appointment(self, appointment_id: str) -> Appointment | None:
        """Rilegge un appuntamento per id, o None se assente."""

    @abstractmethod
    def update_appointment_status(
        self,
        appointment_id: str,
        new_status: ApptStatus,
        expected_current: ApptStatus = ApptStatus.PROVVISORIO,
    ) -> None:
        """Transizione di stato con conditional write.

        Applica new_status solo se lo stato corrente e ancora expected_current
        (default PROVVISORIO). Se la condizione non e soddisfatta solleva
        ConditionalUpdateError e NON modifica lo stato (Requirement 6.6). Se
        l'appuntamento non esiste solleva RepositoryError.
        """

    # --- Task token della Finestra_Conferma (Step Functions) --------------

    @abstractmethod
    def save_task_token(self, appointment_id: str, task_token: str) -> None:
        """Associa il taskToken di Step Functions all'appuntamento.

        Il taskToken (waitForTaskToken) e' troppo lungo per il callback_data di
        Telegram (limite 64 byte), quindi lo persistiamo sull'item APPT quando il
        notificatore invia il recap. Il callback lo recupera per appointment_id e
        sblocca la state machine con SendTaskSuccess (design.md - Decisione 4).
        """

    @abstractmethod
    def get_task_token(self, appointment_id: str) -> str | None:
        """Rilegge il taskToken associato all'appuntamento, o None se assente."""

    # --- Contatti ---------------------------------------------------------

    @abstractmethod
    def save_contact(self, contact: Contact) -> None:
        """Registra un contatto per richiamo/solo-info (Requirement 3.7)."""

    # --- Numero cliente ---------------------------------------------------

    def generate_client_no(self) -> str:
        """Genera un numero cliente univoco con retry (Requirement 4.1, 4.2, 4.3).

        Prova fino a MAX_CLIENT_NO_ATTEMPTS numeri candidati distinti; il primo
        non ancora presente e restituito. Se tutti i tentativi collidono solleva
        ClientNumberExhaustedError senza persistere nulla.

        La generazione dei candidati e delegata a _new_candidate_client_no cosi i
        test possono forzare collisioni in modo deterministico.
        """
        for _ in range(MAX_CLIENT_NO_ATTEMPTS):
            candidate = self._new_candidate_client_no()
            if not self.client_no_exists(candidate):
                return candidate
        raise ClientNumberExhaustedError(
            "impossibile generare un numero cliente univoco dopo "
            f"{MAX_CLIENT_NO_ATTEMPTS} tentativi"
        )

    def register_patient(self, patient: Patient) -> Patient:
        """Assegna un numero cliente univoco al paziente e lo persiste.

        Restituisce il Patient effettivamente persistito (con il client_no
        assegnato). Se non e possibile assegnare un numero univoco, solleva
        ClientNumberExhaustedError SENZA persistere (Requirement 4.3).
        """
        client_no = self.generate_client_no()
        persisted = _with_client_no(patient, client_no)
        self.save_patient(persisted)
        return persisted

    def _new_candidate_client_no(self) -> str:
        """Genera un candidato numero cliente (override-abile nei test).

        Numero pseudo-casuale a CLIENT_NO_WIDTH cifre. Non garantisce da solo
        l'unicita: la verifica avviene in generate_client_no.
        """
        upper = 10 ** CLIENT_NO_WIDTH
        return str(secrets.randbelow(upper)).zfill(CLIENT_NO_WIDTH)


# ---------------------------------------------------------------------------
# Helper puri
# ---------------------------------------------------------------------------

def _with_client_no(patient: Patient, client_no: str) -> Patient:
    """Ritorna un nuovo Patient identico ma con il client_no indicato.

    Non muta l'istanza passata (Property 9: i dati del chiamante restano
    disponibili e invariati anche in caso di fallimento a valle).
    """
    return Patient(
        client_no=client_no,
        nome=patient.nome,
        eta=patient.eta,
        telefono=patient.telefono,
        problema=patient.problema,
        sede=patient.sede,
        data_chiamata=patient.data_chiamata,
        parent_name=patient.parent_name,
        indirizzo=patient.indirizzo,
        email=patient.email,
        data_nascita=patient.data_nascita,
        luogo_nascita=patient.luogo_nascita,
    )


def _validate_search_name(nome: str) -> str:
    """Valida e normalizza il criterio di ricerca (Requirement 4.6).

    Rifiuta nome None, vuoto/solo-spazi o >100 caratteri SENZA eseguire query.
    Ritorna il nome normalizzato pronto per il confronto con GSI1.
    """
    if nome is None or not nome.strip():
        raise InvalidSearchCriteriaError("criterio di ricerca vuoto o solo spazi")
    if len(nome) > MAX_SEARCH_NAME_LEN:
        raise InvalidSearchCriteriaError(
            f"criterio di ricerca oltre {MAX_SEARCH_NAME_LEN} caratteri"
        )
    return normalize_name(nome)


def _patient_from_item(item: dict[str, Any]) -> Patient:
    """Ricostruisce un Patient da un item single-table.

    Nota: eta puo arrivare come Decimal da DynamoDB; qui la si normalizza a int.
    """
    return Patient(
        client_no=str(item["clientNo"]),
        nome=item["nome"],
        eta=int(item["eta"]),
        telefono=item["telefono"],
        problema=item["problema"],
        sede=item["sede"],
        data_chiamata=item["dataChiamata"],
        parent_name=item.get("parentName"),
        indirizzo=item.get("indirizzo"),
        email=item.get("email"),
        data_nascita=item.get("dataNascita"),
        luogo_nascita=item.get("luogoNascita"),
    )


# ---------------------------------------------------------------------------
# Backend in-memory (test)
# ---------------------------------------------------------------------------

class InMemoryRepository(AnagraficaRepository):
    """Backend puro in-memory. Nessuna dipendenza AWS, adatto ai test.

    Simula la stessa semantica del backend Dynamo: unicita del numero cliente,
    conditional write sulla transizione di stato, cap e validazione della ricerca.
    Lo store e un dict single-table {(PK, SK): item} come su DynamoDB.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    # --- Pazienti ---------------------------------------------------------

    def save_patient(self, patient: Patient) -> None:
        item = patient.to_item()
        self._items[(item["PK"], item["SK"])] = item

    def get_patient(self, client_no: str) -> Patient | None:
        item = self._items.get((patient_pk(client_no), "PROFILE"))
        if item is None:
            return None
        return _patient_from_item(item)

    def search_by_name(self, nome: str) -> list[Patient]:
        target = _validate_search_name(nome)  # puo sollevare prima di ogni "query"
        results: list[Patient] = []
        target_gsi1 = gsi1_pk(nome)  # PNAME#<normalizzato>
        for item in self._items.values():
            if item.get("entity") != "PATIENT":
                continue
            if item.get("GSI1PK") == target_gsi1 or normalize_name(item["nome"]) == target:
                results.append(_patient_from_item(item))
            if len(results) >= MAX_SEARCH_RESULTS:
                break
        return results[:MAX_SEARCH_RESULTS]

    def client_no_exists(self, client_no: str) -> bool:
        return (patient_pk(client_no), "PROFILE") in self._items

    # --- Appuntamenti -----------------------------------------------------

    def save_appointment(self, appointment: Appointment) -> None:
        item = appointment.to_item()
        self._items[(item["PK"], item["SK"])] = item

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        item = self._items.get((appt_pk(appointment_id), "META"))
        if item is None:
            return None
        return Appointment(
            appointment_id=str(item["appointmentId"]),
            client_no=str(item["clientNo"]),
            nome_paziente=item["nomePaziente"],
            doctor=item["doctor"],
            sede=item["sede"],
            start=item["start"],
            duration_min=int(item["durationMin"]),
            status=ApptStatus(item["status"]),
        )

    def update_appointment_status(
        self,
        appointment_id: str,
        new_status: ApptStatus,
        expected_current: ApptStatus = ApptStatus.PROVVISORIO,
    ) -> None:
        key = (appt_pk(appointment_id), "META")
        item = self._items.get(key)
        if item is None:
            raise RepositoryError(f"appuntamento {appointment_id} non trovato")
        # Conditional write: applica solo se lo stato corrente e quello atteso.
        if item["status"] != expected_current.value:
            raise ConditionalUpdateError(
                f"stato corrente {item['status']} != atteso {expected_current.value}"
            )
        item["status"] = new_status.value
        item["GSI2PK"] = gsi2_pk(new_status)
        item["GSI2SK"] = gsi2_sk(item["start"])

    # --- Task token -------------------------------------------------------

    def save_task_token(self, appointment_id: str, task_token: str) -> None:
        key = (appt_pk(appointment_id), "META")
        item = self._items.get(key)
        if item is None:
            raise RepositoryError(f"appuntamento {appointment_id} non trovato")
        item["taskToken"] = task_token

    def get_task_token(self, appointment_id: str) -> str | None:
        item = self._items.get((appt_pk(appointment_id), "META"))
        if item is None:
            return None
        return item.get("taskToken")

    # --- Contatti ---------------------------------------------------------

    def save_contact(self, contact: Contact) -> None:
        item = contact.to_item()
        self._items[(item["PK"], item["SK"])] = item


# ---------------------------------------------------------------------------
# Backend DynamoDB (boto3)
# ---------------------------------------------------------------------------

class DynamoRepository(AnagraficaRepository):
    """Backend su DynamoDB single-table (tabella centralino-medico).

    Usa l'interfaccia resource di boto3 (auto-marshalling dei tipi Python <->
    DynamoDB). NON viene esercitato su una tabella reale nei test: il costruttore
    accetta una table gia pronta (iniettabile con un fake/mock in-memory) per
    restare a costo zero e offline.

    Semantica chiave:
      - save_patient: PutItem senza condizione (upsert del profilo).
      - client_no_exists: GetItem sulla PK del paziente.
      - update_appointment_status: UpdateItem con ConditionExpression sullo stato
        atteso; su ConditionalCheckFailedException => ConditionalUpdateError.
      - search_by_name: Query su GSI1 (GSI1PK = PNAME#<normalizzato>), Limit=50.
    """

    def __init__(self, table: Any | None = None, table_name: str = "centralino-medico") -> None:
        if table is not None:
            self._table = table
        else:  # pragma: no cover - richiederebbe AWS reale, escluso dai test
            import boto3

            self._table = boto3.resource("dynamodb").Table(table_name)

    # --- Pazienti ---------------------------------------------------------

    def save_patient(self, patient: Patient) -> None:
        try:
            self._table.put_item(Item=patient.to_item())
        except Exception as exc:  # noqa: BLE001 - convertito in errore di dominio
            raise PersistenceError(
                f"salvataggio paziente {patient.client_no} fallito"
            ) from exc

    def get_patient(self, client_no: str) -> Patient | None:
        resp = self._table.get_item(Key={"PK": patient_pk(client_no), "SK": "PROFILE"})
        item = resp.get("Item")
        if item is None:
            return None
        return _patient_from_item(item)

    def search_by_name(self, nome: str) -> list[Patient]:
        _validate_search_name(nome)  # rifiuta PRIMA di qualunque query (Req 4.6)
        from boto3.dynamodb.conditions import Key

        resp = self._table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(gsi1_pk(nome)),
            Limit=MAX_SEARCH_RESULTS,
        )
        items = resp.get("Items", [])[:MAX_SEARCH_RESULTS]
        return [_patient_from_item(it) for it in items]

    def client_no_exists(self, client_no: str) -> bool:
        resp = self._table.get_item(
            Key={"PK": patient_pk(client_no), "SK": "PROFILE"},
            ProjectionExpression="PK",
        )
        return resp.get("Item") is not None

    # --- Appuntamenti -----------------------------------------------------

    def save_appointment(self, appointment: Appointment) -> None:
        try:
            self._table.put_item(Item=appointment.to_item())
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                f"salvataggio appuntamento {appointment.appointment_id} fallito"
            ) from exc

    def get_appointment(self, appointment_id: str) -> Appointment | None:
        resp = self._table.get_item(Key={"PK": appt_pk(appointment_id), "SK": "META"})
        item = resp.get("Item")
        if item is None:
            return None
        return Appointment(
            appointment_id=str(item["appointmentId"]),
            client_no=str(item["clientNo"]),
            nome_paziente=item["nomePaziente"],
            doctor=item["doctor"],
            sede=item["sede"],
            start=item["start"],
            duration_min=int(item["durationMin"]),
            status=ApptStatus(item["status"]),
        )

    def update_appointment_status(
        self,
        appointment_id: str,
        new_status: ApptStatus,
        expected_current: ApptStatus = ApptStatus.PROVVISORIO,
    ) -> None:
        try:
            self._table.update_item(
                Key={"PK": appt_pk(appointment_id), "SK": "META"},
                UpdateExpression="SET #s = :new, GSI2PK = :gpk",
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":new": new_status.value,
                    ":expected": expected_current.value,
                    ":gpk": gsi2_pk(new_status),
                },
            )
        except Exception as exc:  # noqa: BLE001
            if _is_conditional_check_failed(exc):
                raise ConditionalUpdateError(
                    f"transizione da {expected_current.value} rifiutata: stato non piu atteso"
                ) from exc
            raise

    # --- Task token -------------------------------------------------------

    def save_task_token(self, appointment_id: str, task_token: str) -> None:
        try:
            self._table.update_item(
                Key={"PK": appt_pk(appointment_id), "SK": "META"},
                UpdateExpression="SET taskToken = :t",
                ExpressionAttributeValues={":t": task_token},
            )
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                f"salvataggio taskToken per appuntamento {appointment_id} fallito"
            ) from exc

    def get_task_token(self, appointment_id: str) -> str | None:
        resp = self._table.get_item(
            Key={"PK": appt_pk(appointment_id), "SK": "META"},
            ProjectionExpression="taskToken",
        )
        item = resp.get("Item")
        if item is None:
            return None
        return item.get("taskToken")

    # --- Contatti ---------------------------------------------------------

    def save_contact(self, contact: Contact) -> None:
        try:
            self._table.put_item(Item=contact.to_item())
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                f"salvataggio contatto {contact.contact_id} fallito"
            ) from exc


def _is_conditional_check_failed(exc: Exception) -> bool:
    """True se l'eccezione e una ConditionalCheckFailedException di DynamoDB.

    Robusto a fake che sollevano ClientError-like: ispeziona il codice d'errore
    senza dipendere dall'import di botocore a runtime.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
    return type(exc).__name__ == "ConditionalCheckFailedException"
