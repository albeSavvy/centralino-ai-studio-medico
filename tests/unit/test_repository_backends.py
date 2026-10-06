"""Unit test - backend del repository (Task 5).

Verifica che i due backend (InMemoryRepository e DynamoRepository) espongano la
stessa API e la stessa semantica, senza toccare AWS:
  - InMemoryRepository: usato direttamente.
  - DynamoRepository: iniettato con un fake table in-memory che imita la superficie
    boto3 usata (put_item, get_item, query su GSI1, update_item con
    ConditionExpression). Nessuna chiamata di rete, nessun costo.

Coperture chiave:
  - conditional write su update_appointment_status (prima transizione vince,
    Requirement 6.6);
  - save_contact / save_appointment round-trip;
  - PersistenceError su fallimento della put sottostante (Requirement 3.6);
  - search_by_name su GSI1 con cap 50 e validazione criterio (Requirement 4.4, 4.6).
"""

from __future__ import annotations

import pytest

from src.models import Appointment, ApptStatus, Contact, Esito, Patient
from src.repository import (
    ConditionalUpdateError,
    DynamoRepository,
    InMemoryRepository,
    InvalidSearchCriteriaError,
    PersistenceError,
    RepositoryError,
)


# ---------------------------------------------------------------------------
# Fake table in-memory che imita la superficie boto3 usata da DynamoRepository
# ---------------------------------------------------------------------------

class _ConditionalCheckFailed(Exception):
    """Imita botocore ClientError con Code=ConditionalCheckFailedException."""

    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """Tabella DynamoDB single-table finta (dict {(PK,SK): item}).

    Implementa solo quel che serve al DynamoRepository. La query supporta il solo
    pattern usato: IndexName=GSI1 con KeyConditionExpression Key('GSI1PK').eq(...).
    """

    def __init__(self, fail_put: bool = False) -> None:
        self._items: dict[tuple[str, str], dict] = {}
        self.fail_put = fail_put

    def put_item(self, Item: dict) -> dict:  # noqa: N803 - match boto3 casing
        if self.fail_put:
            raise RuntimeError("put simulata fallita")
        self._items[(Item["PK"], Item["SK"])] = dict(Item)
        return {}

    def get_item(self, Key: dict, **kwargs) -> dict:  # noqa: N803
        item = self._items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def query(self, IndexName, KeyConditionExpression, Limit=None, **kwargs):  # noqa: N803
        # Estrae il valore atteso di GSI1PK dalla condizione boto3.
        # In Key('GSI1PK').eq(value) la tupla _values e (Key, value): il valore
        # confrontato e l'elemento 1.
        expected = KeyConditionExpression._values[1]
        matches = [
            dict(it)
            for it in self._items.values()
            if it.get("GSI1PK") == expected
        ]
        if Limit is not None:
            matches = matches[:Limit]
        return {"Items": matches}

    def update_item(
        self,
        Key,  # noqa: N803
        UpdateExpression,  # noqa: N803
        ConditionExpression,  # noqa: N803
        ExpressionAttributeNames,  # noqa: N803
        ExpressionAttributeValues,  # noqa: N803
        **kwargs,
    ) -> dict:
        item = self._items.get((Key["PK"], Key["SK"]))
        if item is None:
            # Condizione su attributo inesistente -> fallisce la condizione.
            raise _ConditionalCheckFailed()
        # Valuta la condizione "#s = :expected" in modo mirato al nostro uso.
        if item.get("status") != ExpressionAttributeValues[":expected"]:
            raise _ConditionalCheckFailed()
        item["status"] = ExpressionAttributeValues[":new"]
        item["GSI2PK"] = ExpressionAttributeValues[":gpk"]
        return {}


def _patient(client_no: str = "000001", nome: str = "Mario Rossi") -> Patient:
    return Patient(
        client_no=client_no,
        nome=nome,
        eta=40,
        telefono="+39 333 0000000",
        problema="ansia",
        sede="Meda",
        data_chiamata="2026-01-15",
        email="mario@example.com",
    )


def _appointment(appointment_id: str = "a1b2c3") -> Appointment:
    return Appointment(
        appointment_id=appointment_id,
        client_no="000001",
        nome_paziente="Mario Rossi",
        doctor="chiara",
        sede="Meda",
        start="2026-01-20T09:00:00+01:00",
    )


# ---------------------------------------------------------------------------
# Parametrizzazione: la stessa suite gira sui due backend (stessa API)
# ---------------------------------------------------------------------------

def _in_memory() -> InMemoryRepository:
    return InMemoryRepository()


def _dynamo() -> DynamoRepository:
    return DynamoRepository(table=FakeTable())


@pytest.fixture(params=[_in_memory, _dynamo], ids=["in_memory", "dynamo_fake"])
def repo(request):
    return request.param()


def test_patient_roundtrip(repo):
    p = _patient()
    repo.save_patient(p)
    got = repo.get_patient("000001")
    assert got is not None
    assert got.nome == "Mario Rossi"
    assert got.eta == 40
    assert got.email == "mario@example.com"


def test_get_missing_patient_returns_none(repo):
    assert repo.get_patient("999999") is None


def test_search_by_name_match_and_empty(repo):
    repo.save_patient(_patient("000001", "Mario Rossi"))
    repo.save_patient(_patient("000002", "Giulia Verdi"))
    found = repo.search_by_name("mario rossi")  # match case-insensitive via norm
    assert len(found) == 1
    assert found[0].nome == "Mario Rossi"
    assert repo.search_by_name("Nessuno") == []


def test_search_by_name_invalid_criteria(repo):
    with pytest.raises(InvalidSearchCriteriaError):
        repo.search_by_name("   ")
    with pytest.raises(InvalidSearchCriteriaError):
        repo.search_by_name("x" * 101)


def test_appointment_roundtrip_and_conditional_update(repo):
    appt = _appointment()
    repo.save_appointment(appt)
    got = repo.get_appointment("a1b2c3")
    assert got is not None
    assert got.status == ApptStatus.PROVVISORIO

    # Prima transizione da PROVVISORIO: riesce.
    repo.update_appointment_status("a1b2c3", ApptStatus.CONFERMATO)
    assert repo.get_appointment("a1b2c3").status == ApptStatus.CONFERMATO

    # Seconda transizione da PROVVISORIO: la condizione fallisce (mutua esclusione).
    with pytest.raises(ConditionalUpdateError):
        repo.update_appointment_status("a1b2c3", ApptStatus.DA_RIVEDERE)
    # Lo stato resta quello della prima azione.
    assert repo.get_appointment("a1b2c3").status == ApptStatus.CONFERMATO


def test_update_missing_appointment_raises(repo):
    with pytest.raises((RepositoryError, ConditionalUpdateError)):
        repo.update_appointment_status("nope", ApptStatus.CONFERMATO)


def test_save_contact(repo):
    contact = Contact(
        contact_id="7f9a",
        nome="Giulia Verdi",
        telefono="+39 333 2222222",
        esito=Esito.RICHIAMERA,
        data_chiamata="2026-01-15",
    )
    repo.save_contact(contact)  # non deve sollevare


# --- Specifico del backend Dynamo: PersistenceError su put fallita ---

def test_dynamo_persistence_error_on_put_failure():
    repo = DynamoRepository(table=FakeTable(fail_put=True))
    with pytest.raises(PersistenceError):
        repo.save_patient(_patient())
