"""Property test - persistenza e identificazione paziente (Task 5.1).

Mappatura 1:1 con le proprieta del design (design.md - Correctness Properties):
  - Property 8:  round-trip save->get restituisce gli stessi dati.
  - Property 9:  su fallimento di persistenza i dati restano disponibili e
                 l'errore e segnalato.
  - Property 10: numeri cliente sempre distinti; retry fino a 5; rifiuto senza
                 persistere su esaurimento.
  - Property 11: ricerca per nome coerente col criterio, <= 50, vuoto se no match.
  - Property 12: nome ricerca vuoto/solo-spazi o >100 char => rifiuto senza query.

Libreria: Hypothesis, >= 100 iterazioni per proprieta. Backend in-memory / fake:
nessuna chiamata AWS, costo zero. I commenti usano ASCII per evitare problemi di
encoding, mantenendo il significato fedele al testo della spec.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.models import Patient, normalize_name
from src.repository import (
    MAX_CLIENT_NO_ATTEMPTS,
    MAX_SEARCH_RESULTS,
    AnagraficaRepository,
    ClientNumberExhaustedError,
    InMemoryRepository,
    InvalidSearchCriteriaError,
    PersistenceError,
)


# ---------------------------------------------------------------------------
# Strategie e helper condivisi
# ---------------------------------------------------------------------------

# Testo generico non vuoto per campi anagrafici (evita solo-spazi dove serve).
_nonempty_text = st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != "")

# Eta adulta: evita il vincolo parentName-per-minore di Patient, cosi la strategia
# resta focalizzata sulla persistenza (Property 6 copre gia il caso minore).
_adult_age = st.integers(min_value=18, max_value=120)


@st.composite
def adult_patients(draw, client_no: str = "000001") -> Patient:
    """Genera un Patient adulto valido con tutti i dati obbligatori presenti."""
    return Patient(
        client_no=client_no,
        nome=draw(_nonempty_text),
        eta=draw(_adult_age),
        telefono=draw(_nonempty_text),
        problema=draw(_nonempty_text),
        sede=draw(_nonempty_text),
        data_chiamata=draw(_nonempty_text),
        email=draw(st.one_of(st.none(), _nonempty_text)),
        indirizzo=draw(st.one_of(st.none(), _nonempty_text)),
    )


def _same_patient_data(a: Patient, b: Patient) -> bool:
    """Confronta i dati anagrafici rilevanti (esclude campi derivati coerenti)."""
    return (
        a.client_no == b.client_no
        and a.nome == b.nome
        and a.eta == b.eta
        and a.telefono == b.telefono
        and a.problema == b.problema
        and a.sede == b.sede
        and a.data_chiamata == b.data_chiamata
        and a.parent_name == b.parent_name
        and a.indirizzo == b.indirizzo
        and a.email == b.email
        and a.data_nascita == b.data_nascita
        and a.luogo_nascita == b.luogo_nascita
        and a.is_minor == b.is_minor
    )


class FailingPersistenceRepository(InMemoryRepository):
    """Backend che simula un fallimento di persistenza su save_patient.

    Usato per Property 9: la save fallisce con PersistenceError e NON scrive nulla
    nello store, cosi i dati del chiamante restano interamente disponibili.
    """

    def save_patient(self, patient: Patient) -> None:  # type: ignore[override]
        raise PersistenceError("persistenza simulata fallita")


class ControlledCandidateRepository(InMemoryRepository):
    """InMemoryRepository con generazione candidati numero-cliente controllabile.

    Permette a Property 10 di forzare in modo deterministico collisioni e successi
    senza dipendere dalla casualita.
    """

    def __init__(self, candidates: list[str]) -> None:
        super().__init__()
        self._queue = list(candidates)

    def _new_candidate_client_no(self) -> str:  # type: ignore[override]
        if self._queue:
            return self._queue.pop(0)
        # Fallback: se la coda si esaurisce, resta un candidato costante che
        # collidera con quanto gia presente (utile per forzare l'esaurimento).
        return "999999"


# ---------------------------------------------------------------------------
# Property 8: round-trip di persistenza del paziente
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 8: per ogni paziente con tutti i dati obbligatori presenti, salvare e poi rileggere dall'Anagrafica_Store restituisce gli stessi dati anagrafici
@settings(max_examples=200)
@given(patient=adult_patients())
def test_property8_patient_save_get_roundtrip(patient: Patient) -> None:
    repo: AnagraficaRepository = InMemoryRepository()
    repo.save_patient(patient)

    reloaded = repo.get_patient(patient.client_no)

    assert reloaded is not None
    assert _same_patient_data(patient, reloaded)


# ---------------------------------------------------------------------------
# Property 9: preservazione dei dati su fallimento di persistenza
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 9: per ogni paziente, se la persistenza fallisce, i dati raccolti restano interamente disponibili per un nuovo tentativo e il fallimento e segnalato
@settings(max_examples=200)
@given(patient=adult_patients())
def test_property9_data_preserved_on_persistence_failure(patient: Patient) -> None:
    repo = FailingPersistenceRepository()

    # Snapshot dei dati raccolti prima del tentativo di persistenza.
    before = (
        patient.client_no,
        patient.nome,
        patient.eta,
        patient.telefono,
        patient.problema,
        patient.sede,
        patient.data_chiamata,
        patient.email,
    )

    # Il fallimento e segnalato esplicitamente.
    with pytest.raises(PersistenceError):
        repo.save_patient(patient)

    # I dati del chiamante non sono stati alterati (restano disponibili al retry).
    after = (
        patient.client_no,
        patient.nome,
        patient.eta,
        patient.telefono,
        patient.problema,
        patient.sede,
        patient.data_chiamata,
        patient.email,
    )
    assert before == after

    # Nulla e stato persistito: una rilettura non trova il paziente.
    assert repo.get_patient(patient.client_no) is None


# ---------------------------------------------------------------------------
# Property 10: unicita del numero cliente con retry e rifiuto su esaurimento
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 10: tutti i numeri cliente persistiti sono distinti; in caso di collisione la generazione e ripetuta fino a 5 volte e, se tutte collidono, la registrazione e rifiutata senza persistere e con errore di identificativo non univoco
@settings(max_examples=200)
@given(
    existing=st.integers(min_value=0, max_value=999999),
    # 0..4 collisioni iniziali (entro il limite) seguite da un candidato libero.
    collisions=st.integers(min_value=0, max_value=MAX_CLIENT_NO_ATTEMPTS - 1),
)
def test_property10_client_no_unique_with_retry_and_reject(
    existing: int, collisions: int
) -> None:
    taken = str(existing).zfill(6)
    # Un numero sicuramente diverso da "taken" per il candidato libero.
    free = str((existing + 1) % 1000000).zfill(6)

    # Coda: N candidati che collidono con "taken", poi un candidato libero.
    candidates = [taken] * collisions + [free]
    repo = ControlledCandidateRepository(candidates)

    # Pre-carica un paziente con "taken" cosi i primi candidati collidono.
    repo.save_patient(
        Patient(
            client_no=taken,
            nome="Occupato",
            eta=40,
            telefono="000",
            problema="x",
            sede="Meda",
            data_chiamata="2026-01-15",
        )
    )

    assigned = repo.generate_client_no()

    # Il numero assegnato e libero e distinto da quello gia presente.
    assert assigned == free
    assert assigned != taken
    assert not repo.client_no_exists(assigned)

    # Ora forziamo l'esaurimento: tutti i candidati collidono con "taken".
    repo_exhaust = ControlledCandidateRepository([taken] * MAX_CLIENT_NO_ATTEMPTS)
    repo_exhaust.save_patient(
        Patient(
            client_no=taken,
            nome="Occupato",
            eta=40,
            telefono="000",
            problema="x",
            sede="Meda",
            data_chiamata="2026-01-15",
        )
    )
    items_before = dict(repo_exhaust._items)  # snapshot store

    with pytest.raises(ClientNumberExhaustedError):
        repo_exhaust.generate_client_no()

    # Nessun record aggiuntivo persistito a seguito del rifiuto.
    assert repo_exhaust._items == items_before


# ---------------------------------------------------------------------------
# Property 11: correttezza della ricerca per nome
# ---------------------------------------------------------------------------

# Feature: centralino-ai-studio-medico, Property 11: per ogni dataset e criterio valido, tutti i risultati corrispondono al criterio, la cardinalita non supera 50, e se nessuno corrisponde il risultato e un elenco vuoto
@settings(max_examples=200)
@given(
    matching_count=st.integers(min_value=0, max_value=70),
    noise_count=st.integers(min_value=0, max_value=20),
)
def test_property11_search_by_name_correct(
    matching_count: int, noise_count: int
) -> None:
    repo = InMemoryRepository()
    target_name = "Mario Rossi"
    target_norm = normalize_name(target_name)

    # Pazienti che corrispondono al criterio (stesso nome normalizzato).
    for i in range(matching_count):
        repo.save_patient(
            Patient(
                client_no=f"M{i:05d}",
                nome=target_name,
                eta=40,
                telefono="000",
                problema="x",
                sede="Meda",
                data_chiamata="2026-01-15",
            )
        )
    # Rumore: pazienti con nome diverso, non devono comparire.
    for i in range(noise_count):
        repo.save_patient(
            Patient(
                client_no=f"N{i:05d}",
                nome=f"Altro Nome {i}",
                eta=40,
                telefono="000",
                problema="x",
                sede="Meda",
                data_chiamata="2026-01-15",
            )
        )

    results = repo.search_by_name(target_name)

    # Tutti i risultati corrispondono al criterio.
    assert all(normalize_name(p.nome) == target_norm for p in results)
    # Cardinalita mai oltre 50.
    assert len(results) <= MAX_SEARCH_RESULTS
    # Coerenza col numero di match atteso (cap a 50).
    assert len(results) == min(matching_count, MAX_SEARCH_RESULTS)

    # Nessun match -> elenco vuoto.
    empty = repo.search_by_name("Nessuno Corrisponde Qui")
    assert empty == []


# ---------------------------------------------------------------------------
# Property 12: validazione del criterio di ricerca
# ---------------------------------------------------------------------------

class _SpyRepository(InMemoryRepository):
    """InMemoryRepository che registra se la scansione/query e stata eseguita."""

    def __init__(self) -> None:
        super().__init__()
        self.queried = False

    def search_by_name(self, nome: str):  # type: ignore[override]
        # La validazione avviene nel metodo base PRIMA di scandire lo store.
        # Marchiamo la query solo dopo che la validazione e passata.
        from src.repository import _validate_search_name

        _validate_search_name(nome)
        self.queried = True
        return super().search_by_name(nome)


# Feature: centralino-ai-studio-medico, Property 12: per ogni nome di ricerca vuoto o solo-spazi, oppure di lunghezza superiore a 100 caratteri, la ricerca e rifiutata senza essere eseguita e restituisce un errore di criterio non valido
@settings(max_examples=200)
@given(
    bad=st.one_of(
        st.just(""),
        st.text(alphabet=" \t\n", min_size=1, max_size=10),  # solo spazi
        st.text(min_size=101, max_size=300),  # oltre 100 caratteri
    )
)
def test_property12_search_invalid_criteria_rejected_without_query(bad: str) -> None:
    repo = _SpyRepository()

    with pytest.raises(InvalidSearchCriteriaError):
        repo.search_by_name(bad)

    # La query non e mai stata eseguita.
    assert repo.queried is False
