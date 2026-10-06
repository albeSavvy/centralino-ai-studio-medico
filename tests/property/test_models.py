"""Property test - modelli dati (Task 3.1).

Property 6: classificazione minore e referente obbligatorio.

Mappatura 1:1 con la proprieta del design (design.md - Correctness Properties,
Property 6). Libreria: Hypothesis, >=100 iterazioni. Nessuna dipendenza AWS.
"""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.models import MINOR_AGE_THRESHOLD, Patient


def _make_patient(eta: int, parent_name: str | None) -> Patient:
    """Costruisce un Patient con i soli campi rilevanti per Property 6."""
    return Patient(
        client_no="000001",
        nome="Mario Rossi",
        eta=eta,
        telefono="+39 333 0000000",
        problema="ansia",
        sede="Meda",
        data_chiamata="2026-01-15",
        parent_name=parent_name,
    )


# Feature: centralino-ai-studio-medico, Property 6: eta <18 <=> minore, e ogni minore ha parentName registrato
@settings(max_examples=200)
@given(
    eta=st.integers(min_value=0, max_value=120),
    parent_name=st.one_of(
        st.none(),
        st.text(),
        st.just("Anna Bianchi"),
    ),
)
def test_property6_minor_classification_and_parent_reference(eta, parent_name):
    expected_minor = eta < MINOR_AGE_THRESHOLD
    has_valid_parent = bool(parent_name and parent_name.strip())

    if expected_minor and not has_valid_parent:
        # Direzione 2: un minore senza referente valido deve essere rifiutato.
        with pytest.raises(ValueError):
            _make_patient(eta, parent_name)
        return

    patient = _make_patient(eta, parent_name)

    # Direzione 1: eta < 18 <=> is_minor True (in entrambi i sensi).
    assert patient.is_minor == expected_minor

    # Ogni minore costruito con successo ha un parentName valido registrato.
    if patient.is_minor:
        assert patient.parent_name is not None
        assert patient.parent_name.strip() != ""
